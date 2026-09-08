"""用户短期记忆、长期候选和已批准长期记忆的领域契约。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


NonBlank128 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
NonBlank256 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]
NonBlank500 = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]

MemoryTier = Literal["short_term", "long_term"]
MemoryStatus = Literal["active", "superseded", "expired", "revoked"]
MemoryKind = Literal[
    "task_goal",
    "pending_item",
    "temporary_preference",
    "stable_preference",
    "role_responsibility",
    "user_constraint",
]
MemoryOrigin = Literal[
    "explicit_user",
    "trusted_identity",
    "system_inference",
]
ShortTermMemoryOrigin = Literal["explicit_user", "system_inference"]
CandidateStatus = Literal["pending", "approved", "rejected", "expired"]
ResolvedMemoryOrigin = Literal[
    "explicit_user",
    "trusted_identity",
    "system_inference",
    "approved_candidate",
]


class UserMemoryScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    user_id: NonBlank128
    # 组织作用域必须与服务端可信身份上下文匹配。
    team_id: NonBlank128 | None = None
    section_id: NonBlank128 | None = None
    role_id: NonBlank128 | None = None


class UserMemoryContent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: MemoryKind
    # 用于冲突分组，例如 output.language 或 writing.tone。
    key: NonBlank128
    value: JsonValue
    summary: NonBlank500


class MemorySourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: Literal[
        "user_message",
        "operator_decision",
        "identity_system",
        "system_observation",
    ]
    source_id: NonBlank256
    captured_at: AwareDatetime


class ShortTermUserMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    scope: UserMemoryScope
    task_id: NonBlank128
    content: UserMemoryContent
    origin: ShortTermMemoryOrigin
    source_refs: tuple[MemorySourceRef, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    status: MemoryStatus = "active"
    created_at: AwareDatetime
    expires_at: AwareDatetime
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_time_window(self) -> Self:
        if self.created_at >= self.expires_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class LongTermMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    scope: UserMemoryScope
    proposed_content: UserMemoryContent
    origin: MemoryOrigin
    source_refs: tuple[MemorySourceRef, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    reason: NonBlank500
    status: CandidateStatus = "pending"
    created_at: AwareDatetime
    expires_at: AwareDatetime
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_time_window(self) -> Self:
        if self.created_at >= self.expires_at:
            raise ValueError("expires_at must be later than created_at")
        return self


class LongTermUserMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    scope: UserMemoryScope
    content: UserMemoryContent
    origin: Literal[
        "explicit_user",
        "trusted_identity",
        "approved_candidate",
    ]
    source_refs: tuple[MemorySourceRef, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    status: MemoryStatus = "active"
    confirmed_by: NonBlank128
    confirmed_at: AwareDatetime
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None
    recorded_at: AwareDatetime
    supersedes_memory_id: UUID | None = None
    version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        if (
            self.valid_until is not None
            and self.valid_from >= self.valid_until
        ):
            raise ValueError("valid_until must be later than valid_from")
        if self.confirmed_at > self.recorded_at:
            raise ValueError("confirmed_at cannot be later than recorded_at")
        return self


class PromoteMemoryCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    user_id: NonBlank128
    candidate_id: UUID
    approved_by: NonBlank128
    expected_version: int = Field(ge=1)
    idempotency_key: Annotated[
        str,
        StringConstraints(
            strip_whitespace=True,
            min_length=8,
            max_length=128,
        ),
    ]
    valid_until: AwareDatetime | None = None
    supersedes_memory_id: UUID | None = None


class ResolvedMemoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    memory_key: NonBlank128
    selected_memory_id: UUID
    selected_tier: MemoryTier
    selected_scope: UserMemoryScope
    content: UserMemoryContent
    origin: ResolvedMemoryOrigin
    source_refs: tuple[MemorySourceRef, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    version: int = Field(ge=1)
    overridden_memory_ids: tuple[UUID, ...] = ()


class ResolvedMemoryContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: NonBlank128
    user_id: NonBlank128
    task_id: NonBlank128
    team_id: NonBlank128 | None = None
    section_id: NonBlank128 | None = None
    role_id: NonBlank128 | None = None
    resolved_at: AwareDatetime
    memories: tuple[ResolvedMemoryItem, ...]
