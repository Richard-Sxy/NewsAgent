"""Set 去重 + ZSet 实时热榜（本系列的重点示例）。

知识点：
1. Set：无序去重，SADD 自动去重，SCARD 数基数，SISMEMBER 判断存在——精确去重的成本是存全量元素；
2. ZSet：每个成员带 score，按分数排序；ZINCRBY 原子加分，ZREVRANGE 取 TopN；
3. 时间窗口 = key 命名：2026101210（10点桶）和 2026101211（11点桶）是两个独立 ZSet；
4. TTL 即窗口淘汰：EXPIRE 到期整个 key 消失，不需要任何后台清理任务；
5. 滑动窗口 = 合并最近 N 个细粒度桶（ZUNIONSTORE），比"过去1小时"粗桶更平滑。

这正是实时热榜在线层（Redis 层）的标准数据模型：
明细日志在数仓，Redis 只保存"可丢弃的加速视图"。
"""
from __future__ import annotations

from common import get_client, need_redis


def main() -> None:
    r = get_client()

    # ---- 1. Set：精确去重（小规模 UV）----
    key = "learn:uv:exact:2026101210"
    r.delete(key)
    r.sadd(key, "u1", "u2", "u3", "u1", "u2")   # 重复元素自动忽略
    print("精确 UV =", r.scard(key))              # 3
    print("u1 来过吗 =", r.sismember(key, "u1"))  # True
    # 注意：桶与桶之间的 UV 不能相加（可能有同一批用户），
    # 精确跨桶要 SUNION 后数基数，元素多时成本高 → 用 04 的 HyperLogLog

    # ---- 2. ZSet：小时桶热榜 ----
    hour_key = "learn:hot:rank:2026101210"
    r.delete(hour_key)
    for news, cnt in (("N123", 3), ("N124", 1), ("N125", 2)):
        r.zincrby(hour_key, cnt, news)            # 原子加分：模拟一次点击 +1
    print("10点桶 TopN =", r.zrevrange(hour_key, 0, -1, withscores=True))
    print("N123 排名 =", r.zrevrank(hour_key, "N123"))     # 0（第1名）
    print("N123 分数 =", r.zscore(hour_key, "N123"))       # 3.0
    r.expire(hour_key, 7200)                       # 窗口关闭：2小时后自动淘汰

    # ---- 3. 滑动窗口：合并最近 60 个分钟桶 ----
    minute_keys = []
    for minute in (0, 1, 2):                       # 演示只建 3 个分钟桶
        mk = f"learn:hot:rank:m:202610121{minute:02d}"   # 实际应为 00~59
        r.delete(mk)
        r.zincrby(mk, 1, "N123")
        r.zincrby(mk, 2, "N126")
        minute_keys.append(mk)
    merged = "learn:hot:rank:merged"
    # ZUNIONSTORE：把多个桶的分数求并集（同成员分数相加）→ 滑动窗口榜单
    total = r.zunionstore(merged, minute_keys)
    print("滑动窗口合并后成员数 =", total)
    print("滑动窗口 TopN =", r.zrevrange(merged, 0, -1, withscores=True))
    for mk in minute_keys:
        r.delete(mk)
    r.delete(merged)


if __name__ == "__main__":
    import redis

    try:
        main()
    except redis.exceptions.ConnectionError as exc:
        need_redis(exc)
