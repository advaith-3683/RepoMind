import json
from pathlib import Path

import pytest

from app import service, store
from app.evals import run as ev

GOLD = Path(ev.HERE) / "golden_smartspend.json"


def row(id, answer="x", cited=(), found=True, status="completed", error=None, usage=None, latency=100):
    return {"id": id, "answer": answer, "cited": list(cited), "found": found, "groundedness": 0.8,
            "status": status, "error": error, "usage": usage or {"tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0},
            "latency_ms": latency}


QS = [
    {"id": "a", "question": "q", "expected_files": ["f1.py", "f2.py"], "expected_keywords": ["alpha"]},
    {"id": "b", "question": "q", "expected_files": ["f3.py"], "expected_keywords": ["beta"]},
    {"id": "c", "question": "q", "expected_files": ["f4.py"], "expected_keywords": []},
    {"id": "d1", "question": "q", "expected_files": [], "expected_keywords": [], "should_decline": True},
    {"id": "d2", "question": "q", "expected_files": [], "expected_keywords": [], "should_decline": True},
]


def test_summary_counts_failures_as_zero_and_scores_declines_separately():
    rows = [
        row("a", "alpha here", ["f1.py"]),                                  # half the files, keyword hit
        row("b", "", status="error", error="rate_limited", found=None),     # error: counts as 0, not skipped
        row("c", "ok", ["f4.py"], found=False),                              # model said "not found": not answered
        row("d1", "I could not find it", found=False),                       # correctly declined
        row("d2", "It uses Redis [x.py:1-2]", ["x.py"]),                     # hallucinated instead of declining
    ]
    s = ev.summarize_agent_rows(rows, QS, "fast")
    assert (s["n"], s["n_answerable"], s["n_should_decline"]) == (5, 3, 2)
    assert s["answer_rate"] == pytest.approx(1 / 3)
    assert s["file_recall"] == pytest.approx(0.5 / 3)
    assert s["keyword_hit"] == pytest.approx(1 / 3)
    assert s["decline_accuracy"] == 0.5
    assert s["error_rate"] == pytest.approx(1 / 5)
    assert "answer" not in s["rows"][0]            # raw answers (could contain repo code) are not stored in results


def test_summary_without_decline_questions_has_no_decline_metric():
    s = ev.summarize_agent_rows([row("a", "alpha", ["f1.py", "f2.py"])], QS[:1])
    assert s["decline_accuracy"] is None and s["file_recall"] == 1.0
    assert "n/a" in ev.to_markdown({"agent": s})


def test_run_agent_eval_retries_rate_limit_once_and_paces(monkeypatch):
    sleeps, calls = [], {"n": 0}
    monkeypatch.setattr(ev.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(store, "get_repo", lambda rid: {"id": rid})

    def fake_stream(repo, question, mode):
        calls["n"] += 1
        if calls["n"] == 1:
            yield {"type": "error", "code": "rate_limited", "message": "m"}
            yield {"type": "done", "status": "error", "usage": {}}
        else:
            yield {"type": "answer", "text": "alpha", "citations": [{"path": "f1.py"}], "found": True, "groundedness": 0.9}
            yield {"type": "done", "status": "completed", "usage": {"latency_ms": 5, "tokens_in": 1, "tokens_out": 1}}

    monkeypatch.setattr(service, "stream_answer", fake_stream)
    golden = {"repo_id": "r", "questions": QS[:2]}
    out = ev.run_agent_eval(golden, "fast", sleep_s=2.0)
    assert out["rows"][0]["retried"] is True and out["rows"][0]["error"] is None
    assert 65 in sleeps and 2.0 in sleeps


def test_exceptions_in_one_question_do_not_stop_the_run(monkeypatch):
    monkeypatch.setattr(store, "get_repo", lambda rid: {"id": rid})

    def boom(repo, question, mode):
        raise RuntimeError("db down")
        yield

    monkeypatch.setattr(service, "stream_answer", boom)
    out = ev.run_agent_eval({"repo_id": "r", "questions": QS[:2]}, "fast")
    assert out["n"] == 2 and out["error_rate"] == 1.0 and out["answer_rate"] == 0.0


def test_resolve_repo_id(monkeypatch):
    monkeypatch.setattr(store, "get_repo_by_url", lambda u: {"id": "uuid-1", "status": "ready"} if u == "https://github.com/o/r" else None)
    assert ev.resolve_repo_id({"repo_url": "https://github.com/o/r"}) == "uuid-1"
    with pytest.raises(SystemExit):
        ev.resolve_repo_id({"repo_url": "https://github.com/o/other"})
    with pytest.raises(SystemExit):
        ev.resolve_repo_id({"repo_id": "PUT-INDEXED-REPO-UUID-HERE"})
    monkeypatch.setattr(store, "get_repo_by_url", lambda u: {"id": "uuid-2", "status": "indexing"})
    with pytest.raises(SystemExit):
        ev.resolve_repo_id({"repo_url": "https://github.com/o/r"})


def test_golden_file_is_well_formed():
    g = json.loads(GOLD.read_text())
    qs = g["questions"]
    assert len({q["id"] for q in qs}) == len(qs) >= 25
    for q in qs:
        if q.get("should_decline"):
            assert q["expected_files"] == []
        else:
            assert q["expected_files"] and q["expected_keywords"]
    assert sum(1 for q in qs if q.get("should_decline")) >= 3
    assert [q["id"] for q in ev._answerable(g)] == [q["id"] for q in qs if not q.get("should_decline")]


def test_error_detail_is_recorded_and_truncated(monkeypatch):
    def fake_stream(repo, question, mode):
        yield {"type": "error", "code": "rate_limited", "message": "m", "detail": "x" * 500}
        yield {"type": "done", "status": "error", "usage": {}}

    monkeypatch.setattr(service, "stream_answer", fake_stream)
    r = ev._ask_once({"id": "r"}, QS[0], "fast")
    assert r["error"] == "rate_limited" and len(r["error_detail"]) == 160


def test_rate_limit_retries_up_to_twice_then_gives_up(monkeypatch):
    sleeps, calls = [], {"n": 0}
    monkeypatch.setattr(ev.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(store, "get_repo", lambda rid: {"id": rid})

    def always_limited(repo, question, mode):
        calls["n"] += 1
        yield {"type": "error", "code": "rate_limited", "message": "m"}
        yield {"type": "done", "status": "error", "usage": {}}

    monkeypatch.setattr(service, "stream_answer", always_limited)
    out = ev.run_agent_eval({"repo_id": "r", "questions": QS[:1]}, "fast")
    assert calls["n"] == 3 and sleeps.count(65) == 2
    assert out["rows"][0]["error"] == "rate_limited" and out["rows"][0]["retried"] is True


def test_ex_errors_metrics_and_warning():
    rows = [row("a", "alpha", ["f1.py"]),
            row("b", "", status="error", error="rate_limited", found=None),
            row("d1", "", status="error", error="rate_limited", found=None),
            row("d2", "none", found=False)]
    s = ev.summarize_agent_rows(rows, QS, "fast")
    assert s["n_errors"] == 2
    assert s["answer_rate"] == pytest.approx(1 / 2) and s["answer_rate_ex_errors"] == 1.0
    assert s["decline_accuracy"] == 0.5 and s["decline_accuracy_ex_errors"] == 1.0
    assert "Warning: 2 of 4" in ev.to_markdown({"agent": s})
    clean = ev.summarize_agent_rows([row("a", "alpha", ["f1.py"])], QS[:1])
    assert "Warning" not in ev.to_markdown({"agent": clean})


def test_models_recorded_and_mixed_models_warn(monkeypatch):
    def fake_stream(repo, question, mode):
        m = "primary:gpt-oss" if question == "p" else "fallback:gemma"
        yield {"type": "answer", "text": "alpha", "citations": [{"path": "f1.py"}], "found": True, "groundedness": 0.9}
        yield {"type": "done", "status": "completed", "usage": {"latency_ms": 5, "models": [m]}}

    monkeypatch.setattr(service, "stream_answer", fake_stream)
    qa, qb = {**QS[0], "question": "p"}, {**QS[1], "question": "f"}
    one = ev.summarize_agent_rows([ev._ask_once({}, qa, "fast")], [qa], "fast")
    assert one["models"] == ["primary:gpt-oss"] and "Model: `primary:gpt-oss`" in ev.to_markdown({"agent": one})
    both = ev.summarize_agent_rows([ev._ask_once({}, qa, "fast"), ev._ask_once({}, qb, "fast")], [qa, qb], "fast")
    assert len(both["models"]) == 2 and "more than one model" in ev.to_markdown({"agent": both})
    assert both["rows"][1]["models"] == ["fallback:gemma"]


def test_early_stop_after_three_consecutive_rate_limits(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(ev.time, "sleep", lambda s: None)
    monkeypatch.setattr(store, "get_repo", lambda rid: {"id": rid})

    def limited(repo, question, mode):
        calls["n"] += 1
        yield {"type": "error", "code": "rate_limited", "message": "m"}
        yield {"type": "done", "status": "error", "usage": {}}

    monkeypatch.setattr(service, "stream_answer", limited)
    out = ev.run_agent_eval({"repo_id": "r", "questions": QS}, "fast")   # 5 questions planned
    assert out["n"] == 3 and out["aborted"] is True and out["n_planned"] == 5
    assert calls["n"] == 3 * (1 + ev.MAX_RATE_LIMIT_RETRIES)
    assert "Run aborted after 3 of 5" in ev.to_markdown({"agent": out})


def test_early_stop_streak_resets_on_success(monkeypatch):
    monkeypatch.setattr(ev.time, "sleep", lambda s: None)
    monkeypatch.setattr(store, "get_repo", lambda rid: {"id": rid})

    def mixed(repo, question, mode):
        yield {"type": "error", "code": "rate_limited", "message": "m"} if question.startswith("L") else {
            "type": "answer", "text": "a", "citations": [], "found": True, "groundedness": 1.0}
        yield {"type": "done", "status": "error" if question.startswith("L") else "completed", "usage": {}}

    monkeypatch.setattr(service, "stream_answer", mixed)
    qs = [{**QS[0], "id": f"x{i}", "question": ("L" if i in (0, 1, 3, 4) else "ok")} for i in range(6)]   # L,L,ok,L,L,ok: streak never reaches 3
    out = ev.run_agent_eval({"repo_id": "r", "questions": qs}, "fast")
    assert out["n"] == 6 and out["aborted"] is False
