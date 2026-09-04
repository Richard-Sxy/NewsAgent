from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

from crawler.tencent_news import TencentNewsCrawler

app = FastAPI(
    title="Tencent News Crawler",
    version="1.0.0",
)

crawler = TencentNewsCrawler()

class CrawlRequest(BaseModel):
    url: HttpUrl

@app.get("/health")
def health():
    return {
        "status": "ok"
    }

@app.post("/crawl")
def crawl_news(request: CrawlRequest):

    try:
        article = crawler.crawl(str(request.url))

        return {
            "code": 200,
            "message": "success",
            "data": article.to_dict(),
        }

    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"爬取失败: {str(e)}",
        )