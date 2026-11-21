"""The agent loop. A plain JSON-action protocol (no SDK function-calling) so it is
LLM-agnostic and fully testable with a scripted fake LLM.

run_agent() is a generator of event dicts; the API layer turns them into SSE.
Event types: step, guardrail, proposal, answer, needs_clarification, error."""
import json
import re
from dataclasses import dataclass
from typing import Iterator

from ..guardrails.budget import Budget, BudgetExceeded
from ..guardrails.errors import GuardrailViolation
from ..guardrails.output import groundedness_score, strip_invalid_citations, verify_citations
from ..hitl.actions import validate_payload
from ..hitl.patching import PatchError, apply_edits, make_diff
from .llm import LLM
from .prompts import SYSTEM_PROMPT
from .tools import TOOL_NAMES, RepoTools, ToolError

OBS_PREVIEW_CHARS = 300


@dataclass
class Action:
    thought: str
    name: str
    args: dict


def parse_action(text: str) -> Action:
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()   # reasoning models
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.S).strip()
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)
        if not m:
            raise ValueError("response is not a JSON object")
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError as e:
            raise ValueError(f"invalid JSON: {e}")
    if not isinstance(obj, dict) or not isinstance(obj.get("action"), str):
        raise ValueError("missing string field 'action'")
    args = obj.get("args") or {}
    if not isinstance(args, dict):
        raise ValueError("'args' must be an object")
    return Action(str(obj.get("thought", ""))[:500], obj["action"], args)


def run_agent(
    question: str,
    tools: RepoTools,
    llm: LLM,
    budget: Budget,
    *,
    max_citation_retries: int = 1,
    groundedness_threshold: float = 0.6,
    system_prompt: str = SYSTEM_PROMPT,
    seed_messages: list[dict] | None = None,
    final_only: bool = False,
) -> Iterator[dict]:
    """final_only=True is "fast mode": retrieval already happened (see seed_messages), so only
    final_answer is accepted; tool calls and proposals are rejected."""
    messages: list[dict] = (
        list(seed_messages) if seed_messages else [{"role": "user", "content": f"Question: {question}"}]
    )
    parse_failures = 0
    citation_retries = 0
    step_no = 0
    emitted_guardrails = len(tools.guardrail_events)   # don't re-emit events from before this run

    def drain_tool_guardrails():
        nonlocal emitted_guardrails
        new = tools.guardrail_events[emitted_guardrails:]
        emitted_guardrails = len(tools.guardrail_events)
        return new

    try:
        while True:
            budget.charge_step()
            res = llm.complete(system_prompt, messages)
            budget.add_tokens(res.tokens_in, res.tokens_out)

            try:
                action = parse_action(res.text)
            except ValueError as e:
                parse_failures += 1
                if parse_failures > 2:
                    yield {"type": "error", "code": "bad_model_output",
                           "message": "The model kept returning invalid output."}
                    return
                messages.append({"role": "assistant", "content": res.text})
                messages.append({"role": "user", "content":
                                 f"Invalid response ({e}). Reply with a single JSON object: "
                                 '{"thought": "...", "action": "...", "args": {...}}'})
                continue

            messages.append({"role": "assistant", "content": res.text})

            # ---------- final answer ----------
            if action.name == "final_answer":
                answer = str(action.args.get("answer", "")).strip()
                found = bool(action.args.get("found", True))
                if not answer:
                    messages.append({"role": "user", "content": "final_answer needs a non-empty 'answer'."})
                    continue
                if not found:
                    yield {"type": "answer", "text": answer, "citations": [], "found": False,
                           "confidence": "low", "groundedness": 1.0}
                    return

                v = verify_citations(answer, tools.evidence.items)
                if v.invalid or not v.valid:
                    if citation_retries < max_citation_retries:
                        citation_retries += 1
                        bad = ", ".join(c.raw for c in v.invalid) or "(no citations at all)"
                        yield {"type": "guardrail", "code": "citation_check_failed",
                               "message": f"Answer had unverifiable citations: {bad}. Asking the model to fix it."}
                        messages.append({"role": "user", "content":
                                         f"Citation check failed: {bad}. Evidence you actually retrieved: "
                                         f"{tools.evidence.summary()}. Re-issue final_answer citing ONLY those "
                                         "ranges as [path:start-end], or retrieve more code first."})
                        continue
                    answer = strip_invalid_citations(answer, v.invalid)
                    v = verify_citations(answer, tools.evidence.items)
                    if not v.valid:
                        yield {"type": "needs_clarification", "message":
                               "I couldn't ground an answer in the code I retrieved. "
                               "Can you point me at a file, module or symbol?"}
                        return
                    downgraded = True
                else:
                    downgraded = False

                g = groundedness_score(answer, tools.evidence.text)
                confidence = "high" if (g >= groundedness_threshold and not downgraded) else "low"
                yield {"type": "answer", "text": answer, "found": True, "confidence": confidence,
                       "groundedness": round(g, 3),
                       "citations": [{"path": c.path, "start": c.start, "end": c.end} for c in v.valid]}
                return

            if final_only:
                messages.append({"role": "user", "content":
                                 "Only final_answer is allowed here. Answer now from the retrieved code above, "
                                 "citing [path:start-end], or set found=false."})
                continue

            # ---------- HITL proposal ----------
            if action.name == "propose_change":
                kind = str(action.args.get("kind", ""))
                try:
                    payload = validate_payload(kind, action.args)
                    if kind == "patch":
                        if payload["path"] not in tools.evidence.paths:
                            raise GuardrailViolation(
                                "invalid_proposal", "You may only patch a file you have already read with read_file.")
                        try:   # the edits must apply cleanly to the real file; the human reviews this diff
                            original = tools.read_text(payload["path"])
                            patched = apply_edits(original, payload["edits"])
                        except (PatchError, ToolError) as pe:
                            raise GuardrailViolation("invalid_proposal", str(pe))
                        payload = {**payload, "diff": make_diff(payload["path"], original, patched)}
                except GuardrailViolation as e:
                    yield {"type": "guardrail", "code": e.code, "message": e.message}
                    messages.append({"role": "user", "content": f"Proposal rejected: {e.message}"})
                    continue
                yield {"type": "proposal", "kind": kind, "payload": payload}
                return  # the agent stops; a human decides via POST /actions/{id}/decision

            # ---------- tool call ----------
            step_no += 1
            if action.name in TOOL_NAMES:
                try:
                    obs = tools.call(action.name, action.args)
                except (ToolError, GuardrailViolation) as e:
                    obs = f"ERROR: {getattr(e, 'message', str(e))}"
            else:
                obs = f"ERROR: unknown action '{action.name}'. Valid: {', '.join(TOOL_NAMES)}, propose_change, final_answer"

            for ev in drain_tool_guardrails():
                yield ev
            yield {"type": "step", "index": step_no, "thought": action.thought, "tool": action.name,
                   "args": action.args, "observation_preview": obs[:OBS_PREVIEW_CHARS],
                   "tokens_total": budget.tokens}
            messages.append({"role": "user", "content": f"Observation:\n{obs}"})

    except BudgetExceeded as e:
        yield {"type": "guardrail", "code": f"budget_{e.reason}",
               "message": f"Stopped: {e.reason} budget exceeded."}
        yield {"type": "needs_clarification", "message":
               "I ran out of budget before I could find a well-supported answer. "
               f"Files I looked at: {', '.join(sorted(tools.evidence.paths)) or 'none'}. "
               "Could you narrow the question?"}
