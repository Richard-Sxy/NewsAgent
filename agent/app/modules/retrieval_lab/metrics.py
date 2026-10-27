"""Deterministic, explicitly pool-limited metrics for human relevance review."""
import math

LABEL_GRADES = {"relevant": 2, "partial": 1, "irrelevant": 0, "unsure": None}


def review_progress(candidates):
    total = len(candidates)
    judged = sum((c.get("judgment") or {}).get("status") == "judged" for c in candidates)
    unable = sum((c.get("judgment") or {}).get("status") == "unable" for c in candidates)
    return {
        "total": total,
        "judged": judged,
        "unable": unable,
        "reviewed": judged + unable,
        "remaining": total - judged - unable,
        "judged_coverage": judged / total if total else 0.0,
        "reviewed_coverage": (judged + unable) / total if total else 0.0,
    }


def calculate_metrics(candidates, k=10):
    """No missing/unsure label is ever treated as a negative.

    Precision uses strict relevance (grade 2). nDCG uses gains 2**grade-1
    and an ideal ordering restricted to this frozen candidate pool. It is
    available only after the whole pool is judged, never a corpus Recall.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    ordered = sorted(candidates, key=lambda c: c["rank"])
    top = ordered[:k]
    judged_top = [c for c in top if (c.get("judgment") or {}).get("status") == "judged"]
    relevant_top = sum(c["judgment"].get("label") == "relevant" for c in judged_top)
    partial_top = sum(c["judgment"].get("label") == "partial" for c in judged_top)
    top_complete = len(top) == k and len(judged_top) == k
    pool_complete = bool(ordered) and all((c.get("judgment") or {}).get("status") == "judged" for c in ordered)
    ndcg = None
    ndcg_reason = "candidate_pool_not_fully_judged"
    if len(ordered) < k:
        ndcg_reason = "fewer_than_k_candidates"
    elif pool_complete:
        grades = [LABEL_GRADES[c["judgment"]["label"]] for c in ordered]
        def dcg(values):
            return sum((2 ** grade - 1) / math.log2(index + 2) for index, grade in enumerate(values))
        ideal = dcg(sorted(grades, reverse=True)[:k])
        if ideal > 0:
            ndcg = dcg(grades[:k]) / ideal
            ndcg_reason = None
        else:
            ndcg_reason = "no_relevant_or_partial_document_in_pool"
    return {
        "scope": "single_human_review_of_frozen_candidate_pool",
        "k": k,
        "returned_at_k": len(top),
        "judged_at_k": len(judged_top),
        "relevant_at_k": relevant_top,
        "partial_at_k": partial_top,
        "judged_coverage_at_k": len(judged_top) / len(top) if top else 0.0,
        "precision_at_k": relevant_top / k if top_complete else None,
        "precision_eligible": top_complete,
        "precision_reason": None if top_complete else ("fewer_than_k_candidates" if len(top) < k else "top_k_not_fully_judged"),
        "precision_relevance_definition": "relevant_only_grade_2",
        "judged_precision_at_k": relevant_top / len(judged_top) if judged_top else None,
        "judged_precision_scope": "descriptive_only_among_judged_top_k_not_standard_precision",
        "pool_ndcg_at_k": ndcg,
        "pool_ndcg_reason": ndcg_reason,
        "pool_ndcg_scope": "ideal_ranking_of_this_frozen_pool_only",
        "pool_fully_judged": pool_complete,
        "recall_at_k": None,
        "recall_reason": "complete_corpus_relevance_gold_not_available",
        "accuracy": None,
        "accuracy_reason": "ranking_similarity_is_not_classification_accuracy",
        "progress": review_progress(ordered),
    }
