"""热点分析运行的 PostgreSQL 持久化实现。"""

from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from app.schemas.hot_news_memory import HotNewsAnalysisMemory

from app.db.session import Database
from app.domain.errors import HotNewsPersistenceError
from app.models.hot_news import HotNewsAnalysisRun
from app.services.hot_news_orchestration import HotNewsRunResult
from app.workflows.contracts import HotNewsActivityOutcome


class PostgresHotNewsRunStore:
    """原子保存热点结果，并为 Temporal Activity 提供幂等重放。"""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_completed(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> HotNewsActivityOutcome | None:
        try:
            async with self._database.session() as session:
                query = select(HotNewsAnalysisRun).where(
                    HotNewsAnalysisRun.tenant_id == tenant_id,
                    HotNewsAnalysisRun.idempotency_key == idempotency_key,
                    HotNewsAnalysisRun.status == "completed",
                )
                db_result = await session.execute(query)
                run = db_result.scalar_one_or_none()
                return None if run is None else self._to_outcome(run)
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError(
                "查询已完成热点运行失败"
            ) from exc

    async def save_completed(
        self,
        *,
        result: HotNewsRunResult,
    ) -> HotNewsActivityOutcome:
        request = result.request
        values = {
            "tenant_id": request.tenant_id,
            "idempotency_key": result.idempotency_key,
            "window_start": request.window_start,
            "window_end": request.window_end,
            "production_bundle_version": request.production_bundle_version,
            "workflow_version": request.workflow_version,
            "status": "completed",
            "fetched_record_count": result.fetched_record_count,
            "metric_snapshot_count": len(result.metric_snapshots),
            "ranked_news_count": len(result.ranked_news),
            "analyzed_news_count": len(result.analyzed_news),
            "payload_schema_version": "2.0",
            "result_payload": self._serialize_result(result),
            "completed_at": datetime.now(timezone.utc),
        }

        try:
            async with self._database.session() as session:
                statement = (
                    insert(HotNewsAnalysisRun)
                    .values(**values)
                    .on_conflict_do_nothing(
                        constraint="uq_analysis_runs_tenant_idempotency_key"
                    )
                    .returning(HotNewsAnalysisRun.id)
                )
                insert_result = await session.execute(statement)
                inserted_id = insert_result.scalar_one_or_none()

                if inserted_id is not None:
                    return self._outcome_from_result(
                        run_id=inserted_id,
                        result=result,
                    )

                existing_result = await session.execute(
                    select(HotNewsAnalysisRun).where(
                        HotNewsAnalysisRun.tenant_id == request.tenant_id,
                        HotNewsAnalysisRun.idempotency_key
                        == result.idempotency_key,
                    )
                )
                existing = existing_result.scalar_one_or_none()
                if existing is None:
                    raise HotNewsPersistenceError(
                        "热点运行写入冲突，但无法读取已有记录"
                    )
                if existing.status != "completed":
                    raise HotNewsPersistenceError(
                        "已有热点运行尚未完成，暂时不能覆盖"
                    )
                return self._to_outcome(existing)
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError("保存热点运行失败") from exc

    async def get_analysis_memory(
        self,
        *,
        tenant_id: str,
        run_id: UUID,
        news_id: str,
    ) -> HotNewsAnalysisMemory | None:
        try:
            async with self._database.session() as session:
                statement = select(HotNewsAnalysisRun).where(
                    HotNewsAnalysisRun.id == run_id,
                    HotNewsAnalysisRun.tenant_id == tenant_id,
                    HotNewsAnalysisRun.status == "completed",
                )
                db_result = await session.execute(statement)
                run = db_result.scalar_one_or_none()

                if run is None:
                    return None

                return self._extract_analysis_memory(run=run, news_id=news_id)
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError("查询热点分析记忆失败") from exc

    async def list_analysis_memories(
        self,
        *,
        tenant_id: str,
        idempotency_key: str,
    ) -> tuple[HotNewsAnalysisMemory, ...]:
        """Load every validated item from one completed run for Data Loop repair."""

        try:
            async with self._database.session() as session:
                statement = select(HotNewsAnalysisRun).where(
                    HotNewsAnalysisRun.tenant_id == tenant_id,
                    HotNewsAnalysisRun.idempotency_key == idempotency_key,
                    HotNewsAnalysisRun.status == "completed",
                )
                db_result = await session.execute(statement)
                run = db_result.scalar_one_or_none()
                if run is None:
                    return ()
                return self._extract_all_analysis_memories(run=run)
        except SQLAlchemyError as exc:
            raise HotNewsPersistenceError("查询热点分析记忆列表失败") from exc

    @classmethod
    def _extract_all_analysis_memories(
        cls,
        *,
        run: HotNewsAnalysisRun,
    ) -> tuple[HotNewsAnalysisMemory, ...]:
        if run.payload_schema_version != "2.0":
            raise HotNewsPersistenceError(
                f"热点运行不包含输入快照：{run.payload_schema_version}"
            )
        if not isinstance(run.result_payload, dict):
            raise HotNewsPersistenceError("热点运行的pay_load格式错误")
        analyzed_news = run.result_payload.get("analyzed_news")
        if not isinstance(analyzed_news, list):
            raise HotNewsPersistenceError("热点运行的 analyzed_news 格式错误")

        memories: list[HotNewsAnalysisMemory] = []
        for item in analyzed_news:
            if not isinstance(item, dict) or not isinstance(item.get("news_id"), str):
                raise HotNewsPersistenceError("热点分析记录格式错误")
            memory = cls._extract_analysis_memory(
                run=run,
                news_id=item["news_id"],
            )
            if memory is None:
                raise HotNewsPersistenceError("热点分析记录缺少可重放快照")
            memories.append(memory)
        return tuple(memories)

    @staticmethod
    def _extract_analysis_memory(
        *,
        run: HotNewsAnalysisRun,
        news_id: str,
    ) -> HotNewsAnalysisMemory | None:
        if run.payload_schema_version != "2.0":
            raise HotNewsPersistenceError(
                f"热点运行不包含输入快照：{run.payload_schema_version}"
            )

        if not isinstance(run.result_payload, dict):
            raise HotNewsPersistenceError("热点运行的pay_load格式错误")

        analyzed_news = run.result_payload.get("analyzed_news")
        if not isinstance(analyzed_news, list):
            raise HotNewsPersistenceError(
                "热点运行的 analyzed_news 格式错误"
            )

        for item in analyzed_news:
            if not isinstance(item, dict):
                raise HotNewsPersistenceError(
                    "热点分析记录格式错误"
                )

            if item.get("news_id") != news_id:
                continue

            analysis = item.get("analysis")
            if not isinstance(analysis, dict):
                raise HotNewsPersistenceError(
                    "热点分析结果格式错误"
                )

            try:
                return HotNewsAnalysisMemory(
                    run_id=run.id,
                    run_idempotency_key=run.idempotency_key,
                    tenant_id=run.tenant_id,
                    news_id=item["news_id"],
                    rank=item["rank"],
                    production_bundle_version=run.production_bundle_version,
                    workflow_version=run.workflow_version,
                    payload_schema_version=run.payload_schema_version,
                    analysis_input=item["analysis_input"],
                    analysis_report=analysis["value"],
                    fastgpt_request_id=analysis.get("request_id"),
                    usage=analysis.get("usage") or {},
                    captured_at=item["captured_at"],
                    validated_at=item["validated_at"],
                    completed_at=run.completed_at,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise HotNewsPersistenceError(
                    "热点分析记忆不能通过 Schema 校验"
                ) from exc

        return None

    @staticmethod
    def _to_outcome(run: HotNewsAnalysisRun) -> HotNewsActivityOutcome:
        return HotNewsActivityOutcome(
            run_id=str(run.id),
            idempotency_key=run.idempotency_key,
            status="completed",
            fetched_record_count=run.fetched_record_count,
            metric_snapshot_count=run.metric_snapshot_count,
            ranked_news_count=run.ranked_news_count,
            analyzed_news_count=run.analyzed_news_count,
        )

    @staticmethod
    def _outcome_from_result(
        *,
        run_id: UUID,
        result: HotNewsRunResult,
    ) -> HotNewsActivityOutcome:
        return HotNewsActivityOutcome(
            run_id=str(run_id),
            idempotency_key=result.idempotency_key,
            status="completed",
            fetched_record_count=result.fetched_record_count,
            metric_snapshot_count=len(result.metric_snapshots),
            ranked_news_count=len(result.ranked_news),
            analyzed_news_count=len(result.analyzed_news),
        )

    @classmethod
    def _serialize_result(cls, result: HotNewsRunResult) -> dict[str, Any]:
        payload = cls._to_json_value(result)
        if not isinstance(payload, dict):
            raise HotNewsPersistenceError(
                "HotNewsRunResult 必须序列化为 JSON 对象"
            )
        return payload

    @classmethod
    def _to_json_value(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, BaseModel):
            return value.model_dump(mode="json")
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, Enum):
            return value.value
        if is_dataclass(value):
            return {
                item.name: cls._to_json_value(getattr(value, item.name))
                for item in fields(value)
            }
        if isinstance(value, dict):
            return {
                str(key): cls._to_json_value(child)
                for key, child in value.items()
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [cls._to_json_value(child) for child in value]
        if isinstance(value, (str, int, float, bool)):
            return value
        raise HotNewsPersistenceError(
            "热点运行包含不能序列化的字段类型："
            f"{type(value).__name__}"
        )
