# RepoMind: project report & handoff

> **Purpose of this file:** if you start a new conversation (with Claude, Codex or anyone), this file plus the repo is everything needed to continue. Last updated: 2026-11-23.

## 0. Paste this into a new conversation
```
I'm building RepoMind, an agentic RAG codebase assistant with guardrails and human-in-the-loop
approval. The repo is attached (or zipped). Read AGENTS.md and docs/PROJECT_REPORT.md first.
Check section 8 (Status) and continue from section 9 (Next steps), starting at the first unchecked
step. Make surgical, minimal edits (no full rewrites), run `cd backend && python -m pytest -q`
before saying anything is done, and tell me plainly what you did not verify.
```

## 1. What it is and why
**RepoMind** lets a user paste a public GitHub repo URL, then ask questions ("where is auth handled?", "how would I add an endpoint?"). A tool-using agent searches and reads the code and answers with **verified `file:line` citations**. It has layered **guardrails** and a **human-in-the-loop (HITL) approval gate** for any change it proposes, plus an **eval harness** that produces measurable numbers.

**Why this project:** it is a resume project meant to show depth that the existing apps (MediFlow AI, SmartSpend AI, Roamly) don't: retrieval engineering, an agent loop written by hand, safety/guardrails, and measured evaluation. The owner's stack (C++, React, FastAPI, Node, Supabase, Gemini) is reused so the build is realistic.

**Differentiators to emphasise:** retrieval chosen by measurement (an ablation on a tune/held-out split showed vector-only beat hybrid on this repo, so vector is the default); every citation verified against what the agent actually retrieved; sandboxed read-only tools; prompt-injection defence for untrusted repo content; budgets; approval gate; red-team eval set.

## 2. Decisions
| Decision | Made by | Notes |
|---|---|---|
| Build an agentic RAG project | Owner | |
| Domain = codebase Q&A ("RepoMind") | Owner agreed to Claude's proposal | Alternative offered: RAG over medical/finance docs |
| Include guardrails + HITL | Owner | |
| Frontend = plain HTML/CSS/JS, built by Codex | Owner | Replaced the earlier React idea |
| Backend = FastAPI + Supabase (pgvector) | Claude proposed from owner's existing stack | |
| **Gemini for embeddings only; answers come from an OpenAI-compatible provider: Groq primary, OpenRouter optional fallback** | Owner chose the split after the Gemini free tier dropped to ~20 requests/day; Claude picked Groq | Groq free limits are per model (e.g. gpt-oss-120b: 30 RPM, 1,000 RPD, 8K TPM, 200K TPD at time of writing; verify in the Groq console). OpenRouter `:free` models allow only 50 req/day until $10 of credits have been bought, so it is a fallback, not the main option |
| Save API calls three ways: disk cache (record/replay), OpenAI-compatible provider layer with fallback, and a **fast mode** (retrieve once + 1 LLM call; default) vs **agent mode** | Owner approved | See "Quota strategy" in section 6 |
| Hand-written agent loop with a JSON-action protocol (no LangGraph / SDK function-calling) | Claude | LLM-agnostic, testable with a fake LLM, good interview material |
| Python `ast` chunking now; tree-sitter later | Claude | Zero extra deps; exact boundaries for Python |
| HITL gates the *side effect*, not the agent's reasoning: agent stops after `propose_change`, human decides via a separate endpoint | Claude | Simple, no resumable-agent state needed |
| **Accounts via Supabase Auth, "Sign in with GitHub"**; history is private per user, indexed repos are shared | Owner asked about auth; Claude designed | One login gives an account AND the user's GitHub token. Backend verifies the bearer token with `GET /auth/v1/user` |
| **Approved changes can be published to GitHub** as a branch + PR (patch) or issue, only by an explicit click, never to the default branch | Owner asked; Claude designed | The user's token is sent per request in `X-GitHub-Token`, never stored/logged. Patches are exact search/replace `edits`; server computes the diff and re-applies the edits to the live file before touching GitHub |
| One FastAPI service serves API + static frontend at `/` | Claude | Same origin, no CORS pain; can split later |

