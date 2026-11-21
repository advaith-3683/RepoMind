"""Input guardrails: user-question validation and indirect prompt-injection
defence for untrusted repo content."""
import re

from .errors import GuardrailViolation

INJECTION_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |any )?(the )?(previous|prior|above|earlier|your) (instructions|prompts?|rules)",
    r"(reveal|show|print|repeat|output|leak) (me )?(your |the )?(system|hidden|initial) (prompt|instructions|message)",
    r"you are now (in )?(developer|dan|jailbreak|god) mode",
    r"act as (an? )?(unrestricted|unfiltered|jailbroken)",
    r"new instructions\s*:",
    r"(exfiltrate|send|post|upload|email) .{0,40}(api[_ -]?key|secret|token|password|credentials)",
    r"</?(system|assistant|untrusted_code)>",
]
_COMPILED = [re.compile(p, re.I) for p in INJECTION_PATTERNS]

NEUTRALIZED_LINE = "[line neutralized: possible prompt injection]"


def looks_like_injection(text: str) -> str | None:
    """Return the matching pattern, or None."""
    for pat in _COMPILED:
        if pat.search(text):
            return pat.pattern
    return None


def validate_query(question: str | None, max_chars: int) -> str:
    q = (question or "").strip()
    if not q:
        raise GuardrailViolation("empty_query", "Please enter a question.")
    if len(q) > max_chars:
        raise GuardrailViolation("query_too_long", f"Question is too long (max {max_chars} characters).")
    if "\x00" in q:
        raise GuardrailViolation("invalid_query", "Question contains invalid characters.")
    if looks_like_injection(q):
        raise GuardrailViolation(
            "prompt_injection",
            "That looks like an attempt to override my instructions, so I blocked it. "
            "Ask a question about the code instead.",
        )
    return q


def neutralize_untrusted(text: str) -> tuple[str, int]:
    """Replace lines of retrieved repo content that look like injected
    instructions. Returns (clean_text, number_of_lines_neutralized)."""
    flagged = 0
    out = []
    for line in text.split("\n"):
        if looks_like_injection(line):
            flagged += 1
            out.append(NEUTRALIZED_LINE)
        else:
            out.append(line)
    return "\n".join(out), flagged


def wrap_untrusted(text: str, path: str, start: int, end: int) -> str:
    """Delimit repo content so the model can tell data from instructions."""
    safe = text.replace("</untrusted_code", "<\\/untrusted_code")
    return f'<untrusted_code path="{path}" lines="{start}-{end}">\n{safe}\n</untrusted_code>'
