"""Publish idempotency: sequential repeats, concurrent requests, failure/retry, and non-approved states.

The claim/finish/release functions are replaced by an in-memory model of store.py's SQL (one atomic
UPDATE guarded by a lock). This verifies the ENDPOINT's behaviour around it; it does not prove the
Postgres UPDATE itself is atomic (that follows from single-statement UPDATE semantics and is not
exercised against a live database here)."""
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import main, store
from app.guardrails.errors import GuardrailViolation

AID = uuid.UUID("22222222-2222-2222-2222-222222222222")
URL = f"/actions/{AID}/publish"
GH = {"X-GitHub-Token": "ghp_x"}
PAYLOAD = {"title": "Fix typo", "body": "b", "path": "README.md", "edits": [{"old": "a", "new": "b"}]}


class FakeActions:
    """In-memory pending_actions row + store functions with the same semantics as store.py."""

    def __init__(self, status="approved"):
        self.row = {"id": AID, "repo_id": uuid.uuid4(), "kind": "patch", "status": status, "payload": PAYLOAD,
                    "edited_payload": None, "result": None, "started": False}
        self.lock = threading.Lock()
        self.published = 0
        self.fail_next = False
        self.finish_fails = False

    def get_action(self, aid, uid):
        with self.lock:
            return dict(self.row)

    def claim(self, aid, uid):
        with self.lock:   # models the single atomic UPDATE ... WHERE ... RETURNING
            if self.row["status"] in ("approved", "edited") and self.row["result"] is None and not self.row["started"]:
                self.row["started"] = True
                return dict(self.row)
        raise GuardrailViolation("publish_in_progress", "busy")

    def finish(self, aid, result):
        if self.finish_fails:
            raise RuntimeError("db down")
        with self.lock:
            self.row["result"] = result

    def release(self, aid):
        with self.lock:
            if self.row["result"] is None:
                self.row["started"] = False

    def publish(self, gh, owner, repo, kind, payload, aid, who):
        time.sleep(0.2)   # keep the first request "in flight" while the second arrives
        if self.fail_next:
            self.fail_next = False
            raise GuardrailViolation("github_conflict", "file changed")
        self.published += 1
        return {"kind": kind, "url": f"https://github.com/o/r/pull/{self.published}", "number": self.published}


@pytest.fixture
def fake(monkeypatch):
    def make(status="approved"):
        f = FakeActions(status)
        monkeypatch.setattr(store, "get_action", f.get_action)
        monkeypatch.setattr(store, "claim_publish", f.claim)
        monkeypatch.setattr(store, "finish_publish", f.finish)
        monkeypatch.setattr(store, "release_publish", f.release)
        monkeypatch.setattr(store, "get_repo", lambda rid: {"url": "https://github.com/o/r"})
        monkeypatch.setattr(main, "publish_action", f.publish)
        return f
    return make


def post():
    return TestClient(main.app, raise_server_exceptions=False).post(URL, headers=GH)


def test_sequential_repeat_returns_same_pr_and_publishes_once(fake):
    f = fake()
    first, second = post(), post()
    assert first.status_code == 200 and first.json()["already"] is False
    assert second.status_code == 200 and second.json()["already"] is True
    assert second.json()["result"] == first.json()["result"]
    assert f.published == 1


def test_concurrent_requests_publish_exactly_once(fake):
    f = fake()
    out = []

    def go():
        r = post()
        out.append((r.status_code, r.json()))

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert f.published == 1
    winners = [o for o in out if o[0] == 200 and o[1]["already"] is False]
    assert len(winners) == 1
    for code, body in out:   # every other request is a clean 409 (in progress) or an "already" 200, never a 5xx
        assert code in (200, 409)
        if code == 409:
            assert body["error"]["code"] == "publish_in_progress"


def test_failure_releases_claim_and_retry_succeeds(fake):
    f = fake()
    f.fail_next = True
    r1 = post()
    assert r1.status_code == 409 and r1.json()["error"]["code"] == "github_conflict"
    assert f.row["started"] is False and f.row["result"] is None
    r2 = post()
    assert r2.status_code == 200 and r2.json()["already"] is False and f.published == 1


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_not_approved_actions_cannot_be_published(fake, status):
    f = fake(status)
    r = post()
    assert r.status_code == 409 and r.json()["error"]["code"] == "publish_not_ready"
    assert f.published == 0 and f.row["started"] is False


def test_edited_action_can_be_published(fake):
    f = fake("edited")
    assert post().status_code == 200 and f.published == 1


def test_known_gap_result_not_recorded_keeps_claim_so_no_immediate_duplicate(fake):
    """If GitHub succeeded but recording the result failed, the claim stays held (5 min window in SQL),
    so an immediate retry cannot open a second PR. After the window, a retry is NOT proven safe for issues."""
    f = fake()
    f.finish_fails = True
    assert post().status_code == 500 and f.published == 1
    r = post()
    assert r.status_code == 409 and r.json()["error"]["code"] == "publish_in_progress"
    assert f.published == 1
