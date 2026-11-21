"""All SQL lives here. Everything user-owned is filtered by user_id; a row that belongs to someone
else looks exactly like a row that doesn't exist. Untested without a live Postgres (see report)."""
import json
from datetime import datetime, timezone

from .db import get_conn
from .guardrails.errors import GuardrailViolation
from .hitl.actions import next_status, validate_payload
from .retrieval.hybrid import vec_literal

_REPO_COLS = {"status", "error", "commit_sha", "files_indexed", "chunks_indexed", "indexed_at", "name"}


# ---------- repos (shared, public) ----------
def get_repo(repo_id) -> dict | None:
    with get_conn() as c:
        return c.execute("SELECT * FROM repos WHERE id = %s", (repo_id,)).fetchone()


def get_repo_by_url(url: str) -> dict | None:
    with get_conn() as c:
        return c.execute("SELECT * FROM repos WHERE url = %s", (url,)).fetchone()


def get_or_create_repo(url: str, name: str, user_id) -> tuple[dict, bool]:
    """Return (repo, should_ingest). The index is shared: if someone already indexed this repo we
    reuse it (saves embedding quota). A previously failed repo is atomically re-queued."""
    with get_conn() as c:
        row = c.execute(
            "INSERT INTO repos (url, name, created_by) VALUES (%s, %s, %s) ON CONFLICT (url) DO NOTHING RETURNING *",
            (url, name, user_id),
        ).fetchone()
        if row:
            return row, True
        row = c.execute("SELECT * FROM repos WHERE url = %s", (url,)).fetchone()
        if row["status"] == "failed":
            claimed = c.execute(
                "UPDATE repos SET status = 'pending', error = NULL WHERE id = %s AND status = 'failed' RETURNING *",
                (row["id"],),
            ).fetchone()
            if claimed:
                return claimed, True
        return row, False


def count_recent_repos(user_id) -> int:
    with get_conn() as c:
        return c.execute(
            "SELECT count(*) AS n FROM repos WHERE created_by = %s AND created_at > now() - interval '1 day'",
            (user_id,),
        ).fetchone()["n"]


def list_repos() -> list[dict]:
    with get_conn() as c:
        return c.execute("SELECT * FROM repos ORDER BY created_at DESC LIMIT 100").fetchall()


def update_repo(repo_id, **fields) -> None:
    bad = set(fields) - _REPO_COLS
    if bad:
        raise ValueError(f"unknown repo columns: {bad}")
    sets = ", ".join(f"{k} = %s" for k in fields)
    with get_conn() as c:
        c.execute(f"UPDATE repos SET {sets} WHERE id = %s", (*fields.values(), repo_id))


# ---------- chunks ----------
def delete_chunks(repo_id) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM chunks WHERE repo_id = %s", (repo_id,))


def insert_chunks(repo_id, chunks, vectors) -> None:
    rows = [
        (repo_id, ch.path, ch.language, ch.symbol, ch.kind, ch.start_line, ch.end_line, ch.content, vec_literal(v))
        for ch, v in zip(chunks, vectors)
    ]
    with get_conn() as c:
        with c.cursor() as cur:
            cur.executemany(
                "INSERT INTO chunks (repo_id, path, language, symbol, kind, start_line, end_line, content, embedding) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::vector)",
                rows,
            )


# ---------- queries / history / traces (per user) ----------
def create_query(repo_id, question: str, user_id):
    with get_conn() as c:
        return c.execute(
            "INSERT INTO queries (repo_id, user_id, question) VALUES (%s, %s, %s) RETURNING id",
            (repo_id, user_id, question),
        ).fetchone()["id"]


def finish_query(query_id, answer: str | None, status: str) -> None:
    with get_conn() as c:
        c.execute("UPDATE queries SET answer = %s, status = %s WHERE id = %s", (answer, status, query_id))


def list_queries(user_id, repo_id=None, limit: int = 30) -> list[dict]:
    sql = "SELECT id, repo_id, question, answer, status, created_at FROM queries WHERE user_id = %s"
    args: list = [user_id]
    if repo_id:
        sql += " AND repo_id = %s"
        args.append(repo_id)
    sql += " ORDER BY created_at DESC LIMIT %s"
    args.append(max(1, min(int(limit), 100)))
    with get_conn() as c:
        return c.execute(sql, args).fetchall()


