"""计数器、TTL 过期与 SET NX（锁的雏形）。

知识点：
1. INCR/INCRBY 是原子操作——单线程命令执行，并发下不会丢计数；
2. EXPIRE/TTL/PERSIST 管理键的生存时间：这是"时间窗口"在 Redis 里的核心机制；
3. SET key value NX：仅当键不存在时才设置，是分布式锁的最简实现；
4. GETSET 返回旧值并写入新值，可用于"令牌/版本号"式刷新。
"""
from __future__ import annotations

import time

from common import get_client, need_redis


def main() -> None:
    r = get_client()

    # ---- 1. 原子计数器：高并发下不会丢失更新 ----
    r.delete("learn:counter")
    for _ in range(10):
        r.incr("learn:counter")           # 等价于 count = GET; count+1; SET，但原子
    r.incrby("learn:counter", 5)
    print("counter =", r.get("learn:counter"))  # 15

    # ---- 2. TTL：键的生存时间 ----
    r.set("learn:temp", "我60秒后消失", ex=60)
    print("temp ttl =", r.ttl("learn:temp"))    # 剩余秒数，约 60
    r.persist("learn:temp")                     # 取消过期 → 永久
    print("persist 后 ttl =", r.ttl("learn:temp"))  # -1 表示永不过期
    r.expire("learn:temp", 3)                   # 重新设置 3 秒
    time.sleep(3.2)
    print("3秒后 get =", r.get("learn:temp"))   # None，惰性删除：访问时发现已过期

    # ---- 3. SET NX：只在不存在的时才写入（锁的雏形）----
    r.delete("learn:lock:job-1")
    holder = r.set("learn:lock:job-1", "worker-A", nx=True, ex=10)
    print("worker-A 抢锁:", holder)             # True
    late = r.set("learn:lock:job-1", "worker-B", nx=True, ex=10)
    print("worker-B 抢锁:", late)               # None（键已存在，设置失败）
    # 释放锁时必须"检查是自己的再删"，完整做法见 06_cache_patterns.py

    # ---- 4. GETSET：读旧值写新值（令牌刷新）----
    r.set("learn:token", "t-001")
    print("旧 token =", r.getset("learn:token", "t-002"))  # t-001
    print("新 token =", r.get("learn:token"))              # t-002


if __name__ == "__main__":
    try:
        main()
    except redis.exceptions.ConnectionError as exc:
        need_redis(exc)
