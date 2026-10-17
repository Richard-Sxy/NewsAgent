"""缓存三大经典问题与应对（旁路缓存模型）。

知识点：
1. 旁路缓存（cache-aside）：读→先查缓存，未命中查库并回填；写→更新库再删缓存；
2. 缓存穿透（查不存在的 key）：缓存"空值"+短TTL，或布隆过滤器前置；
3. 缓存击穿（热点 key 过期瞬间）：SET NX 互斥，只放一个请求去重建，其余等待；
4. 缓存雪崩（大批 key 同时过期）：TTL 加随机抖动；Redis 整体不可用则限流降级。
"""
from __future__ import annotations

import random
import threading
import time

from common import get_client, need_redis

FAKE_DB = {"N123": {"title": "折叠屏发布", "clicks": 15203}}
DB_READS = 0        # 统计"数据库"真实被读的次数，用于验证互斥效果


def db_get(key: str) -> dict | None:
    """演示用的"数据库"：每次调用都计数，代表一次真实查库。"""
    global DB_READS
    DB_READS += 1
    time.sleep(0.05)                    # 模拟查库耗时，放大互斥效果
    return FAKE_DB.get(key)


def _decode(cached: str | None) -> dict | None:
    if cached is None:
        return None
    return None if cached == "\x00NULL\x00" else eval(cached)  # 演示用；生产存 JSON


def read_through(r, key: str) -> dict | None:
    """旁路缓存：读路径。"""
    cached = r.get(f"learn:cache:{key}")
    if cached is not None:
        return _decode(cached)

    value = db_get(key)
    if value is None:
        # 穿透应对①：把"不存在"也缓存起来，短 TTL 防止被恶意打库
        r.set(f"learn:cache:{key}", "\x00NULL\x00", ex=30)
        return None

    # 雪崩应对：TTL 加随机抖动，避免大批 key 同一秒过期
    r.set(f"learn:cache:{key}", str(value), ex=60 + random.randint(0, 30))
    return value


def rebuild_with_lock(r, key: str) -> dict | None:
    """击穿应对：互斥重建，只有一个请求去查库，其余短暂等待后读缓存。"""
    cache_key = f"learn:cache:{key}"
    for _ in range(50):                          # 最多等 5 秒
        cached = r.get(cache_key)
        if cached is not None:
            return _decode(cached)               # 重建者已回填，直接用

        # SET NX：抢到锁的请求负责重建（锁本身也要短 TTL，防持有者崩溃死锁）
        got_lock = r.set(f"learn:lock:rebuild:{key}", "1", nx=True, ex=5)
        if got_lock:
            try:
                value = db_get(key)              # 只有抢到锁的这一位真正查库
                encoded = "\x00NULL\x00" if value is None else str(value)
                r.set(cache_key, encoded, ex=60 + random.randint(0, 30))
                return value
            finally:
                r.delete(f"learn:lock:rebuild:{key}")   # 用完即释放
        time.sleep(0.1)                          # 没抢到锁：等重建者回填
    return None


def write_through(r, key: str, value: dict) -> None:
    """旁路缓存：写路径——先更新库，再删缓存（不是更新缓存）。"""
    FAKE_DB[key] = value
    r.delete(f"learn:cache:{key}")               # 下次读自然回填新值


def main() -> None:
    global DB_READS
    r = get_client()

    # 1. 正常旁路缓存
    print("第一次读(回填) =", read_through(r, "N123"))
    print("第二次读(命中) =", read_through(r, "N123"))

    # 2. 穿透：不存在的 key 也被短缓存
    print("不存在的 key =", read_through(r, "N404"))
    print("空值标记 =", repr(r.get("learn:cache:N404")))

    # 3. 击穿：模拟缓存失效后 20 个并发请求，只有 1 个真正打到"数据库"
    r.delete("learn:cache:N123")
    DB_READS = 0

    def worker():
        rebuild_with_lock(r, "N123")

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"20并发重建，真实查库次数 = {DB_READS}（互斥生效时应为 1）")

    # 4. 写路径
    write_through(r, "N123", {"title": "折叠屏发布(更新)", "clicks": 16000})
    print("写后重读 =", read_through(r, "N123"))
    for k in list(FAKE_DB) + ["N404"]:
        r.delete(f"learn:cache:{k}")


if __name__ == "__main__":
    import redis

    try:
        main()
    except redis.exceptions.ConnectionError as exc:
        need_redis(exc)
