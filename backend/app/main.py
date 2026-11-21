import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from fastapi import BackgroundTasks, Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import store
from .auth import User, current_user
from .config import get_settings
from .files import read_slice
from .github import GitHubClient
from .guardrails.budget import RateLimiter
from .guardrails.errors import GuardrailViolation
from .guardrails.input import validate_query
from .ingestion.clone import parse_github_url
from .ingestion.pipeline import ingest_repo
from .publish import publish_action
from .schemas import AskRequest, DecisionRequest, FeedbackRequest, RepoCreate

settings = get_settings()
app = FastAPI(title="RepoMind", version="0.2.0")
app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_list, allow_methods=["*"], allow_headers=["*"],
)
limiter = RateLimiter(settings.rate_limit_per_min)

_STATUS_BY_CODE = {
    "unauthorized": 401,
    "auth_not_configured": 503, "auth_unavailable": 503,
    "rate_limited": 429, "index_quota_exceeded": 429,
    "not_found": 404,
    "invalid_transition": 409, "repo_not_ready": 409, "publish_not_ready": 409,
    "publish_in_progress": 409, "github_conflict": 409,
    "github_no_permission": 403, "github_auth": 403,
    "github_error": 502,
}


@app.exception_handler(GuardrailViolation)
async def guardrail_handler(_: Request, exc: GuardrailViolation):
    return JSONResponse(
        status_code=_STATUS_BY_CODE.get(exc.code, 400),
        content={"error": {"code": exc.code, "message": exc.message}},
    )


def rate_limit(request: Request, user: User = Depends(current_user)) -> None:
    """Per signed-in user (per IP when auth is off for local dev)."""
    key = user.id if settings.auth_required else (request.client.host if request.client else "anonymous")
    limiter.check(key)


