import base64
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, main, store
from app.config import get_settings
from app.github import GitHubClient
from app.guardrails.errors import GuardrailViolation
from app.hitl.patching import PatchError, apply_edits, make_diff, validate_edits_shape
from app.publish import branch_name, publish_action

client = TestClient(main.app)
TOKEN = "t" * 40
UID = "11111111-2222-3333-4444-555555555555"


def code_of(exc):
    return exc.value.code


# ---------------- Supabase token verification ----------------
@pytest.fixture
def auth_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auth_required", True)
    monkeypatch.setattr(s, "supabase_url", "https://x.supabase.co")
    monkeypatch.setattr(s, "supabase_anon_key", "anon-key")
    calls = []

    def install(handler):
        def h(req):
            calls.append(req)
            return handler(req)
        auth.set_http_client(httpx.Client(transport=httpx.MockTransport(h)))
    return install, calls


def ok_user(req):
    return httpx.Response(200, json={"id": UID, "email": "a@b.c", "user_metadata": {"user_name": "advaith"}})


def test_verify_token_success_and_cache(auth_on):
    install, calls = auth_on
    install(ok_user)
    u = auth.verify_token(TOKEN)
    assert (u.id, u.login, u.email) == (UID, "advaith", "a@b.c")
    assert calls[0].headers["apikey"] == "anon-key" and calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    auth.verify_token(TOKEN)
    assert len(calls) == 1                      # second call served from cache


@pytest.mark.parametrize("status,code", [(401, "unauthorized"), (403, "unauthorized"), (500, "auth_unavailable")])
def test_verify_token_error_statuses(auth_on, status, code):
    install, _ = auth_on
    install(lambda r: httpx.Response(status, json={}))
    with pytest.raises(GuardrailViolation) as e:
        auth.verify_token(TOKEN)
    assert code_of(e) == code


def test_verify_token_transport_error_and_unconfigured(auth_on, monkeypatch):
    install, _ = auth_on

    def boom(req):
        raise httpx.ConnectError("down")
    install(boom)
    with pytest.raises(GuardrailViolation) as e:
        auth.verify_token(TOKEN)
    assert code_of(e) == "auth_unavailable"
    monkeypatch.setattr(get_settings(), "supabase_url", "")
    with pytest.raises(GuardrailViolation) as e:
        auth.verify_token(TOKEN)
    assert code_of(e) == "auth_not_configured"


