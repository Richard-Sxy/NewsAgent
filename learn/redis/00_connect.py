"""连接 Redis 与 String 基本读写。

知识点：
1. redis.Redis() 是线程安全的，内部自带连接池，模块级建一个复用即可；
2. decode_responses=True 让返回值是 str 而不是 bytes；
3. String 是最基础类型：缓存值、数字、序列化 JSON 都可以存；
4. 常用命令：SET / GET / DEL / EXISTS / KEYS（生产别用 KEYS，用 SCAN）。
"""
from __future__ import annotations

import os

import redis

# 与 common.py 保持一致：默认 db 15，可用 LEARN_REDIS_URL 覆盖
REDIS_URL = os.environ.get("LEARN_REDIS_URL", "redis://127.0.0.1:6379/15")


def get_client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def main() -> None:
    r = get_client()

    # PING：连通性检查，部署健康探针常用
    assert r.ping() is True
    print("连接成功:", REDIS_URL)

    # 基本读写：SET key value [EX 秒]
    r.set("learn:greeting", "hello redis", ex=60)
    print("learn:greeting =", r.get("learn:greeting"))

    # 数字也可以直接存（Redis 内部按字符串处理，INCR 时转数字）
    r.set("learn:price", "199")
    print("learn:price =", r.get("learn:price"))

    # EXISTS 判断键是否存在；DEL 删除
    print("exists:", r.exists("learn:greeting"))
    r.delete("learn:greeting")
    print("after delete:", r.get("learn:greeting"))  # 不存在返回 None

    # SCAN 增量遍历（不要用 KEYS，会阻塞单线程）
    matches = [key for key in r.scan_iter("learn:*")]
    print("learn:* keys:", matches)


if __name__ == "__main__":
    try:
        main()
    except redis.exceptions.ConnectionError as exc:
        print(f"连不上 Redis：{exc}\n先启动：docker run --rm -d -p 6379:6379 redis:7")
