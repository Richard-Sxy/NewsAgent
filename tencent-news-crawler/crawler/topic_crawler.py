import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import time
import httpx
from bs4 import BeautifulSoup

from config import settings


ARTICLE_PATH_PATTERN = re.compile(r"^/rain/a/[A-Za-z0-9]+/?$")
ARTICLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
CHANNEL_KEY_PATTERN = re.compile(r'"channelKey"\s*:\s*"([A-Za-z0-9_-]+)"')
EMBEDDED_ARTICLE_PATTERN = re.compile(
    r"(?:https?:)?(?:\\?/\\?/news\.qq\.com)?"
    r"(\\?/rain\\?/a\\?/[A-Za-z0-9]+)"
)

"""新闻索引/标题/url/主题"""
@dataclass(frozen=True)
class NewsIndex:
    title: str
    url: str
    topic: str
    publish_time: str = ""

""" 腾讯新闻主题获取 """
class TencentTopicCrawler:
    def __init__(self):
        self.headers = {
            "User-Agent": settings.crawler_user_agent,
        }
        self.last_request_at = 0.0

    """ 抓取HTML返回文本格式 """
    def fetch_html(self, url: str) -> str:
        response = self.request(
            method="GET",
            url=url,
            headers=self.headers,
            timeout=settings.crawler_timeout,
            follow_redirects=True,
        )
        return response.text

    """ 腾讯分类列表的接口 获取JSON格式文件 """
    def fetch_feed(
        self,
        channel_key: str,              # 频道信息 抓取哪一个领域的频道
        limit: int,                    # 获取多少条信息
        page: int = 0,
    ) -> dict:

        device_id = "0_news_agent_crawler"

        response = self.request(
            method="POST",
            url=(
            "https://i.news.qq.com"
            "/web_feed/getPCList"
            ),
            json={
                "qimei36": device_id,
                "device_id": device_id,
                "forward": "2" if page == 0 else "1",
                "base_req": {
                "from": "pc",
                },
                "flush_num": page,
                "channel_id": (
                f"news_news_{channel_key}"
                ),
                "item_count": limit,
                "is_local_chlid": "0",
            },
            headers=self.headers,
            timeout=settings.crawler_timeout,
            follow_redirects=True,
        )

        payload = response.json()

        if payload.get("code") not in {
            None,
            0,
        }:
            raise RuntimeError(
                "腾讯新闻列表接口错误: "
                f"{payload.get('message', payload)}"
            )

        return payload

    """ 构造请求 """
    def request(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> httpx.Response:
        
        max_retries = (
            settings.crawler_max_retries
        )
        if max_retries < 1:
            raise ValueError("crawler_max_retries 必须大于 0。")

        for attempt in range(max_retries):
            self.wait_for_rate_limit()

            try:
                response = httpx.request(
                    method=method,
                    url=url,
                    **kwargs,
                )

                self.last_request_at = (
                    time.monotonic()
                )

                # 429表示请求过于频繁
                # 5XX表示腾讯服务端临时异常
                if(
                    response.status_code == 429
                    or response.status_code >=500
                ):
                    response.raise_for_status()

                # 其他4XX通常是参数问题
                # 没有必要不断重试。
                response.raise_for_status()

                return response

            except httpx.RequestError:
                self.last_request_at = time.monotonic()
                if attempt == max_retries - 1:
                    raise

            except httpx.HTTPStatusError as exc:
                retryable = (
                    exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )

                if not retryable:
                    raise

                if attempt == max_retries - 1:
                    raise
            
            delay = (
                settings.crawler_retry_base_delay
                * (2 ** attempt)
            )

            time.sleep(delay)

        raise RuntimeError("请求重试流程异常结束")


    @staticmethod
    def normalize_article_url(href: str) -> str | None:
        """把腾讯新闻链接转换成无查询参数的标准正文 URL。"""
        value = href.strip().replace("\\/", "/")     # 把\\/转成/

        if value.startswith("//"):                   # 将相对域名合理的添加成统一格式
            value = "https:" + value
        elif value.startswith("/"):
            value = "https://news.qq.com" + value

        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"}:
            return None
        if parts.hostname not in {"news.qq.com", "new.qq.com"}:
            return None
        if not ARTICLE_PATH_PATTERN.fullmatch(parts.path):
            return None

        path = parts.path.rstrip("/")
        return urlunsplit(("https", "news.qq.com", path, "", ""))

    """解析新闻索引"""
    def parse_news_indexes(
        self,
        html: str,
        topic: str,
    ) -> list[NewsIndex]:
        soup = BeautifulSoup(html, "lxml")           # HTML 转成一个 DOM 树
        results: list[NewsIndex] = []

        for anchor in soup.find_all("a", href=True):
            url = self.normalize_article_url(anchor["href"])
            title = anchor.get_text(" ", strip=True)
            if url and title:
                results.append(
                    NewsIndex(title=title, url=url, topic=topic)
                )

        # 某些分类页将链接放在 script JSON 中，没有对应的 a 标签。
        # 这类链接先用空标题收集，正文抓取时会得到真正标题。
        for script in soup.find_all("script"):
            script_text = script.string or script.get_text()
            for match in EMBEDDED_ARTICLE_PATTERN.finditer(script_text):
                url = self.normalize_article_url(match.group(1))
                if url:
                    results.append(
                        NewsIndex(title="", url=url, topic=topic)
                    )

        return self.deduplicate(results)

    @staticmethod
    def parse_feed_indexes(
        payload: dict,
        topic: str,
    ) -> list[NewsIndex]:
        """解析腾讯新闻列表接口返回的数据。"""
        results: list[NewsIndex] = []

        for parent in payload.get("data") or []:
            # 热点精选中，新闻放在 sub_item 里面。
            # 普通新闻本身就是一条新闻。
            items = parent.get("sub_item") or [
                parent
            ]

            for item in items:
                article_type = str(
                    item.get("articletype", "")
                )

                # 类型4通常是视频，当前正文抓取器
                # 主要处理图文新闻，因此暂时排除。
                if article_type == "4":
                    continue

                # 热点聚合的 id 可能是 UTR 开头，
                # 真正的文章 ID 位于 focus_id。
                thing_info = (
                    item.get("thing_info") or {}
                )

                focus_id = thing_info.get(
                     "focus_id"
                )

                article_id = (
                    focus_id
                    or item.get("id", "")
                )

                title = str(
                    item.get("title", "")
                ).strip()

                publish_time = str(
                    item.get("publish_time", "")
                ).strip()

                if not title:
                    continue

                if not ARTICLE_ID_PATTERN.fullmatch(
                    article_id
                ):
                    continue

                results.append(
                    NewsIndex(
                        title=title,
                        url=(
                            "https://news.qq.com"
                            f"/rain/a/{article_id}"
                        ),
                        topic=topic,
                        publish_time=publish_time,
                    )
                )

        return TencentTopicCrawler.deduplicate(
            results
        )

    """ 函数去重 """
    @staticmethod
    def deduplicate(indexes: list[NewsIndex]) -> list[NewsIndex]:
        results: list[NewsIndex] = []
        positions: dict[str, int] = {}

        for item in indexes:
            position = positions.get(item.url)
            if position is None:
                positions[item.url] = len(results)
                results.append(item)
            elif not results[position].title and item.title:
                results[position] = item

        return results

    """获取新闻索引"""
    def get_news_indexes(
        self,
        url: str,
        topic: str,
        limit: int | None = None,
        target_date: str | None = None,
        max_pages: int = 3,
    ) -> list[NewsIndex]:
        if limit is not None and limit < 1:
            raise ValueError("limit 必须大于 0。")
        if max_pages < 1:
            raise ValueError("max_pages 必须大于 0。")

        # 首先尝试从静态HTML获取
        html = self.fetch_html(url)
        indexes = self.parse_news_indexes(html, topic)

        # 腾讯分类页通常是动态页面
        if not indexes:
            match = CHANNEL_KEY_PATTERN.search(
                html
            )

            if not match:
                raise ValueError(
                    "分类页中未找到 channelKey"
                )
            
            channel_key = match.group(1)

            indexes = []
            page_size = min(max(limit or 20, 10), 20)

            for page in range(max_pages):
                try:
                    payload = self.fetch_feed(
                        channel_key=channel_key,
                        limit=page_size,
                        page=page,
                    )
                
                except (
                    httpx.RequestError,
                    httpx.HTTPStatusError,
                ):
                    if page == 0:
                        raise
                    
                    break

                page_indexes = self.parse_feed_indexes(
                    payload=payload,
                    topic=topic,
                )
                if not page_indexes:
                    break

                indexes = self.deduplicate(
                    [*indexes, *page_indexes]
                )
                matching = self.filter_by_date(
                    indexes=indexes,
                    target_date=target_date,
                )
                if limit is not None and len(matching) >= limit:
                    break

        indexes = self.filter_by_date(
            indexes=indexes,
            target_date=target_date,
        )

        return indexes[:limit]

    """按照日期过滤函数"""
    @staticmethod
    def filter_by_date(
        indexes: list[NewsIndex],
        target_date: str | None,
    ) -> list[NewsIndex]:
        if not target_date:
            return indexes

        return [
            item
            for item in indexes
            if item.publish_time.startswith(target_date)
        ]

    """实现请求限速"""
    def wait_for_rate_limit(self) -> None:
        elapsed = (
            time.monotonic()
            - self.last_request_at
        )

        remaining = (
            settings.crawler_request_interval
            - elapsed
        )

        if remaining > 0:
            time.sleep(remaining)
