"""向量分层决策与冷热迁移 —— 可独立运行的研究脚本。

这个文件不依赖任何第三方库，直接用 `python3 test/vector_tier_policy.py` 就能跑，
用来深度理解「热 / 温 / 冷」分层是怎么算出来的、迁移是怎么执行的。

================================================================================
一、核心思想
================================================================================
分层不是人工维护，而是一个可计算的函数：

    tier = f(年龄 age, 热度 heat, 业务优先级 pinned)

- 年龄：`now - publish_time`，免费且确定，是新闻业务的主线（新闻天然随时间衰减）。
- 热度：时间衰减的访问计数，用来修正——让持续被关注的老事件回到热层，
  让年轻但已无人看的新闻提前降温。
- 业务优先级：人工标记 / 风险等级，一票否决（pinned 永远在热层）。

================================================================================
二、年龄基线（不依赖任何埋点）
================================================================================
    age_days < 30        -> hot
    30 <= age_days < 365 -> warm
    age_days >= 365      -> cold

================================================================================
三、热度（指数衰减计数）
================================================================================
每次被命中/点击/转交写作时更新：

    H <- H * 2^(-(now - last_access) / tau_half) + w

    tau_half = 7 天（新闻热度半衰期）
    w        = 权重（检索命中=1，点击=3，转交写作=5）

只维护「被访问过」的条目，绝大多数冷数据 H=0，存储可控。

================================================================================
四、分层决策规则（带迟滞，防抖动）
================================================================================
    1. pinned 为真                          -> hot
    2. 基线 cold 且 H >= promote_cold       -> warm（老事件回升，逐级提升）
    3. 基线 warm 且 H >= promote_warm       -> hot
    4. 基线 hot 且 age>min_hot_days 且 H<demote_hot -> warm（提前降温）
    5. 否则                                  -> 基线层

提升阈值 > 降低阈值，且要求「连续 N 次达标」才提升，避免来回抖动。

================================================================================
五、迁移如何执行（1.75 亿条也不能逐条扫）
================================================================================
粗粒度：按时间分区（月）整块迁移。只处理「跨过边界」的分区，复杂度 O(越界数据)，
不是 O(总量)。

    热 -> 温：fp16 -> int8
    温 -> 冷：int8 -> binary / PQ

细粒度：少量例外（pinned、热度回升）逐条评估并提升。

一致性：先写目标层 -> 对账 count/hash -> 再删源层；向量主键不变，天然幂等。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

# 三层标识
HOT = "hot"
WARM = "warm"
COLD = "cold"

DAY_SECONDS = 86400.0

# 每层使用的向量精度（迁移时同步改精度）
PRECISION_BY_TIER = {HOT: "fp16", WARM: "int8", COLD: "binary"}


# ==============================================================================
# 一、分层决策策略（纯函数，可单测）
# ==============================================================================
@dataclass(frozen=True)
class TierPolicy:
    """冷热分层参数与决策规则。所有参数都有新闻业务的默认值。"""

    hot_ttl_days: float = 30.0
    warm_ttl_days: float = 365.0

    # 热度阈值：提升要够热，降温要够冷（迟滞）
    promote_warm: float = 5.0
    promote_cold: float = 8.0
    demote_hot: float = 0.5

    min_hot_days: float = 7.0
    promote_consecutive: int = 2

    def base_tier(self, age_days: float) -> str:
        """只看年龄的基线层。"""
        if age_days < self.hot_ttl_days:
            return HOT
        if age_days < self.warm_ttl_days:
            return WARM
        return COLD

    def decide(
        self,
        *,
        age_days: float,
        heat: float,
        pinned: bool = False,
        consecutive_hot: int = 0,
    ) -> str:
        """综合年龄、热度和业务优先级，返回目标层。

        consecutive_hot 表示该条目「连续多少次评估达到提升阈值」，
        用于迟滞，避免在阈值附近反复横跳。
        """
        # 规则 1：业务硬约束一票否决
        if pinned:
            return HOT

        base = self.base_tier(age_days)

        # 规则 2：老事件热度回升，cold -> warm
        if (
            base == COLD
            and heat >= self.promote_cold
            and consecutive_hot >= self.promote_consecutive
        ):
            return WARM

        # 规则 3：温层热点回升，warm -> hot
        if (
            base == WARM
            and heat >= self.promote_warm
            and consecutive_hot >= self.promote_consecutive
        ):
            return HOT

        # 规则 4：年轻但已无热度，提前降温
        if (
            base == HOT
            and age_days > self.min_hot_days
            and heat < self.demote_hot
        ):
            return WARM

        # 规则 5：默认按年龄
        return base

    def target_precision(self, tier: str) -> str:
        return PRECISION_BY_TIER[tier]


# ==============================================================================
# 二、热度衰减计数（内存版，生产可换 Redis + Lua 保证原子）
# ==============================================================================
@dataclass
class HeatTracker:
    """指数衰减计数：H <- H * 2^(-elapsed / tau_half) + weight。"""

    tau_half_days: float = 7.0
    # news_id -> (value, last_update_ts)
    _state: dict[str, tuple[float, float]] = field(default_factory=dict)

    def _decay(self, value: float, elapsed_seconds: float) -> float:
        if elapsed_seconds <= 0 or value == 0:
            return value
        return value * (0.5 ** (elapsed_seconds / (self.tau_half_days * DAY_SECONDS)))

    def touch(
        self,
        news_id: str,
        *,
        weight: float = 1.0,
        now: float | None = None,
    ) -> float:
        """记录一次访问；返回更新后的热度。"""
        now = now if now is not None else time.time()
        value, ts = self._state.get(news_id, (0.0, now))
        value = self._decay(value, now - ts) + weight
        self._state[news_id] = (value, now)
        return value

    def get(self, news_id: str, *, now: float | None = None) -> float:
        """读取当前热度（按经过时间衰减）。"""
        now = now if now is not None else time.time()
        value, ts = self._state.get(news_id, (0.0, now))
        return self._decay(value, now - ts)


# ==============================================================================
# 三、向量存储适配器（内存版，生产可换 Milvus / Qdrant）
# ==============================================================================
@dataclass
class ChunkRecord:
    """一条向量切片记录。id 即幂等主键：news_id:version:chunk_index。"""

    id: str
    news_id: str
    month: str          # 分区键，例如 "2026-09"
    vector: list[float]
    precision: str = "fp16"


class VectorStore(Protocol):
    def scan(self, *, tier: str, month: str, limit: int, offset: int) -> list[ChunkRecord]: ...
    def upsert(self, *, tier: str, records: list[ChunkRecord]) -> None: ...
    def count(self, *, tier: str, month: str) -> int: ...
    def delete(self, *, tier: str, month: str) -> None: ...
    def months(self) -> set[str]: ...


class InMemoryVectorStore:
    """用于研究的最小实现：{tier: {month: [records]}}。"""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, list[ChunkRecord]]] = {
            HOT: {}, WARM: {}, COLD: {}
        }

    def scan(self, *, tier: str, month: str, limit: int, offset: int) -> list[ChunkRecord]:
        return self.data[tier].get(month, [])[offset : offset + limit]

    def upsert(self, *, tier: str, records: list[ChunkRecord]) -> None:
        bucket = self.data[tier].setdefault(records[0].month, [])
        index = {r.id: i for i, r in enumerate(bucket)}
        for record in records:
            if record.id in index:
                bucket[index[record.id]] = record   # 幂等覆盖
            else:
                bucket.append(record)
                index[record.id] = len(bucket) - 1

    def count(self, *, tier: str, month: str) -> int:
        return len(self.data[tier].get(month, []))

    def delete(self, *, tier: str, month: str) -> None:
        self.data[tier].pop(month, None)

    def months(self) -> set[str]:
        return {
            month
            for tier in (HOT, WARM, COLD)
            for month in self.data[tier]
        }


# ==============================================================================
# 四、迁移执行：按时间分区整块迁移 + 改精度
# ==============================================================================
@dataclass
class TierLedger:
    """台账：记录每个分区当前所在层。"""

    partition_tier: dict[str, str] = field(default_factory=dict)

    def get(self, month: str) -> str | None:
        return self.partition_tier.get(month)

    def set(self, month: str, tier: str) -> None:
        self.partition_tier[month] = tier


class TierMigrationService:
    """只处理「跨过边界」的分区，复杂度 O(越界数据)。"""

    def __init__(
        self,
        *,
        store: VectorStore,
        ledger: TierLedger,
        policy: TierPolicy,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._policy = policy

    def _month_last_day(self, month: str) -> datetime:
        year, mon = (int(part) for part in month.split("-"))
        first = datetime(year, mon, 1, tzinfo=timezone.utc)
        next_month = (
            datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            if mon == 12
            else datetime(year, mon + 1, 1, tzinfo=timezone.utc)
        )
        return next_month - timedelta(days=1)

    def target_tier(self, month: str, *, now: datetime) -> str:
        age_days = (now - self._month_last_day(month)).total_seconds() / DAY_SECONDS
        return self._policy.base_tier(age_days)

    def _tier_of(self, month: str) -> str | None:
        for tier in (HOT, WARM, COLD):
            if self._store.count(tier=tier, month=month) > 0:
                return tier
        return None

    def migrate_partitions(self, *, now: datetime) -> list[dict]:
        moved: list[dict] = []
        for month in sorted(self._store.months()):
            target = self.target_tier(month, now=now)
            current = self._tier_of(month)
            if current is None or current == target:
                continue
            self._move(month=month, source=current, target=target)
            self._ledger.set(month, target)
            moved.append(
                {
                    "month": month,
                    "from": current,
                    "to": target,
                    "chunks": self._store.count(tier=target, month=month),
                }
            )
        return moved

    def _move(self, *, month: str, source: str, target: str) -> None:
        offset = 0
        while True:
            batch = self._store.scan(tier=source, month=month, limit=1000, offset=offset)
            if not batch:
                break
            # 迁移时同步改精度：fp16 -> int8 -> binary
            records = [
                replace(record, precision=self._policy.target_precision(target))
                for record in batch
            ]
            self._store.upsert(tier=target, records=records)  # 幂等
            offset += len(batch)

        # 对账通过后才删除源层
        source_count = self._store.count(tier=source, month=month)
        target_count = self._store.count(tier=target, month=month)
        assert target_count >= source_count, (month, source, target)
        self._store.delete(tier=source, month=month)


# ==============================================================================
# 五、演示：直接运行本文件查看决策与迁移结果
# ==============================================================================
def _demo_decisions() -> None:
    policy = TierPolicy()
    print("=" * 72)
    print("【演示 1】分层决策 decide()")
    print("=" * 72)
    scenarios = [
        # (说明, age_days, heat, pinned, consecutive_hot)
        ("发布3天，热度一般", 3, 1.0, False, 0),
        ("发布10天，已无热度", 10, 0.2, False, 0),
        ("发布45天，持续被检索", 45, 12.0, False, 2),
        ("发布45天，几乎无人看", 45, 1.0, False, 0),
        ("发布400天，无人看", 400, 0.0, False, 0),
        ("发布400天，突然翻红", 400, 9.0, False, 2),
        ("发布400天，但被置顶", 400, 0.0, True, 0),
    ]
    for label, age, heat, pinned, consec in scenarios:
        tier = policy.decide(
            age_days=age, heat=heat, pinned=pinned, consecutive_hot=consec
        )
        print(
            f"  {label:20s} age={age:>4} heat={heat:>5} pinned={str(pinned):5s} "
            f"-> {tier:4s} ({policy.target_precision(tier)})"
        )


def _demo_heat_decay() -> None:
    print()
    print("=" * 72)
    print("【演示 2】热度指数衰减（半衰期 7 天）")
    print("=" * 72)
    tracker = HeatTracker(tau_half_days=7.0)
    base = 1_700_000_000.0
    for i in range(10):
        tracker.touch("news-A", weight=1.0, now=base + i)
    print(f"  连续 10 次访问后        H = {tracker.get('news-A', now=base + 9):.2f}")
    for days in (7, 14, 30):
        value = tracker.get("news-A", now=base + 9 + days * DAY_SECONDS)
        print(f"  再过 {days:>2} 天后            H = {value:.2f}")


def _demo_migration() -> None:
    print()
    print("=" * 72)
    print("【演示 3】按分区整块迁移（当前时间 2026-09-15）")
    print("=" * 72)
    policy = TierPolicy()
    store = InMemoryVectorStore()
    ledger = TierLedger()

    # 初始：所有分区都在热层
    for month in ("2025-06", "2026-05", "2026-08", "2026-09"):
        records = [
            ChunkRecord(
                id=f"news-{month}-{i}:v1:{j}",
                news_id=f"news-{month}-{i}",
                month=month,
                vector=[0.1] * 4,
                precision="fp16",
            )
            for i in range(2)
            for j in range(3)
        ]
        store.upsert(tier=HOT, records=records)
        ledger.set(month, HOT)

    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    service = TierMigrationService(store=store, ledger=ledger, policy=policy)
    moved = service.migrate_partitions(now=now)

    print("  分区年龄与目标层：")
    for month in ("2025-06", "2026-05", "2026-08", "2026-09"):
        age = (now - service._month_last_day(month)).days
        print(f"    {month}  age={age:>4} 天 -> 目标 {policy.base_tier(age)}")

    print("  实际迁移：")
    for item in moved:
        print(
            f"    {item['month']}: {item['from']} -> {item['to']} "
            f"({item['chunks']} 条切片)"
        )

    print("  迁移后各层分布：")
    for tier in (HOT, WARM, COLD):
        months = {m: len(store.data[tier][m]) for m in store.data[tier]}
        print(f"    {tier:4s}: {months}")


def main() -> None:
    _demo_decisions()
    _demo_heat_decay()
    _demo_migration()


if __name__ == "__main__":
    main()
