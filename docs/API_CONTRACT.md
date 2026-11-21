# RepoMind API contract (source of truth for the frontend)

Base URL: same origin as the page (the backend serves `/frontend` at `/`). Allow an override via `window.REPOMIND_API_BASE` (default `""`).
All bodies are JSON. UUIDs are strings.

## Authentication
Every endpoint except `GET /health`, `GET /config` and `GET /evals/latest` needs `Authorization: Bearer <supabase access_token>` (see FRONTEND_BRIEF "Sign in"). Missing/expired → `401 unauthorized`: send the user back to sign-in (try one silent refresh first). History, traces, feedback and actions are **per user**: another user's ids return `404 not_found`. Indexed repos are shared (one index per URL), queries/traces/actions are private to their owner.

## Error shape (every non-2xx from the app)
```json
{ "error": { "code": "prompt_injection", "message": "Human-readable text safe to show the user." } }
```
FastAPI validation failures (bad UUID, wrong types) return `422` with FastAPI's default `{ "detail": [...] }` body.

| HTTP | codes |
|---|---|
| 400 | `invalid_repo_url`, `empty_query`, `query_too_long`, `invalid_query`, `prompt_injection`, `path_blocked`, `not_a_file`, `file_too_large`, `binary_file`, `bad_range`, `invalid_decision`, `invalid_proposal` |
| 401 | `unauthorized` |
| 400 (also) | `missing_github_token` (publish without `X-GitHub-Token`) |
| 403 | `github_no_permission` (no write access / repo or file not found for this user), `github_auth` (GitHub rejected the token: sign in again) |
| 404 | `not_found` |
| 409 | `repo_not_ready`, `invalid_transition`, `publish_not_ready` (approve first), `publish_in_progress`, `github_conflict` (file changed on GitHub / edit no longer applies / nothing to change) |
| 429 | `rate_limited` (per user; provider quota problems surface as an SSE `error`/`needs_clarification`, not HTTP 429), `index_quota_exceeded` (new repos per user per day) |
| 502 | `github_error` (GitHub unreachable, unexpected response, or indexed repository snapshot unavailable) |
| 503 | `auth_unavailable`, `auth_not_configured` |

Always show `error.message`; switch on `error.code` only for special UX (e.g. focus the input on `prompt_injection`).

## Endpoints

### `GET /health` → `200 {"status":"ok"}`

### `GET /config` → `200` (public)
`{ "auth_required": true, "supabase_url": "...", "supabase_anon_key": "...", "github_scopes": "public_repo" }`. If `auth_required` is false (local dev) skip sign-in entirely.

### `POST /repos` → `202`
Body `{ "url": "https://github.com/owner/repo" }` (public GitHub repos only). Starts background indexing.
Response = repo object (below), `status: "pending"`.

### `GET /repos` → `200 [repo, ...]` (newest first)
### `GET /repos/{id}` → `200 repo` — poll every 2s while `status` is `pending|cloning|indexing`.

Repo object:
```json
{ "id":"uuid", "url":"https://github.com/o/r", "name":"o/r", "commit_sha":null,
  "status":"pending|cloning|indexing|ready|failed", "error":null,
  "files_indexed":0, "chunks_indexed":0, "created_at":"iso", "indexed_at":null }
```
Show `error` when `status == "failed"`. Only `ready` repos can be queried.

### `GET /repos/{id}/file?path=app/auth.py&start=3&end=40` → `200`
Citation preview. Sandboxed + secret-redacted. Max 200 lines per call.
File previews, agent runs, and patch-diff generation verify anonymous public access and the indexed commit before reading local code. Missing or damaged clones are restored at the stored commit SHA under a repository lock; stored chunks are not reindexed. Restoration failures use the existing error envelope and `github_error` (502), or `repo_not_ready` (409) for an unavailable indexed revision or a busy repository. No fallback to the latest branch is performed.
```json
{ "path":"app/auth.py", "start":3, "end":40, "total_lines":120, "lines":["def login(...):", "..."] }
```
`lines[0]` is line number `start`.

### `POST /ask` → `200 text/event-stream`
Body `{ "repo_id":"uuid", "question":"string (<=1000 chars)", "mode": "fast" | "agent" }` (`mode` optional; the server default is `fast`).
- `fast`: retrieves once, then makes a single LLM call. Cheap and quick. Cannot make follow-up lookups or propose changes.
- `agent`: full multi-step tool loop. Slower and uses more LLM calls; the only mode that can emit `approval_required`.
Validation/guardrail failures return a normal JSON error **before** streaming starts (check `response.ok` first).

**It is a POST, so `EventSource` cannot be used.** Use `fetch` + `response.body.getReader()` + `TextDecoder` and parse SSE frames (`event: <type>\ndata: <json>\n\n`; split on blank lines; a frame may span chunks).

