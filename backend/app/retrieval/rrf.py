def reciprocal_rank_fusion(rankings: list[list], k: int = 60) -> list[tuple]:
    """Merge several ranked id lists. score(id) = sum over lists of 1/(k+rank).

    Returns [(id, score)] sorted best-first. Rank is 1-based."""
    scores: dict = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
