# Evals

Golden set: `backend/app/evals/golden_smartspend.json` (27 answerable questions with expected files/keywords, plus 5 questions
that have no answer in the repo and must be declined). Written against `advaith-3683/smartspendAI` commit `4a860dd`; the expected
files and keywords were checked against that commit's code. If the repo changes, re-validate before trusting old numbers.

## What is measured
| Metric | Meaning |
|---|---|
| recall@k / MRR (`--retrieval`) | vector vs keyword vs hybrid: are the expected files in the top k retrieved chunks (answerable questions only) |
| answer rate | answerable questions that produced a completed, cited answer (errors and refusals count as misses) |
| file recall | share of expected files the answer actually cited (misses count as 0) |
| keyword hit | share of expected keywords the answer mentions |
| groundedness | overlap of the answer with retrieved code (answered questions) |
| correctly declined | should-decline questions where the system said it could not find it instead of inventing an answer |
| error rate, latency p50/p95, tokens, cost | reliability and cost |

Small sample (32 questions, one repo): report it as that, not as a general accuracy figure.

## Run (GitHub Actions, Linux; recommended)
Repo → Settings → Secrets and variables → Actions. Secrets: `DATABASE_URL` (Supabase Session Pooler), `GEMINI_API_KEY`, `LLM_API_KEY`;
optional `LLM_FALLBACK_API_KEY`; optional variable `LLM_FALLBACK_MODEL`. Then Actions → evals → Run workflow:
first with limit 3 (smoke test), then limit 0 (all questions). The repo must already be indexed and Ready in the app.
Results appear in the job summary and as the `eval-results` artifact.

## Run locally (Linux/WSL/macOS with the backend installed)
```bash
cd backend
python -m app.evals.run --redteam
python -m app.evals.run --retrieval --agent --golden app/evals/golden_smartspend.json --mode fast --sleep 25 --limit 3
```
Free-tier Groq allows ~8K tokens/minute and 200K tokens/day, so `--sleep` matters; a rate-limit error is retried up to twice, 65 s apart,
and reported in the error rate if it persists. The run aborts after 3 consecutive questions that still hit the limit. Each row records
`error_detail` and the `models` that answered. Keep `LLM_FALLBACK_API_KEY` unset for evals: a silent switch to a different model
would mix models in one result (the summary warns if that happens). `LLM_CACHE=off` gives honest numbers; the cache is for cheap reruns.

## Results so far (smartspendAI, 27 answerable + 5 should-decline questions; Groq `openai/gpt-oss-120b`, fast mode)

Baseline end-to-end run (retrieval = hybrid with equal-weight RRF, run 2026-11-23):

| Metric | Value |
|---|---|
| answer rate | 85% (23/27) |
| correctly declined | 5/5 |
| error rate | 0% |
| file recall (strict; unanswered = 0) | 0.70 |
| keyword hit | 0.72 |
| groundedness (overlap proxy, not a fact check) | 0.98 |
| latency p50 / p95 | 9.6 s / 19.1 s |

Retrieval ablation (tune = first 14 questions, held-out = last 13; settings were chosen on the tune half only):

| Setting | Held-out recall@5 | Held-out MRR | All recall@5 / MRR |
|---|---|---|---|
| vector only | 0.92 | 0.83 | 0.96 / 0.87 |
| hybrid, equal-weight RRF (original) | 0.73 | 0.63 | 0.83 / 0.70 |
| hybrid, best on tune (keyword weight 0.1, prefix match) | 0.92 | 0.75 | 0.96 / 0.83 |

Finding: on this repo, keyword fusion did not help and equal-weight fusion hurt, so the default is now vector-only
(`RETRIEVAL_MODE=vector`; set `hybrid` to re-enable). Limits: one repository, natural-language questions written by the
project owner, 27 answerable questions, so differences of a few points are noise. Keyword search may still help
identifier-style queries (the agent's `find_symbol` tool covers those); this set does not test that.

## Run 2 (2026-11-23, vector default): INVALID, do not cite
7 of 32 questions ended in `rate_limited` even after a retry (answer rate 74%, error rate 22%, declined 40%), so those numbers measure
the provider limit, not answer quality. Cause: that day's testing used ~247K Groq tokens against a 200K/day cap, and the OpenRouter
free fallback (a different model, `google/gemma-4-31b-it:free`) was also rate-limited and ignored the citation format. On the 23 questions
that did not error, 20 were answered versus 19 in run 1, which is within noise. A valid vector-default end-to-end run is still to be done:
one full fast-mode run on a fresh quota day, no fallback key, `limit 3` first.
