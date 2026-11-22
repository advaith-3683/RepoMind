import json

import httpx
import pytest

from app.agent import llm as llm_mod
from app.agent.llm import (
    CacheMiss, CachingLLM, FallbackLLM, GeminiLLM, LLMError, LLMResult, OpenAICompatLLM, RateLimitError,
    build_llm, is_rate_limit, is_transient,
)
from app.agent.loop import parse_action
from app.cache import DiskCache
from app.config import Settings


def make(handler, **kw):
    kw.setdefault("max_retries", 2)
    return OpenAICompatLLM("https://api.example.com/v1/", "KEY", "m1",
                           client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def ok(content='{"action":"final_answer","args":{}}'):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}],
                                     "usage": {"prompt_tokens": 11, "completion_tokens": 7}})


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(llm_mod.time, "sleep", lambda s: calls.append(s))
    return calls


# ---------- OpenAI-compatible client ----------
def test_success_request_shape_and_usage():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        return ok()

    res = make(handler).complete("SYS", [{"role": "user", "content": "hi"}])
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer KEY"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "SYS"}
    assert seen["body"]["messages"][1]["content"] == "hi"
    assert seen["body"]["response_format"] == {"type": "json_object"} and seen["body"]["model"] == "m1"
    assert (res.tokens_in, res.tokens_out) == (11, 7) and not res.cached


def test_json_mode_can_be_disabled():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return ok()

    make(handler, json_mode=False).complete("s", [])
    assert "response_format" not in seen["body"]


def test_429_with_long_retry_after_fails_fast_as_quota_exhaustion(sleeps):
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(429, headers={"retry-after": "3600"}, text="daily limit")

    with pytest.raises(RateLimitError):
        make(handler).complete("s", [])
    assert n["c"] == 1 and sleeps == []


def test_429_with_short_retry_after_waits_then_succeeds(sleeps):
    responses = [httpx.Response(429, headers={"retry-after": "2"}), ok("done")]
    res = make(lambda req: responses.pop(0)).complete("s", [])
    assert res.text == "done" and sleeps == [2.0]


def test_429_keeps_failing_raises_after_retries(sleeps):
    with pytest.raises(RateLimitError):
        make(lambda req: httpx.Response(429), max_retries=2).complete("s", [])
    assert len(sleeps) == 2


def test_5xx_is_retried(sleeps):
    responses = [httpx.Response(503, text="busy"), ok("fine")]
    assert make(lambda req: responses.pop(0)).complete("s", []).text == "fine"
    assert len(sleeps) == 1


def test_4xx_is_not_retried():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(401, text="bad key")

    with pytest.raises(LLMError) as e:
        make(handler).complete("s", [])
    assert n["c"] == 1 and e.value.status == 401 and not is_transient(e.value)