def save_trace(query_id, events: list[dict], tokens_in: int, tokens_out: int, latency_ms: int, cost: float) -> None:
    guard = [e for e in events if e.get("type") == "guardrail"]
    with get_conn() as c:
        c.execute(
            "INSERT INTO traces (query_id, steps, guardrail_events, tokens_in, tokens_out, latency_ms, cost_usd) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (query_id, json.dumps(events, default=str), json.dumps(guard, default=str),
             tokens_in, tokens_out, latency_ms, cost),
        )


def get_trace(query_id, user_id) -> dict | None:
    with get_conn() as c:
        return c.execute(
            "SELECT t.*, q.question, q.answer, q.status AS query_status, q.repo_id FROM traces t "
            "JOIN queries q ON q.id = t.query_id WHERE t.query_id = %s AND q.user_id = %s",
            (query_id, user_id),
        ).fetchone()


# ---------- HITL actions (per user) ----------
def create_action(query_id, repo_id, user_id, kind: str, payload: dict):
    data = validate_payload(kind, payload)
    if isinstance(payload.get("diff"), str):          # server-computed; never accepted from clients
        data["diff"] = payload["diff"][:50_000]
    with get_conn() as c:
        return c.execute(
            "INSERT INTO pending_actions (query_id, repo_id, user_id, kind, title, payload) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING id",
            (query_id, repo_id, user_id, kind, data["title"], json.dumps(data)),
        ).fetchone()["id"]


def get_action(action_id, user_id) -> dict | None:
    with get_conn() as c:
        return c.execute(
            "SELECT * FROM pending_actions WHERE id = %s AND user_id = %s", (action_id, user_id)
        ).fetchone()


def decide_action(action_id, user_id, decision: str, edited_payload: dict | None = None) -> dict:
    action = get_action(action_id, user_id)
    if not action:
        raise GuardrailViolation("not_found", "Action not found.")
    new_status = next_status(action["status"], decision)
    edited = None
    if new_status.value == "edited":
        if not edited_payload:
            raise GuardrailViolation("invalid_proposal", "edit requires edited_payload.")
        edited = validate_payload(action["kind"], edited_payload)
    with get_conn() as c:
        # status = 'pending' in WHERE makes the transition atomic (no double decision)
        row = c.execute(
            "UPDATE pending_actions SET status = %s, edited_payload = %s, decided_at = %s "
            "WHERE id = %s AND user_id = %s AND status = 'pending' RETURNING *",
            (new_status.value, json.dumps(edited) if edited else None, datetime.now(timezone.utc), action_id, user_id),
        ).fetchone()
    if not row:
        raise GuardrailViolation("invalid_transition", "This action was already decided.")
    return row


def claim_publish(action_id, user_id) -> dict:
    """Atomically mark an approved action as 'being published' (a claim older than 5 minutes is
    considered abandoned). Prevents double-clicks from opening two PRs."""
    with get_conn() as c:
        row = c.execute(
            "UPDATE pending_actions SET publish_started_at = now() "
            "WHERE id = %s AND user_id = %s AND status IN ('approved','edited') AND result IS NULL "
            "AND (publish_started_at IS NULL OR publish_started_at < now() - interval '5 minutes') RETURNING *",
            (action_id, user_id),
        ).fetchone()
    if row:
        return row
    raise GuardrailViolation("publish_in_progress", "This proposal is already being published. Check again in a moment.")


def finish_publish(action_id, result: dict) -> None:
    with get_conn() as c:
        c.execute("UPDATE pending_actions SET result = %s WHERE id = %s", (json.dumps(result), action_id))


def release_publish(action_id) -> None:
    with get_conn() as c:
        c.execute("UPDATE pending_actions SET publish_started_at = NULL WHERE id = %s AND result IS NULL", (action_id,))


# ---------- feedback ----------
def add_feedback(query_id, user_id, rating: int, correction: str | None) -> None:
    with get_conn() as c:
        row = c.execute(
            "INSERT INTO feedback (query_id, user_id, rating, correction) "
            "SELECT id, %s, %s, %s FROM queries WHERE id = %s AND user_id = %s RETURNING id",
            (user_id, rating, correction, query_id, user_id),
        ).fetchone()
    if not row:
        raise GuardrailViolation("not_found", "Query not found.")
