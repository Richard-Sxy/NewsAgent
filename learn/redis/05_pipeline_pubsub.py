"""Pipeline 批量/事务 与 发布订阅。

知识点：
1. 逐条命令 = 每条一次网络往返；pipeline 把多条命令打包一次发送，大幅降低延迟；
2. transaction=True 时用 MULTI/EXEC 包裹：命令排队、服务端原子执行；
   WATCH 可以做乐观锁（CAS）：执行前被别人改过则放弃；
3. 发布订阅 pub/sub：实时广播，但**不持久化**——订阅者离线期间的消息永久丢失，
   且离线发送的命令会因无订阅者而直接丢弃；
4. 需要可靠消息时用 Stream（XADD/XREADGROUP）或专业 MQ，不要用 pub/sub。
"""
from __future__ import annotations

import threading
import time

from common import get_client, need_redis


def demo_pipeline() -> None:
    r = get_client()

    # ---- 1. 批量写入：一次往返 ----
    r.delete("learn:pipe:a", "learn:pipe:b")
    with r.pipeline(transaction=False) as pipe:   # 纯批量，不加事务
        pipe.set("learn:pipe:a", "1")
        pipe.set("learn:pipe:b", "2")
        pipe.incr("learn:pipe:a")
        results = pipe.execute()                  # 一次网络往返执行全部
    print("pipeline 结果 =", results)             # [True, True, 2]

    # ---- 2. 事务：MULTI/EXEC，命令排队原子执行 ----
    with r.pipeline(transaction=True) as pipe:
        pipe.set("learn:pipe:a", "reset")
        pipe.get("learn:pipe:a")
        print("事务结果 =", pipe.execute())       # [True, 'reset']

    # ---- 3. WATCH 乐观锁：余额转账的 CAS 检查 ----
    r.mset({"learn:acct:x": "100", "learn:acct:y": "0"})

    def transfer(amount: int) -> bool:
        for _ in range(3):  # 最多重试 3 次
            with r.pipeline(transaction=True) as pipe:
                try:
                    pipe.watch("learn:acct:x")            # 监视余额键
                    balance = int(pipe.get("learn:acct:x") or 0)
                    if balance < amount:
                        pipe.unwatch()
                        return False
                    pipe.multi()                           # 进入事务队列
                    pipe.decrby("learn:acct:x", amount)
                    pipe.incrby("learn:acct:y", amount)
                    pipe.execute()                         # 期间若被改则抛 WatchError
                    return True
                except redis.WatchError:
                    continue  # 余额被并发修改，重读后重试
        return False

    import redis
    print("转账 30 =", transfer(30), "余额 =", r.mget("learn:acct:x", "learn:acct:y"))
    print("转账 500 =", transfer(500))                    # 余额不足，False
    r.delete("learn:pipe:a", "learn:pipe:b", "learn:acct:x", "learn:acct:y")


def demo_pubsub() -> None:
    r = get_client()
    channel = "learn:news:alert"

    received: list[str] = []

    def subscriber() -> None:
        sub = get_client().pubsub(ignore_subscribe_messages=True)
        sub.subscribe(channel)
        for message in sub.listen():          # 阻塞等待
            received.append(str(message["data"]))
            if str(message["data"]) == "end":
                break
        sub.close()

    thread = threading.Thread(target=subscriber)
    thread.start()
    time.sleep(0.3)                           # 等订阅者就绪（先订阅后发布才收得到）

    r.publish(channel, "热点：N123 冲上第一")
    r.publish(channel, "热点：N124 突发推送")
    r.publish(channel, "end")
    thread.join(timeout=3)
    print("订阅者收到 =", received)


def main() -> None:
    demo_pipeline()
    demo_pubsub()


if __name__ == "__main__":
    import redis

    try:
        main()
    except redis.exceptions.ConnectionError as exc:
        need_redis(exc)
