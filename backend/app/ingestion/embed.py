"""Gemini embeddings + shared retry helper. The google-genai import is lazy so
the rest of the app (and the tests) work without the SDK configured.

NOTE: verify model names / SDK call shapes against the current Gemini docs."""
import math
import random
import time
from collections import OrderedDict
from typing import Callable, Protocol, TypeVar

from ..cache import DiskCache
from ..config import Settings

T = TypeVar("T")


def with_backoff(fn: Callable[[], T], retries: int = 5, base: float = 1.0) -> T:
    """Retry on rate-limit / transient errors (429, 503) with exponential backoff + jitter."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            transient = any(t in msg for t in ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "timed out"))
            if not transient or attempt == retries:
                raise
            time.sleep(base * (2 ** attempt) + random.random() * 0.5)
    raise RuntimeError("unreachable")


class Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


def _normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class GeminiEmbedder:
    def __init__(self, settings: Settings, cache_size: int = 256):
        from google import genai  # lazy

        self._client = genai.Client(api_key=settings.gemini_api_key)
        self._model = settings.gemini_embed_model
        self._dim = settings.embed_dim
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = cache_size
        # persistent query-embedding cache: re-running evals costs no embedding calls
        self._disk = DiskCache(settings.cache_dir) if settings.llm_cache != "off" else None
        self._disk_key = lambda text: DiskCache.key({"m": settings.gemini_embed_model, "d": settings.embed_dim, "t": text})

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        from google.genai import types  # lazy

        def call():
            return self._client.models.embed_content(
                model=self._model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=self._dim),
            )

        resp = with_backoff(call)
        # truncated (<3072) Gemini embeddings are not unit-length => normalise for cosine search
        return [_normalize(list(e.values)) for e in resp.embeddings]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        vec = None
        if self._disk is not None:
            hit = self._disk.get("embed", self._disk_key(text))
            if hit and isinstance(hit.get("v"), list):
                vec = hit["v"]
        if vec is None:
            vec = self._embed([text], "RETRIEVAL_QUERY")[0]
            if self._disk is not None:
                self._disk.set("embed", self._disk_key(text), {"v": vec})
        self._cache[text] = vec
        if len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return vec
