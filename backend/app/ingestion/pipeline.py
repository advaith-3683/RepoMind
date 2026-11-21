import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .. import store
from ..config import Settings, get_settings
from ..guardrails.errors import GuardrailViolation
from ..guardrails.sandbox import is_blocked_name
from ..guardrails.secrets import redact_secrets
from .chunker import Chunk, chunk_file, detect_language
from .clone import clone_repo
from .embed import Embedder, GeminiEmbedder
from ..repositories import _remove_owned, repository_lock
from ..clone_diagnostics import diagnostic_scope, log_failure

SKIP_DIRS = {
    ".git", "node_modules", "dist", "build", "venv", ".venv", "__pycache__", ".next", "vendor",
    "target", ".idea", ".vscode", "coverage", ".pytest_cache", ".mypy_cache",
}
SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "go.sum"}
BATCH = 50


def iter_repo_files(root: Path, max_file_kb: int):
    """Yield (posix_relpath, text) for indexable text files."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name in SKIP_FILES or is_blocked_name(name) or detect_language(name) is None:
                continue
            p = Path(dirpath) / name
            try:
                if p.is_symlink() or p.stat().st_size > max_file_kb * 1024:
                    continue
                text = p.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            yield p.relative_to(root).as_posix(), text


def build_chunks(root: Path, settings: Settings) -> tuple[list[Chunk], int]:
    chunks, files = [], 0
    for rel, text in iter_repo_files(root, settings.max_file_kb):
        files += 1
        for c in chunk_file(rel, text):
            c.content, _ = redact_secrets(c.content)   # never store secrets
            chunks.append(c)
            if len(chunks) > settings.max_chunks_per_repo:
                raise GuardrailViolation("repo_too_large", "Repository has too many code chunks to index.")
    return chunks, files


def ingest_repo(repo_id, url: str, embedder: Embedder | None = None) -> None:
    """Serialize ingestion with snapshot readers/restorers; never reindex a ready row."""
    with diagnostic_scope("ingestion", repo_id):
        _locked_ingestion(repo_id, url, embedder)


def _locked_ingestion(repo_id, url: str, embedder: Embedder | None = None) -> None:
    try:
        with repository_lock(repo_id):
            current = store.get_repo(repo_id)
            if not current or current["status"] != "pending":
                return
            _ingest_repo(repo_id, url, embedder)
    except GuardrailViolation:
        # We did not acquire ownership of ingestion; leave persistent state alone.
        return


def _ingest_repo(repo_id, url: str, embedder: Embedder | None = None) -> None:
    """Background job: clone -> chunk -> embed -> store. Never raises; records status instead."""
    s = get_settings()
    dest = Path(s.repos_dir) / str(repo_id)
    try:
        store.update_repo(repo_id, status="cloning")
        if dest.exists():
            _remove_owned(dest, dest.parent)
        sha = clone_repo(url, dest, s.max_repo_mb)
        store.update_repo(repo_id, status="indexing", commit_sha=sha)

        chunks, files = build_chunks(dest, s)
        if not chunks:
            raise GuardrailViolation("nothing_to_index", "No indexable source files were found.")

        embedder = embedder or GeminiEmbedder(s)
        store.delete_chunks(repo_id)
        size = max(1, s.embed_batch_size)
        for i in range(0, len(chunks), size):
            if i and s.embed_batch_delay_s > 0:
                time.sleep(s.embed_batch_delay_s)
            batch = chunks[i:i + size]
            store.insert_chunks(repo_id, batch, embedder.embed_documents([c.embed_text() for c in batch]))

        store.update_repo(
            repo_id, status="ready", files_indexed=files, chunks_indexed=len(chunks),
            indexed_at=datetime.now(timezone.utc), error=None,
        )
    except GuardrailViolation as e:
        store.update_repo(repo_id, status="failed", error=e.message)
    except Exception as e:  # noqa: BLE001
        log_failure("filesystem_error" if isinstance(e, OSError) else "unexpected_error",
                    errno=e.errno if isinstance(e, OSError) else None)
        _log_error_kind(e)
        quota = type(e).__name__ == "ClientError" and getattr(e, "code", None) == 429
        store.update_repo(repo_id, status="failed", error=(
            "The embedding service is rate-limited right now. Wait a few minutes and retry." if quota
            else "Indexing failed. Please try again."))


def _log_error_kind(exc: Exception) -> None:
    """Log ONLY the exception class name plus a numeric HTTP code / 5-char SQLSTATE if present.
    Never the message or traceback (those can contain URLs, keys or repository text)."""
    code = getattr(exc, "code", None)
    code = code if type(code) is int and 0 <= code < 1000 else None
    state = getattr(exc, "sqlstate", None)
    state = state if isinstance(state, str) and len(state) == 5 and state.isalnum() else None
    logging.getLogger(__name__).warning("ingestion_error_kind type=%s code=%s sqlstate=%s",
                                        type(exc).__name__[:60], code, state)
