import httpx
from datetime import datetime

url = "https://r.inews.qq.com/web_feed/getPCList"

headers = {
    "Content-Type": "application/json",
    "Referer": "https://news.qq.com/",
    "User-Agent": "Mozilla/5.0",
}

payload = {
    "base_req": {
        "from": "pc"
    },
    "forward": "1",
    "qimei36": "0_NhDQ1xCnBNZ70",
    "device_id": "0_NhDQ1xCnBNZ70",
    "flush_num": 1,
    "channel_id": "news_news_tech",
    "item_count": 12,
    "is_local_chlid": "1",
}

today = datetime.now().date()

for page in range(1, 6):
    payload["flush_num"] = page

    response = httpx.post(
        url,
        headers=headers,
        json=payload,
    )

    data = response.json()

    # 这边输出新闻的id和文章标题
    for item in data["data"]:
        publish_time = datetime.strptime(
            item["publish_time"],
            "%Y-%m-%d %H:%M:%S"
        )

        if publish_time.date() == today:
            print(item["id"], item["title"])