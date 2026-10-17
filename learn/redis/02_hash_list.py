"""Hash（对象缓存）与 List（简易队列）。

知识点：
1. Hash 适合存"小对象"：一个键下多个字段，可单独读写字段而不必整体序列化；
2. HINCRBY 可以对 hash 的某个字段原子计数；
3. List 是双端链表：LPUSH+BRPOP 构成最简单的阻塞队列（生产用 Stream/MQ 更稳）；
4. LINDEX/LRANGE 是 O(N) 操作，长列表慎用范围查询。
"""
from __future__ import annotations

from common import get_client, need_redis


def main() -> None:
    r = get_client()

    # ---- 1. Hash：把一个用户对象存成一个键 ----
    r.delete("learn:user:10086")
    r.hset("learn:user:10086", mapping={
        "name": "小明",
        "city": "深圳",
        "level": "3",
    })
    print("name =", r.hget("learn:user:10086", "name"))
    print("all  =", r.hgetall("learn:user:10086"))

    # 只改一个字段，不用读出整个对象再写回
    r.hincrby("learn:user:10086", "level", 1)
    print("level+1 =", r.hget("learn:user:10086", "level"))  # 4
    r.hdel("learn:user:10086", "city")
    print("after hdel =", r.hgetall("learn:user:10086"))

    # ---- 2. List：双端链表，做一个待处理队列 ----
    r.delete("learn:queue:emails")
    r.rpush("learn:queue:emails", "task-1", "task-2", "task-3")  # 尾部进
    print("队列长度 =", r.llen("learn:queue:emails"))             # 3
    print("整个队列 =", r.lrange("learn:queue:emails", 0, -1))    # [task-1,2,3]

    first = r.lpop("learn:queue:emails")                          # 头部出 = FIFO
    print("取出 =", first, "剩余 =", r.llen("learn:queue:emails"))

    # BLPOP 是阻塞版：队列空时等待而不是轮询（简单消费者的核心原语）
    r.blpop("learn:queue:emails", timeout=1)   # 取走 task-2
    r.blpop("learn:queue:emails", timeout=1)   # 取走 task-3
    empty = r.blpop("learn:queue:emails", timeout=1)
    print("队列空时 blpop 返回 =", empty)       # None（超时）


if __name__ == "__main__":
    import redis

    try:
        main()
    except redis.exceptions.ConnectionError as exc:
        need_redis(exc)
