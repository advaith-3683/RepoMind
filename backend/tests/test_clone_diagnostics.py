"""Linux validation: from backend/ on a Linux checkout with Git and requirements installed:

REPOMIND_LINUX_CLONE_CHECK=1 python -B -m pytest -q -p no:cacheprovider tests/test_clone_diagnostics.py

The opt-in case uses a real anonymous HTTPS clone; all other cases are offline.
Do not supply secrets. For the production Docker image, mount backend/tests read-only
at /app/backend/tests first: .dockerignore deliberately excludes tests from the image.
"""
import errno
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from contextvars import Context
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import clone_diagnostics as diagnostics
from app import repositories
from app.guardrails.errors import GuardrailViolation
from app.ingestion import clone, pipeline
from app.config import Settings

SECRET = 'fake-api-key-for-diagnostics\n{"event":"injected","reason":"injected"}'
PUBLIC_ERROR = "Could not clone that repository (is it public?)."
LOG = "app.clone_diagnostics"


@pytest.fixture
def captured(caplog):
    caplog.set_level(logging.WARNING, logger=LOG)
    return caplog


def events(caplog):
    records = [record for record in caplog.records if record.name == LOG]
    assert all(record.exc_info is None and record.stack_info is None and not record.args for record in records)
    return [json.loads(record.getMessage()) for record in records]


def wire_http(monkeypatch, handler):
    client = httpx.Client
    calls = []
    def factory(**kwargs):
        assert kwargs == {"trust_env": False, "follow_redirects": False, "timeout": 10.0}
        def send(request):
            calls.append(request)
            assert "authorization" not in request.headers
            return handler(request)
        return client(**kwargs, transport=httpx.MockTransport(send))
    monkeypatch.setattr(repositories.httpx, "Client", factory)
    return calls


def rejected_clone(tmp_path):
    rid = str(uuid4())
    with pytest.raises(GuardrailViolation) as failure:
        clone.clone_repo("https://github.com/example/project", tmp_path / rid, 50)
    assert failure.value.code == "clone_failed" and str(failure.value) == PUBLIC_ERROR
    assert not (tmp_path / rid).exists()
    return rid


@pytest.mark.parametrize("status,remaining,reason", [
    (403, "0", "github_rate_limited"), (429, "12", "github_rate_limited"),
    (403, "12", "github_http_error"), (404, "12", "github_http_error"),
    (500, None, "github_http_error"), (301, None, "github_http_error"),
    (403, SECRET, "github_http_error"),
], ids=["rate-zero", "rate-429", "forbidden", "not-found", "server-error", "redirect", "injected-header"])
def test_api_status_diagnostics_preserve_public_error(tmp_path, monkeypatch, captured, status, remaining, reason):
    headers = {"x-secret": SECRET.replace("\n", ""), "Location": "https://untrusted.invalid/"}
    if remaining is not None:
        headers["x-ratelimit-remaining"] = remaining
    calls = wire_http(monkeypatch, lambda req: httpx.Response(status, headers=headers, content=SECRET))
    monkeypatch.setattr(repositories, "_git", lambda *a: pytest.fail("Git ran after a failed public check"))
    rid = rejected_clone(tmp_path)
    assert len(calls) == 1  # diagnostics do not introduce retries or follow redirects
    event, = events(captured)
    assert event["phase"] == "github_validation" and event["reason"] == reason
    assert event["repo_id"] == rid and event["http_status"] == status
    assert type(event["elapsed_ms"]) is int and event["elapsed_ms"] >= 0
    assert SECRET not in captured.text and "x-secret" not in captured.text and "untrusted.invalid" not in captured.text
    if remaining == SECRET:
        assert "rate_remaining" not in event


@pytest.mark.parametrize("error,reason", [
    (httpx.ConnectError, "github_network_error"), (httpx.ReadError, "github_network_error"),
    (httpx.ConnectTimeout, "clone_timeout"), (httpx.ReadTimeout, "clone_timeout"),
])
def test_http_failures_do_not_log_exception_messages(tmp_path, monkeypatch, captured, error, reason):
    def fail(request):
        raise error(SECRET, request=request)
    wire_http(monkeypatch, fail)
    rid = rejected_clone(tmp_path)
    event, = events(captured)
    assert event["reason"] == reason and event["repo_id"] == rid
    assert SECRET not in captured.text and "example/project" not in captured.text


@pytest.mark.parametrize("content,reason", [
    (SECRET, "github_invalid_response"),
    (json.dumps({"private": True, "visibility": "private", "full_name": SECRET}), "github_not_public"),
    (json.dumps({"private": False, "visibility": "public", "full_name": SECRET}), "github_invalid_response"),
    ("[]", "github_invalid_response"),
    ("x" * (1024 * 1024 + 1), "github_invalid_response"),
], ids=["malformed", "private", "name-mismatch", "wrong-shape", "too-large"])
def test_bad_api_content_is_never_serialized(tmp_path, monkeypatch, captured, content, reason):
    wire_http(monkeypatch, lambda req: httpx.Response(200, content=content))
    rejected_clone(tmp_path)
    event, = events(captured)
    assert event["reason"] == reason
    assert SECRET not in captured.text and '"injected"' not in captured.text