def test_transport_errors_retried_then_raised(sleeps):
    def handler(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(LLMError) as e:
        make(handler, max_retries=1).complete("s", [])
    assert "network error" in str(e.value) and is_transient(e.value)


def test_unexpected_response_shape():
    with pytest.raises(LLMError):
        make(lambda req: httpx.Response(200, json={"nope": 1})).complete("s", [])


def test_error_classification():
    assert is_rate_limit(RuntimeError("429 RESOURCE_EXHAUSTED")) and is_rate_limit(RateLimitError("x", 429))
    assert is_transient(LLMError("x", 503)) and not is_transient(LLMError("x", 400))
    assert not is_transient(ValueError("random bug"))


# ---------- fallback ----------
class Stub:
    def __init__(self, result=None, error=None, model_id="stub"):
        self.result, self.error, self.calls, self.model_id = result, error, 0, model_id

    def complete(self, system, messages):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def test_fallback_used_on_rate_limit():
    a, b = Stub(error=RateLimitError("429", 429)), Stub(LLMResult("from b"))
    assert FallbackLLM([a, b]).complete("s", []).text == "from b" and a.calls == b.calls == 1


def test_fallback_not_used_for_auth_errors():
    a, b = Stub(error=LLMError("bad key", 401)), Stub(LLMResult("from b"))
    with pytest.raises(LLMError):
        FallbackLLM([a, b]).complete("s", [])
    assert b.calls == 0


def test_fallback_all_fail_raises_last():
    a, b = Stub(error=RateLimitError("a", 429)), Stub(error=LLMError("b down", 503))
    with pytest.raises(LLMError) as e:
        FallbackLLM([a, b]).complete("s", [])
    assert "b down" in str(e.value)


# ---------- cache ----------
def test_caching_llm_hit_avoids_inner_call(tmp_path):
    inner = Stub(LLMResult("answer", 5, 3))
    c = CachingLLM(inner, DiskCache(tmp_path))
    first = c.complete("sys", [{"role": "user", "content": "q"}])
    second = c.complete("sys", [{"role": "user", "content": "q"}])
    assert inner.calls == 1 and not first.cached and second.cached
    assert (second.text, second.tokens_in, second.tokens_out) == ("answer", 5, 3)
    assert (c.hits, c.misses) == (1, 1)
    c.complete("sys", [{"role": "user", "content": "different"}])     # key depends on messages
    assert inner.calls == 2


def test_cache_key_depends_on_model_and_system(tmp_path):
    cache = DiskCache(tmp_path)
    CachingLLM(Stub(LLMResult("A"), model_id="m1"), cache).complete("sys", [])
    inner2 = Stub(LLMResult("B"), model_id="m2")
    assert CachingLLM(inner2, cache).complete("sys", []).text == "B"
    CachingLLM(inner2, cache).complete("other sys", [])
    assert inner2.calls == 2


def test_persists_across_instances_and_replay_mode(tmp_path):
    CachingLLM(Stub(LLMResult("A")), DiskCache(tmp_path)).complete("s", [])
    replay = CachingLLM(Stub(error=AssertionError("must not call")), DiskCache(tmp_path), mode="replay")
    assert replay.complete("s", []).text == "A"
    with pytest.raises(CacheMiss):
        replay.complete("never recorded", [])


def test_empty_responses_not_cached(tmp_path):
    inner = Stub(LLMResult("   "))
    c = CachingLLM(inner, DiskCache(tmp_path))
    c.complete("s", []); c.complete("s", [])
    assert inner.calls == 2


def test_corrupt_cache_file_is_ignored(tmp_path):
    cache = DiskCache(tmp_path)
    key = DiskCache.key({"x": 1})
    cache.set("llm", key, {"text": "ok"})
    cache._path("llm", key).write_text("{not json")
    assert cache.get("llm", key) is None


# ---------- factory ----------
def test_build_llm_composition(tmp_path):
    base = dict(cache_dir=str(tmp_path), llm_api_key="k")
    s = Settings(llm_cache="off", **base)
    assert isinstance(build_llm(s), OpenAICompatLLM)

    s = Settings(llm_cache="off", llm_fallback_api_key="fk", llm_fallback_model="free-model", **base)
    chain = build_llm(s)
    assert isinstance(chain, FallbackLLM) and len(chain.providers) == 2
    assert "primary:openai/gpt-oss-120b" in chain.model_id and "fallback:free-model" in chain.model_id

    s = Settings(llm_cache="readwrite", **base)
    c = build_llm(s)
    assert isinstance(c, CachingLLM) and isinstance(c.inner, OpenAICompatLLM)

    # fallback needs BOTH key and model
    assert isinstance(build_llm(Settings(llm_cache="off", llm_fallback_api_key="fk", **base)), OpenAICompatLLM)


def test_build_llm_gemini_provider(tmp_path):
    s = Settings(llm_provider="gemini", gemini_api_key="x", llm_cache="off", cache_dir=str(tmp_path))
    assert isinstance(build_llm(s), GeminiLLM)


# ---------- parsing reasoning-model output ----------
def test_parse_action_strips_think_blocks():
    a = parse_action('<think>let me reason {"action": "wrong"}</think>{"action":"final_answer","args":{"answer":"x"}}')
    assert a.name == "final_answer"


# ---------- query-embedding disk cache ----------
def test_embedder_disk_cache_survives_restart(tmp_path):
    from app.ingestion.embed import GeminiEmbedder

    s = Settings(gemini_api_key="x", cache_dir=str(tmp_path), llm_cache="readwrite")
    calls = {"n": 0}

    def fake_embed(texts, task):
        calls["n"] += 1
        return [[0.1, 0.2, 0.3] for _ in texts]

    e1 = GeminiEmbedder(s)
    e1._embed = fake_embed
    assert e1.embed_query("where is login") == [0.1, 0.2, 0.3]
    e1.embed_query("where is login")                       # in-memory hit
    e2 = GeminiEmbedder(s)                                  # "restart": new instance, same disk cache
    e2._embed = fake_embed
    assert e2.embed_query("where is login") == [0.1, 0.2, 0.3]
    assert calls["n"] == 1

    off = GeminiEmbedder(Settings(gemini_api_key="x", cache_dir=str(tmp_path), llm_cache="off"))
    off._embed = fake_embed
    off.embed_query("where is login")
    assert calls["n"] == 2                                  # cache disabled => real call


def test_json_validate_failed_retries_once_without_json_mode():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": {"code": "json_validate_failed", "message": "Failed to validate JSON"}})
        return ok("not strict json {\"action\":\"final_answer\",\"args\":{}}")

    res = make(handler).complete("s", [])
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]
    assert len(bodies) == 2 and res.text.startswith("not strict")


def test_other_400_still_not_retried():
    n = {"c": 0}

    def handler(req):
        n["c"] += 1
        return httpx.Response(400, text="bad request")

    with pytest.raises(LLMError):
        make(handler).complete("s", [])
    assert n["c"] == 1


def test_reasoning_effort_sent_only_for_gpt_oss():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return ok()

    OpenAICompatLLM("https://x/v1", "K", "openai/gpt-oss-120b", reasoning_effort="low",
                    client=httpx.Client(transport=httpx.MockTransport(handler))).complete("s", [])
    OpenAICompatLLM("https://x/v1", "K", "other/model", reasoning_effort="low",
                    client=httpx.Client(transport=httpx.MockTransport(handler))).complete("s", [])
    assert seen[0]["reasoning_effort"] == "low" and "reasoning_effort" not in seen[1]


def test_tool_use_failed_is_translated_to_json_action():
    def handler(req):
        return httpx.Response(400, json={"error": {"code": "tool_use_failed", "message": "Tool choice is none, but model called a tool",
            "failed_generation": '{"name": "repo_browser.search_code", "arguments": {"query": "authenticate", "k": 5}}'}})

    res = make(handler).complete("s", [])
    act = parse_action(res.text)
    assert act.name == "search_code" and act.args == {"query": "authenticate", "k": 5}


def test_tool_use_failed_unparseable_still_raises():
    def handler(req):
        return httpx.Response(400, json={"error": {"code": "tool_use_failed", "failed_generation": ""}})

    with pytest.raises(LLMError):
        make(handler).complete("s", [])
