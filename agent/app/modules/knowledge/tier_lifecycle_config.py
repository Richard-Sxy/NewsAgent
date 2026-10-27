"""Explicit, versioned settings for durable three-tier knowledge projections.

This configuration owns lifecycle policy, not endpoint credentials or model
selection. Enterprise vector connections remain in knowledge-search YAML and
model endpoints remain in model-runtime YAML. Lifecycle values are required so
the local simulator's numbers cannot become hidden production defaults. The
optional query_retrieval policy is inactive when omitted for compatibility.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.retrieval.tiered_vector import VectorTier
from app.retrieval.query_policy import QueryRetrievalPolicy


Version = Annotated[str, Field(min_length=1, max_length=256)]
PositiveSeconds = Annotated[float, Field(strict=True, gt=0, le=31_536_000)]
Weight = Annotated[float, Field(strict=True, ge=0)]


class KnowledgeLifecycleConfig(BaseModel):
    """A deliberate policy snapshot; changing it requires a new policy version."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    schema_version: Literal[1]
    policy_version: Version
    embedding_version: Version
    expected_embedding_dimensions: int = Field(strict=True, ge=1, le=65_536)
    chunking_version: Version
    chunk_chars: int = Field(strict=True, ge=1, le=100_000)
    chunk_overlap: int = Field(strict=True, ge=0, le=99_999)
    max_chunks_per_document: int = Field(strict=True, ge=1, le=1_024)
    max_vector_coordinates_per_document: int = Field(strict=True, ge=1, le=1_000_000)
    embedding_batch_size: int = Field(strict=True, ge=1, le=2_048)
    vector_batch_size: int = Field(strict=True, ge=1, le=4_096)

    hot_max_age_seconds: PositiveSeconds
    cold_min_age_seconds: PositiveSeconds
    reheat_hold_seconds: PositiveSeconds
    migration_interval_seconds: PositiveSeconds
    scan_batch_size: int = Field(strict=True, ge=1, le=1_000)
    dispatch_interval_seconds: PositiveSeconds

    max_attempts: int = Field(strict=True, ge=1, le=100)
    retry_initial_interval_seconds: PositiveSeconds
    retry_max_interval_seconds: PositiveSeconds
    activity_timeout_seconds: PositiveSeconds
    workflow_timeout_seconds: PositiveSeconds
    task_queue: str = Field(min_length=1, max_length=256)

    # Candidate counts are vector chunks, before news-level deduplication.
    query_candidate_limit: int = Field(strict=True, ge=1, le=100_000)
    query_max_candidates: int = Field(strict=True, ge=1, le=100_000)
    query_refill_factor: int = Field(strict=True, ge=2, le=16)
    query_request_timeout_seconds: float = Field(strict=True, gt=0, le=300)
    query_rrf_k: int = Field(strict=True, ge=1, le=100_000)
    query_weights: dict[VectorTier, Weight]
    query_max_concurrency: int = Field(strict=True, ge=1, le=256)
    max_search_rows: int = Field(strict=True, ge=1, le=1_000_000)
    # Omitted policies preserve the existing weighted RRF behavior.
    query_retrieval: QueryRetrievalPolicy | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        for name in ("policy_version", "embedding_version", "chunking_version", "task_queue"):
            value = getattr(self, name)
            if not value.strip() or value != value.strip():
                raise ValueError(f"{name} must not be blank or contain surrounding whitespace")
        if self.hot_max_age_seconds >= self.cold_min_age_seconds:
            raise ValueError("hot_max_age_seconds must be less than cold_min_age_seconds")
        if self.chunk_overlap >= self.chunk_chars:
            raise ValueError("chunk_overlap must be less than chunk_chars")
        if self.max_vector_coordinates_per_document < self.expected_embedding_dimensions:
            raise ValueError("max_vector_coordinates_per_document must fit at least one vector")
        if self.retry_initial_interval_seconds > self.retry_max_interval_seconds:
            raise ValueError("retry_max_interval_seconds must cover retry_initial_interval_seconds")
        if self.activity_timeout_seconds > self.workflow_timeout_seconds:
            raise ValueError("workflow_timeout_seconds must cover activity_timeout_seconds")
        if self.query_candidate_limit > self.query_max_candidates:
            raise ValueError("query_max_candidates must cover query_candidate_limit")
        if set(self.query_weights) != set(VectorTier):
            raise ValueError("query_weights must define hot, warm and cold exactly once")
        if not any(self.query_weights.values()):
            raise ValueError("at least one query weight must be greater than zero")
        return self

    def classify_tier(
        self,
        publish_time: datetime,
        now: datetime,
        *,
        reheat_until: datetime | None = None,
    ) -> VectorTier:
        """Classify by business time; never invent missing/future publish times.

        HOT is [0, hot_max), WARM is [hot_max, cold_min), and COLD starts
        at cold_min. A trusted explicit reheat temporarily overrides age. At
        the exact hold boundary normal age classification resumes. Callers
        use a separate monotonic clock to measure elapsed request latency.
        """
        _require_aware_datetime(publish_time, "publish_time")
        _require_aware_datetime(now, "now")
        publish_time = publish_time.astimezone(timezone.utc)
        now = now.astimezone(timezone.utc)
        if publish_time > now:
            raise ValueError("publish_time must not be later than now")
        if reheat_until is not None:
            _require_aware_datetime(reheat_until, "reheat_until")
            if reheat_until.astimezone(timezone.utc) > now:
                return VectorTier.HOT
        age_seconds = (now - publish_time).total_seconds()
        if age_seconds < self.hot_max_age_seconds:
            return VectorTier.HOT
        if age_seconds < self.cold_min_age_seconds:
            return VectorTier.WARM
        return VectorTier.COLD


def _require_aware_datetime(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject ambiguous repeated YAML keys instead of silently using the last."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ValueError("knowledge lifecycle YAML keys must be scalar") from exc
        if duplicate:
            raise ValueError("knowledge lifecycle YAML contains a duplicate key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping,
)


def load_knowledge_lifecycle_config(path: str | Path) -> KnowledgeLifecycleConfig:
    """Load a small, safe YAML policy; no environment interpolation or secrets."""
    source = Path(path)
    try:
        if not source.is_file() or source.stat().st_size > 1_048_576:
            raise ValueError("knowledge lifecycle YAML missing or exceeds 1 MiB")
        value = yaml.load(source.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("knowledge lifecycle YAML cannot be read") from exc
    return KnowledgeLifecycleConfig.model_validate(value)