## 3. Architecture
```
Browser (plain HTML/CSS/JS)
   │  POST /ask  → SSE stream (start, step*, guardrail*, answer | needs_clarification | approval_required, done)
   ▼
FastAPI (backend/app/main.py)
 ├─ Input guardrails ─ validate_query (length, empty, prompt-injection patterns), rate limiter
 ├─ service.stream_answer(mode=fast|agent) ─ wires everything, persists query + trace
 │    fast: retrieve once → 1 LLM call (agent/fast.py)
 │    └─ agent.loop.run_agent  (generator of events)
 │         LLM (Groq, via OpenAI-compatible API; cached + optional fallback) ⇄ JSON action ⇄ tools (read-only, sandboxed)
 │           search_code ─▶ hybrid retrieval (pgvector cosine + Postgres FTS → RRF → symbol rerank)
 │           read_file / find_symbol / list_dir
 │         Budget (steps/tokens/timeout) · citation verification · groundedness score
 │         propose_change ─▶ HITL pending_action (human approves / edits / rejects)
 ├─ Ingestion (background task): clone → chunk (ast/regex/windows) → redact secrets → Gemini embeddings → Supabase
 └─ Evals: golden set, red-team set, metrics → results JSON + markdown table
Supabase Postgres: repos, chunks(vector+tsvector), queries, traces, pending_actions, feedback
```

## 4. Tech stack
- **Backend:** Python 3.11+ (developed on 3.13), FastAPI, pydantic v2 / pydantic-settings, psycopg 3 (sync), `google-genai`
- **DB:** Supabase Postgres + pgvector (HNSW cosine index, 768-dim) + GIN full-text index
- **Embeddings:** Gemini via `google-genai` (`gemini-embedding-001`, 768-dim; env-configurable)
- **Answering LLM:** any OpenAI-compatible `/chat/completions` API through plain `httpx`: Groq by default (`openai/gpt-oss-120b`), OpenRouter as optional fallback. Gemini can still answer via `LLM_PROVIDER=gemini`
- **Frontend:** vanilla HTML/CSS/JS, no build
- **Tests:** pytest (140 tests). **Deploy:** Dockerfile → Render (API + frontend) + Supabase

## 5. Repository map
```
AGENTS.md                      rules for any coding agent (Codex reads this)
Dockerfile  .env.example  .gitignore  README.md
docs/PROJECT_REPORT.md         this file
docs/API_CONTRACT.md           endpoints + SSE event schemas (frontend source of truth)
docs/FRONTEND_BRIEF.md         full spec for Codex
frontend/index.html            PLACEHOLDER, Codex replaces
backend/sql/schema.sql         run once in Supabase SQL editor
backend/app/
  main.py        routes, error mapping, static mount      config.py   settings (env)
  service.py     per-request wiring + persistence         store.py    all SQL
  db.py          connection helper                        files.py    safe line-numbered reads
  schemas.py     request models                           cache.py    on-disk JSON cache (LLM + query embeddings)
  guardrails/    errors input sandbox secrets budget output
  ingestion/     clone chunker embed pipeline
  retrieval/     rrf hybrid
  agent/         loop fast tools prompts llm state   (llm.py = providers, fallback, cache)
  hitl/          actions (state machine + payload validation)
  evals/         metrics run redteam.json golden_set.example.json
backend/tests/   140 tests (no network/DB needed)
```

## 6. Key mechanisms
**Agent protocol.** Each turn the LLM returns one JSON object `{"thought","action","args"}`. Actions: `search_code`, `read_file`, `find_symbol`, `list_dir`, `propose_change`, `final_answer{answer,found}`. Invalid JSON gets two corrective retries, then an `error`. The loop yields events; the API layer turns them into SSE and persists them.

**Retrieval.** Vector search (Gemini `RETRIEVAL_QUERY` embedding, cosine) and keyword search (OR-ed identifier terms, `to_tsquery('simple')`) each fetch a pool of 30. Reciprocal Rank Fusion (k=60) merges them, then `symbol_boost_rerank` nudges chunks whose symbol or filename appears in the question. `mode=vector|keyword|hybrid` exists so evals can compare them. **The default is `vector`**: on the golden set, keyword fusion did not help (see `docs/EVALS.md`).

**Chunking.** Python: stdlib `ast`, one chunk per top-level function/class (decorators included); classes over 120 lines are split into header + methods; leftover module-level code becomes `module` chunks. JS/TS/Go/Rust: top-level-definition regexes. Everything else: 60-line windows with 10-line overlap. Oversized chunks split at 120 lines. Each chunk is embedded with a `# file: … | kind: symbol` header.

