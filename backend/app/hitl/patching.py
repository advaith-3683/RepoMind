"""Patches are search-and-replace edits, not full files or raw diffs:

    {"path": "app/auth.py", "edits": [{"old": "<exact text>", "new": "<replacement>"}]}

Why: the model only has to emit the changed lines (token-cheap on free tiers), `old` must match
exactly once (so the edit is unambiguous and checkable), and the server derives the diff shown to
the human AND the final file content from the same edits, so what is reviewed is what is committed."""
import difflib

from ..guardrails.errors import GuardrailViolation

MAX_EDITS = 10
MAX_EDIT_CHARS = 40_000          # total chars across all old+new texts


class PatchError(ValueError):
    pass


def validate_edits_shape(edits) -> list[dict]:
    def bad(msg: str):
        raise GuardrailViolation("invalid_proposal", msg)

    if not isinstance(edits, list) or not edits:
        bad("patch proposals need a non-empty 'edits' list of {old, new}.")
    if len(edits) > MAX_EDITS:
        bad(f"too many edits (max {MAX_EDITS}).")
    out, total = [], 0
    for e in edits:
        if not isinstance(e, dict) or not isinstance(e.get("old"), str) or not isinstance(e.get("new"), str):
            bad("each edit must be an object with string fields 'old' and 'new'.")
        old, new = e["old"], e["new"]
        if not old.strip():
            bad("each edit needs a non-empty 'old' text.")
        if old == new:
            bad("an edit does not change anything.")
        if "[REDACTED" in new:
            bad("an edit contains a redaction placeholder; never write placeholders into code.")
        total += len(old) + len(new)
        out.append({"old": old, "new": new})
    if total > MAX_EDIT_CHARS:
        bad("the proposed edits are too large.")
    return out


def apply_edits(original: str, edits: list[dict]) -> str:
    """Apply edits in order. Each `old` must occur exactly once in the text as it stands."""
    text = original
    for i, e in enumerate(edits, 1):
        n = text.count(e["old"])
        if n == 0:
            raise PatchError(
                f"edit {i}: the 'old' text was not found in the file. Copy it exactly from read_file "
                "output, WITHOUT the 'N: ' line-number prefixes."
            )
        if n > 1:
            raise PatchError(f"edit {i}: the 'old' text appears {n} times; include more surrounding lines to make it unique.")
        text = text.replace(e["old"], e["new"], 1)
    return text


def make_diff(path: str, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}",
    ))
