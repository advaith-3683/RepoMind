import copy
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app import repositories as snapshots
from app.config import Settings
from app.guardrails.errors import GuardrailViolation


def git(root, *args):
    result = subprocess.run(
        ["git", "-c", "user.name=Snapshot Test", "-c", "user.email=test@example.invalid",
         "-c", "core.autocrlf=false", *args], cwd=root, env=snapshots._git_environment(),
        capture_output=True, check=True,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "--template=")
    (source / "app.py").write_bytes(b"def answer():\n    return 1\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "indexed")
    sha = git(source, "rev-parse", "HEAD")
    (source / "app.py").write_bytes(b"def answer():\n    return 2\n")
    git(source, "commit", "-am", "later branch tip")
    settings = Settings(_env_file=None, repos_dir=str(tmp_path / "repos"), max_repo_mb=5)
    row = {"id": str(uuid4()), "url": "https://github.com/example/project", "status": "ready",
           "commit_sha": sha, "indexed_at": "original-time", "chunks_indexed": 3}
    original = copy.deepcopy(row)
    monkeypatch.setattr(snapshots.store, "get_repo", lambda rid: copy.deepcopy(row))

    def no_write(*args, **kwargs):
        pytest.fail("Restoration attempted a database write")

    for name in ("update_repo", "delete_chunks", "insert_chunks", "create_query", "finish_query", "save_trace"):
        monkeypatch.setattr(snapshots.store, name, no_write)
    public_checks = []
    monkeypatch.setattr(snapshots, "_public_repository", lambda *args: public_checks.append(args))
    real_git = snapshots._git
    fetches = []

    def local_fetch(root, args, max_bytes, deadline):
        if args[0] == "fetch":
            assert args[-2] == "https://github.com/example/project.git"
            assert args[-1] == row["commit_sha"]
            fetches.append(args[-1])
            # Only the test transport can use a local repository; production forbids file://.
            git(root, "fetch", "--depth=1", "--no-tags", "--", str(source), args[-1])
            return b""
        return real_git(root, args, max_bytes, deadline)

    monkeypatch.setattr(snapshots, "_git", local_fetch)
    return SimpleNamespace(source=source, settings=settings, row=row, original=original, sha=sha,
                           dest=Path(settings.repos_dir) / row["id"], fetches=fetches,
                           checks=public_checks, transport=local_fetch)


def snapshot(indexed):
    return snapshots.repository_snapshot(indexed.row["id"], indexed.settings)


def test_exact_commit_restore_and_reuse_without_database_writes(indexed):
    with snapshot(indexed) as root:
        assert (root / "app.py").read_bytes().endswith(b"return 1\n")
        assert git(root, "rev-parse", "HEAD") == indexed.sha
    with snapshot(indexed):
        pass
    assert indexed.fetches == [indexed.sha]
    assert indexed.row == indexed.original
    assert len(indexed.checks) == 3  # including recheck immediately before publication


@pytest.mark.parametrize("corruption", ["missing", "incomplete", "modified", "untracked", "wrong_commit", "head", "object", "config"])
def test_repairs_corrupt_snapshots(indexed, corruption):
    with snapshot(indexed):
        pass
    root = indexed.dest
    if corruption == "missing":
        snapshots._remove_owned(root, root.parent)
    elif corruption == "incomplete":
        (root / "app.py").unlink()
    elif corruption == "modified":
        (root / "app.py").write_bytes(b"def answer():\n    return 9\n")
    elif corruption == "untracked":
        (root / "extra.py").write_text("unexpected")
    elif corruption == "wrong_commit":
        snapshots._remove_owned(root, root.parent)
        shutil.copytree(indexed.source, root)
    elif corruption == "head":
        (root / ".git/HEAD").write_text("invalid HEAD")
    elif corruption == "object":
        obj = root / ".git/objects" / indexed.sha[:2] / indexed.sha[2:]
        obj.chmod(0o600)
        obj.write_bytes(b"corrupt object")
    else:
        with (root / ".git/config").open("a") as f:
            f.write('\n[core]\n\tfsmonitor = must-never-execute\n')
    with snapshot(indexed) as restored:
        assert (restored / "app.py").read_bytes().endswith(b"return 1\n")
    assert len(indexed.fetches) == 2
    assert indexed.row == indexed.original


def test_two_readers_restore_once_and_never_observe_partial_checkout(indexed, monkeypatch):
    fetching, release = threading.Event(), threading.Event()
    transport = indexed.transport

    def blocked(root, args, *rest):
        if args[0] == "fetch":
            fetching.set()
            assert release.wait(5)
        return transport(root, args, *rest)

    monkeypatch.setattr(snapshots, "_git", blocked)

    def read():
        with snapshot(indexed) as root:
            return (root / "app.py").read_bytes()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(read)
        assert fetching.wait(5)
        second = pool.submit(read)
        assert not indexed.dest.exists()
        assert not second.done()
        release.set()
        assert first.result(timeout=10) == second.result(timeout=10)
    assert len(indexed.fetches) == 1


def test_ingestion_waits_for_reader_and_skips_ready_index(indexed, monkeypatch):
    from app.ingestion import pipeline
    monkeypatch.setattr(pipeline, "get_settings", lambda: indexed.settings)
    monkeypatch.setattr(snapshots, "get_settings", lambda: indexed.settings)
    monkeypatch.setattr(pipeline, "_ingest_repo", lambda *a: pytest.fail("Ready index was reingested"))
    with ThreadPoolExecutor(max_workers=1) as pool:
        with snapshot(indexed):
            work = pool.submit(pipeline.ingest_repo, indexed.row["id"], indexed.row["url"])
            time.sleep(0.1)
            assert not work.done()
        work.result(timeout=5)
    assert indexed.row == indexed.original


def test_reader_waits_for_pending_ingestion_to_complete(indexed, monkeypatch):
    from app.ingestion import pipeline
    with snapshot(indexed):
        pass
    indexed.row["status"] = "pending"
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(snapshots, "get_settings", lambda: indexed.settings)
    def ingest(*args):
        indexed.row["status"] = "indexing"
        (indexed.dest / "app.py").write_text("partially indexed checkout")
        started.set()
        assert release.wait(5)
        (indexed.dest / "app.py").write_bytes(b"def answer():\n    return 1\n")
        indexed.row["status"] = "ready"
    monkeypatch.setattr(pipeline, "_ingest_repo", ingest)
    def read():
        with snapshot(indexed) as root:
            return (root / "app.py").read_bytes()
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(pipeline.ingest_repo, indexed.row["id"], indexed.row["url"])
        assert started.wait(5)
        reader = pool.submit(read)
        assert not reader.done()
        release.set()
        writer.result(timeout=5)
        assert reader.result(timeout=5).endswith(b"return 1\n")
    assert len(indexed.fetches) == 1


def test_os_lock_blocks_another_process(indexed):
    with snapshots.repository_lock(indexed.row["id"], indexed.settings):
        code = '''
import sys
from app.config import Settings
from app.repositories import repository_lock
from app.guardrails.errors import GuardrailViolation
try:
    with repository_lock(sys.argv[1], Settings(_env_file=None, repos_dir=sys.argv[2]), timeout=0.1):
        raise SystemExit(1)
except GuardrailViolation as exc:
    raise SystemExit(0 if exc.code == "repo_not_ready" else 2)
'''
        env = {**snapshots._git_environment(), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        result = subprocess.run([sys.executable, "-B", "-c", code, indexed.row["id"], indexed.settings.repos_dir],
                                env=env, capture_output=True, timeout=15)
        assert result.returncode == 0


@pytest.mark.parametrize("failure", ["unavailable", "interrupted", "sha_changed", "publish"])
def test_failed_restores_leave_original_and_database_untouched(indexed, monkeypatch, failure):
    with snapshot(indexed):
        pass
    (indexed.dest / "app.py").write_text("old clone must survive failed restore")
    before = (indexed.dest / "app.py").read_bytes()
    if failure in {"unavailable", "interrupted"}:
        def fail(root, args, *rest):
            if args[0] == "fetch":
                (root / "partial").write_text("incomplete")
                if failure == "interrupted":
                    raise OSError("simulated interruption")
                raise snapshots._unavailable()
            return indexed.transport(root, args, *rest)
        monkeypatch.setattr(snapshots, "_git", fail)
    elif failure == "sha_changed":
        reads = []
        def changed(rid):
            reads.append(rid)
            result = copy.deepcopy(indexed.row)
            if len(reads) > 1:
                result["commit_sha"] = "a" * 40
            return result
        monkeypatch.setattr(snapshots.store, "get_repo", changed)
    else:
        real_rename = Path.rename
        failed = []
        def fail_rename(self, target):
            if not failed and self.name.startswith(".restore-") and Path(target) == indexed.dest:
                failed.append(True)
                raise OSError("simulated publication failure")
            return real_rename(self, target)
        monkeypatch.setattr(Path, "rename", fail_rename)
    with pytest.raises(GuardrailViolation):
        with snapshot(indexed):
            pytest.fail("Failed restoration served content")
    assert (indexed.dest / "app.py").read_bytes() == before
    assert indexed.row == indexed.original


def test_interrupted_stage_is_removed_on_next_attempt(indexed):
    indexed.dest.parent.mkdir()
    orphan = indexed.dest.parent / f".restore-{indexed.dest.name}-orphan"
    orphan.mkdir()
    (orphan / "partial").write_text("unpublished")
    with snapshot(indexed):
        pass
    assert not orphan.exists()


def test_backup_after_interrupted_publication_is_cleaned_without_refetch(indexed):
    with snapshot(indexed):
        pass
    orphan = indexed.dest.parent / f".restore-{indexed.dest.name}-backup"
    orphan.mkdir()
    (orphan / "old").write_text("abandoned backup")
    with snapshot(indexed):
        pass
    assert not orphan.exists()
    assert len(indexed.fetches) == 1


def test_public_access_is_rechecked_for_cached_files(indexed, monkeypatch):
    with snapshot(indexed):
        pass
    monkeypatch.setattr(snapshots, "_public_repository", lambda *a: (_ for _ in ()).throw(snapshots._unavailable()))
    with pytest.raises(GuardrailViolation):
        with snapshot(indexed):
            pytest.fail("Served a repository that is no longer anonymously accessible")
    assert indexed.row == indexed.original
    assert len(indexed.fetches) == 1


def test_public_access_loss_during_restore_prevents_publication(indexed, monkeypatch):
    calls = []
    def public(*args):
        calls.append(True)
        if len(calls) > 1:
            raise snapshots._unavailable()
    monkeypatch.setattr(snapshots, "_public_repository", public)
    with pytest.raises(GuardrailViolation):
        with snapshot(indexed):
            pass
    assert not indexed.dest.exists()
    assert not list(indexed.dest.parent.glob(".restore-*"))
    assert indexed.row == indexed.original


def test_preview_requires_authentication_before_restoration(indexed, monkeypatch):
    from app import auth, main
    monkeypatch.setattr(auth, "get_settings", lambda: indexed.settings)
    assert indexed.settings.auth_required is True
    response = TestClient(main.app).get(f'/repos/{indexed.row["id"]}/file', params={"path": "app.py"})
    assert response.status_code == 401
    assert not indexed.checks and not indexed.fetches


@pytest.mark.parametrize("bad", ["https://github.com/../project", "https://github.com/example/..", "https://github.com/example/project?token=x", "https://user:pass@github.com/example/project"])
def test_invalid_repo_urls_are_never_fetched(indexed, bad):
    indexed.row["url"] = bad
    with pytest.raises(GuardrailViolation):
        with snapshot(indexed):
            pass
    assert not indexed.fetches


@pytest.mark.parametrize("bad_sha", [None, "HEAD", "abc123", "-bad", "a" * 41])
def test_no_fallback_for_invalid_or_missing_sha(indexed, bad_sha):
    indexed.row["commit_sha"] = bad_sha
    with pytest.raises(GuardrailViolation, match="ready indexed snapshot"):
        with snapshot(indexed):
            pass
    assert not indexed.fetches


@pytest.mark.parametrize("condition", ["space", "time", "size", "entries"])
def test_restore_limits_fail_closed(indexed, monkeypatch, condition):
    if condition == "space":
        monkeypatch.setattr(snapshots.shutil, "disk_usage", lambda p: SimpleNamespace(free=0))
    elif condition == "time":
        monkeypatch.setattr(snapshots, "RESTORE_TIMEOUT", 0)
    elif condition == "size":
        indexed.settings.max_repo_mb = 0
    else:
        monkeypatch.setattr(snapshots, "MAX_ENTRIES", 1)
    with pytest.raises(GuardrailViolation):
        with snapshot(indexed):
            pass
    assert not indexed.dest.exists()
    assert indexed.row == indexed.original


def test_linked_destination_is_rejected_without_touching_it(indexed, monkeypatch):
    monkeypatch.setattr(snapshots, "_linked", lambda p: p == indexed.dest)
    with pytest.raises(GuardrailViolation):
        with snapshot(indexed):
            pass
    assert not indexed.fetches


@pytest.mark.parametrize("status,data", [
    (301, {}), (302, {}), (401, {}), (403, {}), (404, {}), (429, {}),
    (200, {"private": True, "visibility": "private", "full_name": "o/r"}),
    (200, {"private": False, "visibility": "public", "full_name": "other/repo"}),
    (200, []),
])
def test_public_check_rejects_redirects_private_and_unavailable(status, data, monkeypatch):
    real_client = httpx.Client
    def factory(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return real_client(**kwargs, transport=httpx.MockTransport(
            lambda req: httpx.Response(status, json=data, headers={"Location": "https://evil.example/"})))
    monkeypatch.setattr(snapshots.httpx, "Client", factory)
    with pytest.raises(GuardrailViolation):
        snapshots._public_repository("o", "r", time.monotonic() + 10)


def test_http_and_git_ignore_credentials_and_unsafe_configuration(tmp_path, monkeypatch):
    for key in ("GITHUB_TOKEN", "GH_TOKEN", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0",
                "GIT_CONFIG_VALUE_0", "HTTPS_PROXY", "GIT_SSH_COMMAND", "LD_PRELOAD", "GIT_OBJECT_DIRECTORY"):
        monkeypatch.setenv(key, "fake-secret-or-unsafe-command")
    env = snapshots._git_environment()
    assert "GITHUB_TOKEN" not in env and "GIT_ASKPASS" not in env and "GIT_CONFIG_COUNT" not in env
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["HOME"] == os.devnull and env["USERPROFILE"] == os.devnull
    marker = tmp_path / "must-not-exist"
    config = tmp_path / "unsafe.gitconfig"
    config.write_text(f'[core]\n hooksPath = {marker}\n[credential]\n helper = unsafe-helper\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    snapshots._git(tmp_path, ["init", "--template="], 1024 * 1024, time.monotonic() + 10)
    assert not marker.exists()
    real_client = httpx.Client
    def handler(req):
        assert "authorization" not in req.headers and "cookie" not in req.headers
        assert str(req.url) == "https://api.github.com/repos/o/r"
        return httpx.Response(200, json={"private": False, "visibility": "public", "full_name": "o/r"})
    monkeypatch.setattr(snapshots.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)))
    snapshots._public_repository("o", "r", time.monotonic() + 10)


def test_checkout_ignores_external_filters_and_hooks_and_never_runs_submodules(indexed, tmp_path, monkeypatch):
    source = indexed.source
    (source / ".gitattributes").write_text("*.py filter=hostile\n")
    (source / ".gitmodules").write_text('[submodule "vendor"]\n path = vendor\n url = file:///must-not-open\n')
    git(source, "add", ".")
    git(source, "update-index", "--add", "--cacheinfo", f"160000,{indexed.sha},vendor")
    git(source, "commit", "-m", "filters and gitlink")
    indexed.row["commit_sha"] = git(source, "rev-parse", "HEAD")
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text('[filter "hostile"]\n smudge = must-never-execute\n required = true\n'
                       '[core]\n hooksPath = must-never-open\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    with snapshot(indexed) as root:
        assert (root / "app.py").read_bytes().endswith(b"return 2\n")
        assert (root / "vendor").is_dir() and not list((root / "vendor").iterdir())
        assert not (root / ".git/modules").exists()


@pytest.mark.parametrize("path", ["../escape", "/absolute", ".git/config", "nested/../../escape", "C:/escape", "nested\\escape"])
def test_git_tree_paths_are_validated_before_reading(indexed, monkeypatch, path):
    with snapshot(indexed):
        pass
    transport = indexed.transport
    def tree(root, args, *rest):
        if args[0] == "ls-tree":
            return b"100644 blob " + b"a" * 40 + b" 1\t" + path.encode() + b"\0"
        return transport(root, args, *rest)
    monkeypatch.setattr(snapshots, "_git", tree)
    assert not snapshots._checkout_valid(indexed.dest, indexed.sha, 5 * 1024 * 1024, time.monotonic() + 10)


def test_git_time_limit_terminates_process(tmp_path, monkeypatch):
    stopped = []
    class Process:
        returncode = None
        def poll(self):
            return self.returncode
    process = Process()
    monkeypatch.setattr(snapshots.subprocess, "Popen", lambda *a, **k: process)
    calls = []
    def limits(*a):
        calls.append(True)
        if len(calls) > 1:
            raise snapshots._unavailable()
    monkeypatch.setattr(snapshots, "_limits", limits)
    monkeypatch.setattr(snapshots, "_stop", lambda p: stopped.append(p))
    with pytest.raises(GuardrailViolation):
        snapshots._git(tmp_path, ["fetch", "--depth=1"], 1024, time.monotonic() + 10)
    assert stopped == [process]


def test_repository_id_cannot_escape_storage(indexed):
    with pytest.raises(GuardrailViolation):
        with snapshots.repository_snapshot("../elsewhere", indexed.settings):
            pass


def test_agent_restoration_failure_emits_controlled_error_and_done(indexed, monkeypatch):
    from app import service
    monkeypatch.setattr(service, "get_settings", lambda: indexed.settings)
    monkeypatch.setattr(service, "get_embedder", lambda: object())
    monkeypatch.setattr(service, "build_llm", lambda s: object())
    monkeypatch.setattr(service.store, "create_query", lambda *a: "query-test")
    monkeypatch.setattr(service.store, "finish_query", lambda *a: None)
    monkeypatch.setattr(service.store, "save_trace", lambda *a: None)
    monkeypatch.setattr(snapshots, "_public_repository", lambda *a: (_ for _ in ()).throw(snapshots._unavailable()))
    events = list(service.stream_answer(indexed.row, "question", "agent"))
    assert [e["type"] for e in events] == ["start", "error", "done"]
    assert events[1]["code"] == "github_error" and "detail" not in events[1]
    assert events[-1]["status"] == "error"


def test_preview_and_patch_diff_use_restored_snapshot(indexed, monkeypatch):
    from app import main, service
    monkeypatch.setattr(main, "settings", indexed.settings)
    monkeypatch.setattr(snapshots, "get_settings", lambda: indexed.settings)
    response = TestClient(main.app).get(f'/repos/{indexed.row["id"]}/file', params={"path": "app.py"})
    assert response.status_code == 200 and "return 1" in "\n".join(response.json()["lines"])
    (indexed.dest / "app.py").unlink()
    diff = service.patch_diff_for(indexed.row["id"], {"path": "app.py", "edits": [{"old": "return 1", "new": "return 3"}]})
    assert "-    return 1" in diff and "+    return 3" in diff
    assert indexed.row == indexed.original


def test_preview_and_patch_errors_do_not_fall_back_to_stale_content(indexed, monkeypatch):
    from app import main, service
    monkeypatch.setattr(main, "settings", indexed.settings)
    monkeypatch.setattr(snapshots, "get_settings", lambda: indexed.settings)
    monkeypatch.setattr(snapshots, "_public_repository", lambda *a: (_ for _ in ()).throw(snapshots._unavailable()))
    response = TestClient(main.app).get(f'/repos/{indexed.row["id"]}/file', params={"path": "app.py"})
    assert response.status_code == 502 and response.json()["error"]["code"] == "github_error"
    with pytest.raises(GuardrailViolation):
        service.patch_diff_for(indexed.row["id"], {"path": "app.py", "edits": []})


def test_agent_reads_under_snapshot_lock(indexed, monkeypatch):
    from app import service
    monkeypatch.setattr(service, "get_settings", lambda: indexed.settings)
    monkeypatch.setattr(service, "get_embedder", lambda: object())
    monkeypatch.setattr(service, "build_llm", lambda s: object())
    monkeypatch.setattr(service.store, "create_query", lambda *a: "query-test")
    monkeypatch.setattr(service.store, "finish_query", lambda *a: None)
    monkeypatch.setattr(service.store, "save_trace", lambda *a: None)
    @contextmanager
    def connection():
        yield object()
    monkeypatch.setattr(service, "get_conn", connection)
    def agent(question, tools, *args):
        assert "return 1" in tools.read_file("app.py")
        with pytest.raises(GuardrailViolation):
            with snapshots.repository_lock(indexed.row["id"], indexed.settings, timeout=0):
                pass
        yield {"type": "answer", "text": "verified"}
    monkeypatch.setattr(service, "run_agent", agent)
    events = list(service.stream_answer(indexed.row, "question", "agent"))
    assert [e["type"] for e in events] == ["start", "answer", "done"]
    assert indexed.row == indexed.original


def _public_client(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(snapshots.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(handler)))


def test_public_check_sends_server_token_only_to_github_api(monkeypatch):
    monkeypatch.setattr(snapshots.get_settings(), "github_api_token", "  ghp_servertoken  ")
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"private": False, "visibility": "public", "full_name": "o/r"})
    _public_client(monkeypatch, handler)
    snapshots._public_repository("o", "r", time.monotonic() + 10)
    assert seen[0].headers["authorization"] == "Bearer ghp_servertoken"
    assert seen[0].url.host == "api.github.com" and "cookie" not in seen[0].headers


def test_public_check_without_server_token_is_anonymous(monkeypatch):
    monkeypatch.setattr(snapshots.get_settings(), "github_api_token", "")
    _public_client(monkeypatch, lambda req: (
        httpx.Response(200, json={"private": False, "visibility": "public", "full_name": "o/r"})
        if "authorization" not in req.headers else httpx.Response(500)))
    snapshots._public_repository("o", "r", time.monotonic() + 10)


def test_server_token_never_reaches_git_or_logs(monkeypatch, caplog):
    monkeypatch.setattr(snapshots.get_settings(), "github_api_token", "ghp_servertoken")
    monkeypatch.setenv("GITHUB_API_TOKEN", "ghp_servertoken")
    assert "GITHUB_API_TOKEN" not in snapshots._git_environment()
    _public_client(monkeypatch, lambda req: httpx.Response(403, headers={"x-ratelimit-remaining": "0"}))
    with caplog.at_level("WARNING"), pytest.raises(GuardrailViolation) as exc:
        snapshots._public_repository("o", "r", time.monotonic() + 10)
    assert "ghp_servertoken" not in caplog.text and "ghp_servertoken" not in exc.value.message
    assert "github_rate_limited" in caplog.text


def test_ingestion_failure_logs_only_error_kind(monkeypatch, caplog):
    from app.ingestion import pipeline

    class Boom(Exception):
        code = 429
    secret = "SECRET-IN-MESSAGE"
    caplog.set_level("WARNING")
    pipeline._log_error_kind(Boom(secret))
    assert "type=Boom code=429" in caplog.text and secret not in caplog.text


def test_backoff_caps_delay_and_retries(monkeypatch):
    from app.ingestion import embed
    sleeps = []
    monkeypatch.setattr(embed.time, "sleep", sleeps.append)
    monkeypatch.setattr(embed.random, "random", lambda: 0.0)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] < 7:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "ok"
    assert embed.with_backoff(fn, retries=7, base=4.0, max_delay=60.0) == "ok"
    assert sleeps == [4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_ingestion_rate_limit_gives_friendly_error_and_paces_batches(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.ingestion import pipeline
    updates, sleeps = [], []
    s = get_settings()
    monkeypatch.setattr(s, "embed_batch_size", 2)
    monkeypatch.setattr(s, "embed_batch_delay_s", 0.5)
    monkeypatch.setattr(s, "repos_dir", str(tmp_path))
    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)
    monkeypatch.setattr(pipeline.store, "update_repo", lambda rid, **kw: updates.append(kw))
    monkeypatch.setattr(pipeline.store, "delete_chunks", lambda rid: None)
    inserted = []
    monkeypatch.setattr(pipeline.store, "insert_chunks", lambda rid, b, v: inserted.append(len(b)))
    monkeypatch.setattr(pipeline, "clone_repo", lambda *a, **k: "a" * 40)
    chunks = [SimpleNamespace(embed_text=lambda: "x") for _ in range(5)]
    monkeypatch.setattr(pipeline, "build_chunks", lambda root, st: (chunks, 1))

    class Quota(Exception):
        code = 429
    Quota.__name__ = "ClientError"

    class Embedder:
        def __init__(self):
            self.n = 0

        def embed_documents(self, texts):
            self.n += 1
            if self.n == 3:
                raise Quota("rate limited")
            return [[0.0]] * len(texts)
    pipeline._ingest_repo(uuid4(), "https://github.com/o/r", Embedder())
    assert inserted == [2, 2] and sleeps == [0.5, 0.5]
    assert updates[-1]["status"] == "failed" and "rate-limited" in updates[-1]["error"]
