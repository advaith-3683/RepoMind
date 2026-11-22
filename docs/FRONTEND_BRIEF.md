# Frontend brief (for Codex)

You are building the **RepoMind frontend**. Read `docs/API_CONTRACT.md` first; it is the source of truth for every request and event. Do not edit anything under `backend/`. If you believe the contract is wrong or missing something, write the question at the bottom of this file under "Questions for backend" and continue with the contract as written.

## Hard constraints
- **Plain HTML + CSS + vanilla JavaScript only.** No frameworks, no bundler, no npm, no CDN dependencies (everything must work offline from the files). ES modules (`<script type="module">`) are fine.
- Files live in `frontend/`. The backend serves that folder at `/`, so use relative URLs. Read an optional `window.REPOMIND_API_BASE` (default `""`) so the page can also be hosted separately.
- Never inject server or model text with `innerHTML`. Use `textContent` / `createElement`. (Repo content and LLM output are untrusted.)
- Responsive down to 375px wide. Keyboard accessible (labels, focus states, `aria-live` on the streaming answer and toasts). Respect `prefers-color-scheme` (light and dark) and `prefers-reduced-motion`.

## Suggested file layout
```
frontend/
  index.html
  styles.css
  js/
    api.js        # fetch wrappers + error normalisation ({error:{code,message}})
    sse.js        # POST-SSE reader (fetch + ReadableStream + frame parser)
    state.js      # tiny pub/sub store (current repo, messages, pending action)
    ui/repos.js   # sidebar: add repo, list, status polling
    ui/chat.js    # question box, streaming answer, citations, feedback
    ui/trace.js   # step timeline panel
    ui/approval.js# HITL modal
    ui/toast.js
    app.js        # wiring
```

## Design reference (Figma): READ THIS FIRST
File: "RepoMind — Product Design (Approved D2)", key `F2ikqB0vWbH4JDyH8rvzxW`, one page, 27 frames. **The Figma is the visual source of truth; `API_CONTRACT.md` is the behavioural source of truth. Where they disagree, the table "Where Figma and the backend disagree" below decides.** The prototype text is illustrative (fake repos, numbers and diffs): never ship it as real data.

### Design tokens (from the approved frame 1:2)
| Token | Value |
|---|---|
| page / nav / sidebar bg | `#eae6de` |
| surface (cards, answer, composer, right rail, inputs) | `#f8f6f0` |
| divider | `#d3cdc2` (1px) |
| primary (links, labels, active, citation text) | `#226192` |
| accent (primary buttons, logo slash) | `#ef8557`, button text `#26383e` |
| text strong / muted | `#26383e` / `#4e5d63` |
| chip bg | `#eae6de`, mono text `#226192` |
| dark theme (frame 31:2, approximate: confirm in Figma) | page `#151f25`-ish, surfaces `#202d34`-ish, same orange button, light-blue primary |
| Fonts | **Instrument Serif** (logo, page titles, question, rail title), **DM Sans** (UI and answer body, 400/600, `opsz` 14), **IBM Plex Mono** (citations chips, run summary, diffs, code) |
| Sizes | answer body 17px; question 25px serif; section label 12px semibold uppercase; body 13-14px; page title 32px serif; rail title 28px serif |
| Layout | 8px grid. Desktop 1440: nav 16px padding; columns **256 (repos) + 816 (main, 704 readable measure) + 368 (agent rail)**. Composer sits at the bottom of main. Square corners, no shadows, hairline dividers: editorial look |

