"""Fast mode: retrieve once, then ONE LLM call (plus at most a citation retry).
Typically 1 API call per question instead of 3-6 for the full agent loop. It uses the same
tools, guardrails, citation verification and event types as agent mode, but cannot propose
changes or make follow-up tool calls. If it can't ground an answer, the UI suggests Agent mode."""
from typing import Iterator

from ..guardrails.budget import Budget
from .llm import LLM
from .loop import OBS_PREVIEW_CHARS, run_agent
from .prompts import FAST_SYSTEM_PROMPT
from .tools import RepoTools


def run_fast(question: str, tools: RepoTools, llm: LLM, budget: Budget, k: int = 5) -> Iterator[dict]:
    from ..config import get_settings
    mode = get_settings().retrieval_mode
    obs = tools.search_code(question, k)
    yield from tools.guardrail_events          # e.g. indirect-injection lines neutralized during retrieval
    yield {"type": "step", "index": 1, "thought": f"Fast mode: single {mode} retrieval.",
           "tool": "search_code", "args": {"query": question, "k": k},
           "observation_preview": obs[:OBS_PREVIEW_CHARS], "tokens_total": 0}

    if obs == "No results.":
        yield {"type": "answer", "text": "I couldn't find any code in this repository related to that question.",
               "citations": [], "found": False, "confidence": "low", "groundedness": 1.0}
        return

    seed = [{"role": "user", "content": f"Question: {question}\n\nRetrieved code:\n{obs}"}]
    yield from run_agent(question, tools, llm, budget,
                         system_prompt=FAST_SYSTEM_PROMPT, seed_messages=seed, final_only=True)
