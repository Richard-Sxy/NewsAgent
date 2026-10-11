import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from xml.etree import ElementTree

import httpx

from config import settings


SITEMAP_INDEX_URL = "https://news.qq.com/sitemap/index.xml"
ARTICLE_URL_PATTERN = re.compile(
    r"^https://news\.qq\.com/rain/a/([A-Za-z0-9]+)$"
)


@dataclass(frozen=True)
class SitemapDiscoveryResult:
    sitemap_count: int
    fetched_sitemaps: int
    failed_sitemaps: int
    urls: list[str]


class TencentSitemapCrawler:
    """从腾讯公开 sitemap 并发发现指定日期的新闻 URL。"""

    def __init__(self, workers: int = 8):
        if workers < 1:
            raise ValueError("workers 必须大于 0。")
        self.workers = workers
        self.headers = {"User-Agent": settings.crawler_user_agent}

    @staticmethod
    def _parse_locations(xml_text: str) -> list[str]:
        root = ElementTree.fromstring(xml_text)
        return [
            (element.text or "").strip()
            for element in root.findall(".//{*}loc")
            if (element.text or "").strip()
        ]

    def _fetch_text(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(settings.crawler_max_retries):
            try:
                response = httpx.get(
                    url,
                    headers=self.headers,
                    timeout=settings.crawler_timeout,
                    follow_redirects=True,
                )
                response.raise_for_status()
                return response.text
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if attempt + 1 < settings.crawler_max_retries:
                    time.sleep(settings.crawler_retry_base_delay * (2 ** attempt))
        assert last_error is not None
        raise last_error

    @staticmethod
    def filter_article_urls(urls: list[str], target_date: str) -> list[str]:
        """按文章 ID 的 YYYYMMDD 前缀过滤并保持首次出现顺序。"""
        date_prefix = target_date.replace("-", "")
        results: list[str] = []
        seen: set[str] = set()
        for url in urls:
            match = ARTICLE_URL_PATTERN.fullmatch(url)
            if not match or not match.group(1).startswith(date_prefix):
                continue
            if url not in seen:
                seen.add(url)
                results.append(url)
        return results

    def discover(self, target_date: str, limit: int | None = None) -> SitemapDiscoveryResult:
        """读取当前 sitemap 索引，并返回指定发布日期的唯一文章 URL。"""
        if limit is not None and limit < 1:
            raise ValueError("limit 必须大于 0。")

        sitemap_urls = self._parse_locations(self._fetch_text(SITEMAP_INDEX_URL))
        discovered: list[str] = []
        fetched = 0
        failed = 0

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = {
                executor.submit(self._fetch_text, url): url
                for url in sitemap_urls
            }
            for future in as_completed(futures):
                try:
                    locations = self._parse_locations(future.result())
                    discovered.extend(self.filter_article_urls(locations, target_date))
                    fetched += 1
                except Exception:
                    failed += 1

        unique_urls = list(dict.fromkeys(discovered))
        if limit is not None:
            unique_urls = unique_urls[:limit]
        return SitemapDiscoveryResult(
            sitemap_count=len(sitemap_urls),
            fetched_sitemaps=fetched,
            failed_sitemaps=failed,
            urls=unique_urls,
        )