def test_endpoints_require_a_valid_token(auth_on, monkeypatch):
    install, _ = auth_on
    install(lambda r: httpx.Response(401, json={}) if "bad" in r.headers["authorization"] else ok_user(r))
    seen = {}
    monkeypatch.setattr(store, "list_queries", lambda user_id, repo_id, limit: seen.update(u=user_id) or [])
    assert client.get("/history").status_code == 401
    assert client.get("/history", headers={"Authorization": "Bearer " + "bad" * 10}).status_code == 401
    r = client.get("/history", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json() == [] and seen["u"] == UID
    assert client.get("/health").status_code == 200       # public


def test_config_is_public(auth_on):
    r = client.get("/config")
    assert r.status_code == 200
    assert r.json() == {"auth_required": True, "supabase_url": "https://x.supabase.co",
                        "supabase_anon_key": "anon-key", "github_scopes": "public_repo"}


def test_owner_id_is_passed_to_store(auth_on, monkeypatch):
    install, _ = auth_on
    install(ok_user)
    h = {"Authorization": f"Bearer {TOKEN}"}
    seen = {}
    qid = str(uuid.uuid4())
    monkeypatch.setattr(store, "get_trace", lambda q, u: seen.update(trace=u))
    monkeypatch.setattr(store, "add_feedback", lambda q, u, rating, c: seen.update(fb=u))
    monkeypatch.setattr(store, "get_action", lambda a, u: seen.update(act=u))
    assert client.get(f"/traces/{qid}", headers=h).status_code == 404       # not the user's -> 404
    assert client.post("/feedback", headers=h, json={"query_id": qid, "rating": 1}).status_code == 201
    assert client.get(f"/actions/{qid}", headers=h).status_code == 404
    assert seen == {"trace": UID, "fb": UID, "act": UID}


# ---------------- patching ----------------
def test_apply_edits():
    assert apply_edits("a\nb\nc\n", [{"old": "b", "new": "B"}]) == "a\nB\nc\n"
    assert apply_edits("x y", [{"old": "x", "new": "y"}, {"old": "y y", "new": "z"}]) == "z"   # sequential
    with pytest.raises(PatchError):
        apply_edits("a", [{"old": "zzz", "new": "b"}])
    with pytest.raises(PatchError):
        apply_edits("a a", [{"old": "a", "new": "b"}])           # ambiguous


def test_validate_edits_shape():
    assert validate_edits_shape([{"old": "a", "new": "b"}]) == [{"old": "a", "new": "b"}]
    for bad in [None, [], "x", [{"old": "a"}], [{"old": " ", "new": "b"}], [{"old": "a", "new": "a"}],
                [{"old": "a", "new": "[REDACTED:key]"}], [{"old": "a", "new": "x" * 50000}],
                [{"old": "a", "new": "b"}] * 11]:
        with pytest.raises(GuardrailViolation):
            validate_edits_shape(bad)


def test_make_diff():
    d = make_diff("a.py", "x\ny\n", "x\nz\n")
    assert "--- a/a.py" in d and "+++ b/a.py" in d and "-y" in d and "+z" in d


# ---------------- fake GitHub ----------------
class FakeGitHub:
    def __init__(self, content="a = 1\nb = 2\n", can_push=True, fail_pr=False, status=None):
        self.content, self.can_push, self.fail_pr, self.status = content, can_push, fail_pr, status
        self.reqs = []

    def __call__(self, req):
        self.reqs.append((req.method, req.url.raw_path.decode(), req))
        if self.status:
            return httpx.Response(self.status, json={"message": "nope"})
        m, p = req.method, req.url.path
        if m == "GET" and p == "/repos/o/r":
            return httpx.Response(200, json={"default_branch": "main", "permissions": {"push": self.can_push}})
        if m == "GET" and p.startswith("/repos/o/r/git/ref/heads/"):
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if m == "GET" and p.startswith("/repos/o/r/contents/"):
            b = base64.b64encode(self.content.encode()).decode()
            return httpx.Response(200, json={"type": "file", "size": len(self.content), "encoding": "base64",
                                             "content": b, "sha": "fsha"})
        if m == "POST" and p == "/repos/o/r/git/refs":
            return httpx.Response(201, json={})
        if m == "PUT" and p.startswith("/repos/o/r/contents/"):
            return httpx.Response(200, json={})
        if m == "POST" and p == "/repos/o/r/pulls":
            if self.fail_pr:
                return httpx.Response(422, json={"message": "Validation Failed"})
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7", "number": 7})
        if m == "POST" and p == "/repos/o/r/issues":
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/issues/3", "number": 3})
        if m == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    def methods(self):
        return [(m, p) for m, p, _ in self.reqs]

    def gh(self):
        return GitHubClient("ghp_SECRET", "https://api.github.com", httpx.Client(transport=httpx.MockTransport(self)))


PAYLOAD = {"title": "Fix b value", "body": "b should be 3", "path": "src/m.py", "edits": [{"old": "b = 2", "new": "b = 3"}]}
AID = uuid.UUID("abcdef12-0000-0000-0000-000000000000")


def test_publish_patch_happy_path():
    f = FakeGitHub()
    res = publish_action(f.gh(), "o", "r", "patch", PAYLOAD, AID, "advaith")
    assert res == {"kind": "patch", "url": "https://github.com/o/r/pull/7", "number": 7,
                   "branch": "repomind/fix-b-value-abcdef12"}
    seq = [m for m, _ in f.methods()]
    assert seq == ["GET", "GET", "GET", "POST", "PUT", "POST"]       # info, file, base sha, branch, commit, PR
    put = json.loads([r for m, _, r in f.reqs if m == "PUT"][0].content)
    assert base64.b64decode(put["content"]).decode() == "a = 1\nb = 3\n"
    assert put["branch"] == "repomind/fix-b-value-abcdef12" and put["sha"] == "fsha"
    pr = json.loads([r for m, p, r in f.reqs if p.endswith("/pulls")][0].content)
    assert pr["base"] == "main" and "approved by advaith" in pr["body"] and "@" not in pr["body"]


def test_publish_never_touches_default_branch():
    f = FakeGitHub()
    publish_action(f.gh(), "o", "r", "patch", PAYLOAD, AID, None)
    put = json.loads([r for m, _, r in f.reqs if m == "PUT"][0].content)
    assert put["branch"] != "main"


def test_no_push_permission_creates_nothing():
    f = FakeGitHub(can_push=False)
    with pytest.raises(GuardrailViolation) as e:
        publish_action(f.gh(), "o", "r", "patch", PAYLOAD, AID, None)
    assert code_of(e) == "github_no_permission"
    assert [m for m, _ in f.methods()] == ["GET"]


def test_stale_edit_conflicts_before_branch_creation():
    f = FakeGitHub(content="completely different\n")
    with pytest.raises(GuardrailViolation) as e:
        publish_action(f.gh(), "o", "r", "patch", PAYLOAD, AID, None)
    assert code_of(e) == "github_conflict"
    assert "POST" not in [m for m, _ in f.methods()]


def test_crlf_is_preserved():
    f = FakeGitHub(content="a = 1\r\nb = 2\r\n")
    publish_action(f.gh(), "o", "r", "patch", PAYLOAD, AID, None)
    put = json.loads([r for m, _, r in f.reqs if m == "PUT"][0].content)
    assert base64.b64decode(put["content"]).decode() == "a = 1\r\nb = 3\r\n"


def test_pr_failure_deletes_branch():
    f = FakeGitHub(fail_pr=True)
    with pytest.raises(GuardrailViolation) as e:
        publish_action(f.gh(), "o", "r", "patch", PAYLOAD, AID, None)
    assert code_of(e) == "github_conflict"
    assert f.methods()[-1][0] == "DELETE"


def test_publish_issue():
    f = FakeGitHub()
    res = publish_action(f.gh(), "o", "r", "issue", {"title": "T", "body": "B"}, AID, "me")
    assert res == {"kind": "issue", "url": "https://github.com/o/r/issues/3", "number": 3}
    assert [m for m, _ in f.methods()] == ["POST"]


@pytest.mark.parametrize("status,code", [(401, "github_auth"), (403, "github_no_permission"),
                                         (404, "github_no_permission"), (422, "github_conflict"),
                                         (500, "github_error")])
def test_github_error_mapping_never_leaks_token(status, code):
    f = FakeGitHub(status=status)
    with pytest.raises(GuardrailViolation) as e:
        publish_action(f.gh(), "o", "r", "issue", {"title": "T", "body": "B"}, AID, None)
    assert code_of(e) == code and "ghp_SECRET" not in e.value.message


def test_paths_are_quoted():
    f = FakeGitHub()
    f.gh().get_file("o", "r", "dir/a b#c.py", "main")
    assert "/contents/dir/a%20b%23c.py" in f.reqs[0][1]


def test_branch_name_slug():
    assert branch_name("Fix: weird *** title!!", AID) == "repomind/fix-weird-title-abcdef12"
    assert branch_name("!!!", AID) == "repomind/change-abcdef12"


# ---------------- publish endpoint ----------------
def action(**kw):
    a = {"id": AID, "repo_id": uuid.uuid4(), "kind": "patch", "status": "approved", "payload": PAYLOAD,
         "edited_payload": None, "result": None}
    a.update(kw)
    return a


@pytest.fixture
def pub(monkeypatch):
    st = {"claimed": 0, "released": 0, "finished": None, "published": 0}
    monkeypatch.setattr(store, "get_repo", lambda rid: {"url": "https://github.com/o/r"})
    monkeypatch.setattr(store, "claim_publish", lambda a, u: st.update(claimed=st["claimed"] + 1))
    monkeypatch.setattr(store, "release_publish", lambda a: st.update(released=st["released"] + 1))
    monkeypatch.setattr(store, "finish_publish", lambda a, r: st.update(finished=r))

    def fake_publish(gh, owner, repo, kind, payload, aid, who):
        st["published"] += 1
        return {"kind": kind, "url": "https://github.com/o/r/pull/1", "number": 1}
    monkeypatch.setattr(main, "publish_action", fake_publish)
    return st


URL = f"/actions/{AID}/publish"
GH = {"X-GitHub-Token": "ghp_x"}


def test_publish_endpoint_requires_token(monkeypatch, pub):
    monkeypatch.setattr(store, "get_action", lambda a, u: action())
    r = client.post(URL)
    assert r.status_code == 400 and r.json()["error"]["code"] == "missing_github_token"
    assert pub["claimed"] == 0


def test_publish_endpoint_not_found_and_not_approved(monkeypatch, pub):
    monkeypatch.setattr(store, "get_action", lambda a, u: None)
    assert client.post(URL, headers=GH).status_code == 404
    monkeypatch.setattr(store, "get_action", lambda a, u: action(status="pending"))
    r = client.post(URL, headers=GH)
    assert r.status_code == 409 and r.json()["error"]["code"] == "publish_not_ready"


def test_publish_endpoint_idempotent(monkeypatch, pub):
    monkeypatch.setattr(store, "get_action", lambda a, u: action(result={"url": "u"}))
    r = client.post(URL, headers=GH)
    assert r.status_code == 200 and r.json() == {"status": "published", "already": True, "result": {"url": "u"}}
    assert pub["published"] == 0


def test_publish_endpoint_success_and_edited_payload(monkeypatch, pub):
    monkeypatch.setattr(store, "get_action", lambda a, u: action(status="edited"))
    r = client.post(URL, headers=GH)
    assert r.status_code == 200 and r.json()["already"] is False
    assert pub["finished"]["number"] == 1 and pub["claimed"] == 1


def test_publish_endpoint_claim_conflict(monkeypatch, pub):
    monkeypatch.setattr(store, "get_action", lambda a, u: action())

    def conflict(a, u):
        raise GuardrailViolation("publish_in_progress", "busy")
    monkeypatch.setattr(store, "claim_publish", conflict)
    assert client.post(URL, headers=GH).status_code == 409
    assert pub["published"] == 0


def test_publish_failure_releases_claim(monkeypatch, pub):
    monkeypatch.setattr(store, "get_action", lambda a, u: action())

    def fail(*a, **k):
        raise GuardrailViolation("github_no_permission", "no")
    monkeypatch.setattr(main, "publish_action", fail)
    r = client.post(URL, headers=GH)
    assert r.status_code == 403 and pub["released"] == 1 and pub["finished"] is None


# ---------------- repos ----------------
def test_add_repo_quota_and_no_double_ingest(monkeypatch):
    monkeypatch.setattr(store, "get_repo_by_url", lambda u: None)
    monkeypatch.setattr(store, "count_recent_repos", lambda u: 999)
    r = client.post("/repos", json={"url": "https://github.com/o/r"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "index_quota_exceeded"

    ingested = []
    monkeypatch.setattr(main, "ingest_repo", lambda *a: ingested.append(a))
    monkeypatch.setattr(store, "get_repo_by_url", lambda u: {"id": "x"})       # already indexed: quota skipped
    monkeypatch.setattr(store, "get_or_create_repo",
                        lambda url, name, uid: ({"id": uuid.uuid4(), "url": url, "status": "ready"}, False))
    r = client.post("/repos", json={"url": "https://github.com/o/r"})
    assert r.status_code == 202 and ingested == []
