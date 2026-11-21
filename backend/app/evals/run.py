"""Eval harness.

  python -m app.evals.run --redteam                         # offline, no DB / API needed
  python -m app.evals.run --retrieval --golden app/evals/golden_set.json   # vector vs keyword vs hybrid (needs DB + Gemini)
  python -m app.evals.run --agent     --golden app/evals/golden_set.json   # end-to-end agent (needs DB + Gemini)

Results are written to app/evals/results/<timestamp>.json and printed as a markdown table.
The --retrieval / --agent paths need live services and are NOT covered by unit tests."""
import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from ..guardrails.errors import GuardrailViolation
from ..guardrails.input import validate_query
from . import metrics

HERE = Path(__file__).parent
RESULTS = HERE / "results"


def run_redteam() -> dict:
    data = json.loads((HERE / "redteam.json").read_text())
    s_max = 1000

    def blocked(q: str) -> bool:
        try:
            validate_query(q, s_max)
            return False
        except GuardrailViolation:
            return True

    attacks = [(q, blocked(q)) for q in data["blocked"]]
    benign = [(q, blocked(q)) for q in data["benign"]]
    return {
        "attacks_total": len(attacks),
        "attacks_blocked": sum(1 for _, b in attacks if b),
        "block_rate": metrics.mean([1.0 if b else 0.0 for _, b in attacks]),
        "benign_total": len(benign),
        "false_positives": sum(1 for _, b in benign if b),
        "missed": [q for q, b in attacks if not b],
        "false_positive_prompts": [q for q, b in benign if b],
    }


def run_retrieval(golden: dict, ks=(1, 3, 5)) -> dict:
    from ..config import get_settings
    from ..db import get_conn
    from ..retrieval.hybrid import hybrid_search
    from ..service import get_embedder

    s, emb, out = get_settings(), get_embedder(), {}
    with get_conn() as conn:
        for mode in ("vector", "keyword", "hybrid"):
            per_k = {k: [] for k in ks}
            rr = []
            for q in golden["questions"]:
                hits = hybrid_search(conn, golden["repo_id"], q["question"], emb, k=max(ks), pool=s.retrieval_pool, mode=mode)
                paths = [h.path for h in hits]
                for k in ks:
                    per_k[k].append(metrics.recall_at_k(paths, q["expected_files"], k))
                rr.append(metrics.reciprocal_rank(paths, q["expected_files"]))
            out[mode] = {**{f"recall@{k}": metrics.mean(v) for k, v in per_k.items()}, "mrr": metrics.mean(rr)}
    return out


def run_agent_eval(golden: dict, mode: str = "agent") -> dict:
    from ..service import stream_answer
    from .. import store

    repo = store.get_repo(golden["repo_id"])
    rows = []
    for q in golden["questions"]:
        t0 = time.monotonic()
        answer, done, status = "", {}, "?"
        cited: list[str] = []
        groundedness = 0.0
        for ev in stream_answer(repo, q["question"], mode):
            if ev["type"] == "answer":
                answer = ev["text"]
                cited = [c["path"] for c in ev["citations"]]
                groundedness = ev["groundedness"]
            elif ev["type"] == "done":
                done = ev
        rows.append({
            "id": q["id"],
            "status": done.get("status"),
            "file_recall": metrics.recall_at_k(cited, q["expected_files"], len(cited) or 1),
            "keyword_hit": metrics.keyword_hit_rate(answer, q.get("expected_keywords", [])),
            "groundedness": groundedness,
            "latency_ms": done.get("usage", {}).get("latency_ms", int((time.monotonic() - t0) * 1000)),
            "cost_usd": done.get("usage", {}).get("cost_usd", 0.0),
            "tokens": done.get("usage", {}).get("tokens_in", 0) + done.get("usage", {}).get("tokens_out", 0),
        })
    lat = [r["latency_ms"] for r in rows]
    return {
        "mode": mode,
        "n": len(rows),
        "file_recall": metrics.mean([r["file_recall"] for r in rows]),
        "keyword_hit": metrics.mean([r["keyword_hit"] for r in rows]),
        "groundedness": metrics.mean([r["groundedness"] for r in rows]),
        "latency_p50_ms": metrics.percentile(lat, 50),
        "latency_p95_ms": metrics.percentile(lat, 95),
        "avg_cost_usd": metrics.mean([r["cost_usd"] for r in rows]),
        "rows": rows,
    }


def to_markdown(results: dict) -> str:
    lines = []
    if "redteam" in results:
        r = results["redteam"]
        lines += ["### Red-team (input guardrails)", "| metric | value |", "|---|---|",
                  f"| attacks blocked | {r['attacks_blocked']}/{r['attacks_total']} ({r['block_rate']:.0%}) |",
                  f"| false positives | {r['false_positives']}/{r['benign_total']} |", ""]
    if "retrieval" in results:
        lines += ["### Retrieval", "| mode | recall@1 | recall@3 | recall@5 | MRR |", "|---|---|---|---|---|"]
        for mode, m in results["retrieval"].items():
            lines.append(f"| {mode} | {m['recall@1']:.2f} | {m['recall@3']:.2f} | {m['recall@5']:.2f} | {m['mrr']:.2f} |")
        lines.append("")
    if "agent" in results:
        a = results["agent"]
        lines += [f"### End-to-end ({a.get('mode', 'agent')} mode)", "| metric | value |", "|---|---|",
                  f"| questions | {a['n']} |", f"| file recall | {a['file_recall']:.2f} |",
                  f"| keyword hit | {a['keyword_hit']:.2f} |", f"| groundedness | {a['groundedness']:.2f} |",
                  f"| latency p50 / p95 | {a['latency_p50_ms']:.0f} / {a['latency_p95_ms']:.0f} ms |",
                  f"| avg cost / query | ${a['avg_cost_usd']:.4f} |", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redteam", action="store_true")
    ap.add_argument("--retrieval", action="store_true")
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--golden", default=str(HERE / "golden_set.json"))
    ap.add_argument("--mode", choices=["fast", "agent"], default="agent", help="answer mode for --agent runs")
    args = ap.parse_args()
    if not (args.redteam or args.retrieval or args.agent):
        ap.error("choose at least one of --redteam --retrieval --agent")

    results: dict = {"timestamp": datetime.now(timezone.utc).isoformat()}
    golden = json.loads(Path(args.golden).read_text()) if (args.retrieval or args.agent) else None
    if args.redteam:
        results["redteam"] = run_redteam()
    if args.retrieval:
        results["retrieval"] = run_retrieval(golden)
    if args.agent:
        results["agent"] = run_agent_eval(golden, args.mode)

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2))
    print(to_markdown(results))
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
