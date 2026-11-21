"""Per-query budgets (steps / tokens / wall clock) and a per-client rate limiter."""
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from .errors import GuardrailViolation


class BudgetExceeded(Exception):
    def __init__(self, reason: str):
        super().__init__(f"budget exceeded: {reason}")
        self.reason = reason  # "steps" | "tokens" | "timeout"


@dataclass
class Budget:
    max_steps: int
    max_tokens: int
    timeout_s: float
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def check(self) -> None:
        if self.steps >= self.max_steps:
            raise BudgetExceeded("steps")
        if self.tokens >= self.max_tokens:
            raise BudgetExceeded("tokens")
        if time.monotonic() - self.started > self.timeout_s:
            raise BudgetExceeded("timeout")

    def charge_step(self) -> None:
        """Call at the top of every agent iteration."""
        self.check()
        self.steps += 1

    def add_tokens(self, tokens_in: int, tokens_out: int) -> None:
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)


class RateLimiter:
    """In-memory sliding window. Fine for a single instance; swap for Redis if scaled out."""

    def __init__(self, limit: int, window_s: float = 60.0):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                raise GuardrailViolation("rate_limited", "Too many requests. Please slow down.")
            q.append(now)
