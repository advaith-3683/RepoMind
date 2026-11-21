"""Exact-commit, public-only repository snapshots. No database writes or embeddings."""
import configparser
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

import httpx

from . import store
from .config import get_settings
from .clone_diagnostics import diagnostic_scope, log_failure, log_unreported
from .guardrails.errors import GuardrailViolation
from .ingestion.clone import parse_github_url

RESTORE_TIMEOUT = 120
LOCK_TIMEOUT = 30
MAX_ENTRIES = 20000
MIN_FREE_BYTES = 64 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{40}")


def _unavailable():
    return GuardrailViolation("github_error", "The indexed repository snapshot is unavailable. Please retry later.")


def _linked(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _paths(repo_id, settings):
    key = str(UUID(str(repo_id)))
    base = Path(settings.repos_dir).absolute()
    for part in (base, *base.parents):
        if _linked(part):
            raise _unavailable()
    base.mkdir(parents=True, exist_ok=True)
    locks = base / ".locks"
    if _linked(locks):
        raise _unavailable()
    locks.mkdir(exist_ok=True)
    dest = base / key
    if _linked(dest):
        raise _unavailable()
    return base, dest, locks / (key + ".lock")


@contextmanager
def repository_lock(repo_id, settings=None, timeout=None):
    """OS file lock shared by threads/processes, held for the entire read or ingestion."""
    settings = settings or get_settings()
    try:
        _, dest, lock_path = _paths(repo_id, settings)
        if _linked(lock_path):
            raise _unavailable()
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except (OSError, ValueError) as exc:
        log_failure("filesystem_error", errno=exc.errno if isinstance(exc, OSError) else None)
        raise _unavailable() from None
    with os.fdopen(fd, "r+b") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _unavailable()
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        deadline = time.monotonic() + (LOCK_TIMEOUT if timeout is None else timeout)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    log_failure("lock_timeout")
                    raise GuardrailViolation("repo_not_ready", "Repository files are busy. Please retry.") from None
                time.sleep(0.05)
        try:
            yield dest
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _public_repository(owner, name, deadline):
    with diagnostic_scope("github_validation") as trace:
        before = trace.failures
        try:
            return _validate_public_repository(owner, name, deadline)
        except httpx.TimeoutException:
            log_failure("clone_timeout")
            raise
        except httpx.HTTPError:
            log_failure("github_network_error")
            raise
        except ValueError:
            log_unreported(trace, before, "github_invalid_response")
            raise


def _validate_public_repository(owner, name, deadline):
    # No netrc, proxy environment, cookies, or redirects. The optional server-side read-only token is
    # sent only to this fixed api.github.com URL (never to git, never user-supplied, never logged).
    headers = {"Accept": "application/vnd.github+json"}
    token = (get_settings().github_api_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=10.0) as client:
        with client.stream("GET", f"https://api.github.com/repos/{owner}/{name}",
                           headers=headers) as response:
            if response.status_code != 200:
                remaining = response.headers.get("x-ratelimit-remaining", "")
                remaining = int(remaining) if remaining.isascii() and remaining.isdecimal() and len(remaining) <= 10 else None
                limited = response.status_code == 429 or (response.status_code == 403 and remaining == 0)
                log_failure("github_rate_limited" if limited else "github_http_error",
                            http_status=response.status_code, rate_remaining=remaining)
                raise _unavailable()
            body = bytearray()
            for block in response.iter_bytes():
                body.extend(block)
                if len(body) > 1024 * 1024:
                    log_failure("github_invalid_response", response_bytes=len(body))
                    raise _unavailable()
                if time.monotonic() >= deadline:
                    log_failure("clone_timeout")
                    raise _unavailable()
    data = json.loads(body)
    if (not isinstance(data, dict) or data.get("private") is not False or data.get("visibility") != "public"
            or str(data.get("full_name", "")).lower() != f"{owner}/{name}".lower()):
        log_failure("github_not_public" if isinstance(data, dict) and data.get("private") is True
                    else "github_invalid_response", http_status=200)
        raise _unavailable()


def _git_environment():
    # Allowlist rather than trying to enumerate every credential/config variable.
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_SYSTEM=os.devnull,
               GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1",
               GIT_LFS_SKIP_SMUDGE="1", HOME=os.devnull, USERPROFILE=os.devnull,
               XDG_CONFIG_HOME=os.devnull, LC_ALL="C", LANG="C")
    return env


