"""LLM interface and providers. The agent talks to the `LLM` protocol, so tests use
a scripted fake and production composes:

    CachingLLM( FallbackLLM([ primary, fallback ]) )

  * OpenAICompatLLM  - any OpenAI-compatible /chat/completions API (Groq, OpenRouter, Ollama, ...)
  * GeminiLLM        - google-genai (optional; Gemini is used for embeddings by default)
  * FallbackLLM      - tries the next provider on rate limits / 5xx / network errors
  * CachingLLM       - record/replay on disk so reruns (evals!) cost zero API calls

NOTE: verify the google-genai call shape / model names against current docs."""
import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from ..cache import DiskCache
from ..config import Settings
from ..ingestion.embed import with_backoff


@dataclass
class LLMResult:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached: bool = False


class LLM(Protocol):
    def complete(self, system: str, messages: list[dict]) -> LLMResult: ...


class LLMError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RateLimitError(LLMError):
    pass


class CacheMiss(Exception):
    """Raised in replay mode when a call was never recorded."""


def is_rate_limit(e: Exception) -> bool:
    return isinstance(e, RateLimitError) or any(t in str(e) for t in ("429", "RESOURCE_EXHAUSTED"))


def is_transient(e: Exception) -> bool:
    if is_rate_limit(e):
        return True
    if isinstance(e, LLMError):
        return (e.status or 0) >= 500 or e.status is None
    return isinstance(e, httpx.TransportError)


def _retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.headers.get("retry-after", ""))
    except ValueError:
        return None


def _rescue_tool_call(r: httpx.Response) -> str | None:
    """Groq rejects gpt-oss "native" tool calls (tool_use_failed) but returns the attempted call in
    failed_generation. Translate {"name": "x.search_code", "arguments": {...}} into our JSON action."""
    try:
        gen = r.json()["error"]["failed_generation"]
        call = json.loads(gen) if isinstance(gen, str) else gen
        name, args = str(call["name"]).rsplit(".", 1)[-1], call.get("arguments") or {}
        if isinstance(args, str):
            args = json.loads(args)
        if not name or not isinstance(args, dict):
            return None
        return json.dumps({"thought": "", "action": name, "args": args})
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


class OpenAICompatLLM:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        label: str = "openai_compat",
        json_mode: bool = True,
        reasoning_effort: str = "",
        max_tokens: int = 1500,
        timeout: float = 60.0,
        max_retries: int = 2,
        max_wait_s: float = 20.0,
        client: httpx.Client | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.json_mode = json_mode
        self.reasoning_effort = reasoning_effort if "gpt-oss" in model else ""   # reasoning tokens share max_tokens
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.max_wait_s = max_wait_s     # longer Retry-After than this => treat as quota exhaustion, fail fast
        self.model_id = f"{label}:{model}"
        self._client = client or httpx.Client(timeout=timeout)

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0.2,
            "max_tokens": self.max_tokens,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"

        for attempt in range(self.max_retries + 1):
            last_attempt = attempt == self.max_retries
            try:
                r = self._client.post(url, json=body, headers=headers)
            except httpx.TransportError as e:
                if last_attempt:
                    raise LLMError(f"network error calling {self.model_id}: {e}") from e
                time.sleep(min(2 ** attempt, 5))
                continue

            if r.status_code == 200:
                return self._parse(r)
            if r.status_code == 429:
                wait = _retry_after(r)
                wait = float(2 ** attempt) if wait is None else wait
                if wait > self.max_wait_s or last_attempt:
                    raise RateLimitError(f"HTTP 429 from {self.model_id} (retry-after {wait:.0f}s)", 429)
                time.sleep(wait)
                continue
            if r.status_code >= 500:
                if last_attempt:
                    raise LLMError(f"HTTP {r.status_code} from {self.model_id}: {r.text[:200]}", r.status_code)
                time.sleep(min(2 ** attempt, 5))
                continue
            if r.status_code == 400 and "tool_use_failed" in r.text:
                rescued = _rescue_tool_call(r)   # model emitted a native tool call; our loop uses JSON actions
                if rescued:
                    return LLMResult(rescued, 0, 0)
            if r.status_code == 400 and "response_format" in body and "json_validate_failed" in r.text:
                body.pop("response_format")   # strict JSON mode rejected the output: retry once, loop parser is tolerant
                continue
            raise LLMError(f"HTTP {r.status_code} from {self.model_id}: {r.text[:300]}", r.status_code)
        raise LLMError("unreachable")

    def _parse(self, r: httpx.Response) -> LLMResult:
        try:
            data = r.json()
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
            usage = data.get("usage") or {}
            if not text.strip() and isinstance(choice["message"].get("reasoning"), str):
                text = choice["message"]["reasoning"]   # gpt-oss sometimes puts its whole reply in `reasoning`
            if not text.strip():
                logging.getLogger(__name__).warning(
                    "empty_llm_content model=%s finish=%s keys=%s completion_tokens=%s", self.model_id,
                    choice.get("finish_reason"), sorted(choice["message"].keys()), usage.get("completion_tokens"))
        except (ValueError, KeyError, IndexError, TypeError) as e:
            raise LLMError(f"unexpected response shape from {self.model_id}") from e
        return LLMResult(text, int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0))


