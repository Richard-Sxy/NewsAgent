from datetime import datetime
from zoneinfo import ZoneInfo

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent
from app.clients.knowledge_base import RelatedNews
from app.retrieval.related_news_reranker import RelatedNewsReranker


def build_source() -> NewsContent:
    return NewsContent(
        news_id="huawei-current",
        title="华为上半年营收4678亿元，净利润234亿元",
        summary="华为发布2026年上半年经营业绩。",
        body="华为投资控股有限公司公布半年报。",
        content_type=ContentType.ARTICLE,
        publish_time=datetime(
            2026, 8, 31, 12, 0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        ),
        source_url="https://news.example.com/huawei-current",
    )


def build_candidate(
    news_id: str,
    title: str,
    score: float,
) -> RelatedNews:
    return RelatedNews(
        collection_id=f"collection-{news_id}",
        news_id=news_id,
        title=title,
        text=title,
        source_url=f"https://news.example.com/{news_id}",
        publish_time="2026-08-30T12:00:00+08:00",
        score=score,
    )


def test_same_company_financial_report_ranks_first() -> None:
    source = build_source()

    candidates = [
        build_candidate(
            "byd-report",
            "比亚迪上半年营收利润双降",
            0.82,
        ),
        build_candidate(
            "huawei-report",
            "华为公布上半年经营业绩",
            0.75,
        ),
    ]

    results = RelatedNewsReranker().rerank(
        source,
        candidates,
        limit=3,
    )

    assert results[0].news.news_id == "huawei-report"
    assert "关键主体一致" in results[0].reasons


def test_unrelated_company_is_filtered_or_ranked_lower() -> None:
    source = build_source()

    candidates = [
        build_candidate(
            "gold-news",
            "国际金价突破4600美元",
            0.58,
        ),
        build_candidate(
            "huawei-history",
            "华为去年营收与净利润公布",
            0.55,
        ),
    ]

    results = RelatedNewsReranker().rerank(
        source,
        candidates,
    )

    assert results
    assert results[0].news.news_id == "huawei-history"


def test_candidate_without_same_title_entity_is_filtered() -> None:
    source = build_source()
    candidate = RelatedNews(
        collection_id="collection-gold-news",
        news_id="gold-news",
        title="国际金价站上4600美元",
        # 模拟知识库切片中出现财报词，使事件类型被识别为一致。
        text="某机构发布报告，提到企业营收同比变化。",
        source_url="https://news.example.com/gold-news",
        publish_time="2026-08-24T12:00:00+08:00",
        score=0.5817,
    )

    results = RelatedNewsReranker().rerank(source, [candidate])

    assert results == []
