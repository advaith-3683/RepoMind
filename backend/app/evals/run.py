"""Eval harness.

  python -m app.evals.run --redteam                         # offline, no DB / API needed
  python -m app.evals.run --retrieval --golden app/evals/golden_set.json   # vector vs keyword vs hybrid (needs DB + Gemini)
  python -m app.evals.run --agent     --golden app/evals/golden_set.json   # end-to-end agent (needs DB + Gemini)
  add --mode fast|agent --sleep 20 (pace for free-tier limits) --limit 3 (smoke test) --repo-url <url>

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
            for q in _answerable(golden):
                hits = hybrid_search(conn, golden["repo_id"], q["question"], emb, k=max(ks), pool=s.retrieval_pool, mode=mode)
                paths = [h.path for h in hits]
                for k in ks:
                    per_k[k].append(metrics.recall_at_k(paths, q["expected_files"], k))
                rr.append(metrics.reciprocal_rank(paths, q["expected_files"]))
            out[mode] = {**{f"recall@{k}": metrics.mean(v) for k, v in per_k.items()}, "mrr": metrics.mean(rr)}
    return out


def _answerable(golden: dict) -> list[dict]:
    """Questions that have a correct answer in the repo (decline questions are scored separately)."""
    return [q for q in golden["questions"] if not q.get("should_decline")]


def _ask_once(repo: dict, q: dict, mode: str) -> dict:
    from ..service import stream_answer

    t0 = time.monotonic()
    row = {"id": q["id"], "answer": "", "cited": [], "found": None, "groundedness": 0.0,
           "status": None, "error": None, "error_detail": None, "models": [], "usage": {}}
    try:
        for ev in stream_answer(repo, q["question"], mode):
            if ev["type"] == "answer":
                row.update(answer=ev["text"], cited=[c["path"] for c in ev.get("citations", [])],
                           found=ev.get("found", True), groundedness=ev.get("groundedness", 0.0))
            elif ev["type"] == "error":
                row["error"] = ev.get("code")
                row["error_detail"] = (ev.get("detail") or "")[:160] or None
            elif ev["type"] == "done":
                row.update(status=ev.get("status"), usage=ev.get("usage", {}), models=ev.get("usage", {}).get("models", []))
    except Exception as e:  # noqa: BLE001  (one bad question must not kill the whole run)
        row["error"] = f"exception:{type(e).__name__}"
        row["error_detail"] = str(e)[:160] or None
    row["latency_ms"] = row["usage"].get("latency_ms", int((time.monotonic() - t0) * 1000))
    return row


def summarize_agent_rows(rows: list[dict], questions: list[dict], mode: str = "agent") -> dict:
    """Pure aggregation (unit-tested). Unanswered/errored answerable questions count as 0 recall, so
    the headline numbers cannot be inflated by only scoring the questions that happened to work."""
    by_id = {q["id"]: q for q in questions}
    ans_rows = [r for r in rows if not by_id[r["id"]].get("should_decline")]
    decl_rows = [r for r in rows if by_id[r["id"]].get("should_decline")]

    def answered(r):
        return bool(r["answer"]) and r["found"] is not False and r["status"] == "completed"

    recalls, hits = [], []
    for r in ans_rows:
        q = by_id[r["id"]]
        ok = answered(r)
        recalls.append(metrics.recall_at_k(r["cited"], q.get("expected_files", []), len(r["cited"]) or 1) if ok else 0.0)
        hits.append(metrics.keyword_hit_rate(r["answer"], q.get("expected_keywords", [])) if ok else 0.0)
    declined = [r for r in decl_rows if r["found"] is False or r["status"] == "needs_clarification"]
    ok_ans = [r for r in ans_rows if not r["error"]]
    ok_decl = [r for r in decl_rows if not r["error"]]
    ok_declined = [r for r in ok_decl if r["found"] is False or r["status"] == "needs_clarification"]
    lat = [r["latency_ms"] for r in rows]
    cost = [r["usage"].get("cost_usd", 0.0) for r in rows]
    toks = [r["usage"].get("tokens_in", 0) + r["usage"].get("tokens_out", 0) for r in rows]
    return {
        "mode": mode,
        "n": len(rows),
        "n_answerable": len(ans_rows),
        "n_should_decline": len(decl_rows),
        "answer_rate": metrics.mean([1.0 if answered(r) else 0.0 for r in ans_rows]),
        "file_recall": metrics.mean(recalls),
        "keyword_hit": metrics.mean(hits),
        "groundedness": metrics.mean([r["groundedness"] for r in ans_rows if answered(r)]),
        "decline_accuracy": (len(declined) / len(decl_rows)) if decl_rows else None,
        "error_rate": metrics.mean([1.0 if r["error"] else 0.0 for r in rows]),
        "n_errors": sum(1 for r in rows if r["error"]),
        "models": sorted({m for r in rows for m in r.get("models", [])}),
        # Same metrics over questions that did NOT error (diagnostic only; the headline numbers above stay strict).
        "answer_rate_ex_errors": metrics.mean([1.0 if answered(r) else 0.0 for r in ok_ans]) if ok_ans else None,
        "decline_accuracy_ex_errors": (len(ok_declined) / len(ok_decl)) if ok_decl else None,
        "latency_p50_ms": metrics.percentile(lat, 50),
        "latency_p95_ms": metrics.percentile(lat, 95),
        "avg_cost_usd": metrics.mean(cost),
        "avg_tokens": metrics.mean(toks),
        "rows": [{"id": r["id"], "status": r["status"], "found": r["found"], "error": r["error"],
                  "error_detail": r.get("error_detail"), "models": r.get("models", []), "cited": r["cited"], "latency_ms": r["latency_ms"], "retried": r.get("retried", False)}
                 for r in rows],
    }


def split_questions(questions: list[dict]) -> tuple[list[dict], list[dict]]:
    """First half = tune set (used to PICK settings), second half = held-out test set (only REPORTED)."""
    cut = (len(questions) + 1) // 2
    return questions[:cut], questions[cut:]


def score_rankings(rankings: list[tuple[list[str], list[str]]], ks=(1, 3, 5)) -> dict:
    """rankings = [(retrieved_paths, expected_paths)]. Pure; unit-tested."""
    out = {f"recall@{k}": metrics.mean([metrics.recall_at_k(r, e, k) for r, e in rankings]) for k in ks}
    out["mrr"] = metrics.mean([metrics.reciprocal_rank(r, e) for r, e in rankings])
    return out


def pick_best(variants: list[dict]) -> dict:
    """Choose by TUNE-set recall@5 + MRR only; ties go to the smaller keyword weight (simpler, closer to vector)."""
    return max(variants, key=lambda v: (round(v["tune"]["recall@5"] + v["tune"]["mrr"], 6), -v["kw_weight"], not v["prefix"]))


SWEEP_WEIGHTS = (0.0, 0.1, 0.25, 0.5, 1.0)


def run_retrieval_sweep(golden: dict) -> dict:
    from ..config import get_settings
    from ..db import get_conn
    from ..retrieval.hybrid import fuse, keyword_search, vector_search
    from ..service import get_embedder

    s, emb = get_settings(), get_embedder()
    qs = _answerable(golden)
    tune_q, test_q = split_questions(qs)
    tune_ids = {q["id"] for q in tune_q}
    cand: dict[str, tuple] = {}
    with get_conn() as conn:
        for q in qs:   # candidate lists fetched once per question, then re-fused offline for every setting
            vec = vector_search(conn, golden["repo_id"], emb.embed_query(q["question"]), s.retrieval_pool)
            cand[q["id"]] = (vec, keyword_search(conn, golden["repo_id"], q["question"], s.retrieval_pool, False),
                             keyword_search(conn, golden["repo_id"], q["question"], s.retrieval_pool, True))

    def evaluate(rank_fn) -> dict:
        per = {"tune": [], "test": []}
        for q in qs:
            paths = [h.path for h in rank_fn(q)]
            per["tune" if q["id"] in tune_ids else "test"].append((paths, q["expected_files"]))
        return {"tune": score_rankings(per["tune"]), "test": score_rankings(per["test"]),
                "all": score_rankings(per["tune"] + per["test"])}

    baseline = evaluate(lambda q: cand[q["id"]][0])
    variants = []
    for prefix in (False, True):
        for w in SWEEP_WEIGHTS:
            r = evaluate(lambda q, w=w, prefix=prefix: fuse(cand[q["id"]][0], cand[q["id"]][2 if prefix else 1], q["question"], w))
            variants.append({"kw_weight": w, "prefix": prefix, **r})
    return {"n_tune": len(tune_q), "n_test": len(test_q), "vector_baseline": baseline,
            "variants": variants, "selected": pick_best(variants)}


def sweep_markdown(sw: dict) -> str:
    def row(label, v):
        return (f"| {label} | {v['tune']['recall@5']:.2f} / {v['tune']['mrr']:.2f} | "
                f"{v['test']['recall@5']:.2f} / {v['test']['mrr']:.2f} | {v['all']['recall@5']:.2f} / {v['all']['mrr']:.2f} |")

    sel = sw["selected"]
    lines = [f"### Retrieval sweep (tune n={sw['n_tune']}, held-out n={sw['n_test']}; cells = recall@5 / MRR)",
             "| setting | tune | held-out | all |", "|---|---|---|---|", row("vector only (baseline)", sw["vector_baseline"])]
    for v in sw["variants"]:
        mark = " **<- selected on tune set**" if v is sel else ""
        lines.append(row(f"hybrid kw_weight={v['kw_weight']} prefix={'on' if v['prefix'] else 'off'}{mark}", v))
    return "\n".join(lines) + "\n"


MAX_RATE_LIMIT_RETRIES = 2
EARLY_STOP_AFTER = 3   # consecutive questions that still hit a rate limit after all retries => quota is gone, abort


def run_agent_eval(golden: dict, mode: str = "agent", sleep_s: float = 0.0, limit: int | None = None) -> dict:
    """sleep_s paces questions (free-tier LLM limits are per minute). A provider rate-limit error is
    retried (up to MAX_RATE_LIMIT_RETRIES times) after the minute window; it is still reported in
    error_rate/retried if it persists."""
    from .. import store

    repo = store.get_repo(golden["repo_id"])
    questions = golden["questions"][:limit] if limit else golden["questions"]
    rows, streak, aborted = [], 0, False
    for i, q in enumerate(questions):
        if i and sleep_s:
            time.sleep(sleep_s)
        row = _ask_once(repo, q, mode)
        retried = False
        for _ in range(MAX_RATE_LIMIT_RETRIES):
            if row["error"] != "rate_limited":
                break
            time.sleep(max(sleep_s, 65))
            row = _ask_once(repo, q, mode)
            retried = True
        if retried:
            row["retried"] = True
        rows.append(row)
        streak = streak + 1 if row["error"] == "rate_limited" else 0
        if streak >= EARLY_STOP_AFTER and i < len(questions) - 1:
            aborted = True   # more attempts only burn quota and produce a meaningless result
            break
    summary = summarize_agent_rows(rows, questions[:len(rows)], mode)
    summary["aborted"], summary["n_planned"] = aborted, len(questions)
    return summary


def resolve_repo_id(golden: dict, repo_url: str | None = None) -> str:
    """repo_id from the golden file, or look it up by URL (--repo-url / golden['repo_url']). The repo must be
    indexed and ready: evals never trigger indexing."""
    from .. import store

    url = repo_url or golden.get("repo_url")
    if url:
        from ..ingestion.clone import parse_github_url

        owner, name = parse_github_url(url)
        repo = store.get_repo_by_url(f"https://github.com/{owner}/{name}")
        if not repo:
            raise SystemExit(f"{url} is not indexed. Add it in the app first and wait for 'Ready'.")
        if repo["status"] != "ready":
            raise SystemExit(f"{url} is '{repo['status']}', not 'ready'.")
        return str(repo["id"])
    rid = golden.get("repo_id", "")
    if not rid or rid.startswith("PUT-"):
        raise SystemExit("Golden file needs 'repo_url' (or a real 'repo_id'), or pass --repo-url.")
    return rid


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
    if "sweep" in results:
        lines += [sweep_markdown(results["sweep"])]
    if "agent" in results:
        a = results["agent"]
        lines += [f"### End-to-end ({a.get('mode', 'agent')} mode)", "| metric | value |", "|---|---|",
                  f"| questions (answerable / should decline) | {a['n']} ({a['n_answerable']} / {a['n_should_decline']}) |",
                  f"| answer rate | {a['answer_rate']:.0%} |", f"| file recall | {a['file_recall']:.2f} |",
                  f"| keyword hit | {a['keyword_hit']:.2f} |", f"| groundedness | {a['groundedness']:.2f} |",
                  "| correctly declined | " + ("n/a" if a["decline_accuracy"] is None else f"{a['decline_accuracy']:.0%}") + " |",
                  f"| error rate | {a['error_rate']:.0%} ({a.get('n_errors', 0)} errored) |",
                  f"| latency p50 / p95 | {a['latency_p50_ms']:.0f} / {a['latency_p95_ms']:.0f} ms |",
                  f"| avg cost / query | ${a['avg_cost_usd']:.4f} |", ""]
        if a.get("aborted"):
            lines += [f"> **Run aborted after {a['n']} of {a['n_planned']} questions: {EARLY_STOP_AFTER} in a row hit the "
                      "provider rate limit even after retries.** The numbers cover only the questions that ran. "
                      "Not a valid result; wait for the quota to reset.", ""]
        if len(a.get("models", [])) > 1:
            lines += ["> **Warning: answers came from more than one model (" + ", ".join(a["models"]) + ").** "
                      "Do not report this as a single-model result.", ""]
        elif a.get("models"):
            lines += [f"Model: `{a['models'][0]}`", ""]
        if a.get("n_errors"):
            ex, dx = a.get("answer_rate_ex_errors"), a.get("decline_accuracy_ex_errors")
            lines += [f"> **Warning: {a['n_errors']} of {a['n']} questions errored (e.g. provider rate limits).** "
                      "The headline numbers count those as failures, so they measure the provider limit, not answer quality. "
                      "Do not compare this run with another. Excluding errored questions: answer rate "
                      + ("n/a" if ex is None else f"{ex:.0%}") + ", declined "
                      + ("n/a" if dx is None else f"{dx:.0%}") + ". Re-run after the quota resets.", ""]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--redteam", action="store_true")
    ap.add_argument("--retrieval", action="store_true")
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--sweep", action="store_true", help="retrieval settings sweep with a tune/held-out split (needs DB + Gemini, no LLM)")
    ap.add_argument("--golden", default=str(HERE / "golden_set.json"))
    ap.add_argument("--mode", choices=["fast", "agent"], default="agent", help="answer mode for --agent runs")
    ap.add_argument("--sleep", type=float, default=0.0, help="seconds between questions (free-tier LLM limits)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N questions (smoke test)")
    ap.add_argument("--repo-url", default=None, help="look up the indexed repo by URL instead of repo_id in the golden file")
    args = ap.parse_args()
    if not (args.redteam or args.retrieval or args.agent or args.sweep):
        ap.error("choose at least one of --redteam --retrieval --agent --sweep")

    results: dict = {"timestamp": datetime.now(timezone.utc).isoformat()}
    golden = json.loads(Path(args.golden).read_text()) if (args.retrieval or args.agent or args.sweep) else None
    if golden is not None:
        golden["repo_id"] = resolve_repo_id(golden, args.repo_url)
        if args.limit:
            golden["questions"] = golden["questions"][: args.limit]
    if args.redteam:
        results["redteam"] = run_redteam()
    if args.retrieval:
        results["retrieval"] = run_retrieval(golden)
    if args.sweep:
        results["sweep"] = run_retrieval_sweep(golden)
    if args.agent:
        results["agent"] = run_agent_eval(golden, args.mode, args.sleep)

    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}.json"
    out.write_text(json.dumps(results, indent=2))
    print(to_markdown(results))
    print(f"saved -> {out}")


if __name__ == "__main__":
    main()