**Guardrails (all unit-tested unless noted).**
| Layer | Guardrail | Where |
|---|---|---|
| Input | Length/empty checks; prompt-injection regex set (12/12 red-team prompts blocked, 0/8 benign false positives) | `guardrails/input.py` |
| Input | Per-IP rate limit (in-memory sliding window) | `guardrails/budget.py` |
| Input | Repo URL must be `https://github.com/owner/repo` (no other hosts/schemes) | `ingestion/clone.py` |
| Ingestion | Repo size, file size, chunk-count caps; skips `node_modules`, lockfiles, secret files | `ingestion/pipeline.py` |
| Ingestion | Secrets redacted before storing chunks (line count preserved) | `guardrails/secrets.py` |
| Agent | Tool allowlist; read-only; path sandbox (blocks `..`, absolute paths, symlink escape, `.git`, `.env`/keys) | `guardrails/sandbox.py`, `agent/tools.py` |
| Agent | **Indirect injection defence:** repo content is wrapped in `<untrusted_code>` tags, suspicious lines are neutralized, system prompt says never to obey it | `guardrails/input.py`, `agent/prompts.py` |
| Agent | Budgets: max steps (8), tokens (60k), timeout (90s) → `needs_clarification` | `guardrails/budget.py` |
| Agent | Secret redaction on every observation and on the file-preview endpoint | `files.py`, `agent/tools.py` |
| Output | Every `[path:start-end]` citation must overlap evidence the agent actually retrieved; one retry, then strip invalid ones (confidence → low) or escalate | `guardrails/output.py`, `agent/loop.py` |
| Output | Groundedness heuristic (backticked identifiers must appear in evidence) → confidence high/low | `guardrails/output.py` |
| HITL | Patch proposals may only target a file the agent already read; payload validated and size-capped | `agent/loop.py`, `hitl/actions.py` |

**HITL.** The agent can only *propose* (`patch` or `issue`). `propose_change` ends the agent run and emits `approval_required` with an `action_id`. The human calls `POST /actions/{id}/decision` with `approve`, `edit` (plus `edited_payload`) or `reject`. State machine: `pending → approved | edited | rejected`, all terminal; a second decision returns 409; the DB update is `WHERE status='pending'` so it's atomic. Other HITL touchpoints: `needs_clarification` (low evidence / budget hit) asks the human to narrow the question; 👍/👎 + correction feedback is stored to grow the golden set.

