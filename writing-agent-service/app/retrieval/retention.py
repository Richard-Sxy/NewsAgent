"""Retention policy and guarded deletion orchestration for news content.

Retention is deterministic business logic.  It never asks an LLM whether a
record should be deleted, and destructive execution is disabled unless an
explicit approver is supplied.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, Sequence

from app.retrieval.tiered_vector import TieredVectorIndexPort, VectorTier


class RetentionAction(StrEnum):
    RETAIN = "retain"
    ARCHIVE = "archive"
    DELETE = "delete"


class RetentionReason(StrEnum):
    LEGAL_HOLD = "legal_hold"
    WITHDRAWN = "withdrawn"
    WITHIN_RETENTION = "within_retention"
    RETENTION_EXPIRED = "retention_expired"


@dataclass(frozen=True, slots=True)
class RetainedChunk:
    chunk_id: str
    tier: VectorTier


@dataclass(frozen=True, slots=True)
class NewsRetentionCandidate:
    tenant_id: str
    news_id: str
    content_version: int
    publish_time: datetime
    object_uri: str
    chunks: tuple[RetainedChunk, ...]
    object_size_bytes: int = 0
    legal_hold: bool = False
    withdrawn: bool = False

    def __post_init__(self) -> None:
        if not self.tenant_id.strip() or not self.news_id.strip():
            raise ValueError("tenant_id and news_id cannot be empty")
        if self.content_version <= 0:
            raise ValueError("content_version must be greater than 0")
        if not self.object_uri.strip():
            raise ValueError("object_uri cannot be empty")
        if self.object_size_bytes < 0:
            raise ValueError("object_size_bytes cannot be negative")
        chunk_ids = [chunk.chunk_id for chunk in self.chunks]
        if any(not chunk_id.strip() for chunk_id in chunk_ids):
            raise ValueError("chunk_id cannot be empty")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("candidate contains duplicate chunk_id values")


@dataclass(frozen=True, slots=True)
class RetentionDecision:
    action: RetentionAction
    reason: RetentionReason
    cutoff: datetime


@dataclass(frozen=True, slots=True)
class RetentionAuditEvent:
    tenant_id: str
    news_id: str
    content_version: int
    action: RetentionAction
    reason: RetentionReason
    status: str
    approved_by: str
    occurred_at: datetime
    chunk_count: int
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class RetentionExecutionResult:
    news_id: str
    action: RetentionAction
    reason: RetentionReason
    dry_run: bool
    deleted_vector_chunks: int = 0


@dataclass(frozen=True, slots=True)
class RetentionCandidatePage:
    items: tuple[NewsRetentionCandidate, ...]
    next_cursor: str | None


class RetentionCandidateRepository(Protocol):
    """Read-only enterprise content-catalog boundary."""

    async def list_candidates(
        self,
        *,
        tenant_id: str,
        published_before: datetime,
        include_withdrawn: bool,
        cursor: str | None,
        limit: int,
    ) -> RetentionCandidatePage: ...


@dataclass(frozen=True, slots=True)
class RetentionDryRunReport:
    tenant_id: str
    generated_at: datetime
    cutoff: datetime
    policy_years: int
    expired_action: RetentionAction
    scanned_news: int
    retain_news: int
    archive_news: int
    delete_news: int
    legal_hold_news: int
    withdrawn_news: int
    total_chunks: int
    chunks_by_tier: dict[str, int]
    estimated_object_bytes: int
    oldest_publish_time: datetime | None
    newest_publish_time: datetime | None
    sample_news_ids: tuple[str, ...]
    truncated: bool


class RetentionDryRunService:
    """Page through metadata and produce a side-effect-free capacity report."""

    def __init__(
        self,
        *,
        repository: RetentionCandidateRepository,
        policy: "RetentionPolicy",
        page_size: int = 500,
        sample_size: int = 20,
    ) -> None:
        if not 1 <= page_size <= 5_000:
            raise ValueError("page_size must be between 1 and 5000")
        if not 0 <= sample_size <= 100:
            raise ValueError("sample_size must be between 0 and 100")
        self._repository = repository
        self._policy = policy
        self._page_size = page_size
        self._sample_size = sample_size

    async def scan(
        self,
        *,
        tenant_id: str,
        now: datetime,
        max_candidates: int | None = None,
    ) -> RetentionDryRunReport:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        RetentionPolicy._validate_datetime(now, "now")
        if max_candidates is not None and max_candidates <= 0:
            raise ValueError("max_candidates must be greater than 0")

        cutoff = RetentionPolicy._subtract_years(
            now,
            self._policy.retention_years,
        )
        action_counts = {action: 0 for action in RetentionAction}
        chunks_by_tier = {tier.value: 0 for tier in VectorTier}
        legal_hold_news = 0
        withdrawn_news = 0
        scanned_news = 0
        total_chunks = 0
        estimated_object_bytes = 0
        oldest: datetime | None = None
        newest: datetime | None = None
        samples: list[str] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        truncated = False

        while True:
            remaining = (
                self._page_size
                if max_candidates is None
                else min(self._page_size, max_candidates - scanned_news)
            )
            if remaining <= 0:
                truncated = True
                break
            page = await self._repository.list_candidates(
                tenant_id=tenant_id,
                published_before=cutoff,
                include_withdrawn=True,
                cursor=cursor,
                limit=remaining,
            )
            if len(page.items) > remaining:
                raise RuntimeError("candidate repository exceeded requested page limit")

            for candidate in page.items:
                if candidate.tenant_id != tenant_id:
                    raise RuntimeError("candidate repository crossed tenant boundary")
                decision = self._policy.decide(candidate, now=now)
                action_counts[decision.action] += 1
                scanned_news += 1
                total_chunks += len(candidate.chunks)
                estimated_object_bytes += candidate.object_size_bytes
                legal_hold_news += int(candidate.legal_hold)
                withdrawn_news += int(candidate.withdrawn)
                for chunk in candidate.chunks:
                    chunks_by_tier[chunk.tier.value] += 1
                oldest = (
                    candidate.publish_time
                    if oldest is None
                    else min(oldest, candidate.publish_time)
                )
                newest = (
                    candidate.publish_time
                    if newest is None
                    else max(newest, candidate.publish_time)
                )
                if (
                    decision.action is not RetentionAction.RETAIN
                    and len(samples) < self._sample_size
                ):
                    samples.append(candidate.news_id)

            next_cursor = page.next_cursor
            if next_cursor is None:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise RuntimeError("candidate repository returned a repeated cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            if not page.items:
                raise RuntimeError("candidate repository returned empty non-final page")

        return RetentionDryRunReport(
            tenant_id=tenant_id,
            generated_at=now,
            cutoff=cutoff,
            policy_years=self._policy.retention_years,
            expired_action=self._policy.expired_action,
            scanned_news=scanned_news,
            retain_news=action_counts[RetentionAction.RETAIN],
            archive_news=action_counts[RetentionAction.ARCHIVE],
            delete_news=action_counts[RetentionAction.DELETE],
            legal_hold_news=legal_hold_news,
            withdrawn_news=withdrawn_news,
            total_chunks=total_chunks,
            chunks_by_tier=chunks_by_tier,
            estimated_object_bytes=estimated_object_bytes,
            oldest_publish_time=oldest,
            newest_publish_time=newest,
            sample_news_ids=tuple(samples),
            truncated=truncated,
        )


class FullTextDeletionPort(Protocol):
    async def delete_news(
        self,
        *,
        tenant_id: str,
        news_id: str,
        content_version: int,
    ) -> None: ...


class NewsCacheInvalidationPort(Protocol):
    async def invalidate_news(
        self,
        *,
        tenant_id: str,
        news_id: str,
    ) -> None: ...


class ContentObjectRetentionPort(Protocol):
    async def archive(self, *, object_uri: str) -> None: ...

    async def delete(self, *, object_uri: str) -> None: ...


class RetentionAuditPort(Protocol):
    async def append(self, event: RetentionAuditEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    retention_years: int = 10
    expired_action: RetentionAction = RetentionAction.ARCHIVE

    def __post_init__(self) -> None:
        if self.retention_years <= 0:
            raise ValueError("retention_years must be greater than 0")
        if self.expired_action is RetentionAction.RETAIN:
            raise ValueError("expired_action must archive or delete")

    def decide(
        self,
        candidate: NewsRetentionCandidate,
        *,
        now: datetime,
    ) -> RetentionDecision:
        self._validate_datetime(candidate.publish_time, "publish_time")
        self._validate_datetime(now, "now")
        cutoff = self._subtract_years(now, self.retention_years)

        # A legal hold always wins, including over withdrawal and age rules.
        if candidate.legal_hold:
            return RetentionDecision(
                RetentionAction.RETAIN,
                RetentionReason.LEGAL_HOLD,
                cutoff,
            )
        if candidate.withdrawn:
            return RetentionDecision(
                RetentionAction.DELETE,
                RetentionReason.WITHDRAWN,
                cutoff,
            )
        if candidate.publish_time > cutoff:
            return RetentionDecision(
                RetentionAction.RETAIN,
                RetentionReason.WITHIN_RETENTION,
                cutoff,
            )
        return RetentionDecision(
            self.expired_action,
            RetentionReason.RETENTION_EXPIRED,
            cutoff,
        )

    @staticmethod
    def _validate_datetime(value: datetime, field: str) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")

    @staticmethod
    def _subtract_years(value: datetime, years: int) -> datetime:
        try:
            return value.replace(year=value.year - years)
        except ValueError:
            # 29 February maps to the final valid day in February.
            return value.replace(year=value.year - years, day=28)


class NewsRetentionService:
    """Remove projections first, verify them, then touch canonical content."""

    def __init__(
        self,
        *,
        policy: RetentionPolicy,
        vector_index: TieredVectorIndexPort,
        full_text: FullTextDeletionPort,
        cache: NewsCacheInvalidationPort,
        objects: ContentObjectRetentionPort,
        audit: RetentionAuditPort,
    ) -> None:
        self._policy = policy
        self._vector_index = vector_index
        self._full_text = full_text
        self._cache = cache
        self._objects = objects
        self._audit = audit

    async def execute(
        self,
        candidate: NewsRetentionCandidate,
        *,
        now: datetime,
        dry_run: bool = True,
        approved_by: str | None = None,
    ) -> RetentionExecutionResult:
        decision = self._policy.decide(candidate, now=now)
        if decision.action is RetentionAction.RETAIN or dry_run:
            return RetentionExecutionResult(
                news_id=candidate.news_id,
                action=decision.action,
                reason=decision.reason,
                dry_run=dry_run,
            )
        approver = (approved_by or "").strip()
        if not approver:
            raise PermissionError(
                "approved_by is required for archive or delete execution"
            )

        await self._audit.append(
            self._audit_event(
                candidate,
                decision,
                status="started",
                approved_by=approver,
                now=now,
            )
        )

        deleted = 0
        try:
            by_tier: dict[VectorTier, list[str]] = {}
            for chunk in candidate.chunks:
                by_tier.setdefault(chunk.tier, []).append(chunk.chunk_id)
            for tier, chunk_ids in by_tier.items():
                deleted += await self._vector_index.delete(
                    tier=tier,
                    chunk_ids=chunk_ids,
                )
                remaining = await self._vector_index.contains(
                    tier=tier,
                    chunk_ids=chunk_ids,
                )
                if remaining:
                    raise RuntimeError(
                        "vector deletion verification failed for "
                        f"{len(remaining)} chunks"
                    )

            await self._full_text.delete_news(
                tenant_id=candidate.tenant_id,
                news_id=candidate.news_id,
                content_version=candidate.content_version,
            )
            await self._cache.invalidate_news(
                tenant_id=candidate.tenant_id,
                news_id=candidate.news_id,
            )

            if decision.action is RetentionAction.ARCHIVE:
                await self._objects.archive(object_uri=candidate.object_uri)
            else:
                await self._objects.delete(object_uri=candidate.object_uri)
        except Exception as exc:
            await self._audit.append(
                self._audit_event(
                    candidate,
                    decision,
                    status="failed",
                    approved_by=approver,
                    now=now,
                    error_type=type(exc).__name__,
                )
            )
            raise

        await self._audit.append(
            self._audit_event(
                candidate,
                decision,
                status="completed",
                approved_by=approver,
                now=now,
            )
        )
        return RetentionExecutionResult(
            news_id=candidate.news_id,
            action=decision.action,
            reason=decision.reason,
            dry_run=False,
            deleted_vector_chunks=deleted,
        )

    async def execute_batch(
        self,
        candidates: Sequence[NewsRetentionCandidate],
        *,
        now: datetime,
        dry_run: bool = True,
        approved_by: str | None = None,
        max_concurrency: int = 4,
    ) -> list[RetentionExecutionResult]:
        if max_concurrency <= 0:
            raise ValueError("max_concurrency must be greater than 0")
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_one(candidate: NewsRetentionCandidate):
            async with semaphore:
                return await self.execute(
                    candidate,
                    now=now,
                    dry_run=dry_run,
                    approved_by=approved_by,
                )

        return list(await asyncio.gather(*(run_one(item) for item in candidates)))

    @staticmethod
    def _audit_event(
        candidate: NewsRetentionCandidate,
        decision: RetentionDecision,
        *,
        status: str,
        approved_by: str,
        now: datetime,
        error_type: str | None = None,
    ) -> RetentionAuditEvent:
        return RetentionAuditEvent(
            tenant_id=candidate.tenant_id,
            news_id=candidate.news_id,
            content_version=candidate.content_version,
            action=decision.action,
            reason=decision.reason,
            status=status,
            approved_by=approved_by,
            occurred_at=now,
            chunk_count=len(candidate.chunks),
            error_type=error_type,
        )
