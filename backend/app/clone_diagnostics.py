"""Bounded, allowlisted clone diagnostics; never serialize external text or exceptions."""
import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

logger = logging.getLogger(__name__)
_PHASES = frozenset({"clone", "ingestion", "restoration", "github_validation", "git_clone",
                     "git_fetch", "git_checkout", "git_verify", "git_init", "git_execution", "internal"})
_REASONS = frozenset({"github_rate_limited", "github_http_error", "github_network_error",
                      "github_invalid_response", "github_not_public", "git_execution_failed",
                      "git_not_found", "clone_timeout", "disk_limit", "filesystem_error",
                      "entry_limit", "output_limit", "lock_timeout", "unexpected_error"})
_NUMBERS = frozenset({"http_status", "rate_remaining", "returncode", "errno", "free_bytes",
                      "min_free_bytes", "used_bytes", "max_bytes", "entry_count", "max_entries",
                      "output_bytes", "response_bytes"})


@dataclass
class _Trace:
    repo_id: str | None
    started: float
    failures: int = 0


_current = ContextVar("clone_diagnostics", default=None)


def _uuid(value):
    if not isinstance(value, (str, UUID)):
        return None
    try:
        return str(UUID(str(value)))
    except ValueError:
        return None


@contextmanager
def diagnostic_scope(phase, repo_id=None):
    parent = _current.get()
    key = _uuid(repo_id)
    trace = parent[1] if parent and (key is None or key == parent[1].repo_id) else _Trace(key, time.monotonic())
    token = _current.set((phase if phase in _PHASES else "internal", trace))
    try:
        yield trace
    finally:
        _current.reset(token)


def log_failure(reason, **numbers):
    phase, trace = _current.get() or ("internal", _Trace(None, time.monotonic()))
    trace.failures += 1
    event = {"event": "repository_operation_failed", "phase": phase,
             "reason": reason if reason in _REASONS else "unexpected_error",
             "elapsed_ms": max(0, int((time.monotonic() - trace.started) * 1000))}
    if trace.repo_id is not None:
        event["repo_id"] = trace.repo_id
    event.update({key: value for key, value in numbers.items()
                  if key in _NUMBERS and type(value) is int and -(2**63) < value < 2**63})
    try:
        logger.warning(json.dumps(event, sort_keys=True), exc_info=None, stack_info=False)
    except Exception:
        # Diagnostics must not change the outcome of an operation if a handler fails.
        pass


def log_unreported(trace, before, reason, **numbers):
    if trace.failures == before:
        log_failure(reason, **numbers)
