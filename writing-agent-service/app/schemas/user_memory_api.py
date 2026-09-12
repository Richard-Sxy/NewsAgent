"""用户 Memory 运营 API 的请求与响应契约。

请求体只允许业务字段：租户、用户和组织作用域一律由可信网关注入，
不接受调用方在 JSON 中声明，避免越权写入其他用户或其他团队的记忆。
"""

from typing import Annotated
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
)

from app.schemas.user_memory import (
    LongTermMemoryCandidate,
    LongTermUserMemory,
    MemorySourceRef,
    ResolvedMemoryContext,
    ShortTermMemoryOrigin,
    ShortTermUserMemory,
    UserMemoryContent,
    MemoryOrigin,
)


IdempotencyKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=8,
        max_length=128,
    ),
]


class CreateShortTermMemoryRequest(BaseModel):
    """创建一条有任务边界和过期时间的短期记忆。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
    ]
    content: UserMemoryContent
    origin: ShortTermMemoryOrigin
    source_refs: tuple[MemorySourceRef, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    expires_at: AwareDatetime
    idempotency_key: IdempotencyKey


class ProposeLongTermMemoryRequest(BaseModel):
    """提交一条待人工审批的长期记忆候选，不能直接写长期记忆。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    proposed_content: UserMemoryContent
    origin: MemoryOrigin
    source_refs: tuple[MemorySourceRef, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ]
    expires_at: AwareDatetime
    idempotency_key: IdempotencyKey


class PromoteMemoryCandidateRequest(BaseModel):
    """人工批准一条长期候选并生成生效的长期记忆。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_version: int = Field(ge=1)
    valid_until: AwareDatetime | None = None
    supersedes_memory_id: UUID | None = None
    idempotency_key: IdempotencyKey


class ShortTermMemoryWriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: ShortTermUserMemory
    created: bool


class MemoryCandidateWriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: LongTermMemoryCandidate
    created: bool


class LongTermMemoryWriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory: LongTermUserMemory
    created: bool


class ShortTermMemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memories: tuple[ShortTermUserMemory, ...]


class LongTermMemoryListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memories: tuple[LongTermUserMemory, ...]


class ResolvedMemoryContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    context: ResolvedMemoryContext