def _jsonable(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if isinstance(v, UUID):
            v = str(v)
        elif isinstance(v, datetime):
            v = v.isoformat()
        elif isinstance(v, Decimal):
            v = float(v)
        out[k] = v
    return out


def _sse(ev: dict) -> str:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"


def _diff_for(repo_id, kind: str, payload: dict | None) -> str | None:
    if kind != "patch" or not payload or not repo_id:
        return None
    from .service import patch_diff_for   # lazy
    return patch_diff_for(repo_id, payload)


# ---------------- public ----------------
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/config")
def public_config():
    """Everything the frontend needs to start the 'Sign in with GitHub' flow. The anon key is public by design."""
    return {
        "auth_required": settings.auth_required,
        "supabase_url": settings.supabase_url,
        "supabase_anon_key": settings.supabase_anon_key,
        "github_scopes": "public_repo",
    }


@app.get("/evals/latest")
def latest_eval():
    results = sorted((Path(__file__).parent / "evals" / "results").glob("*.json"))
    if not results:
        raise GuardrailViolation("not_found", "No eval results yet. Run: python -m app.evals.run")
    return json.loads(results[-1].read_text())


# ---------------- repos ----------------
@app.post("/repos", status_code=202)
def add_repo(req: RepoCreate, background: BackgroundTasks, user: User = Depends(current_user),
             _=Depends(rate_limit)):
    owner, name = parse_github_url(req.url)          # validates before touching the DB
    url = f"https://github.com/{owner}/{name}"
    if not store.get_repo_by_url(url) and \
            store.count_recent_repos(user.id) >= settings.max_new_repos_per_user_per_day:
        raise GuardrailViolation(
            "index_quota_exceeded",
            f"You can index up to {settings.max_new_repos_per_user_per_day} new repositories per day.",
        )
    repo, should_ingest = store.get_or_create_repo(url, f"{owner}/{name}", user.id)
    if should_ingest:
        background.add_task(ingest_repo, repo["id"], repo["url"])
    return _jsonable(repo)


@app.get("/repos")
def repos(_: User = Depends(current_user)):
    return [_jsonable(r) for r in store.list_repos()]


@app.get("/repos/{repo_id}")
def repo_status(repo_id: UUID, _: User = Depends(current_user)):
    r = store.get_repo(repo_id)
    if not r:
        raise GuardrailViolation("not_found", "Repository not found.")
    return _jsonable(r)


@app.get("/repos/{repo_id}/file")
def repo_file(repo_id: UUID, path: str, start: int = 1, end: int | None = None, _: User = Depends(current_user)):
    """Citation preview: sandboxed, secret-redacted, line-numbered slice of a file."""
    root = Path(settings.repos_dir) / str(repo_id)
    if not root.exists():
        raise GuardrailViolation("not_found", "Repository not found.")
    return read_slice(root, path, start, end, max_lines=200)


# ---------------- ask (SSE) ----------------
@app.post("/ask")
def ask(req: AskRequest, user: User = Depends(current_user), _=Depends(rate_limit)):
    from .service import stream_answer   # lazy: needs Gemini/DB config

    question = validate_query(req.question, settings.max_query_chars)   # 400 JSON before streaming
    repo = store.get_repo(req.repo_id)
    if not repo:
        raise GuardrailViolation("not_found", "Repository not found.")
    if repo["status"] != "ready":
        raise GuardrailViolation("repo_not_ready", f"Repository is not ready (status: {repo['status']}).")

    def gen():
        for ev in stream_answer(repo, question, req.mode, user.id):
            yield _sse(ev)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------- history / traces / feedback (own data only) ----------------
@app.get("/history")
def history(repo_id: UUID | None = None, limit: int = Query(30, ge=1, le=100), user: User = Depends(current_user)):
    return [_jsonable(r) for r in store.list_queries(user.id, repo_id, limit)]


@app.get("/traces/{query_id}")
def trace(query_id: UUID, user: User = Depends(current_user)):
    t = store.get_trace(query_id, user.id)
    if not t:
        raise GuardrailViolation("not_found", "Trace not found.")
    return _jsonable(t)


@app.post("/feedback", status_code=201)
def feedback(req: FeedbackRequest, user: User = Depends(current_user), _=Depends(rate_limit)):
    store.add_feedback(req.query_id, user.id, req.rating, req.correction)
    return {"ok": True}


# ---------------- HITL: decide, then publish ----------------
@app.get("/actions/{action_id}")
def get_action(action_id: UUID, user: User = Depends(current_user)):
    a = store.get_action(action_id, user.id)
    if not a:
        raise GuardrailViolation("not_found", "Action not found.")
    final = a["edited_payload"] or a["payload"]
    out = _jsonable(a)
    out["diff"] = _diff_for(a["repo_id"], a["kind"], final) or (a["payload"] or {}).get("diff")
    return out


@app.post("/actions/{action_id}/decision")
def decide(action_id: UUID, req: DecisionRequest, user: User = Depends(current_user), _=Depends(rate_limit)):
    row = store.decide_action(action_id, user.id, req.decision, req.edited_payload)
    final = None if row["status"] == "rejected" else (row["edited_payload"] or row["payload"])
    diff = _diff_for(row.get("repo_id"), row["kind"], final)
    if final is not None:
        final = {k: v for k, v in final.items() if k != "diff"}
    return {"id": str(row["id"]), "status": row["status"], "kind": row["kind"], "payload": final, "diff": diff}


@app.post("/actions/{action_id}/publish")
def publish(action_id: UUID, request: Request, user: User = Depends(current_user), _=Depends(rate_limit)):
    """Open a pull request (patch) or issue (issue) on GitHub for an APPROVED proposal.
    Needs the user's GitHub token in the X-GitHub-Token header; it is used once and never stored."""
    action = store.get_action(action_id, user.id)
    if not action:
        raise GuardrailViolation("not_found", "Action not found.")
    if action["result"]:                                   # idempotent: never open a second PR
        return {"status": "published", "already": True, "result": action["result"]}
    if action["status"] not in ("approved", "edited"):
        raise GuardrailViolation("publish_not_ready", "Approve the proposal before publishing it.")
    token = request.headers.get("x-github-token", "").strip()
    if not token:
        raise GuardrailViolation(
            "missing_github_token", "Sign in with GitHub (allowing repository access) to publish this to GitHub.")

    repo = store.get_repo(action["repo_id"])
    owner, name = parse_github_url(repo["url"])
    payload = action["edited_payload"] or action["payload"]

    store.claim_publish(action_id, user.id)
    try:
        result = publish_action(GitHubClient(token, settings.github_api_url), owner, name,
                                action["kind"], payload, action["id"], user.login or user.email)
    except Exception:
        store.release_publish(action_id)                   # allow a retry
        raise
    store.finish_publish(action_id, result)
    return {"status": "published", "already": False, "result": result}


# Serve the plain HTML/CSS/JS frontend (same origin => no CORS needed). Mounted last.
_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
if _FRONTEND.exists():
    app.mount("/", StaticFiles(directory=_FRONTEND, html=True), name="frontend")
