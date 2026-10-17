"""HyperLogLog 统计 UV（去重计数的工程主力）。

知识点：
1. PFADD 把元素加进草图，PFCOUNT 返回估计基数，PFMERGE 合并多份草图；
2. 固定 ~12KB（16384 桶），从 1 万到 10 亿 UV 空间不变；标准误差约 0.8%；
3. 同一元素重复 PFADD 不影响计数——它存的是 hash 模式不是元素本身；
4. PFMERGE = 草图逐桶取 max，满足结合律 → 任意时间窗口的 UV 都能由桶合并得到；
5. 代价：只能数个数（并集封闭），不能像 Set 那样判断"某人来没来过"、不能做交集。

对照记忆：
  Set        精确、存全量元素、能交并差      → 小规模/需要明细判断
  HyperLogLog 近似(±0.8%)、12KB、只数个数    → 大规模 UV/任意窗口合并
"""
from __future__ import annotations

from common import get_client, need_redis


def main() -> None:
    r = get_client()

    # ---- 1. 单桶 UV：PFADD + PFCOUNT ----
    bucket_10 = "learn:uv:hll:2026101210"
    r.delete(bucket_10)
    users = [f"u{i}" for i in range(10000)]
    r.pfadd(bucket_10, *users)         # 一批加入；内部只留 hash 模式
    estimate = r.pfcount(bucket_10)
    print(f"真实 10000，HLL 估计 = {estimate}，误差 = {abs(estimate-10000)/100:.2f}%")

    # 重复添加不影响计数
    r.pfadd(bucket_10, "u1", "u2", "u3")
    print("重复添加后估计 =", r.pfcount(bucket_10))

    # ---- 2. 跨桶合并：任意窗口 UV 的根基 ----
    bucket_11 = "learn:uv:hll:2026101211"
    r.delete(bucket_11)
    # 11点桶有 8000 个老人（10点也来过）+ 2000 个新人
    r.pfadd(bucket_11, *[f"u{i}" for i in range(8000)] + [f"v{i}" for i in range(2000)])
    print("10点 UV =", r.pfcount(bucket_10))
    print("11点 UV =", r.pfcount(bucket_11))

    # 错误做法：10000 + 10000 = 20000（重复计数！）
    # 正确做法：PFMERGE 求并集
    merged = "learn:uv:hll:20261012_10-12"
    r.pfmerge(merged, bucket_10, bucket_11)
    print("10~12点合并 UV =", r.pfcount(merged))   # 约 12000

    # ---- 3. 顺便看看 HLL 的空间占用 ----
    try:
        debug = r.debug_object(bucket_10)
        print("草图编码 =", debug.get("encoding"))     # sparse/dense，总量级仍是 KB 级
    except Exception as exc:  # DEBUG 在托管/加固实例上常被禁用
        message = str(exc).lower()
        if "unknown command" in message or "not allowed" in message or "debug" in message:
            print("(debug_object 被服务端禁用，跳过空间检查)")
        else:
            raise

    r.delete(bucket_10, bucket_11, merged)


if __name__ == "__main__":
    import redis

    try:
        main()
    except redis.exceptions.ConnectionError as exc:
        need_redis(exc)
