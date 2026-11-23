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
