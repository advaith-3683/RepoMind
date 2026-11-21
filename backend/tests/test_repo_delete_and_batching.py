import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, store
from app.config import get_settings
from app.ingestion.pipeline import MAX_EMBED_CHARS, est_tokens, make_batches

client = TestClient(main.app)
RID = uuid.uuid4()


@pytest.fixture
def repos_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "repos_dir", str(tmp_path))
    return tmp_path


def test_delete_removes_row_and_files(repos_dir, monkeypatch):
    (repos_dir / str(RID)).mkdir()
    (repos_dir / str(RID) / "a.py").write_text("x = 1\n")
    deleted = []
    monkeypatch.setattr(store, "get_repo", lambda r: {"id": r, "created_by": None, "status": "ready"})
    monkeypatch.setattr(store, "delete_repo", lambda r: deleted.append(r) or True)
    res = client.delete(f"/repos/{RID}")
    assert res.status_code == 204 and deleted == [RID]
    assert not (repos_dir / str(RID)).exists()


def test_delete_unknown_repo_is_404(monkeypatch):
    monkeypatch.setattr(store, "get_repo", lambda r: None)
    assert client.delete(f"/repos/{RID}").status_code == 404


def test_delete_requires_owner_when_auth_is_on(repos_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_required", True)
    monkeypatch.setattr(main, "current_user", main.current_user)
    from app import auth
    monkeypatch.setattr(auth, "verify_token", lambda t: auth.User("me", None, "me"))
    monkeypatch.setattr(store, "get_repo", lambda r: {"id": r, "created_by": "someone-else"})
    monkeypatch.setattr(store, "delete_repo", lambda r: pytest.fail("must not delete"))
    res = client.delete(f"/repos/{RID}", headers={"Authorization": "Bearer " + "t" * 30})
    assert res.status_code == 403 and res.json()["error"]["code"] == "forbidden"


def test_delete_is_blocked_while_indexing(repos_dir, monkeypatch):
    from app import repositories
    monkeypatch.setattr(store, "get_repo", lambda r: {"id": r, "created_by": None})
    monkeypatch.setattr(store, "delete_repo", lambda r: pytest.fail("must not delete"))
    with repositories.repository_lock(RID):      # simulates a running ingestion holding the lock
        import threading
        out = {}
        t = threading.Thread(target=lambda: out.update(r=client.delete(f"/repos/{RID}")))
        t.start()
        t.join(10)
    assert out["r"].status_code == 409


def test_batches_respect_item_and_token_limits():
    texts = ["a" * 300] * 10                     # ~101 tokens each
    batches = make_batches(texts, max_items=4, max_tokens=250)
    assert batches[0][:2] == (0, 2)              # 2 items fit in 250 estimated tokens
    assert sum(b - a for a, b, _ in batches) == 10 and batches[0][0] == 0 and batches[-1][1] == 10
    assert all(t <= 250 or (b - a) == 1 for a, b, t in batches)
    assert [b for _, b, _ in batches] == sorted(b for _, b, _ in batches)


def test_one_oversized_text_is_its_own_batch_and_empty_is_empty():
    assert make_batches(["x" * 100000, "y"], 25, 8000) == [(0, 1, est_tokens("x" * 100000)), (1, 2, est_tokens("y"))]
    assert make_batches([], 25, 8000) == []


def test_realistic_big_chunks_never_exceed_the_token_budget():
    texts = ["z" * MAX_EMBED_CHARS] * 60         # worst case: every chunk at the cap
    for a, b, tokens in make_batches(texts, 25, 8000):
        assert b - a == 1 or tokens <= 8000
