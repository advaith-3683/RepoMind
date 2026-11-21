"""Human-in-the-loop approval gate.

The agent can only *propose* a change (patch or issue). Nothing is applied
until a human decides. State machine:

    pending --approve--> approved
    pending --edit-----> edited      (approved, with the human's edited payload)
    pending --reject---> rejected

approved / edited / rejected are terminal. A second decision => 409."""
from enum import Enum

from ..guardrails.errors import GuardrailViolation
from .patching import validate_edits_shape

MAX_PAYLOAD_CHARS = 20_000
KINDS = ("patch", "issue")


class ActionStatus(str, Enum):
    pending = "pending"
    approved = "approved"
    edited = "edited"
    rejected = "rejected"


_DECISION_TO_STATUS = {
    "approve": ActionStatus.approved,
    "edit": ActionStatus.edited,
    "reject": ActionStatus.rejected,
}


def next_status(current: str, decision: str) -> ActionStatus:
    if decision not in _DECISION_TO_STATUS:
        raise GuardrailViolation("invalid_decision", "decision must be approve, edit or reject.")
    if current != ActionStatus.pending.value:
        raise GuardrailViolation("invalid_transition", f"This action was already {current}.")
    return _DECISION_TO_STATUS[decision]


def validate_payload(kind: str, payload: dict) -> dict:
    """Shape + size check for a proposal (from the agent) or an edit (from the human)."""
    if kind not in KINDS:
        raise GuardrailViolation("invalid_proposal", f"kind must be one of {KINDS}.")
    if not isinstance(payload, dict):
        raise GuardrailViolation("invalid_proposal", "payload must be an object.")
    title = str(payload.get("title", "")).strip()
    body = str(payload.get("body", "")).strip()
    if not title or len(title) > 120:
        raise GuardrailViolation("invalid_proposal", "title is required (max 120 chars).")
    if not body:
        raise GuardrailViolation("invalid_proposal", "body is required.")
    if len(title) + len(body) > MAX_PAYLOAD_CHARS:
        raise GuardrailViolation("invalid_proposal", "proposal is too large.")
    out = {"title": title, "body": body}
    if kind == "patch":
        path = str(payload.get("path", "")).strip()
        if not path:
            raise GuardrailViolation("invalid_proposal", "patch proposals need a target path.")
        out["path"] = path
        out["edits"] = validate_edits_shape(payload.get("edits"))
    return out