#### SSE events (the `type` field is also inside `data`)

| type | when | fields |
|---|---|---|
| `start` | first | `query_id`, `mode` (`fast`/`agent`, the mode actually used) |
| `step` | after each tool call | `index`, `thought`, `tool` (`search_code`/`read_file`/`find_symbol`/`list_dir`), `args` (object), `observation_preview` (<=300 chars), `tokens_total` |
| `guardrail` | a guardrail fired (non-fatal) | `code`, `message` — codes: `citation_check_failed`, `indirect_injection`, `invalid_proposal`, `budget_steps`, `budget_tokens`, `budget_timeout` |
| `answer` | final answer | `text`, `found` (bool), `confidence` (`high`/`low`), `groundedness` (0-1), `citations` `[{path,start,end}]` |
| `needs_clarification` | agent could not ground an answer / ran out of budget (HITL escalation) | `message`, `suggest_mode?` (`"agent"` when a **fast** run failed: offer a "Try Agent mode" button that re-asks the same question with `mode:"agent"`) |
| `approval_required` | agent proposed a change; **nothing is applied until a human decides** | `action_id`, `kind` (`patch`/`issue`), `payload` `{title, body, path?, edits?, diff?}` (for `patch`: `edits` = `[{old,new}]` exact search/replace pairs, `diff` = server-computed unified diff to show the reviewer) |
| `error` | failure | `code`, `message`, `detail?` |
| `done` | always last | `query_id`, `status` (`completed`/`needs_clarification`/`awaiting_approval`/`error`), `mode`, `usage` `{steps,tokens_in,tokens_out,latency_ms,cost_usd,llm_cache_hits}` (`llm_cache_hits` > 0 means some answers came from the local cache, so they cost no API calls) |

`answer.text` contains inline citations like `[app/auth.py:3-5]` or `[app/auth.py:7]`. Parse with `/\[([\w./\-@+]+):(\d+)(?:-(\d+))?\]/g` and render as clickable chips that call `GET /repos/{id}/file`.
`answer.text` is untrusted model output: render with `textContent` / escape HTML. Do not use `innerHTML` with it. Simple markdown (backticks, code fences, paragraphs) may be rendered by a tiny hand-written escaper.

### `POST /actions/{action_id}/decision` → `200`
Body `{ "decision":"approve" | "edit" | "reject", "edited_payload": {title, body, path?, edits?} }` (`edited_payload` required only for `edit`; for patches it must include `path` and `edits`; the `diff` is always recomputed by the server).
```json
{ "id":"uuid", "status":"approved|edited|rejected", "kind":"patch|issue", "payload": {title, body, path?, edits?} | null, "diff": "unified diff" | null }
```
`payload` is the final (possibly edited) content, `null` if rejected. Deciding twice → `409 invalid_transition`.
Approve/edit does **not** call GitHub by itself. Show the `diff`, then offer **Publish to GitHub** (below). Copy / Download `.patch` remain as a fallback.

### `POST /actions/{action_id}/publish` → `200`
Header `X-GitHub-Token: <provider_token>` (the user's GitHub token from sign-in; used once, never stored). Only for `approved`/`edited` actions. `patch` → new branch `repomind/<slug>-<id8>` + pull request (never a commit to the default branch; needs write access to the repo); `issue` → GitHub issue (works on any public repo).
```json
{ "status":"published", "already":false, "result": { "kind":"patch|issue", "url":"https://github.com/o/r/pull/7", "number":7, "branch":"repomind/..." } }
```
Idempotent: publishing again returns the stored result with `already:true` (never a second PR). On failure nothing is left behind (the branch is deleted) and you may retry.

### `GET /history?repo_id=<uuid>&limit=30` → `200 [ {id, repo_id, question, status, answer?, created_at, ...} ]` (the caller's own, newest first)

### `GET /actions/{action_id}` → `200` action row (`status`, `kind`, `payload`, `edited_payload`, `result`, `diff`, timestamps); owner only

### `POST /feedback` → `201 {"ok":true}`
Body `{ "query_id":"uuid", "rating": 1 | -1, "correction": "optional string <=2000" }`. Corrections feed the eval golden set.

### `GET /traces/{query_id}` → `200`
`{ query_id, question, answer, query_status, steps:[<all SSE events>], guardrail_events:[...], tokens_in, tokens_out, latency_ms, cost_usd }`

### `GET /evals/latest` → `200` latest eval result JSON (`redteam`, `retrieval`, `agent` sections, any may be absent) or `404 not_found`.

## Rate limiting
20 requests/minute per signed-in user (per IP when auth is off) on mutating endpoints (`/repos` POST, `/ask`, decision, feedback). On `429`, show the message and disable the submit button for ~10s.
