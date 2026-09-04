import httpx
import hashlib
import re
from pathlib import Path
from urllib.parse import urlparse

from config import settings
from crawler.tencent_news import NewsArticle

""" 根据新闻名称生成 Collection名称 """
def build_collection_name(article: NewsArticle) -> str:
    article_id = urlparse(article.url).path.rstrip("/").split("/")[-1]
    safe_article_id = re.sub(r"[^A-Za-z0-9_-]", "-", article_id).strip("-_")

    if not safe_article_id:
        safe_article_id = hashlib.sha256(
            article.url.encode("utf-8")
        ).hexdigest()[:16]

    return f"tencent-news-{safe_article_id[:64]}"

""" Fast客户端 帮助你入库 """
class FastGPTClient:

    """初始化 url/api/databaseId """
    def __init__(self):
        self.base_url = settings.fastgpt_base_url.rstrip("/")
        self.api_key = settings.fastgpt_api_key
        self.dataset_id = settings.fastgpt_dataset_id
    
    """ 创建一个新的collection """
    def create_news_collection(
        self,
        article: NewsArticle,
        text: str,          # 这边是格式化后的新闻全文
        extra_metadata: dict | None = None,
    ) -> dict:
        url = (
            f"{self.base_url}"
            "/api/core/dataset/collection/create/text"
        )

        metadata = {
            **(extra_metadata or {}),
            "source": "tencent_news",
            "news_id": article.news_id,
            "source_url": article.url,
            "original_title": article.title,
            "author": article.author,
            "publish_time": article.publish_time,
        }

        response = httpx.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "datasetId": self.dataset_id,
                "name": build_collection_name(article),
                "text": text,
                "trainingType": "chunk",
                # FastGPT 4.15+ only accepts "auto" or "custom" here.
                # The former "news" preset is no longer part of the API schema.
                "chunkSettingMode": "auto",
                "dataEnhanceCollectionName": True,
                "metadata": metadata,
            },
            timeout=settings.fastgpt_timeout,
        )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = response.text.strip()
            if len(detail) > 2000:
                detail = f"{detail[:2000]}..."

            raise RuntimeError(
                f"FastGPT HTTP {response.status_code}: "
                f"{detail or '响应体为空'}"
            ) from exc

        result = response.json()

        if result.get("code") != 200:
            raise RuntimeError(
                result.get("message", "FastGPT导入失败")
            )
        
        data = result.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(
                "FastGPT导入失败: 返回数据格式错误"
            )
        if not data.get("collectionId"):
            raise RuntimeError(
                "FastGPT未返回collectionId"
            )

        return data

    """ 加载qa的提示词 """
    @staticmethod
    def load_qa_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parent.parent
            / "prompts"
            / "news_qa_generation.txt"
        )

        return prompt_path.read_text(
            encoding="utf-8"
        ).strip()

    """ 创建一个新的 """
    def create_news_qa_collection(
        self,
        article: NewsArticle,
        text: str,
        extra_metadata: dict | None = None,
    ) -> dict:
        url = (
            f"{self.base_url}"
            "/api/core/dataset/collection/create/text"
        )

        metadata = {
            **(extra_metadata or {}),
            "source": "tencent_news_qa",
            "news_id": article.news_id,
            "source_url": article.url,
            "original_title": article.title,
            "author": article.author,
            "publish_time": article.publish_time,
        }

        response = httpx.post(
            url,
            headers={
                "Authorization": (
                    f"Bearer {self.api_key}"
                ),
                "Content-Type": "application/json",
            },
            json={
                "datasetId": (
                    settings.fastgpt_qa_dataset_id
                ),
                "name": (
                    f"{build_collection_name(article)}-qa"
                ),
                "text": text,
                "trainingType": "qa",
                "chunkSettingMode": "auto",
                "qaPrompt": self.load_qa_prompt(),
                "metadata": metadata,
            },
            timeout=settings.fastgpt_timeout,
        )

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"FastGPT QA HTTP {response.status_code}: "
                f"{response.text[:2000]}"
            ) from exc

        result = response.json()

        if result.get("code") != 200:
            raise RuntimeError(
                result.get("message", "FastGPT QA导入失败")
            )

        data = result.get("data")

        if not isinstance(data, dict):
            raise RuntimeError(
                "FastGPT QA返回数据格式错误"
            )

        if not data.get("collectionId"):
            raise RuntimeError(
                "FastGPT QA未返回collectionId"
            )

        return data
