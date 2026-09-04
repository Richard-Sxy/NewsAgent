import re
from copy import deepcopy
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, TypeAdapter, ValidationError, model_validator


class ResearchFact(BaseModel):
    """由研究 Agent 提取、且可以追溯来源的事实。"""

    fact_id: str = Field(pattern=r"^F\d{3,}$")
    claim: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    source_url: HttpUrl
    source_title: str = Field(min_length=1)
    published_at: datetime | None = None
    confidence: float = Field(ge=0, le=1)


class TimelineEvent(BaseModel):
    """新闻事件时间线中的一个节点。"""

    event_id: str = Field(pattern=r"^T\d{3,}$")
    occurred_at: datetime
    description: str = Field(min_length=1)
    supporting_fact_ids: list[str] = Field(default_factory=list)


class EvidenceConflict(BaseModel):
    """不同新闻证据之间的冲突。"""

    conflict_id: str = Field(pattern=r"^C\d{3,}$")
    fact_ids: list[str] = Field(min_length=2)
    description: str = Field(min_length=1)
    resolution: str | None = None


class EvidenceGap(BaseModel):
    """研究阶段尚未获得充分证据的问题。"""

    gap_id: str = Field(pattern=r"^G\d{3,}$")
    question: str = Field(min_length=1)
    importance: Literal["low", "medium", "high"]
    suggested_query: str | None = None


class SuggestedAngle(BaseModel):
    """研究 Agent 推荐的文章写作角度。"""

    angle_id: str = Field(pattern=r"^A\d{3,}$")
    title: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    supporting_fact_ids: list[str] = Field(default_factory=list)


class ResearchMetrics(BaseModel):
    """由 Python 根据研究包确定性计算，禁止由 Agent 自报。"""

    fact_count: int = Field(ge=0)
    unique_source_count: int = Field(ge=0)
    numeric_fact_count: int = Field(ge=0)
    citation_coverage_percent: float = Field(ge=0, le=100)
    average_confidence: float = Field(ge=0, le=1)
    timeline_event_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    evidence_gap_count: int = Field(ge=0)
    angle_count: int = Field(ge=0)


class ResearchPackage(BaseModel):
    """研究 Agent 的完整结构化输出。"""

    job_id: str = Field(min_length=1)
    topic: str = Field(min_length=1)
    facts: list[ResearchFact] = Field(default_factory=list)
    timeline: list[TimelineEvent] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    suggested_angles: list[SuggestedAngle] = Field(default_factory=list)
    metrics: ResearchMetrics | None = None

    @model_validator(mode="before")
    @classmethod
    def quarantine_untraceable_evidence(cls, value):
        """Do not let null provenance poison the whole package or invent citations."""
        if not isinstance(value, dict):
            return value
        data = deepcopy(value)
        facts = data.get("facts") if isinstance(data.get("facts"), list) else []
        kept_facts: list[dict] = []
        dropped_fact_ids: set[str] = set()
        gap_questions: list[str] = []
        url_adapter = TypeAdapter(HttpUrl)
        datetime_adapter = TypeAdapter(datetime)

        for fact in facts:
            if not isinstance(fact, dict):
                kept_facts.append(fact)
                continue
            try:
                url_adapter.validate_python(fact.get("source_url"))
            except ValidationError:
                fact_id = fact.get("fact_id")
                if isinstance(fact_id, str):
                    dropped_fact_ids.add(fact_id)
                claim = str(fact.get("claim") or "未命名事实")[:300]
                gap_questions.append(f"缺少可追溯来源，未采用该事实：{claim}")
                continue
            kept_facts.append(fact)
        data["facts"] = kept_facts

        timeline = data.get("timeline") if isinstance(data.get("timeline"), list) else []
        kept_timeline: list[dict] = []
        for event in timeline:
            if not isinstance(event, dict):
                kept_timeline.append(event)
                continue
            try:
                datetime_adapter.validate_python(event.get("occurred_at"))
            except ValidationError:
                description = str(event.get("description") or "未命名事件")[:300]
                gap_questions.append(f"缺少明确发生时间，未采用该事件：{description}")
                continue
            event["supporting_fact_ids"] = [
                item for item in event.get("supporting_fact_ids", [])
                if item not in dropped_fact_ids
            ]
            kept_timeline.append(event)
        data["timeline"] = kept_timeline

        conflicts = data.get("conflicts") if isinstance(data.get("conflicts"), list) else []
        cleaned_conflicts: list[dict] = []
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                cleaned_conflicts.append(conflict)
                continue
            conflict["fact_ids"] = [
                item for item in conflict.get("fact_ids", [])
                if item not in dropped_fact_ids
            ]
            if len(conflict["fact_ids"]) >= 2:
                cleaned_conflicts.append(conflict)
        data["conflicts"] = cleaned_conflicts

        for angle in data.get("suggested_angles", []):
            if isinstance(angle, dict):
                angle["supporting_fact_ids"] = [
                    item for item in angle.get("supporting_fact_ids", [])
                    if item not in dropped_fact_ids
                ]

        gaps = data.get("evidence_gaps")
        if not isinstance(gaps, list):
            gaps = []
        used_gap_ids = {
            gap.get("gap_id") for gap in gaps
            if isinstance(gap, dict) and isinstance(gap.get("gap_id"), str)
        }
        next_number = 1
        for question in gap_questions:
            while f"G{next_number:03d}" in used_gap_ids:
                next_number += 1
            gap_id = f"G{next_number:03d}"
            used_gap_ids.add(gap_id)
            gaps.append({
                "gap_id": gap_id,
                "question": question,
                "importance": "high",
                "suggested_query": "检索包含正式发布日期和可访问原文链接的一手来源",
            })
            next_number += 1
        data["evidence_gaps"] = gaps
        return data

    @model_validator(mode="after")
    def validate_fact_references(self) -> "ResearchPackage":
        """确保 ID 不重复，而且所有事实引用均真实存在。"""
        fact_ids = [fact.fact_id for fact in self.facts]

        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("研究包中的 fact_id 不能重复")

        known_fact_ids = set(fact_ids)
        referenced_fact_ids: set[str] = set()

        for event in self.timeline:
            referenced_fact_ids.update(event.supporting_fact_ids)
        for conflict in self.conflicts:
            referenced_fact_ids.update(conflict.fact_ids)
        for angle in self.suggested_angles:
            referenced_fact_ids.update(angle.supporting_fact_ids)

        unknown_fact_ids = referenced_fact_ids - known_fact_ids
        if unknown_fact_ids:
            unknown = ", ".join(sorted(unknown_fact_ids))
            raise ValueError(f"引用了不存在的 fact_id: {unknown}")

        fact_count = len(self.facts)
        numeric_fact_count = sum(
            bool(re.search(r"\d", f"{fact.claim} {fact.evidence}"))
            for fact in self.facts
        )
        self.metrics = ResearchMetrics(
            fact_count=fact_count,
            unique_source_count=len({str(fact.source_url) for fact in self.facts}),
            numeric_fact_count=numeric_fact_count,
            citation_coverage_percent=(100.0 if fact_count else 0.0),
            average_confidence=(
                round(sum(fact.confidence for fact in self.facts) / fact_count, 4)
                if fact_count
                else 0.0
            ),
            timeline_event_count=len(self.timeline),
            conflict_count=len(self.conflicts),
            evidence_gap_count=len(self.evidence_gaps),
            angle_count=len(self.suggested_angles),
        )

        return self
