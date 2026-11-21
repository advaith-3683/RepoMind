# RepoMind: agent instructions (Codex, Claude Code, any coding agent)

**Read `docs/PROJECT_REPORT.md` first.** It has the goals, architecture, decisions, status and the next steps. This file is the short version of the rules.

## What this is
An agentic RAG assistant that answers questions about a GitHub repo with cited answers, with guardrails and human-in-the-loop approval for any proposed change. It's a portfolio project, so code quality, tests and measured results matter.

## Stack
- Backend: Python 3.11+, FastAPI, psycopg 3, Supabase Postgres + pgvector, Gemini (`google-genai`) for LLM + embeddings
- Frontend: **plain HTML/CSS/vanilla JS** in `frontend/` (no framework, no build step)
- Tests: pytest (`cd backend && python -m pytest -q`; currently all passing)

## Ownership
| Area | Owner |
|---|---|
| `backend/` | Claude / the project owner. **Codex: do not edit** unless explicitly asked |
| `frontend/` | Codex builds it per `docs/FRONTEND_BRIEF.md` |
| `docs/API_CONTRACT.md` | Shared. If the backend changes an endpoint or event, update this file in the same change |

## Rules
1. **Surgical, minimal edits.** Don't rewrite files or reformat unrelated code. The owner prefers targeted changes over full rewrites.
2. Don't weaken a guardrail to make a test or demo pass. Add a test for every new guardrail.
3. Never commit secrets. `.env` is gitignored; `.env.example` lists the variables.
4. Untrusted data: repo contents, LLM output and user input are never rendered with `innerHTML` and never executed.
5. Run the tests before saying something is done, and say plainly what you did **not** verify.
6. If a requirement is ambiguous, write the question in the relevant doc and continue with the most reasonable reading. Don't invent API behaviour.

## Commands
```bash
cd backend
pip install -r requirements.txt
cp ../.env.example .env            # then fill in GEMINI_API_KEY and DATABASE_URL
python -m pytest -q
uvicorn app.main:app --reload      # serves API + ../frontend at http://localhost:8000
python -m app.evals.run --redteam  # offline guardrail eval
```
