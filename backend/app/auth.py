"""Authentication: the frontend signs users in with Supabase Auth ("Sign in with GitHub")
and sends `Authorization: Bearer <access_token>`. We verify the token by asking Supabase
(`GET /auth/v1/user`), which works for any JWT signing setup, and cache the result briefly.

With AUTH_REQUIRED=false (local dev only) every request runs as a fixed anonymous user."""
import hashlib
import threading
import time
from dataclasses import dataclass

import httpx
from fastapi import Request

from .config import get_settings
from .guardrails.errors import GuardrailViolation

ANON_USER_ID = "00000000-0000-0000-0000-000000000000"
_MAX_CACHE = 1000


@dataclass(frozen=True)
class User:
    id: str
    email: str | None = None
    login: str | None = None     # GitHub username when signed in with GitHub


_cache: dict[str, tuple[float, User]] = {}
_lock = threading.Lock()
_client: httpx.Client | None = None


def set_http_client(client: httpx.Client | None) -> None:
    """Tests inject a mock-transport client here."""
    global _client
    _client = client


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _http() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=10.0)
    return _client


def verify_token(token: str) -> User:
    s = get_settings()
    if not s.supabase_url or not s.supabase_anon_key:
        raise GuardrailViolation("auth_not_configured", "Authentication is not configured on the server.")

    key = hashlib.sha256(token.encode()).hexdigest()
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]

    try:
        r = _http().get(
            f"{s.supabase_url.rstrip('/')}/auth/v1/user",
            headers={"apikey": s.supabase_anon_key, "Authorization": f"Bearer {token}"},
        )
    except httpx.TransportError:
        raise GuardrailViolation("auth_unavailable", "Could not reach the authentication service.")
    if r.status_code in (400, 401, 403):
        raise GuardrailViolation("unauthorized", "Your session has expired. Please sign in again.")
    if r.status_code != 200:
        raise GuardrailViolation("auth_unavailable", "The authentication service returned an error.")

    try:
        data = r.json()
    except ValueError:
        raise GuardrailViolation("auth_unavailable", "The authentication service returned an invalid response.")
    uid = data.get("id")
    if not uid:
        raise GuardrailViolation("unauthorized", "Your session has expired. Please sign in again.")
    meta = data.get("user_metadata") or {}
    user = User(str(uid), data.get("email"), meta.get("user_name") or meta.get("preferred_username"))

    with _lock:
        if len(_cache) >= _MAX_CACHE:
            _cache.clear()
        _cache[key] = (now + s.auth_cache_ttl_s, user)
    return user


def current_user(request: Request) -> User:
    """FastAPI dependency."""
    if not get_settings().auth_required:
        return User(ANON_USER_ID, None, "local-dev")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer ") or len(header) < 20:
        raise GuardrailViolation("unauthorized", "Please sign in to continue.")
    return verify_token(header[7:].strip())
