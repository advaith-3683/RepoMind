import pytest

from app.config import Settings
from app.evals import metrics
from app.guardrails.errors import GuardrailViolation
from app.ingestion.chunker import MAX_CHUNK_LINES, chunk_file, chunk_windows
from app.ingestion.clone import parse_github_url
from app.ingestion.pipeline import build_chunks, iter_repo_files
from app.retrieval.hybrid import Hit, build_or_tsquery, query_terms, symbol_boost_rerank, vec_literal
from app.retrieval.rrf import reciprocal_rank_fusion


# ---------- chunker ----------
PY = '''import os

X = 1
Y = 2

@decorator
def alpha(a):
    return a


class Foo:
    def bar(self):
        return 1
'''


def test_python_chunks_have_exact_boundaries():
    chunks = chunk_file("m.py", PY)
    by_sym = {c.symbol: c for c in chunks if c.symbol}
    assert by_sym["alpha"].start_line == 6 and by_sym["alpha"].end_line == 8   # includes decorator
    assert by_sym["Foo"].kind == "class" and by_sym["Foo"].start_line == 11
    mods = [c for c in chunks if c.kind == "module"]
    assert mods and "import os" in mods[0].content
    for c in chunks:
        assert c.content.splitlines()[0] == PY.splitlines()[c.start_line - 1]


def test_large_python_class_split_into_methods():
    body = "".join(f"    def m{i}(self):\n" + "        x = 1\n" * 3 for i in range(40))
    chunks = chunk_file("big.py", "class Big:\n    attr = 1\n" + body)
    syms = {c.symbol for c in chunks}
    assert "Big" in syms and "Big.m0" in syms and "Big.m39" in syms
    assert all(c.end_line - c.start_line + 1 <= MAX_CHUNK_LINES for c in chunks)


def test_python_syntax_error_falls_back_to_windows():
    chunks = chunk_file("bad.py", "def (:\n" * 5)
    assert chunks and all(c.kind == "text" for c in chunks)


def test_js_regex_chunker():
    src = "import x from 'x'\nimport y from 'y'\n\nexport function foo() {\n  return 1\n}\n\nclass Bar {\n  m() {}\n}\n\nexport const baz = async (a) => {\n  return a\n}\n"
    syms = {c.symbol for c in chunk_file("a.js", src) if c.symbol}
    assert syms == {"foo", "Bar", "baz"}


def test_windows_overlap_and_coverage():
    lines = [f"l{i}" for i in range(1, 151)]
    ch = chunk_windows("x.md", "markdown", lines, window=60, overlap=10)
    assert ch[0].start_line == 1 and ch[0].end_line == 60
    assert ch[1].start_line == 51
    assert ch[-1].end_line == 150


def test_unknown_extension_skipped():
    assert chunk_file("image.png", "binary") == []


def test_ingestion_skips_secrets_and_redacts(repo):
    paths = [p for p, _ in iter_repo_files(repo, 200)]
    assert ".env" not in paths and "app/auth.py" in paths
    chunks, files = build_chunks(repo, Settings())
    joined = "\n".join(c.content for c in chunks)
    assert "hunter2hunter2" not in joined and "supersecretvalue123" not in joined


def test_chunk_cap_enforced(repo):
    with pytest.raises(GuardrailViolation) as e:
        build_chunks(repo, Settings(max_chunks_per_repo=1))
    assert e.value.code == "repo_too_large"


# ---------- clone url ----------
@pytest.mark.parametrize("url", [
    "https://github.com/octocat/Hello-World", "https://github.com/octocat/Hello-World.git",
    "https://github.com/octocat/Hello-World/",
])
def test_github_url_ok(url):
    assert parse_github_url(url) == ("octocat", "Hello-World")


@pytest.mark.parametrize("url", [
    "http://github.com/a/b", "https://gitlab.com/a/b", "git@github.com:a/b.git", "file:///etc/passwd",
    "https://github.com/a/b/../../c", "https://user:pw@github.com/a/b", "https://github.com/a", "",
    "https://github.com/a/b; rm -rf /",
])
def test_github_url_rejected(url):
    with pytest.raises(GuardrailViolation):
        parse_github_url(url)


# ---------- retrieval ----------
def test_rrf_prefers_items_ranked_well_in_both_lists():
    fused = dict(reciprocal_rank_fusion([["a", "b", "c"], ["c", "a", "d"]]))
    order = sorted(fused, key=fused.get, reverse=True)
    assert order[0] == "a" and set(order[:2]) == {"a", "c"} and order[-1] == "d"
    assert fused["a"] == pytest.approx(1 / 61 + 1 / 62)


def test_query_terms_and_tsquery():
    assert "login" in query_terms("Where is the login handled?")
    assert "the" not in query_terms("Where is the login handled?")
    t = query_terms("how does get_user_token work")
    assert "get_user_token" in t and "user" in t and "token" in t
    assert "make" in query_terms("makeToken") and "token" in [x.lower() for x in query_terms("makeToken")]
    assert build_or_tsquery("login user") == "login | user"
    assert build_or_tsquery("?? !!") == ""
    assert all(c.isalnum() or c in "_| " for c in build_or_tsquery("a'; DROP TABLE x;--  login"))


def test_symbol_boost_rerank():
    a = Hit(1, "app/misc.py", "helper", "function", 1, 5, "", score=0.0160)
    b = Hit(2, "app/auth.py", "login", "function", 1, 5, "", score=0.0150)
    out = symbol_boost_rerank("where is login?", [a, b])
    assert out[0].id == 2


def test_vec_literal():
    assert vec_literal([0.5, -1.0]) == "[0.500000,-1.000000]"


# ---------- eval metrics ----------
def test_metrics():
    assert metrics.recall_at_k(["a", "b", "c"], ["a", "c"], 2) == 0.5
    assert metrics.recall_at_k(["a"], [], 3) == 1.0
    assert metrics.reciprocal_rank(["x", "a"], ["a"]) == 0.5
    assert metrics.reciprocal_rank(["x"], ["a"]) == 0.0
    assert metrics.keyword_hit_rate("Uses a JWT token", ["jwt", "login"]) == 0.5
    assert metrics.percentile([1, 2, 3, 4, 100], 50) == 3
