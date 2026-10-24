"""Synchronous crawler adapter for the Python NewsAgent knowledge API."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from zoneinfo import ZoneInfo

import httpx

from config import settings
from crawler.tencent_news import NewsArticle


class NewsAgentKnowledgeClient:
    def __init__(self) -> None:
        if not settings.newsagent_knowledge_token.strip():
            raise ValueError("NEWSAGENT_KNOWLEDGE_TOKEN is required")
        if not settings.newsagent_tenant_id.strip():
            raise ValueError("NEWSAGENT_TENANT_ID is required")
        self._base_url = settings.newsagent_base_url.rstrip("/")
        self._timeout = settings.newsagent_timeout
        self._headers = {
            "Authorization": f"Bearer {settings.newsagent_knowledge_token}",
            "X-Tenant-ID": settings.newsagent_tenant_id,
        }

    @staticmethod
    def _publish_time(value: str) -> str:
        if not value.strip():
            raise ValueError("news publish_time is required for versioned indexing")
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        return parsed.isoformat()

    def _document(
        self,
        article: NewsArticle,
        text: str,
        metadata: dict | None,
    ) -> dict:
        document_id = f"news-{article.news_id}"
        if len(document_id) > 160:
            document_id = f"news-{sha256(article.news_id.encode('utf-8')).hexdigest()}"
        return {
            "document_id": document_id,
            "news_id": article.news_id,
            "content_version": 1,
            "title": article.title,
            "text": text,
            "publish_time": self._publish_time(article.publish_time),
            "source_url": article.url,
            "media_type": "article",
            "text_source": "tencent-news-crawler",
            "metadata": {
                **{str(key): str(value) for key, value in (metadata or {}).items()},
                "source": "tencent_news",
                "author": article.author,
            },
        }

    def _post(self, path: str, payload: dict) -> dict:
        response = httpx.post(
            f"{self._base_url}/api/v1/knowledge/{path}",
            headers=self._headers,
            json=payload,
            timeout=self._timeout,
        )
        response.raise_for_status()
        body = response.json()
        outcomes = body.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            raise RuntimeError("NewsAgent knowledge API returned no index outcome")
        failures = [item for item in outcomes if item.get("status") == "failed"]
        if failures:
            raise RuntimeError(str(failures[0].get("detail") or "knowledge indexing failed"))
        if any(item.get("status") not in {"created", "updated", "skipped"} for item in outcomes):
            raise RuntimeError("NewsAgent knowledge API returned an unknown status")
        return {
            "documentId": outcomes[0]["document_id"],
            "status": outcomes[0]["status"],
            "detail": outcomes[0].get("detail"),
            "indexedCount": sum(
                item["status"] in {"created", "updated"} for item in outcomes
            ),
        }

    def index_news_document(
        self,
        article: NewsArticle,
        text: str,
        extra_metadata: dict | None = None,
    ) -> dict:
        return self._post(
            "documents",
            {"documents": [self._document(article, text, extra_metadata)]},
        )

    def index_news_qa(
        self,
        article: NewsArticle,
        text: str,
        extra_metadata: dict | None = None,
    ) -> dict:
        return self._post("qa", self._document(article, text, extra_metadata))
