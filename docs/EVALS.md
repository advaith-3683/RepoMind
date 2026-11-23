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
Free-tier Groq allows ~8K tokens/minute, so `--sleep` matters; a rate-limit error is retried once after 65 s and reported in
the error rate if it persists. `LLM_CACHE=off` gives honest numbers; the cache is for cheap reruns.
