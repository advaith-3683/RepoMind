import pytest

from app import service
from app.agent.llm import LLMError, RateLimitError


def _run(monkeypatch, exc):
    monkeypatch.setattr(service.store, "create_query", lambda *a, **k: "q1")
    monkeypatch.setattr(service.store, "finish_query", lambda *a, **k: None)
    monkeypatch.setattr(service.store, "save_trace", lambda *a, **k: None)
    monkeypatch.setattr(service, "get_embedder", lambda: object())

    def boom(_s):
        raise exc

    monkeypatch.setattr(service, "build_llm", boom)
    return list(service.stream_answer({"id": "11111111-1111-1111-1111-111111111111"}, "q", mode="fast"))


@pytest.mark.parametrize("exc,code", [
    (RateLimitError("HTTP 429 from m (retry-after 30s)", 429), "rate_limited"),
    (LLMError("HTTP 413 from m: too large", 413), "rate_limited"),
    (LLMError("HTTP 503 from m", 503), "llm_unavailable"),
])
def test_llm_errors_get_friendly_codes(monkeypatch, exc, code):
    events = _run(monkeypatch, exc)
    err = next(e for e in events if e["type"] == "error")
    assert err["code"] == code and "detail" in err
    assert events[-1]["type"] == "done" and events[-1]["status"] == "error"


def test_unknown_exception_still_internal(monkeypatch):
    err = next(e for e in _run(monkeypatch, ValueError("x")) if e["type"] == "error")
    assert err["code"] == "internal"
