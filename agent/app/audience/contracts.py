"""Aggregate audience contracts; recipient identities never cross this Port."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    field_validator,
    model_validator,
)


Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class AudienceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    taxonomy_version: Identifier
    include_topic_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=10)
    exclude_topic_ids: tuple[Identifier, ...] = Field(default=(), max_length=10)
    match: Literal["any", "all"] = "any"

    @field_validator("include_topic_ids", "exclude_topic_ids")
    @classmethod
    def canonical_topics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("audience topics cannot contain duplicates")
        return tuple(sorted(value))

    @model_validator(mode="after")
    def disjoint_topics(self) -> Self:
        if set(self.include_topic_ids) & set(self.exclude_topic_ids):
            raise ValueError("included and excluded audience topics must be disjoint")
        return self


class AudienceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    run_id: UUID
    news_id: Identifier
    window_start: AwareDatetime
    window_end: AwareDatetime
    selection: AudienceSelection

    @model_validator(mode="after")
    def ordered_window(self) -> Self:
        if self.window_start >= self.window_end:
            raise ValueError("audience window_start must precede window_end")
        return self


def audience_query_fingerprint(query: AudienceQuery) -> str:
    content = json.dumps(query.model_dump(mode="json"), ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class AudienceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_id: UUID
    run_id: UUID
    news_id: Identifier
    taxonomy_version: Identifier
    query_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_id: Identifier
    profile_version: Identifier
    source_version: Identifier
    window_start: AwareDatetime
    window_end: AwareDatetime
    generated_at: AwareDatetime
    expires_at: AwareDatetime
    watermark: AwareDatetime
    is_complete: StrictBool
    matched_users: StrictInt = Field(ge=0)
    eligible_users: StrictInt = Field(ge=0)
    source_mode: Literal["enterprise", "synthetic"]

    @model_validator(mode="after")
    def coherent_snapshot(self) -> Self:
        if self.window_start >= self.window_end:
            raise ValueError("audience snapshot window is invalid")
        if self.generated_at >= self.expires_at:
            raise ValueError("audience snapshot must expire after generation")
        if self.eligible_users > self.matched_users:
            raise ValueError("eligible audience cannot exceed matched audience")
        return self


_PROVIDER_MESSAGES = {
    "timeout": "Audience provider request timed out",
    "unavailable": "Audience provider is unavailable",
    "rate_limited": "Audience provider is rate limited",
    "unauthorized": "Audience provider authorization failed",
    "redirect": "Audience provider redirects are not allowed",
    "rejected": "Audience provider rejected the request",
    "invalid_response": "Audience provider returned an invalid aggregate snapshot",
    "response_too_large": "Audience provider response exceeds the configured limit",
    "closed": "Audience provider client is closed",
    "invalid_query": "Audience query is not valid for this provider",
}


class AudienceProviderError(RuntimeError):
    """Static safe errors: no response body, endpoint, credentials or raw error."""

    def __init__(self, code: str = "unavailable", *, retryable: bool = False,
                 status_code: int | None = None) -> None:
        self.code = code if code in _PROVIDER_MESSAGES else "unavailable"
        self.retryable = retryable
        self.status_code = status_code
        super().__init__(_PROVIDER_MESSAGES[self.code])


class AudienceProfilePort(Protocol):
    async def estimate(self, query: AudienceQuery) -> AudienceSnapshot: ...

    async def close(self) -> None: ...
