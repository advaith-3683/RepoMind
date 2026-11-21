import json

from app.agent.llm import LLMResult
from app.agent.loop import parse_action, run_agent
from app.agent.state import EvidenceStore
from app.agent.tools import RepoTools
from app.guardrails.budget import Budget
from tests.conftest import make_hit


class FakeLLM:
    """Replays a scripted list of actions (dicts or raw strings)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def complete(self, system, messages):
        self.calls += 1
        item = self.script.pop(0)
        text = item if isinstance(item, str) else json.dumps(item)
        return LLMResult(text, tokens_in=100, tokens_out=20)


def act(action, **args):
    return {"thought": "t", "action": action, "args": args}


def tools_for(repo, hits=None):
    hits = hits if hits is not None else [make_hit()]
    return RepoTools(repo, search_fn=lambda q, k: hits, symbol_fn=lambda n: hits, evidence=EvidenceStore())


def budget(steps=8, tokens=100_000):
    return Budget(max_steps=steps, max_tokens=tokens, timeout_s=60)


def run(repo, script, **kw):
    llm = FakeLLM(script)
    t = tools_for(repo, kw.pop("hits", None))
    b = kw.pop("budget", budget())
    events = list(run_agent("where is login?", t, llm, b, **kw))
    return events, t, llm


def types(events):
    return [e["type"] for e in events]


# ---------- happy path ----------
def test_search_then_grounded_answer(repo):
    events, t, _ = run(repo, [
        act("search_code", query="login"),
        act("final_answer", answer="`login` calls `make_token` [app/auth.py:3-5].", found=True),
    ])
    assert types(events) == ["step", "answer"]
    ans = events[-1]
    assert ans["confidence"] == "high" and ans["citations"] == [{"path": "app/auth.py", "start": 3, "end": 5}]
    assert "app/auth.py" in t.evidence.paths


def test_read_file_numbers_lines_and_records_evidence(repo):
    events, t, _ = run(repo, [
        act("read_file", path="app/auth.py", start=3, end=5),
        act("final_answer", answer="See [app/auth.py:3-5]", found=True),
    ])
    assert events[0]["tool"] == "read_file" and "app/auth.py" in t.evidence.paths
    obs = t.read_file("app/auth.py", 3, 5)
    assert "3: def login" in obs and "<untrusted_code" in obs
    assert types(events)[-1] == "answer"


# ---------- citation guardrail ----------
def test_bad_citation_triggers_retry_then_passes(repo):
    events, _, llm = run(repo, [
        act("search_code", query="login"),
        act("final_answer", answer="It is in [app/ghost.py:1-9]", found=True),
        act("final_answer", answer="It is in [app/auth.py:3-5]", found=True),
    ])
    assert types(events) == ["step", "guardrail", "answer"]
    assert events[1]["code"] == "citation_check_failed"
    assert events[-1]["confidence"] == "high" and llm.calls == 3


def test_persistent_bad_citation_needs_clarification(repo):
    events, _, _ = run(repo, [
        act("search_code", query="login"),
        act("final_answer", answer="It is in [app/ghost.py:1-9]", found=True),
        act("final_answer", answer="It is in [app/ghost.py:1-9]", found=True),
    ])
    assert types(events)[-1] == "needs_clarification"
    assert "answer" not in types(events)


def test_partial_bad_citations_downgrade_confidence(repo):
    events, _, _ = run(repo, [
        act("search_code", query="login"),
        act("final_answer", answer="A [app/auth.py:3-5] B [app/ghost.py:1-2]", found=True),
        act("final_answer", answer="A [app/auth.py:3-5] B [app/ghost.py:1-2]", found=True),
    ])
    ans = events[-1]
    assert ans["type"] == "answer" and ans["confidence"] == "low" and "ghost" not in ans["text"]


def test_not_found_answer_allowed_without_citations(repo):
    events, _, _ = run(repo, [act("final_answer", answer="Nothing about billing in this repo.", found=False)])
    assert events[-1]["type"] == "answer" and events[-1]["found"] is False


# ---------- budgets ----------
def test_step_budget_stops_runaway_agent(repo):
    events, _, llm = run(repo, [act("search_code", query="x")] * 10, budget=budget(steps=3))
    assert llm.calls == 3
    assert types(events)[-2:] == ["guardrail", "needs_clarification"]
    assert events[-2]["code"] == "budget_steps"


def test_token_budget(repo):
    events, _, _ = run(repo, [act("search_code", query="x")] * 10, budget=budget(tokens=250))
    assert events[-2]["code"] == "budget_tokens"


# ---------- robustness ----------
def test_invalid_json_recovers_then_gives_up(repo):
    events, _, _ = run(repo, ["not json", act("final_answer", answer="nope", found=False)])
    assert events[-1]["type"] == "answer"
    events, _, _ = run(repo, ["bad", "bad", "bad"])
    assert events[-1]["type"] == "error" and events[-1]["code"] == "bad_model_output"


def test_parse_action_handles_code_fences_and_prose():
    a = parse_action('```json\n{"action": "list_dir", "args": {"path": "."}}\n```')
    assert a.name == "list_dir"
    assert parse_action('Sure! {"action":"final_answer","args":{"answer":"x"}}').name == "final_answer"


def test_unknown_tool_is_reported_not_crashed(repo):
    events, _, _ = run(repo, [act("rm_rf", path="/"), act("final_answer", answer="x", found=False)])
    assert events[0]["type"] == "step" and "unknown action" in events[0]["observation_preview"]


# ---------- tool sandbox via the agent ----------
def test_path_traversal_and_secret_files_blocked_via_agent(repo):
    events, _, _ = run(repo, [
        act("read_file", path="../../etc/passwd"),
        act("read_file", path=".env"),
        act("final_answer", answer="x", found=False),
    ])
    assert events[0]["observation_preview"].startswith("ERROR")
    assert events[1]["observation_preview"].startswith("ERROR")


def test_secrets_redacted_in_observations(repo):
    t = tools_for(repo)
    obs = t.read_file("config.py")
    assert "hunter2hunter2" not in obs and "[REDACTED]" in obs


def test_indirect_injection_in_repo_file_is_neutralized(repo):
    events, t, _ = run(repo, [
        act("read_file", path="app/evil.py"),
        act("final_answer", answer="It has a function `harmless` [app/evil.py:2-3]", found=True),
    ])
    assert any(e.get("code") == "indirect_injection" for e in events)
    obs = t.read_file("app/evil.py")
    assert "system prompt" not in obs.lower() and "neutralized" in obs


def test_list_dir_hides_git_and_secrets(repo):
    (repo / ".git").mkdir()
    out = tools_for(repo).list_dir(".")
    assert "app/" in out and ".env.example" in out
    assert ".git" not in out.split() and ".env" not in out.split()


# ---------- HITL proposals ----------
GOOD_EDIT = [{"old": "    return token\n", "new": "    return token.lower()\n"}]


def test_patch_proposal_requires_file_was_read(repo):
    events, _, _ = run(repo, [
        act("propose_change", kind="patch", title="Fix", body="why", path="app/auth.py", edits=GOOD_EDIT),
        act("read_file", path="app/auth.py"),
        act("propose_change", kind="patch", title="Fix login", body="why", path="app/auth.py", edits=GOOD_EDIT),
    ])
    assert types(events) == ["guardrail", "step", "proposal"]
    assert events[0]["code"] == "invalid_proposal"
    p = events[-1]
    assert p["kind"] == "patch" and p["payload"]["path"] == "app/auth.py"
    assert "+    return token.lower()" in p["payload"]["diff"]      # server-computed diff


def test_patch_with_non_matching_edit_is_rejected_then_retried(repo):
    bad = [{"old": "does not exist", "new": "x"}]
    events, _, _ = run(repo, [
        act("read_file", path="app/auth.py"),
        act("propose_change", kind="patch", title="Fix", body="why", path="app/auth.py", edits=bad),
        act("propose_change", kind="patch", title="Fix", body="why", path="app/auth.py", edits=GOOD_EDIT),
    ])
    assert types(events) == ["step", "guardrail", "proposal"]
    assert events[1]["code"] == "invalid_proposal"


def test_issue_proposal_pauses_agent(repo):
    events, _, llm = run(repo, [act("propose_change", kind="issue", title="Typo", body="Fix the typo in README")])
    assert types(events) == ["proposal"] and llm.calls == 1
