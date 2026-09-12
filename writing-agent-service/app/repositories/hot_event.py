"""热点事件的 PostgreSQL Repository。"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import Database
from app.domain.errors import HotNewsPersistenceError
from app.models.hot_event import HotEventRecord
from app.schemas.hot_event import HotEventSnapshot


_OPEN_STATES = ("emerging", "active", "cooling")


class PostgresHotEventRepository:
    """热点事件状态快照的幂等读写入口。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_open(
        self,
        *,
        tenant_id: str,
    ) -> tuple[HotEventSnapshot, ...]:
        """读取租户当前未关闭的事件，用于下一次运行的合并。"""

        try:
            async with self._database.session() as session:
                statement = (
                    select(HotEventRecord)
                    .where(
                        HotEventRecord.tenant_id == tenant_id,
                        HotEventRecord.state.in_(_OPEN_STATES),
                    )
                    .order_by(
                        HotEventRecord.last_seen_at,
                        HotEventRecord.event_key,
                    )
                )
                result = await session.execute(statement)
                return tuple(
                    self._to_domain(record)
                    for record in result.scalars().all()
                )
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError("查询热点事件失败") from exc

    async def upsert(
        self,
        *,
        snapshots: tuple[HotEventSnapshot, ...],
    ) -> None:
        """按租户和 event_key 幂等写入，冲突时只更新可变状态。"""

        if not snapshots:
            return

        rows = [self._to_values(snapshot) for snapshot in snapshots]
        try:
            async with self._database.session() as session:
                statement = insert(HotEventRecord).values(rows)
                statement = statement.on_conflict_do_update(
                    index_elements=[
                        HotEventRecord.tenant_id,
                        HotEventRecord.event_key,
                    ],
                    set_={
                        "title": statement.excluded.title,
                        "content_type": statement.excluded.content_type,
                        "state": statement.excluded.state,
                        "first_seen_at": statement.excluded.first_seen_at,
                        "last_seen_at": statement.excluded.last_seen_at,
                        "last_window_end": (
                            statement.excluded.last_window_end
                        ),
                        "occurrence_count": (
                            statement.excluded.occurrence_count
                        ),
                        "distinct_news_count": (
                            statement.excluded.distinct_news_count
                        ),
                        "member_news_ids": (
                            statement.excluded.member_news_ids
                        ),
                        "latest_hot_score": (
                            statement.excluded.latest_hot_score
                        ),
                        "latest_metrics": statement.excluded.latest_metrics,
                        "version": statement.excluded.version,
                    },
                )
                await session.execute(statement)
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError("写入热点事件失败") from exc

    @staticmethod
    def _to_values(snapshot: HotEventSnapshot) -> dict:
        return {
            "id": uuid4(),
            "tenant_id": snapshot.tenant_id,
            "event_key": snapshot.event_key,
            "title": snapshot.title,
            "content_type": snapshot.content_type,
            "state": snapshot.state,
            "first_seen_at": snapshot.first_seen_at,
            "last_seen_at": snapshot.last_seen_at,
            "last_window_end": snapshot.last_window_end,
            "occurrence_count": snapshot.occurrence_count,
            "distinct_news_count": snapshot.distinct_news_count,
            "member_news_ids": list(snapshot.member_news_ids),
            "latest_hot_score": snapshot.latest_hot_score,
            "latest_metrics": snapshot.latest_metrics,
            "version": snapshot.version,
        }

    @staticmethod
    def _to_domain(record: HotEventRecord) -> HotEventSnapshot:
        return HotEventSnapshot(
            tenant_id=record.tenant_id,
            event_key=record.event_key,
            title=record.title,
            content_type=record.content_type,
            state=record.state,
            first_seen_at=record.first_seen_at,
            last_seen_at=record.last_seen_at,
            last_window_end=record.last_window_end,
            occurrence_count=record.occurrence_count,
            distinct_news_count=record.distinct_news_count,
            member_news_ids=tuple(record.member_news_ids),
            latest_hot_score=record.latest_hot_score,
            latest_metrics=dict(record.latest_metrics),
            version=record.version,
        )
