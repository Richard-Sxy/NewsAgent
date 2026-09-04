from __future__ import annotations

from intelligence.schemas import EventDataset, EventPairAnnotation

"""新闻聚类集群"""
def article_cluster_map(dataset: EventDataset) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for cluster in dataset.clusters:
        for member in cluster.members:
            mapping[member.article_id] = cluster.event_id
    return mapping

""""""
def evaluate_pairs(
    dataset: EventDataset,
    annotations: list[EventPairAnnotation],
) -> dict[str, float | int]:
    """以 same_event 为正类计算事件对分类指标。"""
    mapping = article_cluster_map(dataset)
    true_positive = false_positive = false_negative = true_negative = 0
    missing = 0
    for pair in annotations:
        if pair.left_article_id not in mapping or pair.right_article_id not in mapping:
            missing += 1
            continue
        predicted = mapping[pair.left_article_id] == mapping[pair.right_article_id]
        expected = pair.relation == "same_event"
        if predicted and expected:
            true_positive += 1
        elif predicted and not expected:
            false_positive += 1
        elif not predicted and expected:
            false_negative += 1
        else:
            true_negative += 1
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    evaluated = true_positive + false_positive + false_negative + true_negative
    accuracy = (true_positive + true_negative) / evaluated if evaluated else 0.0
    return {
        "annotations": len(annotations),
        "evaluated": evaluated,
        "missing": missing,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "accuracy": round(accuracy, 6),
    }
