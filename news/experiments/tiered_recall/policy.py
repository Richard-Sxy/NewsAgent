"""分层策略：按时间决定基础层，按热度决定晋升/提前降温。

对应 knowledge-base-scale-design.md 的 TierPolicy：
    hot_ttl_days=30, warm_ttl_days=365
    promote_warm=5.0 (warm->hot), promote_cold=8.0 (cold->warm)
    min_hot_days=7, promote_consecutive=2
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

HOT = "hot"
WARM = "warm"
COLD = "cold"
TIERS = (HOT, WARM, COLD)


@dataclass(frozen=True)
class TierPolicy:
    hot_ttl_days: int = 30
    warm_ttl_days: int = 365
    promote_warm: float = 5.0
    promote_cold: float = 8.0
    min_hot_days: int = 7
    promote_consecutive: int = 2

    def age_tier(self, publish: date, now: date) -> str:
        age = (now - publish).days
        if age <= self.hot_ttl_days:
            return HOT
        if age <= self.warm_ttl_days:
            return WARM
        return COLD

    def decide(
        self,
        *,
        publish: date,
        now: date,
        heat: float,
        current: str,
        streak: int,
    ) -> tuple[str, int]:
        """返回 (目标层, 新的连续达标计数)。

        规则：
          老事件热度回流   cold -> warm   heat >= promote_cold
          温层热点回升     warm -> hot    heat >= promote_warm
          提前降温         hot  -> warm   已过 min_hot_days 且 heat < promote_warm
          否则按时间自然衰减到 age_tier, 但不低于热度支撑的层
        """
        age = (now - publish).days
        target = self.age_tier(publish, now)

        if current == HOT and heat >= self.promote_warm:
            return HOT, 0

        if current == COLD and heat >= self.promote_cold:
            streak = streak + 1
            if streak >= self.promote_consecutive:
                return WARM, 0
            return COLD, streak

        if current == WARM and heat >= self.promote_warm:
            streak = streak + 1
            if streak >= self.promote_consecutive:
                return HOT, 0
            return WARM, streak

        if current == HOT and age > self.min_hot_days and heat < self.promote_warm:
            return WARM, 0

        if TIERS.index(target) > TIERS.index(current):
            return target, 0
        return current, streak
