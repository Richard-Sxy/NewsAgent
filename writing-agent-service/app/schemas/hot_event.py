"""热点事件去重、合并与生命周期状态机的确定性领域契约。

事件归并只使用 news_id 之间的关系边（来自检索重排后的关联新闻）和窗口
出现情况，不做模糊聚类，也不调用大模型。相同输入必须得到相同结果。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


HotEventState = Literal["emerging", "active", "cooling", "closed"]

HOT_EVENT_KEY_PREFIX = "hot-event-"

_OPEN_STATES = frozenset({"emerging", "active", "cooling"})


class HotEventObservation(BaseModel):
    """一次运行中观察到的单条热点新闻及其关系边。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    news_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    content_type: Literal["article", "video"]
    window_end: AwareDatetime
    hot_score: float = Field(ge=0, le=1)
    metrics: dict[str, Any] = Field(default_factory=dict)
    related_news_ids: tuple[str, ...] = Field(default=(), max_length=5)


class HotEventSnapshot(BaseModel):
    """持久化事件的状态快照，用于幂等 upsert。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: str = Field(min_length=1, max_length=128)
    event_key: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=500)
    content_type: str = Field(min_length=1, max_length=32)
    state: HotEventState
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    last_window_end: AwareDatetime
    occurrence_count: int = Field(ge=1)
    distinct_news_count: int = Field(ge=1)
    member_news_ids: tuple[str, ...] = Field(min_length=1, max_length=50)
    latest_hot_score: float = Field(ge=0, le=1)
    latest_metrics: dict[str, Any] = Field(default_factory=dict)
    version: int = Field(default=1, ge=1)


class HotEventLifecyclePolicy:
    """按出现情况推进事件状态，并确定性地合并同一连通分量。"""

    def __init__(
        self,
        *,
        policy_version: str = "hot-event-lifecycle-v1",
        cooling_after: timedelta = timedelta(hours=6),
        close_after: timedelta = timedelta(hours=24),
        max_members: int = 50,
    ) -> None:
        policy_version = policy_version.strip()
        if not policy_version:
            raise ValueError("policy_version cannot be empty")
        if cooling_after <= timedelta(0):
            raise ValueError("cooling_after must be positive")
        if close_after <= cooling_after:
            raise ValueError("close_after must be later than cooling_after")
        if not 1 <= max_members <= 50:
            raise ValueError("max_members must be between 1 and 50")
        self.policy_version = policy_version
        self.cooling_after = cooling_after
        self.close_after = close_after
        self.max_members = max_members

    def plan(
        self,
        *,
        tenant_id: str,
        observations: tuple[HotEventObservation, ...],
        existing_events: tuple[HotEventSnapshot, ...],
        now: datetime,
    ) -> tuple[HotEventSnapshot, ...]:
        """返回本次运行需要 upsert 的事件快照（含合并与老化）。

        未观察且尚未达到老化阈值的事件不会出现在结果中，保持原状。
        """

        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        observed_by_id = {item.news_id: item for item in observations}
        existing_by_member: dict[str, list[HotEventSnapshot]] = {}
        for event in existing_events:
            for member in event.member_news_ids:
                existing_by_member.setdefault(member, []).append(event)

        parent: dict[str, str] = {}

        def find(node: str) -> str:
            parent.setdefault(node, node)
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                # 固定指向字典序更小的根，保证结果与输入顺序无关。
                smaller, larger = sorted((left_root, right_root))
                parent[larger] = smaller

        for news_id in observed_by_id:
            find(news_id)
        for event in existing_events:
            for member in event.member_news_ids:
                find(member)

        known_ids = set(parent)
        for observation in observations:
            for related_id in observation.related_news_ids:
                if related_id in known_ids:
                    union(observation.news_id, related_id)

        components: dict[str, set[str]] = {}
        for news_id in parent:
            components.setdefault(find(news_id), set()).add(news_id)

        planned: dict[str, HotEventSnapshot] = {}
        absorbed_keys: set[str] = set()

        for root in sorted(components):
            members = components[root]
            observed_members = sorted(members & set(observed_by_id))
            if not observed_members:
                continue

            touching = sorted(
                {
                    event.event_key: event
                    for member in members
                    for event in existing_by_member.get(member, ())
                }.values(),
                key=lambda item: (item.first_seen_at, item.event_key),
            )
            representative = touching[0] if touching else None
            absorbed_keys.update(
                event.event_key for event in touching[1:]
            )

            merged_members = self._bounded_members(
                members=members,
                observed_members=observed_members,
            )
            representative_observation = max(
                (observed_by_id[news_id] for news_id in observed_members),
                key=lambda item: (item.hot_score, item.news_id),
            )
            window_end = max(
                observed_by_id[news_id].window_end
                for news_id in observed_members
            )
            previous_occurrence = (
                representative.occurrence_count
                if representative is not None
                else 0
            )
            # 同一窗口重放（Activity 幂等重试）不重复计数、不推进状态。
            already_applied = (
                representative is not None
                and representative.last_window_end >= window_end
            )
            occurrence_count = (
                previous_occurrence
                if already_applied
                else previous_occurrence + 1
            )
            first_seen_at = (
                representative.first_seen_at
                if representative is not None
                else representative_observation.window_end
            )
            event_key = (
                representative.event_key
                if representative is not None
                else self._new_event_key(merged_members)
            )
            if already_applied and representative is not None:
                state = representative.state
            else:
                state = "emerging" if occurrence_count == 1 else "active"
            planned[event_key] = HotEventSnapshot(
                tenant_id=tenant_id,
                event_key=event_key,
                title=representative_observation.title,
                content_type=representative_observation.content_type,
                state=state,
                first_seen_at=first_seen_at,
                last_seen_at=window_end,
                last_window_end=window_end,
                occurrence_count=occurrence_count,
                distinct_news_count=len(merged_members),
                member_news_ids=merged_members,
                latest_hot_score=representative_observation.hot_score,
                latest_metrics=dict(representative_observation.metrics),
                version=(
                    1
                    if representative is None
                    else (
                        representative.version
                        if already_applied
                        else representative.version + 1
                    )
                ),
            )

        # 被合并吸收的事件显式关闭，避免同一连通分量存在多个活跃事件。
        for event in existing_events:
            if event.event_key in absorbed_keys:
                if event.state == "closed":
                    continue
                planned[event.event_key] = event.model_copy(
                    update={
                        "state": "closed",
                        "version": event.version + 1,
                    }
                )
                continue
            if event.event_key in planned:
                continue
            aged_state = self._aged_state(event=event, now=now)
            if aged_state is not None:
                planned[event.event_key] = event.model_copy(
                    update={
                        "state": aged_state,
                        "version": event.version + 1,
                    }
                )

        return tuple(planned[key] for key in sorted(planned))

    def _bounded_members(
        self,
        *,
        members: set[str],
        observed_members: list[str],
    ) -> tuple[str, ...]:
        ordered = sorted(members)
        if len(ordered) <= self.max_members:
            return tuple(ordered)
        remaining = [
            member for member in ordered if member not in set(observed_members)
        ]
        room = max(0, self.max_members - len(observed_members))
        return tuple(sorted(set(observed_members) | set(remaining[:room])))

    def _aged_state(
        self,
        *,
        event: HotEventSnapshot,
        now: datetime,
    ) -> HotEventState | None:
        if event.state not in _OPEN_STATES:
            return None
        idle = now - event.last_seen_at
        if idle >= self.close_after:
            return "closed"
        if idle >= self.cooling_after and event.state != "cooling":
            return "cooling"
        return None

    @staticmethod
    def _new_event_key(member_news_ids: tuple[str, ...]) -> str:
        identity = "\x1f".join(sorted(member_news_ids))
        digest = sha256(identity.encode("utf-8")).hexdigest()
        return f"{HOT_EVENT_KEY_PREFIX}{digest}"
