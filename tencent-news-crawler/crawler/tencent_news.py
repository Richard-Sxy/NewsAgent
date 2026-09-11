from dataclasses import dataclass, asdict
import re
from urllib.parse import urlparse

from config import settings
import httpx
from bs4 import BeautifulSoup


@dataclass
class NewsArticle:
    url: str
    title: str
    publish_time: str
    author: str
    content: str

    def to_dict(self):
        return asdict(self)

    @property
    def news_id(self) -> str:
        """从腾讯新闻 URL 提取业务新闻 ID。"""
        value = urlparse(self.url).path.rstrip("/").split("/")[-1].strip()
        if not value:
            raise ValueError(f"腾讯新闻 URL 缺少 news_id: {self.url}")
        return value

""" 根据URL抓取正文, 组装成统一对象 """
class TencentNewsCrawler:

    def __init__(self):
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0 Safari/537.36"
            )
        }

    """请求腾讯新闻页面"""
    def fetch_html(self, url: str) -> str:
        
        if not url.startswith("https://news.qq.com/"):
            raise ValueError("当前只支持腾讯新闻 URL")

        response = httpx.get(
            url,
            headers={
                "User-Agent": settings.crawler_user_agent
            },
            timeout=settings.crawler_timeout,
            follow_redirects=True,
        )
        
        # 发生错误就返回错误状态/正确就未响应
        response.raise_for_status()
        
        # 最后输出新闻文本
        return response.text

    """解析腾讯新闻页面"""
    def parse(self, url: str, html: str) -> NewsArticle:

        soup = BeautifulSoup(self._remove_no_read_blocks(html), "lxml")

        title = self._parse_title(soup)
        publish_time = self._parse_publish_time(soup)
        author = self._parse_author(soup)
        content = self._parse_content(soup)

        return NewsArticle(
            url=url,
            title=title,
            publish_time=publish_time,
            author=author,
            content=content,
        )

    def crawl(self, url: str) -> NewsArticle:
        """完整爬取流程"""
        # 根据URL获取网页HTML
        html = self.fetch_html(url)
        # 解析HTML
        # print(html)
        return self.parse(url, html)

    @staticmethod
    def _remove_no_read_blocks(html: str) -> str:
        """删除腾讯页面显式标记为不可阅读的完整 HTML 区块。"""
        return re.sub(
            r"<!--\s*NO_READ_BEGIN\s*-->.*?<!--\s*NO_READ_END\s*-->",
            "",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )

    # 判断是否是噪声文件
    @staticmethod
    def _is_noise_paragraph(text: str) -> bool:
        """识别独立成段的署名、编辑和图片来源，避免误删普通新闻句子。"""
        normalized = re.sub(r"\s+", " ", text).strip()

        if normalized in {"责任编辑：", "责任编辑:", "举报"}:
            return True

        labeled_credit = re.compile(
            r"^(?:作者|编辑|责编|责任编辑|记者|撰文|文|校对|审核|"
            r"图源|图片来源|图片|摄影|摄图|供图)[：:]\s*\S.{0,100}$"
        )
        compact_image_credit = re.compile(
            r"^(?:图源|图片来源|图片|摄影|摄图|供图)\s+\S.{0,100}$"
        )

        return bool(
            labeled_credit.fullmatch(normalized)
            or compact_image_credit.fullmatch(normalized)
        )

    @staticmethod
    def _parse_title(soup: BeautifulSoup) -> str:
        """
        解析标题。腾讯页面结构可能变化，因此提供多个 fallback。
        """

        selectors = [
            "h1",
            ".title",
            ".content-title",
        ]

        for selector in selectors:
            element = soup.select_one(selector)

            if element:
                text = element.get_text(strip=True)

                if text:
                    return text

        if soup.title:
            title = soup.title.get_text(strip=True)

            # 去掉类似 "_腾讯新闻"
            title = title.replace("_腾讯新闻", "")

            return title

        return ""

    @staticmethod
    def _parse_publish_time(soup: BeautifulSoup) -> str:
        """
        尝试从 meta 中获取发布时间。
        """

        selectors = [
            'meta[property="article:published_time"]',
            'meta[name="apub:time"]',
            'meta[name="publish_time"]',
        ]

        for selector in selectors:
            element = soup.select_one(selector)

            if element:
                value = element.get("content")

                if value:
                    return value.strip()

        return ""

    @staticmethod
    def _parse_author(soup: BeautifulSoup) -> str:
        """解析作者"""

        selectors = [
            'meta[name="author"]',
            ".author",
            ".media-name",
        ]

        for selector in selectors:
            element = soup.select_one(selector)

            if not element:
                continue

            if element.name == "meta":
                value = element.get("content")

                if value:
                    return value.strip()

            text = element.get_text(strip=True)

            if text:
                return text

        return ""

    @staticmethod
    def _parse_content(soup: BeautifulSoup) -> str:
        """
        解析正文。第一版先使用正文容器 + p 标签。
        """

        article_selectors = [
            ".content-article",
            ".article-content",
            ".content",
            "article",
        ]

        article = None

        for selector in article_selectors:
            article = soup.select_one(selector)

            if article:
                break

        # 找不到正文容器时退化为整个页面
        if article is None:
            article = soup

        paragraphs = []

        for p in article.find_all("p"):

            text = p.get_text(
                separator=" ",
                strip=True,
            )

            if not text:
                continue

            if TencentNewsCrawler._is_noise_paragraph(text):
                continue

            paragraphs.append(text)

        return "\n".join(paragraphs)
