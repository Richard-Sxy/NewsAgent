import httpx
from datetime import datetime
import pytest

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

@pytest.mark.integration
def test_get_pc_list():
    """真实接口探测；默认离线回归不访问网络，显式 -m integration 才执行。"""
    today = datetime.now().date()

    with httpx.Client(timeout=20) as client:
        for page in range(1, 6):
            response = client.post(
                url,
                headers=headers,
                json={**payload, "flush_num": page},
            )
            response.raise_for_status()
            data = response.json()
            assert isinstance(data.get("data"), list), "news feed must contain an item list"

            # 这边输出新闻的id和文章标题
            for item in data["data"]:
                publish_time = datetime.strptime(
                    item["publish_time"],
                    "%Y-%m-%d %H:%M:%S"
                )
                if publish_time.date() == today:
                    print(item["id"], item["title"])


if __name__ == "__main__":
    test_get_pc_list()
