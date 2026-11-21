"""Safe, redacted, line-numbered file reads shared by the agent's read_file tool
and the frontend's citation-preview endpoint."""
from pathlib import Path

from .guardrails.sandbox import SandboxError, rel_posix, resolve_in_repo
from .guardrails.secrets import redact_secrets


def read_slice(
    repo_root: Path,
    rel: str,
    start: int = 1,
    end: int | None = None,
    max_lines: int = 200,
    max_bytes: int = 400_000,
) -> dict:
    p = resolve_in_repo(repo_root, rel)
    if not p.is_file():
        raise SandboxError("not_a_file", f"'{rel}' is not a file.")
    if p.stat().st_size > max_bytes:
        raise SandboxError("file_too_large", "File is too large to read.")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise SandboxError("binary_file", "File is not UTF-8 text.")

    lines = text.splitlines()
    total = len(lines)
    try:
        start = max(1, int(start))
        end = total if end is None else min(int(end), total)
    except (TypeError, ValueError):
        raise SandboxError("bad_range", "start/end must be integers.")
    if total == 0:
        return {"path": rel_posix(repo_root, p), "start": 1, "end": 0, "total_lines": 0, "lines": []}
    if end < start:
        raise SandboxError("bad_range", "end must be >= start.")
    end = min(end, start + max_lines - 1)

    redacted, _ = redact_secrets("\n".join(lines[start - 1:end]))
    return {
        "path": rel_posix(repo_root, p),
        "start": start,
        "end": end,
        "total_lines": total,
        "lines": redacted.split("\n"),
    }