def _usage(root):
    total = count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            p = Path(directory) / name
            count += 1
            if count > MAX_ENTRIES:
                log_failure("entry_limit", entry_count=count, max_entries=MAX_ENTRIES)
                raise _unavailable()
            try:
                if not _linked(p) and p.is_file():
                    total += p.stat().st_size
            except FileNotFoundError:
                # Git atomically renames temporary object/pack files while fetching.
                continue
    return total


def _limits(root, max_bytes, deadline):
    if time.monotonic() >= deadline:
        log_failure("clone_timeout")
        raise _unavailable()
    used = _usage(root)
    if used > max_bytes * 4:
        log_failure("disk_limit", used_bytes=used, max_bytes=max_bytes * 4)
        raise _unavailable()
    free = shutil.disk_usage(root.parent).free
    if free < MIN_FREE_BYTES:
        log_failure("disk_limit", free_bytes=free, min_free_bytes=MIN_FREE_BYTES)
        raise _unavailable()


def _stop(process):
    if os.name == "nt":
        subprocess.run([str(Path(os.environ["SYSTEMROOT"]) / "System32/taskkill.exe"),
                        "/PID", str(process.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=10)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def _git(root, args, max_bytes, deadline):
    phase = {"clone": "git_clone", "fetch": "git_fetch", "checkout": "git_checkout",
             "init": "git_init", "rev-parse": "git_verify", "fsck": "git_verify", "ls-tree": "git_verify"}.get(args[0] if args else None, "git_execution")
    with diagnostic_scope(phase) as trace:
        before = trace.failures
        try:
            return _run_git(root, args, max_bytes, deadline)
        except subprocess.TimeoutExpired:
            log_unreported(trace, before, "clone_timeout")
            raise
        except OSError as exc:
            log_unreported(trace, before, "filesystem_error", errno=exc.errno)
            raise


def _run_git(root, args, max_bytes, deadline):
    command = ["git", "-c", "credential.helper=", "-c", "credential.interactive=false",
               "-c", "http.extraHeader=", "-c", "http.proxy=", "-c", "http.followRedirects=false",
               "-c", "http.cookieFile=", "-c", "http.saveCookies=false", "-c", "http.emptyAuth=false",
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
               "-c", "core.attributesFile=" + os.devnull, "-c", "core.autocrlf=false",
               "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
               "-c", "submodule.recurse=false", "-c", "fetch.recurseSubmodules=false",
               "-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true", *args]
    _limits(root, max_bytes, deadline)
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen(command, cwd=root, env=_git_environment(), stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, start_new_session=os.name != "nt")
        except FileNotFoundError as exc:
            log_failure("git_not_found" if exc.filename in {None, "git", b"git"} else "filesystem_error", errno=exc.errno)
            raise
        try:
            while process.poll() is None:
                _limits(root, max_bytes, deadline)
                if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > 8 * 1024 * 1024:
                    log_failure("output_limit", output_bytes=os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size)
                    raise _unavailable()
                time.sleep(0.05)
            _limits(root, max_bytes, deadline)
            if os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size > 8 * 1024 * 1024:
                log_failure("output_limit", output_bytes=os.fstat(out.fileno()).st_size + os.fstat(err.fileno()).st_size)
                raise _unavailable()
            if process.returncode:
                log_failure("git_execution_failed", returncode=process.returncode)
                raise _unavailable()
            out.seek(0)
            result = out.read(8 * 1024 * 1024 + 1)
            if len(result) > 8 * 1024 * 1024:
                log_failure("output_limit", output_bytes=len(result))
                raise _unavailable()
            return result
        finally:
            if process.poll() is None:
                _stop(process)


def _trusted_git_directory(root):
    """Never execute Git using a worktree's arbitrary local config or alternates."""
    gitdir = root / ".git"
    if not gitdir.is_dir() or _linked(gitdir):
        return False
    if not all((gitdir / name).is_file() for name in ("config", "HEAD", "index")):
        return False
    for directory, dirs, files in os.walk(gitdir, followlinks=False):
        if any(_linked(Path(directory) / name) for name in dirs + files):
            return False
        if any(not stat.S_ISREG((Path(directory) / name).stat().st_mode) for name in files):
            return False
    for rel in ("objects/info/alternates", "objects/info/http-alternates", "info/grafts", "refs/replace",
                "commondir", "gitdir", "worktrees", "modules", "config.worktree"):
        if (gitdir / rel).exists():
            return False
    config = configparser.RawConfigParser()
    config.read(gitdir / "config", encoding="utf-8")
    allowed = {"core": {"repositoryformatversion", "filemode", "bare", "logallrefupdates",
                        "symlinks", "ignorecase", "protecthfs", "protectntfs"},
               'remote "origin"': {"url", "fetch"}}
    return all(section in allowed and set(config[section]) <= allowed[section] for section in config.sections())


def _checkout_valid(root, sha, max_bytes, deadline):
    try:
        _limits(root, max_bytes, deadline)
        if not root.is_dir() or _linked(root) or not _trusted_git_directory(root):
            return False
        if _git(root, ["rev-parse", "--verify", "HEAD^{commit}"], max_bytes, deadline).decode().strip() != sha:
            return False
        _git(root, ["fsck", "--full", "--strict", "--no-reflogs"], max_bytes, deadline)
        tree = _git(root, ["ls-tree", "-r", "-z", "-l", sha], max_bytes, deadline)
        expected, directories, total = set(), set(), 0
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, raw_path = record.split(b"\t", 1)
            mode, kind, oid, size = metadata.split()
            rel = raw_path.decode("utf-8")
            parts = PurePosixPath(rel).parts
            if (not parts or PurePosixPath(rel).as_posix() != rel or PurePosixPath(rel).is_absolute() or "\\" in rel or ":" in rel
                    or any(p in {".", ".."} or p.lower() == ".git" for p in parts)):
                return False
            p = root.joinpath(*parts)
            if not p.parent.resolve().is_relative_to(root.resolve()):
                return False
            directories.update(parent.as_posix() for parent in PurePosixPath(rel).parents if str(parent) != ".")
            if mode == b"160000":
                # Gitlinks are empty placeholders; submodules are never initialized.
                if _linked(p) or not p.is_dir() or any(p.iterdir()):
                    return False
                directories.add(rel)
                continue
            if kind != b"blob" or mode not in {b"100644", b"100755", b"120000"}:
                return False
            n = int(size)
            total += n
            if total > max_bytes:
                return False
            if mode == b"120000":
                if not p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
                    return False
                content = os.fsencode(os.readlink(p))
            else:
                if _linked(p) or not p.is_file() or p.stat().st_size != n:
                    return False
                if os.name != "nt" and bool(p.stat().st_mode & 0o111) != (mode == b"100755"):
                    return False
                content = p.read_bytes()
            digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
            if len(content) != n or digest != oid.decode():
                return False
            expected.add(rel)
            if time.monotonic() >= deadline:
                return False
        actual_files, actual_dirs = set(), set()
        for directory, dirs, files in os.walk(root, followlinks=False):
            if Path(directory) == root:
                dirs[:] = [name for name in dirs if name != ".git"]
            for name in dirs[:]:
                p = Path(directory) / name
                rel = p.relative_to(root).as_posix()
                if _linked(p):
                    actual_files.add(rel)
                    dirs.remove(name)
                else:
                    actual_dirs.add(rel)
            actual_files.update((Path(directory) / name).relative_to(root).as_posix() for name in files)
        return actual_files == expected and actual_dirs == directories
    except (OSError, ValueError, RuntimeError, configparser.Error, GuardrailViolation):
        return False


def _remove_owned(path, base):
    # Only our UUID destinations/staging directories, never a resolved link target.
    if path.parent != base or _linked(path):
        raise _unavailable()
    if path.is_dir():
        def writable_retry(operation, target, exc):
            candidate = Path(target).absolute()
            if not candidate.is_relative_to(path.absolute()) or _linked(candidate):
                raise _unavailable()
            candidate.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
            operation(target)
        shutil.rmtree(path, onerror=writable_retry)
    elif path.exists():
        path.unlink()


def _restore(repo, dest, max_bytes, deadline):
    owner, name = parse_github_url(repo["url"])
    sha = repo["commit_sha"]
    base = dest.parent
    # Recover garbage from a previously killed process under the same OS lock.
    for orphan in base.glob(f".restore-{dest.name}-*"):
        _remove_owned(orphan, base)
    stage = Path(tempfile.mkdtemp(prefix=f".restore-{dest.name}-", dir=base))
    backup = base / f".restore-{dest.name}-{uuid4().hex}"
    published = False
    try:
        if shutil.disk_usage(base).free < MIN_FREE_BYTES + max_bytes * 4:
            log_failure("disk_limit", min_free_bytes=MIN_FREE_BYTES + max_bytes * 4)
            raise _unavailable()
        _git(stage, ["init", "--template="], max_bytes, deadline)
        _git(stage, ["fetch", "--depth=1", "--no-tags", "--no-recurse-submodules", "--",
                     f"https://github.com/{owner}/{name}.git", sha], max_bytes, deadline)
        # Fresh config, no templates/global attributes/filters/hooks, no submodules.
        _git(stage, ["checkout", "--detach", "--force", sha, "--"], max_bytes, deadline)
        if not _checkout_valid(stage, sha, max_bytes, deadline):
            raise _unavailable()
        current = store.get_repo(repo["id"])
        if not current or any(current.get(k) != repo.get(k) for k in ("url", "commit_sha", "status")):
            raise _unavailable()
        _public_repository(owner, name, deadline)
        if dest.exists():
            dest.rename(backup)
        try:
            stage.rename(dest)  # same filesystem; readers hold the same lock
            published = True
        except BaseException:
            if backup.exists():
                backup.rename(dest)
            raise
    finally:
        _remove_owned(stage, base)
        if published:
            _remove_owned(backup, base)


@contextmanager
def repository_snapshot(repo_id, settings=None):
    """Yield a verified snapshot under lock; preserve all existing database state."""
    with ExitStack() as stack:
        # Limit ContextVar lifetime to preparation: SSE can resume on a new worker context.
        with diagnostic_scope("restoration", repo_id):
            dest = stack.enter_context(_repository_snapshot(repo_id, settings))
        yield dest


@contextmanager
def _repository_snapshot(repo_id, settings=None):
    settings = settings or get_settings()
    with repository_lock(repo_id, settings) as dest:
        repo = store.get_repo(repo_id)
        if not repo:
            raise GuardrailViolation("not_found", "Repository not found.")
        sha = repo.get("commit_sha")
        if repo.get("status") != "ready" or not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise GuardrailViolation("repo_not_ready", "The repository has no ready indexed snapshot.")
        deadline = time.monotonic() + RESTORE_TIMEOUT
        try:
            # Also clean a process killed after publication but before backup cleanup.
            for orphan in dest.parent.glob(f".restore-{dest.name}-*"):
                _remove_owned(orphan, dest.parent)
            owner, name = parse_github_url(repo["url"])
            _public_repository(owner, name, deadline)
            max_bytes = settings.max_repo_mb * 1024 * 1024
            if not _checkout_valid(dest, repo["commit_sha"], max_bytes, deadline):
                _restore(repo, dest, max_bytes, deadline)
        except (OSError, ValueError, RuntimeError, configparser.Error, httpx.HTTPError, subprocess.SubprocessError) as exc:
            if isinstance(exc, OSError):
                log_failure("filesystem_error", errno=exc.errno)
            raise _unavailable() from None
        yield dest
