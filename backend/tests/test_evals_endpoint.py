import json

from starlette.testclient import TestClient

from app import main


def _client():
    return TestClient(main.app, raise_server_exceptions=False)


def test_serves_published_result_when_no_local_runs(tmp_path, monkeypatch):
    (tmp_path / "published").mkdir()
    (tmp_path / "published" / "latest.json").write_text(json.dumps({"agent": {"answer_rate": 0.85}}))
    monkeypatch.setattr(main, "EVALS_DIR", tmp_path)
    r = _client().get("/evals/latest")
    assert r.status_code == 200 and r.json()["agent"]["answer_rate"] == 0.85


def test_local_run_takes_precedence_over_published(tmp_path, monkeypatch):
    (tmp_path / "published").mkdir()
    (tmp_path / "results").mkdir()
    (tmp_path / "published" / "latest.json").write_text(json.dumps({"src": "published"}))
    (tmp_path / "results" / "20260101T000000.json").write_text(json.dumps({"src": "local-old"}))
    (tmp_path / "results" / "20261125T000000.json").write_text(json.dumps({"src": "local-new"}))
    monkeypatch.setattr(main, "EVALS_DIR", tmp_path)
    assert _client().get("/evals/latest").json() == {"src": "local-new"}


def test_404_when_nothing_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "EVALS_DIR", tmp_path)
    assert _client().get("/evals/latest").status_code == 404


def test_committed_published_file_is_valid_and_honest():
    d = json.loads((main.EVALS_DIR / "published" / "latest.json").read_text())
    assert {"redteam", "retrieval", "agent", "meta", "history"} <= d.keys()
    assert d["agent"]["n_errors"] == 0 and d["agent"]["models"] == [d["meta"]["model"]]
    assert d["meta"]["fast_top_k"] == 5 and d["meta"]["limits"]
    assert any(h["valid"] is False for h in d["history"])          # the invalid run stays visible
    assert all("cited" not in r for r in d["agent"]["rows"])        # no per-answer data is published