**Quota strategy (free tiers are the main constraint).**
- *Two answer modes.* `fast` (default): one retrieval (vector by default, see `RETRIEVAL_MODE`), then a single LLM call with a dedicated prompt (`agent/fast.py`); about 1 API call per question. `agent`: the full tool loop (3-6 calls), and the only mode that can propose changes. If fast mode can't ground an answer it returns `needs_clarification` with `suggest_mode:"agent"`.
- *Provider chain.* `CachingLLM( FallbackLLM([primary, fallback]) )` built by `build_llm()` in `agent/llm.py`. The fallback is used only for rate limits, 5xx and network errors (never for bad keys or malformed requests, so config mistakes aren't masked). A 429 with a long `Retry-After` (>20s) is treated as quota exhaustion and fails over immediately instead of sleeping.
- *Disk cache.* LLM calls are cached on disk keyed by `sha256(model, system prompt, messages)`; query embeddings likewise (document embeddings live in Postgres). `LLM_CACHE=readwrite` (default) makes re-running evals nearly free; `replay` never calls the API (a miss raises `CacheMiss`), which makes eval runs deterministic and offline. Changing the prompt or retrieved code changes the key, so nothing goes stale silently.
- *Token budget on Groq.* The binding free limit is tokens (8K/min, 200K/day on gpt-oss-120b), not requests. Search previews are capped at 60 lines per chunk (`SEARCH_PREVIEW_LINES`; 30 was the original value) and `LLM_MAX_OUTPUT_TOKENS=1500` to stay inside it. Short 429s are retried using `Retry-After`.

**Evals.** Metrics (`evals/metrics.py`): `recall@k` = fraction of expected files in the top-k retrieved paths; `MRR`; `keyword_hit_rate`; latency p50/p95; cost/query; groundedness. Runs: `--redteam` (offline), `--retrieval` (vector vs keyword vs hybrid), `--sweep` (RRF weight / prefix tune vs held-out), `--agent` with `--mode fast|agent` (end-to-end). Eval rows record `error_detail` and the answering `models`; a run aborts after 3 consecutive rate-limit failures, and results carry warnings when any question errored or more than one model answered. A manual GitHub Actions workflow (`.github/workflows/evals.yml`) runs them on Linux. Results go to `backend/app/evals/results/<timestamp>.json` and print as markdown tables for the README.

**Auth & ownership.** `auth.py` verifies the Supabase access token (60s cache) and yields `User(id,email,login)`; `AUTH_REQUIRED=false` is local-dev only. Every query/trace/action/feedback row carries `user_id` and `store.py` filters on it (other users' ids → 404). RLS is enabled on all tables with no policies (the backend's DB role bypasses it; the point is that the public anon key can read nothing). Per-user rate limit and daily new-repo quota.

**GitHub publishing.** `publish.py` + `github.py`: check push permission, fetch the file from the default branch, re-apply `edits` (must each match exactly once; CRLF preserved), create branch `repomind/<slug>-<id8>`, commit, open PR; any failure after the branch exists deletes it. `publish_started_at` claim (stale after 5 min) + stored `result` make publish idempotent. Issues work for any public repo; PRs need write access (fork flow not built).

## 7. API
See `docs/API_CONTRACT.md` (authoritative). Summary: `POST /repos`, `GET /repos[/{id}]`, `GET /repos/{id}/file`, `POST /ask` (SSE), `POST /actions/{id}/decision`, `GET /actions/{id}`, `POST /feedback`, `GET /traces/{query_id}`, `GET /evals/latest`, `GET /health`. Errors are always `{"error":{"code","message"}}`.

## 8. Status
**Verified (actually run):**
- [x] 318 backend pytest tests pass (1 skipped on Windows: symlink test) and 140 frontend Node tests pass (guardrails, sandbox incl. symlink escape, secret redaction, budgets, rate limiter, citation verification, chunkers, RRF, query-term/tsquery building, rerank, eval metrics, URL validation, agent loop with a scripted fake LLM covering happy path / bad citations / budgets / invalid JSON / injection / traversal / HITL proposals, HITL state machine, SSE framing, error-code→HTTP mapping; **OpenAI-compatible client against a mock HTTP transport** (retries, Retry-After, 429/5xx/4xx), fallback chain, LLM + embedding disk cache incl. replay mode, provider factory, **fast mode** end to end with a scripted LLM)
- [x] Offline red-team eval: 12/12 attacks blocked, 0/8 false positives (on this small hand-written set; see risks)
- [x] Server boots; `/health`, static `/`, bad-URL and injection rejections verified with curl
- [x] Real `git clone` of a public repo and chunking works (156 files → 1,548 chunks, no DB needed)

**Written but NOT verified (needs your keys / a database). Expect small fixes on first run:**
- [ ] **Live provider calls.** The OpenAI-compatible client is unit-tested against a mock HTTP transport (request shape, retries, `Retry-After`, 429/5xx/4xx handling, fallback, caching) but has **never talked to real Groq/OpenRouter**. Check: the model id exists, `response_format=json_object` is accepted (else `LLM_JSON_MODE=false`), and the model reliably returns the JSON action format. Reasoning models' `<think>` blocks are stripped.
- [ ] **Gemini embeddings** (`GeminiEmbedder`): the call shape (`client.models.embed_content(model="gemini-embedding-001", contents=[...], config=EmbedContentConfig(output_dimensionality=..., task_type=...))`) was checked against the current `google-genai` docs, but it has not run against the live API, and the `.embeddings[i].values` response access is assumed. Isolated in `ingestion/embed.py`. Also unverified: the optional `GeminiLLM` answering path.
- [ ] All SQL in `store.py` and `retrieval/hybrid.py`, `schema.sql` (never run against Postgres)
- [ ] Full ingestion → embed → insert pipeline; hybrid search ranking quality
- [ ] End-to-end `/ask` with a real LLM (the loop is tested with a fake LLM, not Gemini)
- [x] Eval runners (`--retrieval`, `--sweep`, `--agent`) run live via GitHub Actions. Five valid full runs exist (run 2 was invalidated by Groq rate limits); the final configuration (vector, fast mode, 60-line preview) was run twice and answered 25/27 both times, 5/5 declined, 0 errors. See `docs/EVALS.md`
- [x] **Frontend (built with Codex, plain HTML/CSS/JS, 140 Node tests):** workspace with Fast/Agent modes, streamed trace and verified citations; approve/edit/reject and publish as PR or issue; GitHub sign-in; public **Evaluations** and **Documentation** pages (served at `/evaluations` and `/documentation`, no login; Evaluations reads `GET /evals/latest` and shows stat cards, run-history and retrieval charts, tables and the stated limits); light/dark theme toggle (saved choice, else system setting); the active run is restored after a page reload from `/history` and `/traces/{id}` (only the query id is kept in `sessionStorage`) and a New chat action clears it. Deployed on Render
- [ ] Dockerfile build

**Not built yet:** LLM-as-judge faithfulness, tree-sitter chunking, LLM reranker, LLM-as-judge; fork-based PRs for repos the user can't write to. Built but **unverified live**: Supabase Auth + GitHub provider, the GitHub publish flow (tested only against a mock GitHub), the new `schema.sql` migrations.

## 9. Next steps (in order; tick as you go)
0. [ ] **Auth + GitHub setup.** In Supabase: Authentication → Providers → GitHub (create a GitHub OAuth App with callback `https://<project>.supabase.co/auth/v1/callback`, paste its client id/secret); add your site URL(s) to the redirect allow-list; set `SUPABASE_URL` and `SUPABASE_ANON_KEY` (public key only) in `backend/.env`. Re-run `backend/sql/schema.sql` (idempotent). Then test publish on a **throwaway repo you own**.
1. [ ] **Accounts & config (~30 min).** Create a Supabase project; run `backend/sql/schema.sql` in its SQL editor; copy the Postgres connection string; get a Gemini API key (embeddings only) and a Groq API key (console.groq.com; optionally an OpenRouter key + a `:free` model id for the fallback); `cp .env.example backend/.env` and fill `GEMINI_API_KEY`, `LLM_API_KEY`, `DATABASE_URL`. Open the Groq console and confirm the limits and model id for `LLM_MODEL`. `cd backend && pip install -r requirements.txt && python -m pytest -q`.
2. [ ] **First live ingest.** `uvicorn app.main:app --reload`, then `curl -X POST localhost:8000/repos -H 'content-type: application/json' -d '{"url":"https://github.com/pallets/click"}'` (or your own repo). Poll `GET /repos/{id}` until `ready`. **Fix whatever breaks** (most likely: embedding model name, SDK response shape, the `vector(768)` dimension vs the model's output, DB connection string). Re-run the tests after every fix.
3. [ ] **First live question.** `curl -N -X POST localhost:8000/ask -H 'content-type: application/json' -d '{"repo_id":"<id>","question":"How does the Command class parse arguments?"}'`. You should see `start`, some `step`s, then `answer` with citations, then `done`. Check `GET /traces/{query_id}`.
4. [ ] **Hand the frontend to Codex** (section 10) and review its PR against the acceptance checklist in `docs/FRONTEND_BRIEF.md`.
5. [ ] **Golden set.** Pick one repo and freeze it. Write 30–50 questions with `expected_files` and `expected_keywords` (copy `golden_set.example.json` to `golden_set.json`, set `repo_id`). Use your own repos too, since you know the right answers.
6. [ ] **Baseline vs improvements.** `python -m app.evals.run --retrieval --golden app/evals/golden_set.json`, then record vector-only vs keyword vs hybrid. Tune (pool size, RRF k, rerank boosts, chunk sizes). Optional: LLM reranker. Keep the before/after table: **this is your headline resume number.**
7. [ ] **Agent eval + red-team.** `--agent` run for latency/cost/groundedness. Add 15–20 more adversarial cases (including indirect injection: plant a malicious comment in a test repo and confirm it's neutralized). Optional upgrade: LLM-as-judge faithfulness.
8. [ ] **Deploy.** Render web service from the Dockerfile (API + frontend). Set env vars. Use the Supabase *pooled* connection string. Read the ephemeral-disk risk below first. Add a simple API key check before making it public.
9. [ ] **Polish.** README with architecture diagram, results table, demo GIF, live URL; update the resume bullets (section 12) with the real numbers.

## 10. Working with Codex (frontend)
**Division:** Codex builds `frontend/` only. Claude/you own `backend/`. The contract between them is `docs/API_CONTRACT.md`; if the backend changes an endpoint or event, update that file in the same change.

**Prompt to give Codex:**
```
Build the RepoMind frontend. Read AGENTS.md, docs/API_CONTRACT.md and docs/FRONTEND_BRIEF.md in this
repo. Plain HTML/CSS/vanilla JS only (no frameworks, no CDN, no build step), files in frontend/. Do not
modify backend/. Work through the acceptance checklist at the bottom of FRONTEND_BRIEF.md and tell me
which items you could not verify. If the contract is unclear, add a question under "Questions for
backend" in FRONTEND_BRIEF.md instead of guessing.
```
**Review checklist for what Codex returns:** `grep -rn innerHTML frontend/` should show nothing touching server/model text; Stop button aborts; approval modal can't be dismissed with Esc; works at 375px; no network requests to other hosts; try each guardrail error with curl-style inputs (injection text, empty question, 21 quick requests for the 429). You can also ask Codex for a Node mock server (no deps) so it can test without the backend.

## 11. Risks & gotchas
- **Gemini names/SDK drift.** Verify `GEMINI_CHAT_MODEL`, `GEMINI_EMBED_MODEL`, the `embed_content` / `generate_content` call shapes, and that `EMBED_DIM` (768) matches the schema. If you change the dimension, change `vector(768)` in `schema.sql` too (and re-index).
- **Free-tier quotas.** Google cut Gemini's free chat quota to ~20 requests/day (December 2025), which is why Gemini only does embeddings now; embeddings are a separate model with their own limit, and indexing batches 50 chunks per call. `MAX_CHUNKS_PER_REPO` (4000) caps indexing spend; a 1,500-chunk repo is ~30 embedding calls. Query embeddings are cached in memory and on disk.
- **Observed 2026-11-23: a day of eval testing used ~247K tokens (205K uncached input), over the 200K/day cap; Groq then returned 429s and the OpenRouter free fallback was rate-limited too, so one eval run was invalid.** Budget one full fast-mode eval per day (~40-60K tokens), keep the fallback unset for evals so one model answers, and don't run agent-mode evals the same day.
- **Groq limits are per model and mostly token-based** (8K TPM / 200K TPD on gpt-oss-120b at time of writing; sources disagree on some models, so trust your console). Agent mode sends growing context each step and can burn ~15-20K tokens per question, so use **fast mode** for demos and day-to-day, agent mode when you need it. Rate limits are per organization, not per key; extra keys/accounts don't add capacity (and creating them to dodge quotas is against provider terms).
- **OpenRouter free models:** 20 req/min; 50 req/day until $10 of credits have ever been bought, then 1,000/day; failed attempts count toward the limit; a negative balance returns 402 even for free models.
- **Cache vs. production correctness.** The LLM cache returns the same answer for the same prompt+code. Good for evals and demos; if you ever want fresh sampling, set `LLM_CACHE=off`.
- **Render ephemeral disk.** Cloned repos live in `REPOS_DIR` on local disk; chunks live in Supabase. After a redeploy/restart the clone disappears, so `read_file` and the citation preview fail while search still works. (Same class of problem as SQLite on Render.) Fix options: Render persistent disk, or re-clone lazily on demand using the stored `commit_sha`.
- **Supabase pooled connections** need `prepare_threshold=None` (already set in `db.py`).
- **Cold starts** on Render free tier: first request after idle is slow.
- **Auth/abuse.** Login + per-user rate limit + daily new-repo quota now exist; the limiter is in-memory (single instance only). Never deploy with `AUTH_REQUIRED=false`.
- **`provider_token` handling.** Supabase returns the GitHub token only at sign-in and doesn't refresh it; the frontend keeps it in `sessionStorage` and sends it only to the publish endpoint. `public_repo` scope lets the app write to the user's public repos, so the publish step is explicit and never touches the default branch.
- **RLS is unverified.** Confirm with Supabase's advisor/`get_advisors` that all tables have RLS on, and that the anon key can't read them.
- **PR flow only for writable repos.** Others get `github_no_permission`; use the issue kind. Fork flow not built.
- **Injection regex has false positives/negatives.** A user asking *about* the phrase "ignore previous instructions" gets blocked; novel phrasings can slip through. It is one layer among several (delimiting, neutralizing, tool sandbox, citation check), not the only defence. Don't claim it is complete; report the red-team numbers honestly and keep growing the set.
- **Groundedness is a heuristic** (identifier overlap), not a faithfulness judge. Say so in interviews; the LLM-judge upgrade is listed in Next steps.
- **Eval config.** Results apply to `FAST_TOP_K=5` and `SEARCH_PREVIEW_LINES=60` (runs 5 and 6); the deployed app must use the same value (an earlier Render setting of 3 is not what was measured).
- **Evidence granularity.** Search results only count as evidence for the lines actually shown to the model (first `SEARCH_PREVIEW_LINES` = 60 lines of a chunk; chunks can be up to 120 lines).
- **Python-only precision.** AST chunking is exact for Python; JS/TS/Go/Rust are heuristic; Java/C/C++ fall back to windows.
- **Tests don't cover** anything needing Gemini or Postgres (see section 8).

## 12. Resume framing (fill in X/Y only after measuring; never invent numbers)
> **RepoMind** · Agentic RAG codebase assistant · FastAPI, Supabase (pgvector), Gemini, vanilla JS
> - Built a retrieval layer (pgvector + Postgres full-text with weighted reciprocal-rank fusion) and chose the default by ablation on a tune/held-out split of a 32-question golden set (27 answerable, 5 should-decline) on one repo: vector-only matched the best hybrid on held-out recall@5 (0.92), so I shipped the simpler option
> - Debugged a drop in answered questions (85% → 67%) to answers rejected for non-ASCII bracket/hyphen citation formatting and missing citations, fixed by normalising citation characters, tightening the prompt and showing the model 60 instead of 30 lines per retrieved chunk; the final configuration answered 93% (25/27) in two separate full runs, with 5/5 out-of-scope questions declined and 0 errors (one model, one repo; the 2-question margin over the 30-line run is a single comparison and within noise)
> - Implemented a hand-written tool-using agent loop with sandboxed read-only tools and verified `file:line` citations (invalid citations are retried or stripped; low-evidence answers escalate to the user)
> - Added layered guardrails (prompt-injection and indirect-injection defence, secret redaction, step/token/time budgets, rate limiting) and a human-in-the-loop approval gate for proposed patches; blocked 12/12 adversarial prompts with 0/8 false positives in a red-team suite
> - Created an automated eval harness tracking recall@k, MRR, groundedness, p50/p95 latency and cost per query; 300+ automated tests

**Interview talking points:** why RRF instead of weighted sum of scores (different score scales); why a hand-written loop with JSON actions (testability, no vendor lock-in); how indirect prompt injection works and your layered defence; why HITL gates the side effect rather than pausing the agent; what the eval showed and what you'd improve (LLM judge, tree-sitter, reranker model); the Render ephemeral-disk trade-off.

## 13. Setup & run reference
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env                    # fill GEMINI_API_KEY, DATABASE_URL
# Supabase SQL editor: paste & run backend/sql/schema.sql
python -m pytest -q                        # expect 318 passed, 1 skipped on Windows
uvicorn app.main:app --reload              # http://localhost:8000
python -m app.evals.run --redteam          # offline
python -m app.evals.run --retrieval --golden app/evals/golden_set.json
python -m app.evals.run --agent     --golden app/evals/golden_set.json
```
Environment variables are documented in `.env.example`. Defaults: 8 steps, 60k tokens, 90s per query, 1000-char questions, 20 req/min/IP, 50 MB repos, 200 KB files, 4000 chunks.

## 14. Glossary
**RAG** retrieval-augmented generation · **RRF** reciprocal rank fusion, `score = Σ 1/(k+rank)` · **HITL** human-in-the-loop · **SSE** server-sent events (streaming over HTTP) · **Indirect prompt injection** malicious instructions hidden in data the model reads (here: repo files) · **recall@k** share of expected files found in the top-k results · **MRR** mean reciprocal rank of the first correct file.
