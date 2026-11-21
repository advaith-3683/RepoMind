"""Wires agent + retrieval + persistence together for one /ask request."""
from pathlib import Path
from typing import Iterator

from . import store
from .agent.fast import run_fast
from .auth import ANON_USER_ID
from .guardrails.sandbox import resolve_in_repo
from .hitl.patching import apply_edits, make_diff
from .agent.llm import build_llm
from .agent.loop import run_agent
from .agent.state import EvidenceStore
from .agent.tools import RepoTools
from .config import get_settings
from .db import get_conn
from .guardrails.budget import Budget
from .ingestion.embed import GeminiEmbedder
from .retrieval.hybrid import find_symbols, hybrid_search

_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = GeminiEmbedder(get_settings())   # holds the query-embedding caches
    return _embedder


def cost_usd(tokens_in: int, tokens_out: int) -> float:
    s = get_settings()
    return tokens_in * s.price_in_per_mtok / 1e6 + tokens_out * s.price_out_per_mtok / 1e6


def patch_diff_for(repo_id, payload: dict) -> str | None:
    """Unified diff of a patch proposal against the indexed clone (None if it can't be computed)."""
    try:
        root = Path(get_settings().repos_dir) / str(repo_id)
        original = resolve_in_repo(root, payload["path"]).read_text(encoding="utf-8")
        return make_diff(payload["path"], original, apply_edits(original, payload["edits"]))
    except Exception:  # noqa: BLE001
        return None


def stream_answer(repo: dict, question: str, mode: str | None = None, user_id: str = ANON_USER_ID) -> Iterator[dict]:
    """Yields event dicts (start, step, guardrail, approval_required, needs_clarification,
    answer, error, done). mode: 'fast' (retrieve once + 1 LLM call) or 'agent' (full loop).
    Persists the query + full trace when finished."""
    s = get_settings()
    mode = mode if mode in ("fast", "agent") else s.default_ask_mode
    repo_id = repo["id"]
    query_id = store.create_query(repo_id, question, user_id)
    budget = Budget(s.fast_max_steps if mode == "fast" else s.max_agent_steps,
                    s.max_tokens_per_query, s.query_timeout_s)
    events: list[dict] = []
    answer_text, status = None, "completed"
    llm = None

    def emit(ev: dict) -> dict:
        events.append(ev)
        return ev

    yield emit({"type": "start", "query_id": str(query_id), "mode": mode})
    try:
        embedder, llm = get_embedder(), build_llm(s)
        with get_conn() as conn:
            tools = RepoTools(
                repo_root=Path(s.repos_dir) / str(repo_id),
                search_fn=lambda q, k: hybrid_search(conn, repo_id, q, embedder, k=k, pool=s.retrieval_pool),
                symbol_fn=lambda name: find_symbols(conn, repo_id, name),
                evidence=EvidenceStore(),
            )
            gen = (run_fast(question, tools, llm, budget, k=s.fast_top_k) if mode == "fast"
                   else run_agent(question, tools, llm, budget))
            for ev in gen:
                if ev["type"] == "proposal":
                    action_id = store.create_action(query_id, repo_id, user_id, ev["kind"], ev["payload"])
                    ev = {"type": "approval_required", "action_id": str(action_id),
                          "kind": ev["kind"], "payload": ev["payload"]}
                    status = "awaiting_approval"
                elif ev["type"] == "answer":
                    answer_text = ev["text"]
                elif ev["type"] == "needs_clarification":
                    status = "needs_clarification"
                    if mode == "fast":
                        ev = {**ev, "suggest_mode": "agent"}
                elif ev["type"] == "error":
                    status = "error"
                yield emit(ev)
    except Exception as e:  # noqa: BLE001
        status = "error"
        yield emit({"type": "error", "code": "internal", "message": "Something went wrong answering that.",
                    "detail": str(e)[:200]})

    cost = cost_usd(budget.tokens_in, budget.tokens_out)
    try:
        store.finish_query(query_id, answer_text, status)
        store.save_trace(query_id, events, budget.tokens_in, budget.tokens_out, budget.elapsed_ms, cost)
    except Exception:  # noqa: BLE001  (persistence failure must not break the stream)
        pass
    yield {"type": "done", "query_id": str(query_id), "status": status, "mode": mode,
           "usage": {"steps": budget.steps, "tokens_in": budget.tokens_in, "tokens_out": budget.tokens_out,
                     "latency_ms": budget.elapsed_ms, "cost_usd": round(cost, 6),
                     "llm_cache_hits": getattr(llm, "hits", 0)}}
