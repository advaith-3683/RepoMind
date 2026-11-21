"""Hybrid retrieval: pgvector cosine + Postgres full-text, merged with RRF,
then a light symbol-aware rerank. Pure helpers are unit-tested; the SQL paths
need a live database."""
import re
from dataclasses import dataclass

from .rrf import reciprocal_rank_fusion

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "how", "what", "where", "when", "why", "which",
    "does", "do", "did", "this", "that", "these", "those", "in", "on", "of", "to", "for", "and",
    "or", "with", "from", "by", "it", "its", "be", "can", "i", "me", "my", "we", "our", "you",
    "your", "code", "repo", "repository", "function", "file", "work", "works", "used", "use",
}


@dataclass
class Hit:
    id: int
    path: str
    symbol: str | None
    kind: str | None
    start_line: int
    end_line: int
    content: str
    score: float = 0.0
    sources: tuple = ()


def query_terms(question: str) -> list[str]:
    """Identifier-ish terms for keyword search (alphanumeric only => safe for tsquery)."""
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", question)
    terms: list[str] = []
    for t in raw:
        parts = [t] + [p for p in re.split(r"_", t) if len(p) >= 3]   # snake_case parts
        parts += [p for p in re.findall(r"[A-Z]?[a-z]{3,}|[A-Z]{3,}", t)]  # camelCase parts
        for p in parts:
            pl = p.lower()
            if pl not in _STOPWORDS and pl not in terms:
                terms.append(pl)
    return terms[:12]


def build_or_tsquery(question: str) -> str:
    """'login user' -> 'login | user' (to_tsquery syntax). Empty string if nothing usable."""
    return " | ".join(query_terms(question))


def symbol_boost_rerank(question: str, hits: list[Hit]) -> list[Hit]:
    """Boost chunks whose symbol name or file name appears in the question."""
    terms = set(query_terms(question))
    for h in hits:
        boost = 0.0
        if h.symbol:
            sym = h.symbol.split("#")[0].split(".")[-1].lower()
            if sym in terms:
                boost += 0.010
        stem = h.path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
        if stem in terms:
            boost += 0.004
        h.score += boost
    return sorted(hits, key=lambda h: h.score, reverse=True)


def vec_literal(values: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in values) + "]"


_COLS = "id, path, symbol, kind, start_line, end_line, content"


def _row_to_hit(r: dict) -> Hit:
    return Hit(r["id"], r["path"], r["symbol"], r["kind"], r["start_line"], r["end_line"], r["content"])


def vector_search(conn, repo_id, query_vec: list[float], limit: int) -> list[Hit]:
    rows = conn.execute(
        f"SELECT {_COLS} FROM chunks WHERE repo_id = %s ORDER BY embedding <=> %s::vector LIMIT %s",
        (repo_id, vec_literal(query_vec), limit),
    ).fetchall()
    return [_row_to_hit(r) for r in rows]


def keyword_search(conn, repo_id, question: str, limit: int) -> list[Hit]:
    q = build_or_tsquery(question)
    if not q:
        return []
    rows = conn.execute(
        f"SELECT {_COLS} FROM chunks WHERE repo_id = %s AND tsv @@ to_tsquery('simple', %s) "
        f"ORDER BY ts_rank_cd(tsv, to_tsquery('simple', %s)) DESC LIMIT %s",
        (repo_id, q, q, limit),
    ).fetchall()
    return [_row_to_hit(r) for r in rows]


def find_symbols(conn, repo_id, name: str, limit: int = 15) -> list[Hit]:
    like = "%" + re.sub(r"[^\w.]", "", name) + "%"
    rows = conn.execute(
        f"SELECT {_COLS} FROM chunks WHERE repo_id = %s AND symbol ILIKE %s ORDER BY path, start_line LIMIT %s",
        (repo_id, like, limit),
    ).fetchall()
    return [_row_to_hit(r) for r in rows]


def hybrid_search(conn, repo_id, question: str, embedder, k: int = 5, pool: int = 30,
                  mode: str = "hybrid") -> list[Hit]:
    """mode: 'vector' (baseline), 'keyword', or 'hybrid' (vector + keyword + RRF + rerank)."""
    vec_hits = keyword_hits = []
    if mode in ("vector", "hybrid"):
        vec_hits = vector_search(conn, repo_id, embedder.embed_query(question), pool)
    if mode in ("keyword", "hybrid"):
        keyword_hits = keyword_search(conn, repo_id, question, pool)

    by_id = {h.id: h for h in vec_hits + keyword_hits}
    if mode == "vector":
        return vec_hits[:k]
    if mode == "keyword":
        return keyword_hits[:k]

    fused = reciprocal_rank_fusion([[h.id for h in vec_hits], [h.id for h in keyword_hits]])
    vec_ids = {h.id for h in vec_hits}
    kw_ids = {h.id for h in keyword_hits}
    hits = []
    for cid, score in fused:
        h = by_id[cid]
        h.score = score
        h.sources = tuple(s for s, ids in (("vector", vec_ids), ("keyword", kw_ids)) if cid in ids)
        hits.append(h)
    return symbol_boost_rerank(question, hits)[:k]
