import os
import re
import shutil
import subprocess
from pathlib import Path

from ..guardrails.errors import GuardrailViolation

GITHUB_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def parse_github_url(url: str) -> tuple[str, str]:
    """Only plain https://github.com/<owner>/<repo> URLs are accepted
    (no other hosts, schemes, ports or credentials => no SSRF / arbitrary git)."""
    m = GITHUB_RE.match((url or "").strip())
    if not m:
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
    owner, repo = parse_github_url(url)
    canonical = f"https://github.com/{owner}/{repo}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", "--single-branch", "--", canonical, str(dest)],
            check=True, timeout=timeout, capture_output=True, env=env,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise GuardrailViolation("clone_timeout", "Cloning took too long.")
    except subprocess.CalledProcessError:
        shutil.rmtree(dest, ignore_errors=True)
        raise GuardrailViolation("clone_failed", "Could not clone that repository (is it public?).")

    if _dir_size_mb(dest) > max_repo_mb:
        shutil.rmtree(dest, ignore_errors=True)
        raise GuardrailViolation("repo_too_large", f"Repository is larger than {max_repo_mb} MB.")

    sha = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return sha
