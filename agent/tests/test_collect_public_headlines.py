from datetime import date
import pytest

from tools.collect_public_headlines import allowed_url, parse_headline


URL = "https://news.qq.com/rain/a/20261006A0123400"
PAGE = '''<h1>公开新闻真实标题</h1>
<meta property="article:author" content="公开来源">
<meta property="article:published_time" content="2026-10-06 12:34:56">
<meta name="category" content="tech">'''


def test_parse_public_metadata_and_keep_original_title():
    row = parse_headline(URL, PAGE, date(2026, 10, 6))
    assert row == {"news_id": "20261006A0123400", "title": "公开新闻真实标题",
                   "source": "公开来源", "url": URL, "published_at": "2026-10-06T12:34:56+08:00",
                   "category": "科技", "content_type": "article"}


@pytest.mark.parametrize("url", ["http://news.qq.com/rain/a/20261006A0123400",
    "https://evil.example/rain/a/20261006A0123400", "https://news.qq.com@evil.example/x",
    "https://news.qq.com/rain/a/20261006A0123400?redirect=x", "https://news.qq.com/private",
    "https://news.qq.com:444/rain/a/20261006A0123400"])
def test_reject_unapproved_network_targets(url):
    with pytest.raises(ValueError):
        allowed_url(url)


def test_require_verified_publication_metadata_and_explicit_date():
    with pytest.raises(ValueError, match="publication metadata"):
        parse_headline(URL, "<h1>公开新闻真实标题</h1>", date(2026, 10, 6))
    with pytest.raises(ValueError, match="outside"):
        parse_headline(URL, PAGE, date(2026, 10, 7))


def test_video_and_html_entities_are_metadata_not_commands():
    row = parse_headline(URL.replace("A012", "V012"), PAGE.replace("真实", "&amp;"), date(2026, 10, 6))
    assert row["content_type"] == "video" and "&" in row["title"]
