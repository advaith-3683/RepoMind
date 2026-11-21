import json
import sys
import types

from fastapi.testclient import TestClient

from app import main

client = TestClient(main.app)
RID = "11111111-1111-1111-1111-111111111111"


def parse_sse(body: str):
    frames = []
    for block in body.strip().split("\n\n"):
        lines = dict(l.split(": ", 1) for l in block.split("\n"))
        frames.append((lines["event"], json.loads(lines["data"])))
    return frames


def test_ask_streams_sse_frames(monkeypatch):
    monkeypatch.setattr(main.store, "get_repo", lambda rid: {"id": rid, "status": "ready"})

    seen = {}

    def fake_stream(repo, question, mode=None, user_id=None):
        seen["mode"] = mode
        yield {"type": "start", "query_id": "q1"}
        yield {"type": "step", "index": 1, "tool": "search_code", "args": {"query": question}}
        yield {"type": "answer", "text": "multi\nline <b>x</b>", "citations": [], "confidence": "high"}
        yield {"type": "done", "query_id": "q1", "status": "completed", "usage": {}}

    fake_module = types.ModuleType("app.service")
    fake_module.stream_answer = fake_stream
    monkeypatch.setitem(sys.modules, "app.service", fake_module)

    r = client.post("/ask", json={"repo_id": RID, "question": "where is login?", "mode": "agent"})
    assert r.status_code == 200 and seen["mode"] == "agent"
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    frames = parse_sse(r.text)
    assert [t for t, _ in frames] == ["start", "step", "answer", "done"]
    assert all(t == d["type"] for t, d in frames)
    assert frames[2][1]["text"] == "multi\nline <b>x</b>"   # newlines survive JSON-in-SSE framing


def test_ask_rejects_repo_not_ready(monkeypatch):
    monkeypatch.setattr(main.store, "get_repo", lambda rid: {"id": rid, "status": "indexing"})
    r = client.post("/ask", json={"repo_id": RID, "question": "hi"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "repo_not_ready"


def test_ask_unknown_repo_404(monkeypatch):
    monkeypatch.setattr(main.store, "get_repo", lambda rid: None)
    r = client.post("/ask", json={"repo_id": RID, "question": "hi"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_decision_conflict_maps_to_409(monkeypatch):
    from app.guardrails.errors import GuardrailViolation

    def boom(*a, **k):
        raise GuardrailViolation("invalid_transition", "This action was already approved.")

    monkeypatch.setattr(main.store, "decide_action", boom)
    r = client.post(f"/actions/{RID}/decision", json={"decision": "approve"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"


def test_decision_success_shape(monkeypatch):
    monkeypatch.setattr(main.store, "decide_action", lambda *a, **k: {
        "id": RID, "status": "edited", "kind": "patch",
        "payload": {"title": "old", "body": "x", "path": "a.py"},
        "edited_payload": {"title": "new", "body": "y", "path": "a.py"}})
    r = client.post(f"/actions/{RID}/decision", json={"decision": "edit", "edited_payload": {"title": "new", "body": "y", "path": "a.py"}})
    assert r.status_code == 200 and r.json()["payload"]["title"] == "new" and r.json()["status"] == "edited"
