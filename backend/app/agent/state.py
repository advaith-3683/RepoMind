from dataclasses import dataclass, field

from ..guardrails.output import Evidence


@dataclass
class EvidenceStore:
    """Everything the agent has actually seen. Citations are checked against this."""

    items: list[Evidence] = field(default_factory=list)
    _texts: list[str] = field(default_factory=list)

    def add(self, path: str, start: int, end: int, text: str = "") -> None:
        self.items.append(Evidence(path, start, end))
        self._texts.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self._texts)

    @property
    def paths(self) -> set[str]:
        return {e.path for e in self.items}

    def summary(self) -> str:
        by_path: dict[str, list[str]] = {}
        for e in self.items:
            by_path.setdefault(e.path, []).append(f"{e.start}-{e.end}")
        return "; ".join(f"{p}: {', '.join(r)}" for p, r in by_path.items()) or "(nothing retrieved yet)"
