import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_memory import MemoryPromotionRequestRecord
from app.repositories.user_memory import PostgresUserMemoryRepository
from app.schemas.user_memory import (
    LongTermUserMemory,
    PromoteMemoryCandidateCommand
)
from app.services.memory_promotion import (
    MemoryPromotionRejectedError,
    MemoryPromotionService,
)

class MemoryPromotionTargetNotFoundError(LookupError):
    """候选或被替代记忆不存在，或不属于当前租户或用户"""

class MemoryPromotionConflictError(RuntimeError):
    """幂等键冲突、版本冲突或晋升状态冲突。"""

class MemoryPromotionPersistenceError(RuntimeError):
    """晋升事务持久化失败。"""

@dataclass(frozen=True, slots=True)
class MemoryPromotionOutcome:
    memory: LongTermUserMemory
    created: bool

def build_command_fingerprint(
    command: PromoteMemoryCandidateCommand,
) -> str:
    payload = command.model_dump(
        mode="json",
        exclude={"idempotency_key"},
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    return hashlib.sha256(encoded).hexdigest()

class MemoryPromotionApplicationService:
    def __init__(
        self,
        domain_service: MemoryPromotionService | None = None,
    ) -> None:
        self._domain_service = domain_service or MemoryPromotionService()

    async def promote(
        self,
        session: AsyncSession,
        *,
        command: PromoteMemoryCandidateCommand,
        now: datetime,
        memory_id: UUID | None = None,
    ) -> MemoryPromotionOutcome:
        repository = PostgresUserMemoryRepository(session)
        fingerprint = build_command_fingerprint(command)

        try:
            # 查询已有幂等请求
            existing_request = await repository.get_promotion_request_for_update(
                tenant_id=command.tenant_id, 
                idempotency_key=command.idempotency_key
            )

            # 存在时走幂等重放
            if existing_request is not None:
                return await self._replay_existing_request(
                    repository=repository,
                    command=command,
                    request=existing_request,
                    fingerprint=fingerprint,
                )
            # 读取并锁定待审批的长期记忆候选
            candidate = await repository.get_candidate_for_update(
                tenant_id=command.tenant_id,
                user_id=command.user_id,
                candidate_id=command.candidate_id,
            )
            # 查询不到时，不区分不存在，跨租户或跨用户，避免泄露其他租户的数据是否存在
            if candidate is None:
                raise MemoryPromotionTargetNotFoundError(
                    "长期记忆候选不存在"
                )
            # 如果本次晋升要替换旧记忆，则读取并锁定旧记忆
            superseded_memory = None
            if command.supersedes_memory_id is not None:
                superseded_memory = (
                    await repository.get_long_term_for_update(
                        tenant_id=command.tenant_id,
                        user_id=command.user_id,
                        memory_id=command.supersedes_memory_id,
                    )
                )
                if superseded_memory is None:
                    raise MemoryPromotionTargetNotFoundError(
                        "被替代的长期记忆不存在"
                    )
            # 调用纯领域服务进行业务规则校验并晋升结果
            promotion_result = self._domain_service.promote(
                candidate=candidate,
                command=command,
                now=now,
                superseded_memory=superseded_memory,
                memory_id=memory_id,
            )
            # 尝试占用本次请求的幂等键
            promotion_request = (
                await repository.reserve_promotion_request(
                    command=command,
                    request_fingerprint=fingerprint,
                )
            )
            # 如果占用失败，说明另一个并发请求已经使用了幂等键
            if promotion_request is None:
                concurrent_request = (
                    await repository.get_promotion_request_for_update(
                        tenant_id=command.tenant_id,
                        idempotency_key=command.idempotency_key,
                    )
                )
                if concurrent_request is None:
                    raise MemoryPromotionPersistenceError(
                        "幂等键写入冲突，但无法读取已有晋升请求。"
                    )
                return await self._replay_existing_request(
                    repository=repository,
                    command=command,
                    request=concurrent_request,
                    fingerprint=fingerprint,
                )
            # 如果存在旧记忆，现将旧记忆标记为superseded，先失效旧版本再插入新版本，否则会触发active长期记忆唯一索引
            if superseded_memory is not None:
                superseded = await repository.mark_memory_superseded(
                    memory=superseded_memory,
                    superseded_at=now,
                )
                if not superseded:
                    raise MemoryPromotionConflictError(
                        "旧长期记忆状态已经发生变化"
                    )
            # 通过状态和版本条件更新候选
            candidate_updated = (
                await repository.mark_candidate_approved(
                    candidate=promotion_result.approved_candidate,
                )
            )    
            if not candidate_updated:
                raise MemoryPromotionConflictError(
                    "长期记忆候选版本或状态已经发生变化"
                )
            # 插入新生成的长期记忆
            await repository.insert_long_term_memory(
                memory=promotion_result.long_term_memory,
                candidate_id=candidate.id,
            )
            # 将幂等请求更新为 completed
            request_completed = (
                await repository.complete_promotion_request(
                    request_id=promotion_request.id,
                    tenant_id=command.tenant_id,
                    memory_id=(
                        promotion_result.long_term_memory.id
                    ),
                )
            )
            if not request_completed:
                raise MemoryPromotionConflictError(
                    "长期记忆晋升请求状态已经发生变化"
                )
            return MemoryPromotionOutcome(
                memory=promotion_result.long_term_memory,
                created=True,
            )

        # 业务异常直接向上抛出，由 API 层转换成 404 或 409
        except (
            MemoryPromotionTargetNotFoundError,
            MemoryPromotionConflictError,
            MemoryPromotionRejectedError,
            MemoryPromotionPersistenceError,
        ):
            raise

        # 唯一索引、外键或者并发写入冲突统一转换为业务冲突
        except IntegrityError as exc:
            raise MemoryPromotionConflictError(
                "长期记忆晋升发生数据库并发冲突"
            ) from exc

        # 其他数据库异常统一转换为可重试的持久化异常
        except SQLAlchemyError as exc:
            raise MemoryPromotionPersistenceError(
                "长期记忆晋升持久化失败"
            ) from exc

    async def _replay_existing_request(
        self,
        *,
        repository: PostgresUserMemoryRepository,
        command: PromoteMemoryCandidateCommand,
        request: MemoryPromotionRequestRecord,
        fingerprint: str,
    ) -> MemoryPromotionOutcome:
        """校验并返回已经完成的幂等请求结果。"""

        # 第一步：相同幂等键不能对应不同的业务内容
        if request.request_fingerprint != fingerprint:
            raise MemoryPromotionConflictError(
                "同一个 idempotency_key 对应不同的晋升内容"
            )

        # 第二步：只有 completed 请求可以安全重放
        if request.status != "completed":
            raise MemoryPromotionConflictError(
                "长期记忆晋升请求仍在处理中"
            )

        # 第三步：completed 请求必须保存最终记忆 ID
        if request.resulting_memory_id is None:
            raise MemoryPromotionPersistenceError(
                "已完成的晋升请求缺少 resulting_memory_id"
            )

        # 第四步：读取第一次请求生成的长期记忆
        memory = await repository.get_long_term(
            tenant_id=command.tenant_id,
            user_id=command.user_id,
            memory_id=request.resulting_memory_id,
        )

        # 第五步：晋升请求存在但结果不存在，说明数据库数据不完整
        if memory is None:
            raise MemoryPromotionPersistenceError(
                "无法读取已完成晋升请求对应的长期记忆"
            )

        # 第六步：返回第一次生成的结果，不重复写数据库
        return MemoryPromotionOutcome(
            memory=memory,
            created=False,
        )
