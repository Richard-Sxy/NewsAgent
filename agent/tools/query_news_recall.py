#!/usr/bin/env python3
"""Read a BGE snapshot through NewsAgent's native related-news search port.

Run from any directory::

    python agent/tools/query_news_recall.py --tenant-id TENANT --query '新闻主题'
    python agent/tools/query_news_recall.py --tenant-id TENANT --news-id NEWS_ID --related

This command performs read-only recall. Retrieval scores do not measure news heat.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys


AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT))

from app.analytics.entities import ContentType  # noqa: E402
from app.analytics.news_content import NewsContent  # noqa: E402
from app.clients.knowledge_base import RelatedNewsSearchQuery  # noqa: E402
from app.knowledge.search_config import load_knowledge_search_config  # noqa: E402
from app.knowledge.search_factory import (  # noqa: E402
    build_knowledge_search_index,
    close_knowledge_search_index,
)
from app.model_runtime.knowledge_factory import build_native_knowledge_search  # noqa: E402
from app.retrieval.related_news_reranker import RelatedNewsReranker  # noqa: E402


DEFAULT_CONFIG = AGENT_ROOT / "deploy" / "knowledge-search.bge-snapshot.yml"
QUERY_ID = "news-recall-cli"
QUERY_SOURCE_ID = "__query__:news-recall-cli"


def nonblank(value: str) -> str:
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("value cannot be blank")
    return value


def recall_limit(value: str) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= result <= 50:
        raise argparse.ArgumentTypeError("limit must be between 1 and 50")
    return result


def aware_publish_time(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("publish time must be an ISO datetime") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise argparse.ArgumentTypeError("publish time must include a UTC offset")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="BGE snapshot retrieval YAML")
    parser.add_argument("--tenant-id", required=True, type=nonblank)
    query_input = parser.add_mutually_exclusive_group(required=True)
    query_input.add_argument("--query", type=nonblank, help="News title or a semantic search question")
    query_input.add_argument("--news-id", type=nonblank, help="Use a snapshot article's title and summary")
    parser.add_argument("--source-news-id", type=nonblank, help="Exclude the source article from recall")
    parser.add_argument("--limit", type=recall_limit, default=10)
    parser.add_argument("--related", action="store_true", help="Apply the existing related-news reranker when source metadata is available")
    parser.add_argument("--publish-time", type=aware_publish_time, help="Source publication datetime including a UTC offset")
    return parser


def _metadata_publish_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return aware_publish_time(value)
    except argparse.ArgumentTypeError:
        return None


async def query_news(args: argparse.Namespace) -> dict:
    if args.news_id and args.source_news_id and args.news_id != args.source_news_id:
        raise ValueError("--source-news-id must match --news-id when both are supplied")
    config = load_knowledge_search_config(args.config)
    if config.backend != "bge_snapshot":
        raise ValueError("this command requires the bge_snapshot backend")
    if args.tenant_id != config.bge_snapshot.tenant_id:
        raise ValueError("requested tenant does not match the snapshot tenant")

    index = build_knowledge_search_index(config_path=args.config, postgres_store=None)
    if index is None:
        raise ValueError("snapshot search index is unavailable")
    try:
        article = None
        title = args.query
        summary = ""
        source_news_id = args.news_id or args.source_news_id
        if args.news_id:
            article = index.get_article(args.news_id, tenant_id=args.tenant_id)
            if article is None:
                raise ValueError("source article does not exist in the snapshot")
            title = str(article.get("title") or "").strip()
            summary = str(article.get("summary") or article.get("text") or "").strip()
            if not title:
                raise ValueError("source article has no usable title")
        publish_time = args.publish_time or _metadata_publish_time(
            article.get("publish_time") if article else None,
        )
        rerank_enabled = bool(args.related and source_news_id and publish_time)
        query = RelatedNewsSearchQuery(
            query_id=QUERY_ID,
            source_news_id=source_news_id or QUERY_SOURCE_ID,
            title=title,
            summary=summary,
            exclude_news_ids=(source_news_id,) if source_news_id else (),
            candidate_limit=50 if rerank_enabled else args.limit,
        )
        search = build_native_knowledge_search(
            config=None, embedding=None, database=None, index=index,
        )
        result = await search.batch_search_related_news((query,), tenant_id=args.tenant_id)
        candidates = result[query.query_id]

        def project(candidate) -> dict:
            return {
                "collection_id": candidate.collection_id,
                "news_id": candidate.news_id,
                "title": candidate.title,
                "text": candidate.text,
                "source_url": candidate.source_url,
                "publish_time": candidate.publish_time,
                "retrieval_score": candidate.score,
            }

        if rerank_enabled:
            source = NewsContent(
                news_id=source_news_id,
                title=title,
                summary=summary,
                content_type=ContentType.ARTICLE,
                publish_time=publish_time,
                source_url=str(article.get("source_url") or "") if article else "",
            )
            source.validate()
            ranked = RelatedNewsReranker().rerank(source, candidates, limit=args.limit)
            items = [
                {**project(item.news), "related_score": item.final_score, "reasons": list(item.reasons)}
                for item in ranked
            ]
        else:
            items = [project(candidate) for candidate in candidates[:args.limit]]

        payload = {
            "query": query.query_text,
            "source_news_id": source_news_id,
            "tenant_id": args.tenant_id,
            "snapshot_version": index.snapshot_version,
            "embedding_version": index.embedding_version,
            "mode": "related_news_rerank" if rerank_enabled else "semantic_recall",
            "score_semantics": "retrieval_score is normalized RRF rank fusion; it is not news hotness or raw vector similarity",
            "evidence_is_untrusted": True,
            "items": items,
        }
        if args.related and not rerank_enabled:
            payload["rerank_note"] = "Semantic recall only: related-news reranking needs a source news ID and a timezone-aware source publication datetime."
        return payload
    finally:
        await close_knowledge_search_index(index)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(query_news(args))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