Fonts must be **self-hosted** (`frontend/fonts/*.woff2` + `@font-face`; download from Google Fonts' repos, all three are OFL). Do not link Google Fonts: the acceptance checklist forbids external requests. Support light and dark via CSS variables on `:root` and `[data-theme=dark]`, default to `prefers-color-scheme`, with a toggle persisted in localStorage (try/catch).

### Frame map (what to build, in this order)
| Frame | Build it as |
|---|---|
| 01 Main Workspace (1:2) | Base 3-column layout. Nav: `R / M` logo, Workspace / Evaluations / Documentation, "● System online" (from `GET /health`) |
| 02 / 01B Citation drawer | Right slide-over "SOURCE EVIDENCE": file path, `LINES a-b`, language, line-numbered code with the cited range highlighted, "CITATION 01 OF 02", **Copy file path**, **Back to answer**. Data from `GET /repos/{id}/file`. Highlight the cited lines; the prototype's "illustrative" footnote is not shipped |
| 01C Approval modal | "HUMAN-IN-THE-LOOP / Review before action": title, target path, scope, the **server `diff`**, buttons **Reject / Edit proposal / Approve patch**; copy "Nothing has been applied yet" |
| 06C | Edit mode (Title*, Path*, monospace patch editor; see edits note below), 409 "already decided" state, approved-result state. Focus trapped; **Esc must not dismiss** |
| 01D / 06A | Clarification: show `needs_clarification.message` and, if `suggest_mode == "agent"`, **Try Agent mode ↗** (re-sends the same question with `mode:"agent"`, focus the textarea, don't prefill) |
| 01E Feedback | Radio reasons + optional text. Mapping below |
| 01F Error & recovery / 06B | Normalized error panel: show server `error.message`, **Retry** where it makes sense, keep composer + nav usable. Cover `repo_not_ready`, `empty_query`, `rate_limited`, `prompt_injection` |
| 01G / 06D Repo lifecycle | Status badges pending/cloning/indexing (animated), ready (selectable), failed (expandable error, **Retry connection** = POST /repos again). Poll every 2s. The "62%" progress bar is **not available from the backend**: use an indeterminate bar and the 4-step checklist driven by `status` (pending/cloning = "connected", indexing = "building index", ready = done) |
| 01H First-use empty state | Hero "Ask better questions about your code" + 3 starter-question chips that fill the composer; right rail "Ready when you are / Waiting for a question" |
| 01I / 06E Loading + live trace | "Agent working" card, **Stop run ■** (AbortController), live rail with a spinner on the current step, amber guardrail cards, running footer `N steps / tokens / elapsed`, done summary `steps / tokens / sec / $cost`, optional **Served from cache** when `usage.llm_cache_hits > 0`. Stream answer text with `textContent`, never `innerHTML` |
| 06A Composer | Counter `n / 1000`, **Fast ✓ / Agent** segmented control with tooltips (hover AND focus), persisted; Send disabled with no ready repo or while streaming; Enter sends, Shift+Enter newline |
| 06B Answer variants | `confidence=low` → "Low confidence: verify against the cited code" banner; `found=false` → "Not found in this repo" card; both still show citations when present |
| 02A / 06G Mobile (390 / 375) | Single column: repo picker card (▾ sheet), question + answer card, collapsible "Agent trace" summary row (`1 step · 342 tokens · 1.1s ▾`), composer with mode buttons and full-width orange **Send question ↗**, bottom tab bar **Workspace / Repos / Activity** (Activity = history list). Breakpoint ≈ 900px: below it the 3 columns become this layout, the agent rail becomes the collapsible row |
| 03A Dark theme | Same layout, dark tokens |
| 00 / 00B Landing | Public page at `/` for signed-out visitors (hero "Understand code. Trust every answer.", live mini-demo card, 3 pillars Discover / Verify / Control). **Replace "Launch Workspace" with "Sign in with GitHub"** when signed out (and "Open workspace" when signed in) |
| 04A / 04B Eval dashboard + run details | **Do not build as drawn** (all numbers are demo data). Build only 06F |
| 06F Evals | `#/evals` renders only the sections returned by `GET /evals/latest` (red-team block rate, retrieval recall@k / MRR fast vs agent, agent latency p50/p95, groundedness/cost) as tables; on 404 show "No evaluation results yet" with **Back to workspace**. Never invent metrics |
| 05A Repository management | Build as a simplified Repos page (mobile "Repos" tab): list of indexed repos with status, files/chunks counts, "Add repository". **Omit** target branch, write-access and Activity-log/Permissions controls (no backend) |

### Where Figma and the backend disagree (backend wins; implement as written here)
| Figma says | Do this instead |
|---|---|
| 01J "Changes applied successfully… applied to the selected working branch", "View checks", "View applied diff" | Approval does **not** change any repo. After Approve/Edit show the diff and a **Publish to GitHub** button. After publishing, the success card says "Pull request opened" (or "Issue created") with the `url` link, branch name and the diff. Reuse the 01J layout and visuals; no "View checks". The 06C note "direct apply is a future workflow" still applies to anything beyond this publish step |
| 01D clarification has three choice cards | Backend returns only a message (+ optional `suggest_mode`). Render the message and the Try-Agent-mode button; no choice cards |
| 01E three reasons + text | `POST /feedback`: Yes = `rating:1`; No or the form = `rating:-1`, `correction` = `"<reason label>: <text>"` (≤2000 chars) |
| 01G "62%" and file counts | Only `status`, `files_indexed`, `chunks_indexed` exist; no percentage |
| 06C edit mode edits a raw diff body | The backend patch format is `edits[{old,new}]`. Show the server `diff` read-only, and let the user edit **title and body** (and path only if you also let them edit the edit pairs); on submit send `edited_payload` with `path` and the (possibly modified) `edits`. Simplest correct build: editable title/body, `path` and `edits` passed through unchanged |
| Sidebar shows 3 hard-coded repos; "Add repository" form | Real list from `GET /repos` (shared index); the add form validates `^https://github\.com/[\w.-]+/[\w.-]+/?$` |
| Nav "Documentation", "System online" | Documentation links to the GitHub README; the status dot reflects `GET /health` |
| Eval dashboard and repo-management numbers | Demo only (see above) |
| Not in Figma at all | **Sign-in state** (use the landing page + a header user chip with login and Sign out), **History** (Activity tab on mobile; on desktop a "Recent questions" list in the left sidebar under Repositories), **Publish to GitHub** button/states, **toast** component for `budget_*` guardrails and `rate_limited`. Design these in the same style (flat, hairline dividers, orange only for the one primary action per view) |

### Interaction notes taken from the Figma specs
Focus is trapped inside modals and returned to the trigger on close; Esc does not dismiss the approval modal; tooltips show on hover and keyboard focus; the selected repo id persists in localStorage with try/catch; the mode toggle persists; `answer.text` and all model output is rendered as text only.

## Sign in with GitHub (Supabase Auth, implicit flow)
1. `GET /config`. If `auth_required` is false, skip this section.
2. **Sign in** button → `location.href = SUPABASE_URL + "/auth/v1/authorize?provider=github&scopes=public_repo&redirect_to=" + encodeURIComponent(location.origin + "/")`.
3. On load, if `location.hash` has `access_token`, parse `access_token`, `refresh_token`, `expires_in`, `provider_token` from it, store them in `sessionStorage` (try/catch), then `history.replaceState(null, "", location.pathname)` so tokens leave the URL.
4. Send `Authorization: Bearer <access_token>` on every API call. Before expiry (or on a `401`) refresh once: `POST SUPABASE_URL/auth/v1/token?grant_type=refresh_token` with header `apikey: <anon key>` and body `{refresh_token}`; save the new tokens. If that fails, show the signed-out state.
5. `provider_token` is the user's GitHub token. Keep it in `sessionStorage` only, send it **only** as `X-GitHub-Token` on `POST /actions/{id}/publish`, never log or render it. It is not refreshed by Supabase; if publish returns `github_auth`, ask the user to sign in again.
6. Signed-out state: show only a sign-in screen. Header shows the user's login + **Sign out** (clear storage). Backend setup (not your job): GitHub provider enabled in Supabase and the site URL allow-listed.
7. **History sidebar section:** `GET /history?repo_id=` for the selected repo; clicking an item loads `GET /traces/{id}` and re-renders its answer and steps read-only.

## Layout
Landing uses `/`; the authenticated app uses `/workspace`. Frontend navigation uses `history.pushState` and handles `popstate`; auth guards redirect signed-out workspace visits to `/` and signed-in landing visits to `/workspace`. The OAuth redirect remains `${location.origin}/`: the blocking callback script saves and clears the fragment before authenticated navigation. No hash routes are used. Direct requests/reloads of `/workspace` require the server to serve the existing frontend entry point there; backend routing is owned separately.

Three regions (stack vertically on mobile):
1. **Sidebar: Repositories.** "Add repository" form (URL input + button, client-side check against `^https://github\.com/[\w.-]+/[\w.-]+/?$`). List of repos with a status badge (`pending`/`cloning`/`indexing` = animated, `ready` = green, `failed` = red with `error` text on hover/expand). Clicking a `ready` repo selects it. Poll `GET /repos/{id}` every 2s for any non-terminal repo; stop on `ready`/`failed`. Persist the selected repo id in `localStorage` (wrap in try/catch).
2. **Main: Chat.** Question textarea (Enter sends, Shift+Enter newline; 1000-char counter), Send button (disabled while streaming or no ready repo selected, shows a Stop button while streaming that aborts the fetch with `AbortController`). Conversation list: user question bubbles, assistant answer bubbles.
3. **Right panel: Agent trace.** Live timeline for the current question: one card per `step` event (tool icon/name, the `thought`, the `args` as compact JSON, and an expandable `observation_preview`). A running footer: steps, tokens (`tokens_total`), elapsed time. After `done`, show the `usage` summary including `cost_usd`. Guardrail events appear in the timeline as amber cards.

## Behaviours per SSE event
- `start`: create the assistant bubble in a "thinking…" state; store `query_id`.
- `step`: append to trace; update the bubble status line ("Searching code…", "Reading app/auth.py…").
- `guardrail`: amber card in trace + small toast for `indirect_injection` and `budget_*`; `citation_check_failed` only in trace.
- `answer`: render text (escaped, minimal markdown: paragraphs, `code`, fenced blocks). Turn `[path:start-end]` into clickable citation chips. Show a **confidence badge**; if `confidence == "low"` show a banner: "Low confidence — verify against the cited code." If `found == false` show a neutral "Not found in this repo" style.
- `needs_clarification`: show an inline callout with `message`, focus the question box, pre-fill nothing. This is the human-in-the-loop escalation.
- `approval_required`: open the **approval modal** (below). Block the page behind it (focus trap, Esc does **not** close it, because the user must decide).
- `error`: red bubble with `message`.
- `done`: stop spinner, show usage, show feedback buttons.

## Answer mode (Fast / Agent)
Next to the Send button, a two-option segmented control: **Fast** (default) and **Agent**, each with a one-line tooltip ("1 retrieval + 1 LLM call" / "multi-step search, can propose changes"). Send the choice as `mode` in `POST /ask`. Remember the choice in `localStorage` (try/catch). The trace panel header shows the mode from the `start` event. When `needs_clarification` arrives with `suggest_mode: "agent"`, show a **Try Agent mode** button in the callout that re-sends the same question with `mode:"agent"`. After `done`, if `usage.llm_cache_hits > 0`, show a small "served from cache" note.

## Citation preview
Clicking a citation chip opens a drawer/modal that calls `GET /repos/{id}/file?path=&start=&end=` (pad ±5 lines of context, clamp to >=1) and shows line-numbered code in a monospace block, highlighting the cited range. Show the API error message if it fails.

## Approval modal (HITL)
Shows `kind` (Patch / Issue), `title`, and `body` in a monospace area (for patches, colour `+` lines green and `-` lines red). Three buttons:
- **Approve** → `POST /actions/{id}/decision {decision:"approve"}`
- **Edit** → makes title/body editable; then **Approve edited version** → `{decision:"edit", edited_payload:{...}}` (include `path` for patches)
- **Reject** → `{decision:"reject"}`
For patches render the server's `diff` (from the `decision` response or `payload.diff`), not the model's prose. After approving, show a **Publish to GitHub** button (`patch` → "Open pull request", `issue` → "Create issue"): `POST /actions/{id}/publish` with `X-GitHub-Token`; on success show the returned `url` as a link. Explain `github_no_permission` ("you need write access to open a PR; publish as an issue instead") and `github_conflict` ("file changed on GitHub; re-index and ask again"). Keep Copy / Download .patch as a fallback, plus a confirmation line. On `409` show "This action was already decided." Do not auto-apply anything anywhere.

## Feedback
After `done`, show 👍 / 👎. 👎 reveals an optional "What should the answer have been?" textarea. Submit to `POST /feedback`. Show a thank-you and disable the buttons.

## Extra page: Evals (optional, nice for the portfolio)
A "Results" view (hash route `#/evals`) fetching `GET /evals/latest` and rendering whichever sections exist as tables (red-team block rate, retrieval recall@k/MRR per mode, agent latency p50/p95, groundedness, cost). Handle `404` with an empty-state message.

## Visual direction
Clean developer-tool look: neutral background, one accent colour, system UI font for text and `ui-monospace` for code, 8px spacing grid, subtle borders instead of heavy shadows. Avoid generic "AI purple gradient" styling. Skeleton/loading states for every async region. Empty states: no repos, no messages, no trace yet.

## Acceptance checklist (tick before finishing)
- [ ] Works with the backend running (`uvicorn app.main:app` from `backend/`) and loading `http://localhost:8000/`
- [ ] Add repo → status badge updates → becomes selectable when `ready`
- [ ] Streaming answer renders token-by-event with live trace; Stop button aborts cleanly
- [ ] Guardrail errors (`prompt_injection`, `empty_query`, `rate_limited`, `repo_not_ready`) show the server message and don't break the UI
- [ ] Citations clickable and previewed; no `innerHTML` with untrusted text anywhere (grep for it)
- [ ] Approval modal covers approve / edit / reject / 409 and cannot be dismissed accidentally
- [ ] Sign-in/out works, tokens never stay in the URL, history sidebar loads past questions, 401 returns to sign-in
- [ ] Publish button shows the PR/issue link and handles `github_*` errors; token only ever in the `X-GitHub-Token` header
- [ ] Feedback works; low-confidence banner and needs-clarification callout work
- [ ] Fast/Agent toggle sends `mode`, persists, and "Try Agent mode" re-asks correctly
- [ ] 375px mobile layout usable; light/dark both fine; keyboard-only flow works
- [ ] No console errors; no external network requests (fonts self-hosted)
- [ ] Each Figma frame in the frame map is implemented or explicitly listed as omitted; light and dark both match tokens; 1440 / 390 / 375 widths checked

To test without a backend, you may create `frontend/dev/mock-server.js` (Node, no deps) that mimics `API_CONTRACT.md`, including an SSE `/ask` that emits each event type. Keep it out of the production path.

## Questions for backend
- Step 7.2: The contract's inline-citation regex excludes spaces and parentheses, while verified citation paths can include both. Should that example be updated? The frontend now uses `answer.citations` as the allowlist and leaves unmatched references as plain text, per the Step 7.2 request.
- Step 7.2 interpretation: approval is an inline card in the answer area, as requested for this step, rather than the earlier modal. It remains visible until the answer is replaced; Escape does not dismiss it. Patch editing changes title, body and each edit's `new` text while preserving path and `old`; only the server recomputes the diff.
- Resolved in Step 7.3: retain the decision response's diff after publish, alongside the returned URL, optional PR/issue number and branch. Show a prominent GitHub link and hide the publish button. The contract's top-level `already: true` is displayed as "Already published." No extra action-status request is needed.
