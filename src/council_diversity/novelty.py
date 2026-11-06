"""Best-match similarity, noise floors, novelty and bootstrap intervals.

Pure numerics over unit vectors: no text comes in or goes out.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import numpy as np

SEED = 20261105
RESAMPLES = 10_000
FLOOR_PERCENTILE = 5.0


def best_match(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """For each row of `a` (unit vectors), its highest cosine similarity to any
    row of `b`, and that row's index."""
    s = a @ b.T
    idx = s.argmax(axis=1)
    return s[np.arange(len(a)), idx], idx


def floor(similarities: Sequence[float], pct: float = FLOOR_PERCENTILE) -> float:
    return float(np.percentile(np.asarray(similarities, dtype=float), pct))


def bootstrap_share(
    units: Sequence[tuple[int, int]],
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> tuple[float, float, float]:
    """Pooled share sum(novel)/sum(total) over units (novel, total), with a
    95% percentile interval from resampling the units with replacement."""
    arr = np.asarray(units, dtype=float).reshape(-1, 2)
    if len(arr) == 0 or arr[:, 1].sum() == 0:
        return float("nan"), float("nan"), float("nan")
    point = arr[:, 0].sum() / arr[:, 1].sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(arr), size=(resamples, len(arr)))
    num = arr[idx, 0].sum(axis=1)
    den = arr[idx, 1].sum(axis=1)
    shares = num / np.where(den == 0, np.nan, den)
    lo, hi = np.nanpercentile(shares, [2.5, 97.5])
    return float(point), float(lo), float(hi)


def bootstrap_diff(
    a: Sequence[tuple[int, int]],
    b: Sequence[tuple[int, int]],
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> tuple[float, float, float]:
    """share(a) - share(b), each resampled independently over its own units."""
    A = np.asarray(a, dtype=float).reshape(-1, 2)
    B = np.asarray(b, dtype=float).reshape(-1, 2)
    if len(A) == 0 or len(B) == 0:
        return float("nan"), float("nan"), float("nan")
    point = A[:, 0].sum() / A[:, 1].sum() - B[:, 0].sum() / B[:, 1].sum()
    rng = np.random.default_rng(seed)
    ia = rng.integers(0, len(A), size=(resamples, len(A)))
    ib = rng.integers(0, len(B), size=(resamples, len(B)))
    d = A[ia, 0].sum(1) / A[ia, 1].sum(1) - B[ib, 0].sum(1) / B[ib, 1].sum(1)
    lo, hi = np.percentile(d, [2.5, 97.5])
    return float(point), float(lo), float(hi)


def bootstrap_clusters(
    clusters: dict[str, list[tuple[int, int]]],
    stat: Callable[[list[tuple[int, int]]], float] | None = None,
    resamples: int = RESAMPLES,
    seed: int = SEED,
) -> tuple[float, float, float]:
    """Pooled share, resampling whole clusters (items) with replacement."""
    keys = sorted(clusters)
    tot = np.array(
        [[sum(n for n, _ in clusters[k]), sum(t for _, t in clusters[k])] for k in keys],
        dtype=float,
    )
    if len(tot) == 0 or tot[:, 1].sum() == 0:
        return float("nan"), float("nan"), float("nan")
    point = tot[:, 0].sum() / tot[:, 1].sum()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(tot), size=(resamples, len(tot)))
    shares = tot[idx, 0].sum(1) / tot[idx, 1].sum(1)
    lo, hi = np.percentile(shares, [2.5, 97.5])
    return float(point), float(lo), float(hi)


def auroc(scores: Sequence[float], positive: Sequence[bool]) -> float:
    """Probability a random positive outranks a random negative (ties 1/2)."""
    s = np.asarray(scores, dtype=float)
    y = np.asarray(positive, dtype=bool)
    pos, neg = s[y], s[~y]
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    order = np.argsort(np.concatenate([pos, neg]), kind="mergesort")
    allv = np.concatenate([pos, neg])[order]
    ranks = np.empty(len(allv))
    # average ranks for ties
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1] == allv[i]:
            j += 1
        ranks[i : j + 1] = (i + j) / 2 + 1
        i = j + 1
    r = np.empty(len(allv))
    r[order] = ranks
    rp = r[: len(pos)].sum()
    return float((rp - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))