class GeminiLLM:
    def __init__(self, settings: Settings):
        from google import genai  # lazy

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_chat_model
        self.model_id = f"gemini:{self._model}"

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        from google.genai import types  # lazy

        contents = [
            types.Content(
                role="user" if m["role"] == "user" else "model",
                parts=[types.Part(text=m["content"])],
            )
            for m in messages
        ]

        def call():
            return self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.2,
                ),
            )

        resp = with_backoff(call)
        usage = getattr(resp, "usage_metadata", None)
        return LLMResult(
            text=resp.text or "",
            tokens_in=getattr(usage, "prompt_token_count", 0) or 0,
            tokens_out=getattr(usage, "candidates_token_count", 0) or 0,
        )


class FallbackLLM:
    """Try providers in order; move on only for rate-limit / 5xx / network errors
    (a bad API key or a malformed request should surface, not be masked)."""

    def __init__(self, providers: list):
        if not providers:
            raise ValueError("FallbackLLM needs at least one provider")
        self.providers = providers
        self.model_id = "|".join(getattr(p, "model_id", "?") for p in providers)
        self.models_used: list[str] = []   # providers that actually returned an answer (eval rows record this)

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        last: Exception | None = None
        for p in self.providers:
            try:
                res = p.complete(system, messages)
                mid = getattr(p, "model_id", "?")
                if mid not in self.models_used:
                    self.models_used.append(mid)
                return res
            except Exception as e:  # noqa: BLE001
                if not is_transient(e):
                    raise
                last = e
        assert last is not None
        raise last


def models_used(llm) -> list[str]:
    """Which model(s) produced this run's answers. A fallback chain reports only providers that answered,
    so a silent switch to the fallback model is visible in eval results."""
    inner = getattr(llm, "inner", llm)
    used = getattr(inner, "models_used", None)
    if used is not None:
        return list(used)
    mid = getattr(inner, "model_id", None)
    return [mid] if mid else []


def _is_valid_action(text: str) -> bool:
    from .loop import parse_action   # lazy: loop imports this module

    try:
        parse_action(text)
        return True
    except ValueError:
        return False


class CachingLLM:
    """mode: 'readwrite' (default) or 'replay' (never call the API; miss => CacheMiss)."""

    def __init__(self, inner, cache: DiskCache, mode: str = "readwrite"):
        self.inner = inner
        self.cache = cache
        self.mode = mode
        self.model_id = getattr(inner, "model_id", "llm")
        self.hits = 0
        self.misses = 0

    def complete(self, system: str, messages: list[dict]) -> LLMResult:
        key = DiskCache.key({"m": self.model_id, "s": system, "msgs": messages})
        hit = self.cache.get("llm", key)
        if hit and hit.get("text"):
            self.hits += 1
            return LLMResult(hit["text"], hit.get("tokens_in", 0), hit.get("tokens_out", 0), cached=True)
        if self.mode == "replay":
            raise CacheMiss("LLM call not found in cache (replay mode).")
        self.misses += 1
        res = self.inner.complete(system, messages)
        if res.text.strip() and _is_valid_action(res.text):   # never cache malformed output: it would replay the failure
            self.cache.set("llm", key, {"text": res.text, "tokens_in": res.tokens_in, "tokens_out": res.tokens_out})
        return res


def build_llm(s: Settings) -> LLM:
    """Compose the provider chain from settings."""
    chain: list = []
    if s.llm_provider == "gemini":
        chain.append(GeminiLLM(s))
    else:
        chain.append(OpenAICompatLLM(
            s.llm_base_url, s.llm_api_key, s.llm_model, label="primary",
            json_mode=s.llm_json_mode, reasoning_effort=s.llm_reasoning_effort, max_tokens=s.llm_max_output_tokens,
        ))
    if s.llm_fallback_api_key and s.llm_fallback_model:
        chain.append(OpenAICompatLLM(
            s.llm_fallback_base_url, s.llm_fallback_api_key, s.llm_fallback_model, label="fallback",
            json_mode=s.llm_json_mode, reasoning_effort=s.llm_reasoning_effort, max_tokens=s.llm_max_output_tokens,
        ))
    llm: LLM = chain[0] if len(chain) == 1 else FallbackLLM(chain)
    if s.llm_cache in ("readwrite", "replay"):
        llm = CachingLLM(llm, DiskCache(s.cache_dir), s.llm_cache)
    return llm