@pytest.mark.parametrize("failure,reason", [
    (FileNotFoundError(errno.ENOENT, SECRET, "git"), "git_not_found"),
    (FileNotFoundError(errno.ENOENT, SECRET), "git_not_found"),
    (FileNotFoundError(errno.ENOENT, SECRET, "missing-working-directory"), "filesystem_error"),
    (PermissionError(errno.EACCES, SECRET), "filesystem_error"),
])
def test_executable_and_filesystem_diagnostics(tmp_path, monkeypatch, captured, failure, reason):
    def fail(*args, **kwargs):
        raise failure
    monkeypatch.setattr(repositories.subprocess, "Popen", fail)
    with diagnostics.diagnostic_scope("clone", str(uuid4())):
        with pytest.raises(OSError):
            repositories._git(tmp_path, ["clone"], 1024 * 1024, time.monotonic() + 10)
    event, = events(captured)
    assert event["reason"] == reason and event["phase"] == "git_clone"
    assert event["errno"] == failure.errno
    assert SECRET not in captured.text and "missing-working-directory" not in captured.text


@pytest.mark.parametrize("returncode", [1, 128, -9])
def test_git_errors_log_only_exit_code(tmp_path, monkeypatch, captured, returncode):
    def fail(command, **kwargs):
        kwargs["stderr"].write(SECRET.encode())
        kwargs["stdout"].write(SECRET.encode())
        return SimpleNamespace(returncode=returncode, poll=lambda: returncode)
    monkeypatch.setattr(repositories.subprocess, "Popen", fail)
    with pytest.raises(GuardrailViolation):
        repositories._git(tmp_path, ["clone"], 1024 * 1024, time.monotonic() + 10)
    event, = events(captured)
    assert event["reason"] == "git_execution_failed" and event["returncode"] == returncode
    assert SECRET not in captured.text and '"injected"' not in captured.text


@pytest.mark.parametrize("limit,reason", [
    ("time", "clone_timeout"), ("space", "disk_limit"),
    ("size", "disk_limit"), ("entries", "entry_limit"),
])
def test_limit_failures_keep_existing_thresholds(tmp_path, monkeypatch, captured, limit, reason):
    deadline = time.monotonic() + 10
    if limit == "time":
        deadline = time.monotonic() - 1
    elif limit == "space":
        monkeypatch.setattr(repositories.shutil, "disk_usage", lambda p: SimpleNamespace(free=repositories.MIN_FREE_BYTES - 1))
    elif limit == "size":
        monkeypatch.setattr(repositories, "_usage", lambda p: 4 * 1024 + 1)
    else:
        monkeypatch.setattr(repositories, "MAX_ENTRIES", 0)
        (tmp_path / "file").write_text("entry")
    with pytest.raises(GuardrailViolation):
        repositories._git(tmp_path, ["clone"], 1024, deadline)
    event, = events(captured)
    assert event["phase"] == "git_clone" and event["reason"] == reason


def test_output_limit_does_not_log_captured_text(tmp_path, monkeypatch, captured):
    def excessive(command, **kwargs):
        kwargs["stderr"].write(SECRET.encode())
        kwargs["stderr"].truncate(8 * 1024 * 1024 + 1)
        return SimpleNamespace(returncode=0, poll=lambda: 0)
    monkeypatch.setattr(repositories.subprocess, "Popen", excessive)
    with pytest.raises(GuardrailViolation):
        repositories._git(tmp_path, ["clone"], 1024 * 1024, time.monotonic() + 10)
    event, = events(captured)
    assert event["reason"] == "output_limit" and event["output_bytes"] == 8 * 1024 * 1024 + 1
    assert SECRET not in captured.text


def test_running_git_is_stopped_on_timeout(tmp_path, monkeypatch, captured):
    process = SimpleNamespace(returncode=None, poll=lambda: None)
    started, stopped = [], []
    def start(*a, **k):
        started.append(True)
        return process
    real_limits = repositories._limits
    def expire(root, max_bytes, deadline):
        real_limits(root, max_bytes, time.monotonic() - 1 if started else deadline)
    monkeypatch.setattr(repositories.subprocess, "Popen", start)
    monkeypatch.setattr(repositories, "_limits", expire)
    monkeypatch.setattr(repositories, "_stop", lambda p: stopped.append(p))
    with pytest.raises(GuardrailViolation):
        repositories._git(tmp_path, ["clone"], 1024, time.monotonic() + 10)
    assert stopped == [process]
    assert events(captured)[0]["reason"] == "clone_timeout"


@pytest.mark.parametrize("failure,reason", [(RuntimeError(SECRET), "unexpected_error"),
                                           (PermissionError(errno.EACCES, SECRET), "filesystem_error")])
