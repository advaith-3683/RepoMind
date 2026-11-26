# RepoMind

Agentic RAG assistant for codebases (Gemini embeddings, Groq answers, free-tier friendly: response cache + fast mode): ask questions about a GitHub repo and get answers with verified `file:line` citations. Built with guardrails (injection defence, sandboxed read-only tools, budgets, citation verification) and a human-in-the-loop approval gate for proposed changes, plus an eval harness.

**Full details, architecture, status and next steps: [`docs/PROJECT_REPORT.md`](docs/PROJECT_REPORT.md)**

## Live app and pages
- App: https://repomind-gt4x.onrender.com (sign in with GitHub to ask questions; the free Render instance sleeps, so the first load can take about 30 seconds)
- `/evaluations`: measured results (public, no login). On a 32-question golden set for one repo: 25 of 27 answerable questions answered in two separate runs, 5 of 5 out-of-scope questions declined, 0 errors, 12 of 12 red-team attacks blocked. One repository and one model; the limits are listed on the page and in [`docs/EVALS.md`](docs/EVALS.md)
- `/documentation`: how Fast and Agent mode work, how citations are verified, guardrails, and how to read the evaluations
- Light and dark theme (toggle in the header); the current run is restored after a page reload

## Quick start
```bash
cd backend
pip install -r requirements.txt
cp ../.env.example .env          # fill in GEMINI_API_KEY (embeddings), LLM_API_KEY (Groq) and DATABASE_URL
# run backend/sql/schema.sql once in the Supabase SQL editor
python -m pytest -q              # 319 tests (317 pass, 2 skip on Windows), no network or DB needed
uvicorn app.main:app --reload    # http://localhost:8000  (API + frontend)
python -m app.evals.run --redteam
```

On Windows with the official PostgreSQL 19 Command Line Tools installed, start the
backend from the repository root:

```powershell
python scripts/start_backend.py
# Optional read-only connection verification (does not start the server):
python scripts/start_backend.py --check-db
```

The launcher uses `backend/.venv`, reads `backend/.env`, and selects Psycopg's Python
implementation using child-process-only settings. The calling shell is unchanged.
Use `--postgres-bin` for a different official client installation, `--port` for another
local port, or `--no-reload` to disable development reload. It does not change
system-wide PATH, execution policy, or other security settings.

Repository endpoints require authentication when `AUTH_REQUIRED=true`. Authenticated
live frontend use still needs the sign-in flow and bearer-token handling; the launcher
does not bypass authentication.

## Layout
```
backend/app/guardrails/   input, sandbox, secrets, budget, output checks
backend/app/ingestion/    clone, chunker, embeddings, pipeline
backend/app/retrieval/    hybrid search (pgvector + full-text + RRF + rerank)
backend/app/agent/        loop, tools, prompts, LLM interface
backend/app/hitl/         approval-gate state machine
backend/app/evals/        metrics, red-team set, eval runner
frontend/                 plain HTML/CSS/JS (built with Codex, see docs/FRONTEND_BRIEF.md)
docs/                     report, API contract, frontend brief
```
