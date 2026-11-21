"""Secret redaction. Applied at ingestion (before storing chunks), on every tool
observation, and on the file-preview endpoint. Redaction never changes the
number of lines, so line-number citations stay valid."""
import re

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)

_TOKEN_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),                       # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),                 # Google API key
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),             # GitHub tokens
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),          # Slack tokens
    re.compile(r"sk-[A-Za-z0-9]{32,}"),                    # generic sk- style keys
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
]

# key = "value" / key: 'value'  where key looks sensitive
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|passwd|password|private[_-]?key|client[_-]?secret)\b"
    r"(\s*[:=]\s*)(['\"])([^'\"\s]{8,})\3"
)

REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> tuple[str, int]:
    """Return (redacted_text, number_of_redactions)."""
    count = 0

    def _key(m: re.Match) -> str:
        nonlocal count
        count += 1
        # keep line count identical
        return "\n".join(["[REDACTED PRIVATE KEY]"] + [""] * m.group(0).count("\n"))

    text = _PRIVATE_KEY.sub(_key, text)

    for pat in _TOKEN_PATTERNS:
        text, n = pat.subn(REDACTED, text)
        count += n

    def _assign(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"{m.group(1)}{m.group(2)}{m.group(3)}{REDACTED}{m.group(3)}"

    text = _ASSIGNMENT.sub(_assign, text)
    return text, count
