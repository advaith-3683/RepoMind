"""Read-only, sandboxed tools. Every observation is secret-redacted,
injection-neutralized, delimiter-wrapped, and recorded as evidence."""
import inspect
from pathlib import Path
from typing import Callable

from ..files import read_slice
from ..guardrails.errors import GuardrailViolation
from ..guardrails.input import neutralize_untrusted, wrap_untrusted
from ..guardrails.sandbox import is_blocked_name, resolve_in_repo, rel_posix
from ..guardrails.secrets import redact_secrets
from ..retrieval.hybrid import Hit
from .state import EvidenceStore

TOOL_NAMES = ("search_code", "read_file", "find_symbol", "list_dir")
SEARCH_PREVIEW_LINES = 30   # kept small: free LLM tiers are limited by tokens/minute
MAX_DIR_ENTRIES = 100


class ToolError(Exception):
    pass


class RepoTools:
    def __init__(
        self,
        repo_root: Path,
        search_fn: Callable[[str, int], list[Hit]],
        symbol_fn: Callable[[str], list[Hit]],
        evidence: EvidenceStore | None = None,
        max_read_lines: int = 200,
    ):
        self.repo_root = Path(repo_root)
        self._search = search_fn
        self._symbols = symbol_fn
        self.evidence = evidence or EvidenceStore()
        self.max_read_lines = max_read_lines
        self.guardrail_events: list[dict] = []

    # ---- dispatch ----
    def call(self, name: str, args: dict) -> str:
        if name not in TOOL_NAMES:
            raise ToolError(f"unknown tool '{name}'. Available: {', '.join(TOOL_NAMES)}")
        if not isinstance(args, dict):
            raise ToolError("args must be an object")
        fn = getattr(self, name)
        try:
            inspect.signature(fn).bind(**args)
        except TypeError as e:
            raise ToolError(f"bad arguments for {name}: {e}")
        return fn(**args)

    # ---- helpers ----
    def _render(self, path: str, start: int, lines: list[str]) -> str:
        numbered = "\n".join(f"{start + i}: {line}" for i, line in enumerate(lines))
        cleaned, _ = redact_secrets(numbered)
        cleaned, flagged = neutralize_untrusted(cleaned)
        if flagged:
            self.guardrail_events.append({
                "type": "guardrail", "code": "indirect_injection",
                "message": f"{flagged} suspicious line(s) neutralized in {path}",
            })
        return wrap_untrusted(cleaned, path, start, start + max(len(lines) - 1, 0))

    # ---- tools ----
    def search_code(self, query: str, k: int = 5) -> str:
        if not isinstance(query, str) or not query.strip():
            raise ToolError("query must be a non-empty string")
        k = max(1, min(int(k), 8))
        hits = self._search(query, k)
        if not hits:
            return "No results."
        parts = []
        for h in hits:
            lines = h.content.splitlines()[:SEARCH_PREVIEW_LINES]
            end = h.start_line + len(lines) - 1
            self.evidence.add(h.path, h.start_line, end, "\n".join(lines))
            label = f"{h.kind} {h.symbol}" if h.symbol else (h.kind or "chunk")
            parts.append(f"## {h.path} ({label}) lines {h.start_line}-{end}\n"
                         + self._render(h.path, h.start_line, lines))
        return "\n\n".join(parts)

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        d = read_slice(self.repo_root, path, start, end, max_lines=self.max_read_lines)
        self.evidence.add(d["path"], d["start"], d["end"], "\n".join(d["lines"]))
        body = self._render(d["path"], d["start"], d["lines"])
        return f"{body}\n(showing lines {d['start']}-{d['end']} of {d['total_lines']})"

    def read_text(self, path: str, max_bytes: int = 400_000) -> str:
        """Raw (unredacted, unnumbered) file text, for applying proposed patches. Not an agent tool."""
        p = resolve_in_repo(self.repo_root, path)
        if not p.is_file():
            raise ToolError(f"'{path}' is not a file")
        if p.stat().st_size > max_bytes:
            raise ToolError(f"'{path}' is too large to patch")
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            raise ToolError(f"'{path}' is not UTF-8 text")

    def find_symbol(self, name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ToolError("name must be a non-empty string")
        hits = self._symbols(name)
        if not hits:
            return f"No symbols matching '{name}'."
        return "\n".join(
            f"{h.path}:{h.start_line}-{h.end_line}  {h.kind} {h.symbol}" for h in hits
        ) + "\n(use read_file to see the code before citing it)"

    def list_dir(self, path: str = ".") -> str:
        p = resolve_in_repo(self.repo_root, path)
        if not p.is_dir():
            raise ToolError(f"'{path}' is not a directory")
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
            if child.name == ".git" or is_blocked_name(child.name):
                continue
            entries.append(child.name + ("/" if child.is_dir() else ""))
        shown = entries[:MAX_DIR_ENTRIES]
        more = f"\n... and {len(entries) - len(shown)} more" if len(entries) > len(shown) else ""
        return f"{rel_posix(self.repo_root, p)}/\n" + "\n".join(shown) + more
