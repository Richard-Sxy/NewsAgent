"""公共连接助手：所有示例脚本共用。

redis.Redis.from_url() 内部创建连接池；
redis-py 的客户端对象是线程安全的，进程内复用一个即可。
"""
from __future__ import annotations

import os

import redis

# 学习统一用 db 15，键名统一 learn: 前缀，避免污染其他数据；
# 可用环境变量 LEARN_REDIS_URL 覆盖（例如指向另一个端口的练习实例）
REDIS_URL = os.environ.get("LEARN_REDIS_URL", "redis://127.0.0.1:6379/15")


def get_client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def need_redis(exc: Exception) -> None:
    print(f"连不上 Redis：{exc}\n先启动：docker run --rm -d -p 6379:6379 redis:7")
