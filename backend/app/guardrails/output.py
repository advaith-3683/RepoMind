"""Output guardrails: every citation must point at code the agent actually
retrieved, and answers should be grounded in that evidence."""
import re
from dataclasses import dataclass, field

# [path/to/file.py:10-20] or [file.py:10]
CITATION_RE = re.compile(r"\[([\w./\-@+()][\w./\-@+() ]*):(\d+)(?:-(\d+))?\]")   # spaces allowed: repos can have folders like "my backend/"
_BACKTICK_RE = re.compile(r"`([A-Za-z_][\w.]{2,})`")


@dataclass(frozen=True)
class Evidence:
    path: str
    start: int
    end: int


@dataclass(frozen=True)
class Citation:
    raw: str
    path: str
    start: int
    end: int


@dataclass
class Verification:
    valid: list[Citation] = field(default_factory=list)
    invalid: list[Citation] = field(default_factory=list)


def extract_citations(answer: str) -> list[Citation]:
    out = []
    for m in CITATION_RE.finditer(answer):
        start = int(m.group(2))
        end = int(m.group(3)) if m.group(3) else start
        if end < start:
            start, end = end, start
        out.append(Citation(m.group(0), m.group(1), start, end))
    return out


def verify_citations(answer: str, evidence: list[Evidence]) -> Verification:
    """A citation is valid if its path was retrieved and its line range
    overlaps a retrieved range."""
    v = Verification()
    for c in extract_citations(answer):
        ok = any(e.path == c.path and c.start <= e.end and c.end >= e.start for e in evidence)
        (v.valid if ok else v.invalid).append(c)
    return v


def strip_invalid_citations(answer: str, invalid: list[Citation]) -> str:
    for c in invalid:
        answer = answer.replace(c.raw, "")
    return re.sub(r"[ \t]{2,}", " ", answer).strip()


def groundedness_score(answer: str, evidence_text: str) -> float:
    """Cheap heuristic: fraction of `backticked` identifiers in the answer that
    literally occur in retrieved evidence. 1.0 if the answer names none.
    (An LLM-as-judge faithfulness score is the Phase-2 upgrade; see report.)"""
    idents = {m.group(1) for m in _BACKTICK_RE.finditer(answer)}
    if not idents:
        return 1.0
    found = sum(1 for i in idents if i in evidence_text or i.split(".")[-1] in evidence_text)
    return found / len(idents)
