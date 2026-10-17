"""Strict, bounded aggregate contracts shared by local and enterprise providers."""

from __future__ import annotations

from datetime import timezone
from decimal import Decimal, ROUND_HALF_EVEN
from hashlib import sha256
import json
from typing import Annotated, Literal, Protocol, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=160)]
Digest = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
Rate = Annotated[Decimal, Field(ge=0, le=1, allow_inf_nan=False)]
MAX_NEWS_PER_QUERY = 20
MAX_TOPIC_ITEMS = 50
PROFILE_POLICY_VERSION = "prewindow-exclude-trigger-interest-v1"
READER_DEFINITION = "detail-view-distinct-v1"
IDENTITY_POLICY_VERSION = "synthetic-user-id-tenant-scoped-v1"
MIN_LIFT_SUPPORT = 5


class AggregateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def ratio_decimal(numerator: int, denominator: int) -> Decimal:
    return (Decimal(numerator) / denominator).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN) if denominator else Decimal(0)


class ReaderProfileQuery(AggregateModel):
    tenant_id: UUID
    news_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=MAX_NEWS_PER_QUERY)
    window_start: AwareDatetime
    window_end: AwareDatetime
    source_release_sha256: Digest

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.window_start >= self.window_end or (self.window_end-self.window_start).total_seconds() > 86400:
            raise ValueError("reader query requires a positive window within 24 hours")
        if len(set(self.news_ids)) != len(self.news_ids):
            raise ValueError("reader news IDs cannot repeat")
        object.__setattr__(self, "news_ids", tuple(sorted(self.news_ids)))
        object.__setattr__(self, "window_start", self.window_start.astimezone(timezone.utc))
        object.__setattr__(self, "window_end", self.window_end.astimezone(timezone.utc))
        return self


def reader_profile_query_fingerprint(query: ReaderProfileQuery) -> str:
    return sha256(canonical_json(query.model_dump(mode="json")).encode()).hexdigest()


class ReaderTopic(AggregateModel):
    topic_id: Identifier
    reader_count: StrictInt = Field(ge=0)
    share_all: Rate
    share_profiled: Rate
    baseline_reader_count: StrictInt = Field(ge=0)
    baseline_profiled_count: StrictInt = Field(ge=0)
    baseline_share: Rate
    lift: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    low_sample: StrictBool

    @model_validator(mode="after")
    def baseline_consistent(self) -> Self:
        if self.baseline_reader_count > self.baseline_profiled_count:
            raise ValueError("baseline topic count exceeds population")
        if self.baseline_share != ratio_decimal(self.baseline_reader_count, self.baseline_profiled_count):
            raise ValueError("baseline topic share differs from counts")
        if self.lift is not None:
            if not self.baseline_share or self.low_sample or self.reader_count < MIN_LIFT_SUPPORT:
                raise ValueError("unsupported lift must be suppressed")
            expected = (self.share_profiled / self.baseline_share).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)
            if self.lift != expected:
                raise ValueError("reader topic lift differs from shares")
        return self


class ReaderInterestSummary(AggregateModel):
    lookback_hours: StrictInt = Field(gt=0, le=8760)
    profiled_reader_count: StrictInt = Field(ge=0)
    unknown_reader_count: StrictInt = Field(ge=0)
    cold_start_reader_count: StrictInt = Field(ge=0)
    baseline_profiled_count: StrictInt = Field(ge=0)
    topics: tuple[ReaderTopic, ...] = Field(default=(), max_length=MAX_TOPIC_ITEMS)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.cold_start_reader_count > self.unknown_reader_count:
            raise ValueError("cold start count must be included in unknown readers")
        if len({item.topic_id for item in self.topics}) != len(self.topics):
            raise ValueError("reader topics cannot repeat")
        total = self.profiled_reader_count + self.unknown_reader_count
        for item in self.topics:
            if item.reader_count > self.profiled_reader_count or item.baseline_profiled_count != self.baseline_profiled_count:
                raise ValueError("reader topic denominator differs from its interest population")
            if item.share_all != ratio_decimal(item.reader_count, total) or item.share_profiled != ratio_decimal(item.reader_count, self.profiled_reader_count):
                raise ValueError("reader topic shares differ from counts")
        return self


