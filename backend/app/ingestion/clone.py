import os
import re
import shutil
import time
from pathlib import Path

from ..guardrails.errors import GuardrailViolation
from ..clone_diagnostics import diagnostic_scope, log_failure, log_unreported

GITHUB_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def parse_github_url(url: str) -> tuple[str, str]:
    """Only plain https://github.com/<owner>/<repo> URLs are accepted
    (no other hosts, schemes, ports or credentials => no SSRF / arbitrary git)."""
    m = GITHUB_RE.match((url or "").strip())
    if not m or any(part in {".", ".."} for part in m.groups()):
        raise GuardrailViolation(
            "invalid_repo_url", "Only public https://github.com/<owner>/<repo> URLs are supported."
        )
    return m.group(1), m.group(2)


def _dir_size_mb(root: Path) -> float:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for f in filenames:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def clone_repo(url: str, dest: Path, max_repo_mb: int, timeout: int = 120) -> str:
    """Shallow-clone a public GitHub repo. Returns the HEAD commit sha."""
    with diagnostic_scope("clone", dest.name) as trace:
        before = trace.failures
        try:
            return _clone_repo(url, dest, max_repo_mb, timeout, trace)
        except Exception as exc:
            log_unreported(trace, before, "filesystem_error" if isinstance(exc, OSError) else "unexpected_error",
                           errno=exc.errno if isinstance(exc, OSError) else None)
            raise


def _clone_repo(url: str, dest: Path, max_repo_mb: int, timeout: int, trace) -> str:
    before = trace.failures
    owner, repo = parse_github_url(url)
    # Shared transport hardening, without invoking restoration or indexing.
    from ..repositories import _git, _public_repository
    canonical = f"https://github.com/{owner}/{repo}.git"
    deadline = time.monotonic() + timeout
    max_bytes = max_repo_mb * 1024 * 1024
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir(exist_ok=False)
    try:
        _public_repository(owner, repo, deadline)
        _git(dest, ["clone", "--template=", "--depth", "1", "--single-branch", "--", canonical, "."],
             max_bytes, deadline)
    except Exception as exc:
        log_unreported(trace, before, "filesystem_error" if isinstance(exc, OSError) else "unexpected_error",
                       errno=exc.errno if isinstance(exc, OSError) else None)
        shutil.rmtree(dest, ignore_errors=True)
        raise GuardrailViolation("clone_failed", "Could not clone that repository (is it public?).") from None

    if _dir_size_mb(dest) > max_repo_mb:
        log_failure("disk_limit", max_bytes=max_bytes)
        shutil.rmtree(dest, ignore_errors=True)
        raise GuardrailViolation("repo_too_large", f"Repository is larger than {max_repo_mb} MB.")

    sha = _git(dest, ["rev-parse", "--verify", "HEAD^{commit}"], max_bytes, deadline).decode().strip()
    return sha
