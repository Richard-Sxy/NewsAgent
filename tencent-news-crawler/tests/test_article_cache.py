import json

from crawler.tencent_news import NewsArticle
from service.article_cache import ArticleCache


def make_article(url="https://news.qq.com/rain/a/CACHE001"):
    return NewsArticle(
        url=url,
        title="缓存测试新闻",
        publish_time="2026-08-20 10:00:00",
        author="腾讯新闻",
        content="这是一段用于测试本地新闻正文缓存的内容。" * 10,
    )


def test_cache_saves_and_loads_article(tmp_path):
    cache = ArticleCache(tmp_path / "articles")
    article = make_article()

    path = cache.save(article)
    loaded = cache.load(article.url)

    assert path.is_file()
    assert loaded == article
    assert json.loads(path.read_text(encoding="utf-8"))["title"] == article.title


def test_get_or_fetch_only_fetches_once(tmp_path):
    cache = ArticleCache(tmp_path / "articles")
    article = make_article()
    calls = []

    def fetcher(url):
        calls.append(url)
        return article

    first, first_hit = cache.get_or_fetch(article.url, fetcher)
    second, second_hit = cache.get_or_fetch(article.url, fetcher)

    assert first == second == article
    assert first_hit is False
    assert second_hit is True
    assert calls == [article.url]


def test_corrupted_cache_is_refetched_and_replaced(tmp_path):
    cache = ArticleCache(tmp_path / "articles")
    article = make_article()
    path = cache.path_for_url(article.url)
    path.write_text("not-json", encoding="utf-8")

    loaded, cache_hit = cache.get_or_fetch(
        article.url,
        lambda url: article,
    )

    assert loaded == article
    assert cache_hit is False
    assert json.loads(path.read_text(encoding="utf-8"))["url"] == article.url


def test_cache_filename_does_not_contain_url_characters(tmp_path):
    cache = ArticleCache(tmp_path / "articles")
    path = cache.path_for_url("https://news.qq.com/rain/a/test?x=1")

    assert path.suffix == ".json"
    assert len(path.stem) == 64
    assert "/" not in path.name