class ReaderProfileSummary(AggregateModel):
    news_id: Identifier
    source_mode: Literal["synthetic", "enterprise"]
    taxonomy_version: Identifier
    profile_policy_version: Identifier
    content_sha256: Digest | None = None
    content_topic_ids: tuple[Identifier, ...] = Field(default=(), max_length=512)
    reader_count: StrictInt = Field(ge=0)
    long_term: ReaderInterestSummary
    short_term: ReaderInterestSummary
    profile_cutoff: AwareDatetime
    trigger_article_excluded: StrictBool
    reader_definition: Identifier = READER_DEFINITION
    identity_policy_version: Identifier = IDENTITY_POLICY_VERSION
    count_method: Literal["exact_synthetic_distinct", "enterprise_exact_distinct", "enterprise_approx_distinct"]
    error_bound: Rate | None = None
    status: Literal["complete", "empty"]
    limitations: tuple[Annotated[str, StringConstraints(strict=True, min_length=1, max_length=500)], ...] = Field(default=(), max_length=12)

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.source_mode == "enterprise" and self.identity_policy_version == IDENTITY_POLICY_VERSION:
            raise ValueError("enterprise reader snapshots require an explicit enterprise identity policy")
        if (self.profile_policy_version == PROFILE_POLICY_VERSION
                and (self.long_term.lookback_hours, self.short_term.lookback_hours) != (720, 24)):
            raise ValueError("the bound reader interest policy requires 720-hour long-term and 24-hour short-term lookbacks")
        if len(set(self.content_topic_ids)) != len(self.content_topic_ids):
            raise ValueError("content topic IDs cannot repeat")
        for interest in (self.long_term, self.short_term):
            if interest.profiled_reader_count + interest.unknown_reader_count != self.reader_count:
                raise ValueError("reader coverage does not cover the visitor population")
            if self.profile_policy_version == PROFILE_POLICY_VERSION:
                for topic in interest.topics:
                    expected_low_sample = min(interest.profiled_reader_count, interest.baseline_profiled_count,
                        topic.reader_count, topic.baseline_reader_count) < MIN_LIFT_SUPPORT
                    if topic.low_sample != expected_low_sample:
                        raise ValueError("reader topic low-sample flag differs from the bound policy support")
            if (self.profile_policy_version == PROFILE_POLICY_VERSION
                    and self.count_method in {"exact_synthetic_distinct", "enterprise_exact_distinct"}):
                if interest.baseline_profiled_count < interest.profiled_reader_count:
                    raise ValueError("exact reader profiles must be contained in their baseline population")
                if any(topic.baseline_reader_count < topic.reader_count for topic in interest.topics):
                    raise ValueError("exact reader topic members must be contained in their baseline topic population")
        if (self.reader_count == 0) != (self.status == "empty"):
            raise ValueError("empty reader status differs from count")
        if self.count_method == "enterprise_approx_distinct" and self.error_bound is None:
            raise ValueError("approximate counts require an error bound")
        if self.count_method != "enterprise_approx_distinct" and self.error_bound not in (None, Decimal(0)):
            raise ValueError("exact counts cannot declare a nonzero approximation error")
        return self


class ReaderProfileBatch(AggregateModel):
    tenant_id: UUID
    window_start: AwareDatetime
    window_end: AwareDatetime
    source_release_sha256: Digest
    query_fingerprint: Digest
    snapshot_id: Identifier
    source_revision: StrictInt = Field(ge=1)
    taxonomy_version: Identifier
    profile_policy_version: Identifier
    generated_at: AwareDatetime
    expires_at: AwareDatetime | None = None
    watermark: AwareDatetime
    is_complete: StrictBool
    source_mode: Literal["synthetic", "enterprise"]
    items: tuple[ReaderProfileSummary, ...] = Field(min_length=1, max_length=MAX_NEWS_PER_QUERY)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.window_start >= self.window_end:
            raise ValueError("reader snapshot window is invalid")
        if len({item.news_id for item in self.items}) != len(self.items):
            raise ValueError("reader snapshot news IDs cannot repeat")
        if any(item.profile_cutoff > self.window_start for item in self.items):
            raise ValueError("reader profiles contain future features")
        if any((item.source_mode, item.taxonomy_version, item.profile_policy_version) !=
               (self.source_mode, self.taxonomy_version, self.profile_policy_version) for item in self.items):
            raise ValueError("reader summary provenance differs from its batch")
        if self.expires_at is not None and self.expires_at <= self.generated_at:
            raise ValueError("reader snapshot expiration precedes generation")
        if self.source_mode == "enterprise" and self.expires_at is None:
            raise ValueError("enterprise reader snapshots must expire")
        if self.source_mode == "synthetic" and any(item.count_method != "exact_synthetic_distinct" for item in self.items):
            raise ValueError("synthetic source must use the synthetic count method")
        if self.source_mode == "enterprise" and any(item.count_method == "exact_synthetic_distinct" for item in self.items):
            raise ValueError("enterprise snapshots cannot use synthetic counts")
        return self


_MESSAGES = {"timeout": "Reader profile request timed out", "unavailable": "Reader profile service is unavailable",
    "busy": "Reader profile service is busy", "closed": "Reader profile service is closed",
    "invalid_query": "Reader profile query is invalid or unsupported",
    "invalid_response": "Reader profile aggregate response failed validation",
    "unauthorized": "Reader profile provider authorization failed", "rate_limited": "Reader profile provider is rate limited",
    "redirect": "Reader profile redirects are not allowed", "response_too_large": "Reader profile response exceeds its bound"}


class ReaderProfileProviderError(RuntimeError):
    def __init__(self, code: str = "unavailable", *, retryable: bool = False, status_code: int | None = None):
        self.code = code if code in _MESSAGES else "unavailable"
        self.retryable, self.status_code = retryable, status_code
        super().__init__(_MESSAGES[self.code])


class NewsReaderProfilePort(Protocol):
    identity_sha256: str
    source_release_sha256: str
    expected_source_mode: str
    expected_taxonomy_version: str
    expected_profile_policy_version: str

    async def fetch(self, query: ReaderProfileQuery) -> ReaderProfileBatch: ...
    async def close(self) -> None: ...