def test_pipeline_never_persists_exception_text(tmp_path, monkeypatch, captured, failure, reason):
    rid = str(uuid4())
    settings = Settings(_env_file=None, repos_dir=str(tmp_path))
    @contextmanager
    def lock(*args):
        yield tmp_path / rid
    monkeypatch.setattr(pipeline, "repository_lock", lock)
    monkeypatch.setattr(pipeline, "get_settings", lambda: settings)
    monkeypatch.setattr(pipeline.store, "get_repo", lambda rid: {"status": "pending"})
    writes = []
    monkeypatch.setattr(pipeline.store, "update_repo", lambda rid, **kwargs: writes.append(kwargs))
    def fail(*a):
        raise failure
    monkeypatch.setattr(pipeline, "clone_repo", fail)
    pipeline.ingest_repo(rid, "https://github.com/example/project")
    assert writes == [{"status": "cloning"}, {"status": "failed", "error": "Indexing failed. Please try again."}]
    event, = events(captured)
    assert event["phase"] == "ingestion" and event["repo_id"] == rid and event["reason"] == reason
    assert SECRET not in captured.text and SECRET not in json.dumps(writes)


def test_failed_index_api_exposes_only_original_generic_clone_message(tmp_path, monkeypatch, captured):
    from app import main
    rid = str(uuid4())
    row = {"id": rid, "status": "pending", "error": None}
    @contextmanager
    def lock(*args):
        yield tmp_path / rid
    monkeypatch.setattr(pipeline, "repository_lock", lock)
    monkeypatch.setattr(pipeline, "get_settings", lambda: Settings(_env_file=None, repos_dir=str(tmp_path)))
    monkeypatch.setattr(pipeline.store, "get_repo", lambda rid: row.copy())
    monkeypatch.setattr(pipeline.store, "update_repo", lambda rid, **kwargs: row.update(kwargs))
    wire_http(monkeypatch, lambda req: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, content=SECRET))
    pipeline.ingest_repo(rid, "https://github.com/example/project")
    response = TestClient(main.app).get(f"/repos/{rid}")
    assert response.status_code == 200 and response.json()["status"] == "failed"
    assert response.json()["error"] == PUBLIC_ERROR
    assert "github_rate_limited" not in response.text and SECRET not in response.text
    assert events(captured)[0]["reason"] == "github_rate_limited"


def test_successful_clone_has_no_failure_logs_or_behavior_changes(tmp_path, monkeypatch, captured):
    calls = wire_http(monkeypatch, lambda req: httpx.Response(200, json={
        "private": False, "visibility": "public", "full_name": "example/project"}))
    commands = []
    sha = "a" * 40
    def git(root, args, max_bytes, deadline):
        commands.append(args)
        return sha.encode() if args[0] == "rev-parse" else b""
    monkeypatch.setattr(repositories, "_git", git)
    assert clone.clone_repo("https://github.com/example/project", tmp_path / str(uuid4()), 50) == sha
    assert len(calls) == 1 and [args[0] for args in commands] == ["clone", "rev-parse"]
    assert commands[0] == ["clone", "--template=", "--depth", "1", "--single-branch", "--",
                            "https://github.com/example/project.git", "."]
    assert not events(captured)


def test_log_injection_is_excluded_instead_of_string_redaction(captured):
    with diagnostics.diagnostic_scope(SECRET, str(uuid4()) + SECRET):
        diagnostics.log_failure(SECRET, http_status=500, errno=SECRET, headers=SECRET, body=SECRET,
                                environment=SECRET, returncode=True, free_bytes=float("nan"), max_bytes=2**100)
    event, = events(captured)
    assert set(event) == {"event", "phase", "reason", "elapsed_ms", "http_status"}
    assert event["phase"] == "internal" and event["reason"] == "unexpected_error"
    assert SECRET not in captured.text and "injected" not in captured.text


def test_logging_handler_failure_does_not_change_clone_error(tmp_path, monkeypatch):
    wire_http(monkeypatch, lambda req: httpx.Response(403))
    def broken(*a, **k):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(diagnostics.logger, "warning", broken)
    rejected_clone(tmp_path)


def test_snapshot_diagnostics_do_not_span_sse_worker_contexts(tmp_path, monkeypatch):
    @contextmanager
    def prepared(*a):
        yield tmp_path
    monkeypatch.setattr(repositories, "_repository_snapshot", prepared)
    manager = repositories.repository_snapshot(str(uuid4()))
    assert Context().run(manager.__enter__) == tmp_path
    assert Context().run(manager.__exit__, None, None, None) is False


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("REPOMIND_LINUX_CLONE_CHECK") != "1",
                    reason="Opt-in anonymous HTTPS clone inside the Linux deployment image")
def test_linux_public_clone_integration(tmp_path):
    root = tmp_path / str(uuid4())
    sha = clone.clone_repo("https://github.com/octocat/Hello-World", root, 5)
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
    assert (root / ".git").is_dir() and (root / "README").is_file()
