from app.evals import run as ev
from app.retrieval.hybrid import Hit, build_or_tsquery, fuse, hybrid_search
from app.retrieval.rrf import reciprocal_rank_fusion


def hit(i, path="f.py", symbol=None):
    return Hit(i, path, symbol, "function", 1, 5, "x")


def test_rrf_default_weights_unchanged():
    assert dict(reciprocal_rank_fusion([["a", "b"], ["b", "a"]])) == dict(reciprocal_rank_fusion([["a", "b"], ["b", "a"]], weights=[1.0, 1.0]))


def test_rrf_weight_zero_ignores_second_list_and_small_weight_demotes_keyword_only_hits():
    zero = reciprocal_rank_fusion([["a", "b"], ["c", "b"]], weights=[1.0, 0.0])
    assert [i for i, _ in zero][:2] == ["a", "b"] and zero[-1][1] == 0.0
    equal = [i for i, _ in reciprocal_rank_fusion([["a", "b"], ["c"]])]
    assert equal.index("c") < equal.index("b")     # equal weights: a keyword-only rank-1 hit outranks vector rank 2
    light = [i for i, _ in reciprocal_rank_fusion([["a", "b"], ["c"]], weights=[1.0, 0.1])]
    assert light.index("b") < light.index("c")     # weak keyword list: it no longer displaces the vector hits


def test_prefix_tsquery_stems_plain_terms_only():
    q = build_or_tsquery("how are passwords hashed and validated", prefix=True)
    assert "password:*" in q and "hash:*" in q and "validat:*" in q
    assert build_or_tsquery("how are passwords hashed", prefix=False) == "passwords | hashed"
    assert "log_expense" in build_or_tsquery("what does log_expense do", prefix=True)   # identifiers keep their form
    assert build_or_tsquery("the", prefix=True) == ""


def test_fuse_weight_zero_keeps_vector_order():
    vec = [hit(1, "a.py"), hit(2, "b.py"), hit(3, "c.py")]
    kw = [hit(3, "c.py"), hit(9, "z.py")]
    assert [h.id for h in fuse(vec, kw, "q", kw_weight=0.0)][:3] == [1, 2, 3]
    assert {h.id for h in fuse(vec, kw, "q", kw_weight=1.0)} == {1, 2, 3, 9}    # default path keeps every candidate


def test_hybrid_search_uses_settings_defaults(monkeypatch):
    from app import config
    calls = {}
    monkeypatch.setattr("app.retrieval.hybrid.vector_search", lambda *a, **k: [hit(1)])
    monkeypatch.setattr("app.retrieval.hybrid.keyword_search", lambda conn, rid, q, lim, prefix=False: calls.setdefault("prefix", prefix) and [] or [])

    class Emb:
        def embed_query(self, q): return [0.0]

    monkeypatch.setenv("KEYWORD_PREFIX_MATCH", "true")
    config.get_settings.cache_clear()
    try:
        hybrid_search(None, "r", "q", Emb(), k=3)
    finally:
        config.get_settings.cache_clear()
    assert calls["prefix"] is True


def test_split_and_scoring_and_selection():
    qs = [{"id": f"q{i}"} for i in range(5)]
    tune, test = ev.split_questions(qs)
    assert [q["id"] for q in tune] == ["q0", "q1", "q2"] and [q["id"] for q in test] == ["q3", "q4"]
    s = ev.score_rankings([(["a", "b"], ["b"]), (["x"], ["y"])])
    assert s["recall@1"] == 0.0 and s["recall@3"] == 0.5 and s["mrr"] == 0.25
    mk = lambda w, p, r5, mrr, t5=0.0: {"kw_weight": w, "prefix": p, "tune": {"recall@5": r5, "mrr": mrr},
                                         "test": {"recall@5": t5, "mrr": 0.0}, "all": {}}
    best = ev.pick_best([mk(1.0, False, 0.8, 0.6), mk(0.25, True, 0.9, 0.8, t5=0.1), mk(0.1, True, 0.9, 0.8, t5=0.0)])
    assert (best["kw_weight"], best["prefix"]) == (0.1, True)      # tie on tune -> smaller weight; test numbers never decide


def test_sweep_markdown_marks_selection():
    v = {"kw_weight": 0.25, "prefix": True, "tune": {"recall@5": 1.0, "mrr": 0.9}, "test": {"recall@5": 0.9, "mrr": 0.8}, "all": {"recall@5": 0.95, "mrr": 0.85}}
    sw = {"n_tune": 2, "n_test": 2, "vector_baseline": v, "variants": [v], "selected": v}
    md = ev.sweep_markdown(sw)
    assert "selected on tune set" in md and "vector only (baseline)" in md and "hybrid kw_weight=0.25 prefix=on" in md
