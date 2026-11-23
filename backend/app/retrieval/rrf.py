def reciprocal_rank_fusion(rankings: list[list], k: int = 60, weights: list[float] | None = None) -> list[tuple]:
    """Merge several ranked id lists. score(id) = sum over lists of weight/(k+rank).

    weights (default all 1.0) lets a weak list count for less. Returns [(id, score)] best-first. Rank is 1-based."""
    weights = weights or [1.0] * len(rankings)
    scores: dict = {}
    for ranking, w in zip(rankings, weights):
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + w / (k + rank)
    return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
