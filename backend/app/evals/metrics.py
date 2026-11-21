"""Pure eval metrics (unit-tested)."""


def recall_at_k(retrieved_paths: list[str], expected_paths: list[str], k: int) -> float:
    """Fraction of expected files that appear in the top-k retrieved paths."""
    if not expected_paths:
        return 1.0
    top = set(retrieved_paths[:k])
    return sum(1 for p in set(expected_paths) if p in top) / len(set(expected_paths))


def reciprocal_rank(retrieved_paths: list[str], expected_paths: list[str]) -> float:
    exp = set(expected_paths)
    for i, p in enumerate(retrieved_paths, 1):
        if p in exp:
            return 1.0 / i
    return 0.0


def keyword_hit_rate(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    a = answer.lower()
    return sum(1 for k in keywords if k.lower() in a) / len(keywords)


def mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    idx = min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))
    return s[idx]
