from crawler.tencent_news import NewsArticle, TencentNewsCrawler
import pytest


def test_news_article_extracts_news_id_from_url() -> None:
    article = NewsArticle(
        url="https://news.qq.com/rain/a/20260902A0AVJC00?source=feed",
        title="测试",
        publish_time="",
        author="",
        content="正文",
    )

    assert article.news_id == "20260902A0AVJC00"

def test_parse_news():

    html = """
    <html>
        <head>
            <title>测试新闻_腾讯新闻</title>

            <meta
                name="author"
                content="腾讯新闻"
            >

            <meta
                property="article:published_time"
                content="2026-08-16 10:00:00"
            >
        </head>

        <body>

            <article>

                <p>这是第一段正文。</p>

                <p>这是第二段正文。</p>

            </article>

        </body>
    </html>
    """

    crawler = TencentNewsCrawler()

    article = crawler.parse(
        url="https://news.qq.com/rain/a/test",
        html=html,
    )

    assert article.title == "测试新闻"

    assert article.author == "腾讯新闻"

    assert article.publish_time == "2026-08-16 10:00:00"

    assert "这是第一段正文。" in article.content

    assert "这是第二段正文。" in article.content

def test_parse_title_prefers_h1():
    """正文 h1 应优先于 HTML title"""
    html = """
    <html>
        <head>
            <title>备用标题_腾讯新闻</title>
        </head>
        <body>
            <h1>主要标题</h1>
            <article>
                <p>正文。</p>
            </article>
        </body>
    </html>
    """
    crawler = TencentNewsCrawler()
    article = crawler.parse(
        url="https://news.qq.com/rain/a/test",
        html=html,
    )
    assert article.title == "主要标题"

def test_parse_title_falls_back_to_html_title():
    """没有 h1 等标题元素时,应该用 title 标签。"""
    html = """
    <html>
        <head>
            <title>备用标题_腾讯新闻</title>
        </head>
        <body>
            <article>
                <p>正文。</p>
            </article>
        </body>
    </html>
    """

    crawler = TencentNewsCrawler()
    article = crawler.parse(
        url="https://news.qq.com/rain/a/test",
        html=html,
    )
    assert article.title == "备用标题"

def test_parse_author_from_visible_element():
    """没有 author meta 时，应从可见作者元素提取。"""
    html = """
    <html>
        <body>
            <div class="author">测试作者</div>
            <article>
                <p>正文。</p>
            </article>
        </body>
    </html>
    """

    crawler = TencentNewsCrawler()
    article = crawler.parse(
        url="https://news.qq.com/rain/a/test",
        html=html,
    )

    assert article.author == "测试作者"

def test_parse_missing_optional_fields():
    """缺少标题、作者和发布时间时应返回空字符串。"""
    html = """
    <html>
        <body>
            <article>
                <p>只有正文。</p>
            </article>
        </body>
    </html>
    """

    crawler = TencentNewsCrawler()
    article = crawler.parse(
        url="https://news.qq.com/rain/a/test",
        html=html,
    )

    assert article.title == ""
    assert article.author == ""
    assert article.publish_time == ""
    assert article.content == "只有正文。"

def test_parse_content_ignores_empty_and_noise_paragraphs():
    """空段落和已知无意义段落不应进入正文。"""
    html = """
    <html>
        <body>
            <article>
                <p>第一段。</p>
                <p>   </p>
                <p>举报</p>
                <p>责任编辑：</p>
                <p>第二段。</p>
            </article>
        </body>
    </html>
    """

    crawler = TencentNewsCrawler()
    article = crawler.parse(
        url="https://news.qq.com/rain/a/test",
        html=html,
    )

    assert article.content == "第一段。\n第二段。"


def test_parse_content_removes_no_read_block_and_credit_paragraphs():
    html = """
        <html>
          <body>
            <article>
              <p>第一段正文。</p>
              <!-- NO_READ_BEGIN -->
              <div class="recommendation">
                <p>相关阅读：这段内容不能进入正文。</p>
                <p>广告内容。</p>
              </div>
              <!-- NO_READ_END -->
              <p>图源：视觉中国</p>
              <p>作者：测试作者</p>
              <p>编辑：测试编辑</p>
              <p>责任编辑：测试责编</p>
              <p>第二段正文。</p>
            </article>
          </body>
        </html>
    """

    article = TencentNewsCrawler().parse(
        "https://news.qq.com/rain/a/test",
        html,
    )

    assert article.content == "第一段正文。\n第二段正文。"


def test_parse_content_keeps_normal_sentences_containing_credit_words():
    html = """
        <article>
          <p>作者认为这一变化将影响行业发展。</p>
          <p>编辑团队随后公布了完整调查结果。</p>
          <p>图片显示现场设备仍在正常运行。</p>
        </article>
    """

    article = TencentNewsCrawler().parse(
        "https://news.qq.com/rain/a/test",
        html,
    )

    assert article.content == (
        "作者认为这一变化将影响行业发展。\n"
        "编辑团队随后公布了完整调查结果。\n"
        "图片显示现场设备仍在正常运行。"
    )

def test_parse_content_falls_back_to_whole_page():
    """找不到正文容器时，应从整个页面收集段落。"""
    html = """
    <html>
        <body>
            <div>
                <p>页面中的第一段。</p>
                <p>页面中的第二段。</p>
            </div>
        </body>
    </html>
    """

    crawler = TencentNewsCrawler()
    article = crawler.parse(
        url="https://news.qq.com/rain/a/test",
        html=html,
    )

    assert article.content == (
        "页面中的第一段。\n页面中的第二段。"
    )

@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/news/1",
        "http://news.qq.com/rain/a/test",
        "not-a-url",
    ],
)
def test_fetch_html_rejects_unsupported_url(url: str):
    """不支持的 URL 应在发送网络请求前被拒绝。"""
    crawler = TencentNewsCrawler()

    with pytest.raises(
        ValueError,
        match="当前只支持腾讯新闻 URL",
    ):
        crawler.fetch_html(url)

def test_crawl_fetches_and_parses(monkeypatch):
    """crawl 应把下载得到的 HTML 交给解析器。"""
    crawler = TencentNewsCrawler()
    url = "https://news.qq.com/rain/a/test"

    html = """
    <html>
        <body>
            <h1>模拟新闻</h1>
            <article>
                <p>模拟正文。</p>
            </article>
        </body>
    </html>
    """

    def fake_fetch_html(request_url: str) -> str:
        assert request_url == url
        return html

    monkeypatch.setattr(
        crawler,
        "fetch_html",
        fake_fetch_html,
    )

    article = crawler.crawl(url)

    assert article.url == url
    assert article.title == "模拟新闻"
    assert article.content == "模拟正文。"
