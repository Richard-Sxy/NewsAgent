import json

import pytest

from intelligence.dataset import load_cached_articles
from intelligence.evaluation import evaluate_pairs
from intelligence.event_clustering import EventClusterer, SimilarityWeights, tokenize
from intelligence.schemas import EventArticle, EventPairAnnotation


def article(article_id: str, title: str, content: str, publish_time: str) -> EventArticle:
    return EventArticle(
        article_id=article_id,
        url=f"https://news.qq.com/rain/a/{article_id}",
        title=title,
        content=content,
        publish_time=publish_time,
    )


def test_tokenize_includes_chinese_bigrams_and_ascii_words():
    tokens = tokenize("英伟达发布 Blackwell GPU 2026")
    assert "英伟" in tokens
    assert "blackwell" in tokens
    assert "gpu" in tokens
    assert "2026" in tokens


def test_cluster_groups_same_event_and_keeps_unrelated_article_separate():
    rows = [
        article("A1", "国家发改委回应机器人产业发展", "机器人产业需要健康有序发展", "2026-08-28 10:00:00"),
        article("A2", "发改委：机器人产业要防止一哄而上", "国家发改委表示机器人产业应有序发展", "2026-08-28 11:00:00"),
        article("A3", "欧冠决赛球队公布首发名单", "足球比赛将在今晚举行", "2026-08-28 12:00:00"),
    ]
    dataset = EventClusterer(threshold=0.42).cluster(rows)
    sizes = sorted(cluster.article_count for cluster in dataset.clusters)
    assert sizes == [1, 2]
    assert dataset.multi_article_cluster_count == 1


def test_cluster_respects_time_window():
    rows = [
        article("A1", "公司发布新一代人工智能芯片", "公司发布AI芯片", "2026-08-01 10:00:00"),
        article("A2", "公司发布新一代人工智能芯片", "公司发布AI芯片", "2026-08-20 10:00:00"),
    ]
    dataset = EventClusterer(threshold=0.1, max_time_gap_days=3).cluster(rows)
    assert dataset.cluster_count == 2


def test_invalid_weights_are_rejected():
    with pytest.raises(ValueError, match="权重之和"):
        EventClusterer(weights=SimilarityWeights(title=0.5, content=0.5, time=0.5))


def test_load_cached_articles_skips_invalid_files(tmp_path):
    (tmp_path / "bad.json").write_text("not-json", encoding="utf-8")
    (tmp_path / "empty.json").write_text(json.dumps({"url": "u"}), encoding="utf-8")
    (tmp_path / "good.json").write_text(json.dumps({
        "url": "https://news.qq.com/rain/a/A1",
        "title": "测试新闻",
        "content": "有效正文",
        "publish_time": "2026-08-28 10:00:00",
    }), encoding="utf-8")
    rows = load_cached_articles(tmp_path)
    assert len(rows) == 1
    assert rows[0].article_id == "A1"


def test_evaluate_pairs_reports_pairwise_metrics():
    rows = [
        article("A1", "机器人产业需要健康发展", "发改委回应机器人产业", "2026-08-28 10:00:00"),
        article("A2", "发改委谈机器人产业发展", "机器人产业避免一哄而上", "2026-08-28 11:00:00"),
        article("A3", "足球比赛公布首发名单", "球队今晚进行比赛", "2026-08-28 12:00:00"),
    ]
    dataset = EventClusterer(threshold=0.35).cluster(rows)
    metrics = evaluate_pairs(dataset, [
        EventPairAnnotation(left_article_id="A1", right_article_id="A2", relation="same_event"),
        EventPairAnnotation(left_article_id="A1", right_article_id="A3", relation="unrelated"),
    ])
    assert metrics["precision"] == 1.0
    assert metrics["recall"] == 1.0
    assert metrics["f1"] == 1.0
