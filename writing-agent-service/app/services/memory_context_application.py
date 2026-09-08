"""从持久化层读取长短期记忆并生成确定性运行上下文。"""

from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.user_memory import PostgresUserMemoryRepository
from app.schemas.user_memory import ResolvedMemoryContext
from app.services.memory_context import UserMemoryContextResolver


class MemoryContextPersistenceError(RuntimeError):
    """读取 Memory Context 时发生可重试的数据库错误。"""

    retryable = True


class MemoryContextApplicationService:
    """长短期记忆读取、作用域过滤和冲突解析的统一入口。"""

    def __init__(
        self,
        resolver: UserMemoryContextResolver | None = None,
    ) -> None:
        self._resolver = resolver or UserMemoryContextResolver()

    async def resolve_for_task(
        self,
        session: AsyncSession,
        *,
        tenant_id: str,
        user_id: str,
        task_id: str,
        now: datetime,
        team_id: str | None = None,
        section_id: str | None = None,
        role_id: str | None = None,
    ) -> ResolvedMemoryContext:
        """读取当前有效记忆并返回可重放的确定性解析结果。"""

        tenant_id = self._normalize_required("tenant_id", tenant_id)
        user_id = self._normalize_required("user_id", user_id)
        task_id = self._normalize_required("task_id", task_id)
        team_id = self._normalize_optional("team_id", team_id)
        section_id = self._normalize_optional("section_id", section_id)
        role_id = self._normalize_optional("role_id", role_id)
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        repository = PostgresUserMemoryRepository(session)
        try:
            short_term = await repository.list_active_short_term(
                tenant_id=tenant_id,
                user_id=user_id,
                task_id=task_id,
                now=now,
            )
            long_term = await repository.list_active_long_term(
                tenant_id=tenant_id,
                user_id=user_id,
                now=now,
            )
        except SQLAlchemyError as exc:
            raise MemoryContextPersistenceError(
                "读取长短期记忆失败"
            ) from exc

        return self._resolver.resolve(
            tenant_id=tenant_id,
            user_id=user_id,
            task_id=task_id,
            short_term=short_term,
            long_term=long_term,
            now=now,
            team_id=team_id,
            section_id=section_id,
            role_id=role_id,
        )

    @staticmethod
    def _normalize_required(name: str, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} cannot be empty")
        return normalized

    @staticmethod
    def _normalize_optional(name: str, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{name} cannot be blank when provided")
        return normalized
