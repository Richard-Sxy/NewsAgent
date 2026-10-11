"""Dependency-free, news-level retrieval metrics for offline experiments.

Callers supply ranked ``news_id`` values and independently annotated positive
relevance grades. Chunk duplicates count once, in their first-ranked position.
Missing or out-of-corpus labels must be audited by the caller before evaluation;
this module never silently drops a label or changes the recall denominator.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping

METRIC_NAMES = ("hit_rate", "recall", "precision", "mrr", "ndcg")
MetricsByK = dict[int, dict[str, float]]


def _positive_k(k: int) -> int:
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError("K must be a positive integer")
    return k


def _validated_ks(ks: Iterable[int]) -> list[int]:
    out = list(dict.fromkeys(_positive_k(k) for k in ks))
    if not out:
        raise ValueError("at least one positive K is required")
    return out


def _news_id(news_id: str) -> str:
    if not isinstance(news_id, str) or not news_id.strip():
        raise ValueError("news_id must be a nonempty string")
    return news_id


def unique_ranked(ids: Iterable[str], k: int) -> list[str]:
    """Return at most K unique news IDs, preserving the first occurrence.

    This operates on the complete candidate stream supplied by the caller; it
    cannot replenish candidates already truncated by a chunk-level search.
    """
    _positive_k(k)
    ranked: list[str] = []
    seen: set[str] = set()
    for news_id in ids:
        news_id = _news_id(news_id)
        if news_id not in seen:
            seen.add(news_id)
            ranked.append(news_id)
            if len(ranked) == k:
                break
    return ranked


def _positive_grades(relevance: Mapping[str, float]) -> dict[str, float]:
    if not relevance:
        raise ValueError("relevance must contain at least one positive label")
    grades: dict[str, float] = {}
    for news_id, raw_grade in relevance.items():
        _news_id(news_id)
        if isinstance(raw_grade, bool) or not isinstance(raw_grade, (int, float)):
            raise ValueError("relevance grades must be finite positive numbers")
        try:
            grade = float(raw_grade)
        except OverflowError as exc:
            raise ValueError("relevance grades must be finite positive numbers") from exc
        if not math.isfinite(grade) or grade <= 0:
            raise ValueError("relevance grades must be finite positive numbers")
        grades[news_id] = grade
    return grades


def _scaled_gain(grade: float, max_grade: float) -> float:
    # (2**grade - 1) / 2**max_grade, expressed without overflow or loss of
    # tiny positive grades. The common scaling cancels in DCG / ideal DCG.
    return math.exp((grade - max_grade) * math.log(2)) * -math.expm1(-grade * math.log(2))


def query_metrics(ranked_ids: Iterable[str], relevance: Mapping[str, float],
                  ks: Iterable[int]) -> MetricsByK:
    """Compute article-level HitRate, Recall, Precision, MRR and graded nDCG.

    Returns ``{K: {metric_name: value}}``. Precision uses K as its denominator,
    even when fewer results are returned. MRR is truncated at each K. nDCG uses
    gain ``2**grade - 1`` and log2(rank + 1) discount. Empty ranked results yield
    zeros, while missing labels and empty K lists raise ValueError.
    """
    cutoffs = _validated_ks(ks)
    grades = _positive_grades(relevance)
    ranked = unique_ranked(ranked_ids, max(cutoffs))
    max_grade = max(grades.values())
    ideal_gains = sorted((_scaled_gain(g, max_grade) for g in grades.values()), reverse=True)
    first_relevant = next((rank for rank, nid in enumerate(ranked, 1) if nid in grades), None)
    result: MetricsByK = {}
    for k in cutoffs:
        selected = ranked[:k]
        matched = sum(nid in grades for nid in selected)
        dcg = math.fsum(
            _scaled_gain(grades[nid], max_grade) / math.log2(rank + 1)
            for rank, nid in enumerate(selected, 1) if nid in grades
        )
        ideal_dcg = math.fsum(gain / math.log2(rank + 1)
                              for rank, gain in enumerate(ideal_gains[:k], 1))
        result[k] = {
            "hit_rate": float(matched > 0),
            "recall": matched / len(grades),
            "precision": matched / k,
            "mrr": 1.0 / first_relevant if first_relevant is not None and first_relevant <= k else 0.0,
            "ndcg": dcg / ideal_dcg,
        }
    return result


def aggregate_metrics(rows: Iterable[Mapping[int, Mapping[str, float]]],
                      ks: Iterable[int]) -> MetricsByK:
    """Macro-average query_metrics rows; reject empty or malformed samples."""
    cutoffs = _validated_ks(ks)
    collected = list(rows)
    if not collected:
        raise ValueError("cannot aggregate an empty question set")
    values: dict[int, dict[str, list[float]]] = {
        k: {name: [] for name in METRIC_NAMES} for k in cutoffs
    }
    for row in collected:
        for k in cutoffs:
            if k not in row:
                raise ValueError(f"metric row is missing K={k}")
            for name in METRIC_NAMES:
                value = row[k].get(name)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"metric row is missing or invalid: {name}@{k}")
                if not math.isfinite(value) or value < 0 or value > 1:
                    raise ValueError(f"metric must be finite and in [0, 1]: {name}@{k}")
                values[k][name].append(float(value))
    return {k: {name: math.fsum(numbers) / len(collected)
                for name, numbers in by_name.items()} for k, by_name in values.items()}


def ann_overlap(approximate_ids: Iterable[str], exact_ids: Iterable[str], k: int) -> float:
    """Return news-level ANN agreement with exact top K, separately from labels.

    The denominator is the number of unique IDs in the exact reference, up to
    K. An empty approximate result has agreement 0; an empty reference is an
    invalid benchmark and raises ValueError.
    """
    exact = unique_ranked(exact_ids, k)
    approximate = unique_ranked(approximate_ids, k)
    if not exact:
        raise ValueError("exact reference must contain at least one news_id")
    return len(set(approximate) & set(exact)) / len(exact)
