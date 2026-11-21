"""Filesystem sandbox: the agent may only read inside the cloned repo, never
.git, and never obviously sensitive files."""
import fnmatch
from pathlib import Path

from .errors import GuardrailViolation


class SandboxError(GuardrailViolation):
    def __init__(self, code: str, message: str):
        super().__init__(code, message)


_BLOCKED_PATTERNS = [
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*",
    "credentials.json", "secrets.*", "*.keystore", ".npmrc", ".pypirc", ".netrc",
]
_SAFE_SUFFIXES = (".example", ".sample", ".template", ".dist")


def is_blocked_name(name: str) -> bool:
    n = name.lower()
    if n.endswith(_SAFE_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(n, pat) for pat in _BLOCKED_PATTERNS)


def resolve_in_repo(repo_root: Path, rel: str) -> Path:
    """Resolve `rel` inside `repo_root` or raise SandboxError.

    Handles ../ traversal, absolute paths and symlink escapes (resolve()
    follows symlinks, so a link pointing outside the repo is rejected)."""
    if rel is None or not isinstance(rel, str) or "\x00" in rel:
        raise SandboxError("path_blocked", "Invalid path.")
    root = repo_root.resolve()
    candidate = (root / rel).resolve()
    if candidate != root and root not in candidate.parents:
        raise SandboxError("path_blocked", "Path escapes the repository.")
    parts = candidate.relative_to(root).parts
    if ".git" in parts:
        raise SandboxError("path_blocked", "Access to .git is not allowed.")
    if any(is_blocked_name(p) for p in parts):
        raise SandboxError("path_blocked", "Access to sensitive files is not allowed.")
    return candidate


def rel_posix(repo_root: Path, p: Path) -> str:
    rel = p.resolve().relative_to(repo_root.resolve())
    return rel.as_posix() if str(rel) != "." else "."
