"""Versioned synthetic story evolution fixtures; no approval or delivery writes."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
import json

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.analytics.entities import ContentType
from app.analytics.hot_score import HotScoreCalculator
from app.evaluation.hot_news import (
    ExpectedHotNewsAnalysis, HotNewsEvaluationCase, HotNewsEvaluationDataset,
)
from app.schemas.evaluation_dataset import canonical_json_sha256
from app.schemas.hot_news import HotNewsAnalysisInput, HotNewsMetrics, HotScoreComponents, RelatedNewsEvidence
from app.schemas.hot_news_decision import DecisionType
from app.schemas.operations_feedback import OperationsReasonCode, OperationsFeedbackCategory, is_fresh_bad_case_eligible
from app.schemas.push import AudienceRule, PushChannel
from evaluation.operations_synthetic import metric_snapshot, metrics_view, validate_metrics


VERSION = "2026-10-10.synthetic-evolution-v2"
DEFAULT_CATALOG = Path(__file__).parent / "seeds" / "operations_evolution_v2.json"
DEFAULT_OUTPUT = Path(__file__).parent / "datasets" / "operations_evolution_v2"
Split = Literal["development", "validation", "test"]
Layer = Literal["golden", "fresh_bad_case", "high_risk"]
StageName = Literal["emerging", "confirmed", "developing", "repeat", "cooling", "corrected"]
RevisionField = Literal["copy", "audience", "channel", "send_time"]
STAGE_NAMES = ("emerging", "confirmed", "developing", "repeat", "cooling", "corrected")
SPLITS = ("development", "validation", "test")
LAYERS = ("golden", "fresh_bad_case", "high_risk")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SyntheticSource(StrictModel):
    source_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(pattern=r"^synthetic://operations-evolution-v2/[a-z0-9_-]+$")
    synthetic: Literal[True] = True


class EvolutionFact(StrictModel):
    fact_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=500)
    available_at: AwareDatetime
    source_id: str = Field(min_length=1, max_length=128)


class CandidateConditions(StrictModel):
    frequency_budget: Literal["available", "exhausted", "unknown"]
    subscription: Literal["active", "revoked", "unknown", "not_required"]
    consent: Literal["allowed", "revoked", "unknown"]
    quiet_hours: bool


class EvolutionCandidate(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    summary: str = Field(min_length=1, max_length=1000)
    analysis_claim: str = Field(min_length=1, max_length=1500)
    audience: AudienceRule
    channel: PushChannel
    notification_copy: str = Field(min_length=1, max_length=500)
    send_after: AwareDatetime
    expire_at: AwareDatetime
    conditions: CandidateConditions

    @model_validator(mode="after")
    def ordered_send_window(self):
        if self.send_after >= self.expire_at:
            raise ValueError("candidate send window must be ordered")
        return self


class ExpectedJudgment(StrictModel):
    decision_type: DecisionType
    allowed_decision_types: tuple[DecisionType, ...] = Field(min_length=1, max_length=4)
    category: OperationsFeedbackCategory
    required_reason_codes: tuple[OperationsReasonCode, ...] = Field(min_length=1, max_length=15)
    optional_reason_codes: tuple[OperationsReasonCode, ...] = Field(default=(), max_length=15)
    explanation: str = Field(min_length=1, max_length=2000)
    revision: str | None = Field(default=None, max_length=2000)
    blocking_expectations: tuple[OperationsReasonCode, ...] = Field(default=(), max_length=15)
    analysis_verdict: Literal["correct", "incorrect", "partially_correct"]
    layer: Layer
    must_state_limitation: bool = True
    must_block: bool
    required_revision_fields: tuple[RevisionField, ...] = Field(default=(), max_length=4)

    @model_validator(mode="before")
    @classmethod
    def default_block_expectation(cls, value):
        if isinstance(value, dict) and "must_block" not in value:
            value = {**value, "must_block": bool(value.get("blocking_expectations"))}
        return value

    @model_validator(mode="after")
    def reviewed_reference_contract(self):
        for values in (self.allowed_decision_types, self.required_reason_codes,
                       self.optional_reason_codes, self.blocking_expectations, self.required_revision_fields):
            if len(values) != len(set(values)):
                raise ValueError("expected reference values must be unique")
        if self.decision_type not in self.allowed_decision_types:
            raise ValueError("primary decision must be one of allowed decisions")
        if set(self.required_reason_codes) & set(self.optional_reason_codes):
            raise ValueError("required and optional reasons cannot overlap")
        if self.blocking_expectations and not self.must_block:
            raise ValueError("blocking expectations require must_block")
        if ("corrected" in self.allowed_decision_types) != bool(self.revision and self.required_revision_fields):
            raise ValueError("corrected reference requires revision text and revision fields")
        if self.category == "positive_example" and self.decision_type != "accepted":
            raise ValueError("positive reference must be accepted")
        if self.category == "analysis_error" and self.analysis_verdict == "correct":
            raise ValueError("analysis error cannot have a correct reference verdict")
        if self.layer == "fresh_bad_case" and not is_fresh_bad_case_eligible(
            verdict=self.analysis_verdict, source_type=f"operator_{self.decision_type}",
            source_reference={"operator_feedback": {"category": self.category}},
        ):
            raise ValueError("strategy/editorial/positive judgments cannot enter Fresh")
        return self

    @property
    def allowed_decisions(self):
        return self.allowed_decision_types

    @property
    def reference_revision(self):
        return self.revision

    @property
    def rationale(self):
        return self.explanation


class CandidateScenario(EvolutionCandidate):
    candidate_key: str = Field(min_length=1, max_length=128)
    expected: ExpectedJudgment


class EvolutionStage(StrictModel):
    stage_index: int = Field(ge=1, le=6)
    observed_at: AwareDatetime
    stage_name: StageName
    prior_stage_refs: tuple[int, ...] = Field(default=(), max_length=5)
    facts: tuple[EvolutionFact, ...] = Field(min_length=1, max_length=5)
    aggregate_profile: Literal["click", "consumption", "interaction", "growth"]
    content_type: Literal["article", "video"]
    candidates: tuple[CandidateScenario, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def bounded_past_context(self):
        if len(set(self.prior_stage_refs)) != len(self.prior_stage_refs) or any(
            value < 1 or value >= self.stage_index for value in self.prior_stage_refs
        ):
            raise ValueError("prior stage references must be unique past stages")
        if any(fact.available_at > self.observed_at for fact in self.facts):
            raise ValueError("future evidence is unavailable at the stage observation")
        if len({fact.fact_id for fact in self.facts}) != len(self.facts):
            raise ValueError("duplicate fact ID within stage")
        if len({candidate.candidate_key for candidate in self.candidates}) != 2:
            raise ValueError("contrast candidate keys must be unique")
        public = [candidate.model_dump(exclude={"candidate_key", "expected"}) for candidate in self.candidates]
        if public[0] == public[1]:
            raise ValueError("contrast candidates must differ in observed input")
        return self


class EvolutionStory(StrictModel):
    story_id: str = Field(min_length=1, max_length=128)
    split: Split
    title: str = Field(min_length=1, max_length=500)
    stages: tuple[EvolutionStage, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def chronological_story(self):
        if tuple(stage.stage_index for stage in self.stages) != tuple(range(1, 7)):
            raise ValueError("story stages must be contiguous and ordered")
        if tuple(stage.stage_name for stage in self.stages) != STAGE_NAMES:
            raise ValueError("story evolution stage names must follow the six-stage contract")
        if any(left.observed_at >= right.observed_at for left, right in zip(self.stages, self.stages[1:])):
            raise ValueError("story observation dates must increase")
        facts = {}
        for stage in self.stages:
            for fact in stage.facts:
                if fact.fact_id in facts and facts[fact.fact_id] != fact:
                    raise ValueError("same fact ID cannot silently change content")
                facts[fact.fact_id] = fact
        return self


class EvolutionBoard(StrictModel):
    board_id: str = Field(pattern=r"^[a-z0-9_-]+$")
    board_name: str = Field(min_length=1, max_length=128)
    operator_id: str = Field(min_length=1, max_length=128)
    operator_name: str = Field(min_length=1, max_length=128)
    scope_type: Literal["operator"] = "operator"
    applicable_scenarios: tuple[str, ...] = Field(min_length=1, max_length=20)
    metric_objectives: tuple[str, ...] = Field(min_length=1, max_length=20)
    audience_summary: str = Field(min_length=1, max_length=1000)
    risk_focus: tuple[str, ...] = Field(min_length=1, max_length=20)
    stories: tuple[EvolutionStory, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def split_coverage(self):
        if set(story.split for story in self.stories) != set(SPLITS):
            raise ValueError("each board requires one independent story for each split")
        return self


class OperationsEvolutionCatalog(StrictModel):
    schema_version: Literal["operations-evolution-catalog-v2"]
    dataset_version: Literal[VERSION] = VERSION
    synthetic: Literal[True] = True
    sources: tuple[SyntheticSource, ...] = Field(min_length=1)
    boards: tuple[EvolutionBoard, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_lineage_and_sources(self):
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("duplicate source ID")
        for name in ("board_id", "operator_id"):
            if len({getattr(board, name) for board in self.boards}) != len(self.boards):
                raise ValueError(f"duplicate {name}; each fixture board has an independent operator")
        story_ids = set()
        fact_story = {}
        for board in self.boards:
            for story in board.stories:
                if story.story_id in story_ids:
                    raise ValueError("one story cannot appear in different boards or splits")
                story_ids.add(story.story_id)
                for stage in story.stages:
                    for fact in stage.facts:
                        if fact.source_id not in source_ids:
                            raise ValueError("fact references a missing synthetic source")
                        if fact_story.setdefault(fact.fact_id, story.story_id) != story.story_id:
                            raise ValueError("fact ID cannot leak across independent stories")
        return self


class SyntheticScope(StrictModel):
    tenant_id: str = Field(pattern=r"^synthetic-tenant-[0-9a-f]{20}$")
    user_id: str = Field(pattern=r"^synthetic-operator-[0-9a-f]{20}$")
    section_id: str = Field(pattern=r"^synthetic-board-[0-9a-f]{20}$")
    board_name: str
    operator_name: str
    scope_type: Literal["operator"] = "operator"
    applicable_scenarios: tuple[str, ...]
    metric_objectives: tuple[str, ...]
    audience_summary: str
    risk_focus: tuple[str, ...]

    @property
    def operator_id(self):
        return self.user_id

    @property
    def board_id(self):
        return self.section_id


class OperationsEvolutionCase(StrictModel):
    case_id: str = Field(pattern=r"^evo-[0-9a-f]{20}$")
    story_id: str = Field(pattern=r"^synthetic-story-[0-9a-f]{20}$")
    event_id: str = Field(pattern=r"^synthetic-event-[0-9a-f]{20}$")
    source_story_id: str
    source_board_id: str
    source_operator_id: str
    scope: SyntheticScope
    split: Split
    story_title: str
    stage_index: int = Field(ge=1, le=6)
    stage_name: StageName
    observed_at: AwareDatetime
    prior_stage_refs: tuple[int, ...]
    known_facts: tuple[EvolutionFact, ...] = Field(min_length=1)
    candidate: EvolutionCandidate
    expected: ExpectedJudgment
    analysis_case: HotNewsEvaluationCase
    metric_profile: Literal["click", "consumption", "interaction", "growth"]
    baseline: HotNewsMetrics | None
    synthetic: Literal[True] = True
    annotation_status: Literal["generated_pending_review"] = "generated_pending_review"
    delivery_status: Literal["not_executed"] = "not_executed"

    @property
    def scope_sha256(self):
        return canonical_json_sha256(self.scope)


class OperationsEvolutionDataset(StrictModel):
    schema_version: Literal["operations-evolution-dataset-v2"] = "operations-evolution-dataset-v2"
    dataset_version: Literal[VERSION] = VERSION
    synthetic: Literal[True] = True
    annotation_status: Literal["generated_pending_review"] = "generated_pending_review"
    strategy_status: Literal["research_seed_not_production_policy"] = "research_seed_not_production_policy"
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: tuple[SyntheticSource, ...]
    cases: tuple[OperationsEvolutionCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def integrity(self):
        validate_dataset(self)
        return self


def opaque_id(prefix: str, *values: str) -> str:
    return prefix + canonical_json_sha256({"values": values})[:20]


def load_catalog(path: Path = DEFAULT_CATALOG) -> OperationsEvolutionCatalog:
    return OperationsEvolutionCatalog.model_validate_json(Path(path).read_text(encoding="utf-8"))


def build_dataset(catalog: OperationsEvolutionCatalog) -> OperationsEvolutionDataset:
    cases = []
    for board in catalog.boards:
        scope = SyntheticScope(
            tenant_id=opaque_id("synthetic-tenant-", "operations-evolution-v2"),
            user_id=opaque_id("synthetic-operator-", board.operator_id),
            section_id=opaque_id("synthetic-board-", board.board_id),
            **board.model_dump(exclude={"board_id", "operator_id", "stories"}),
        )
        for story in board.stories:
            story_id = opaque_id("synthetic-story-", board.board_id, story.story_id)
            event_id = opaque_id("synthetic-event-", board.board_id, story.story_id)
            known = {}
            for stage in story.stages:
                for fact in stage.facts:
                    known[fact.fact_id] = fact
                facts = tuple(sorted(known.values(), key=lambda fact: (fact.available_at, fact.fact_id)))
                excerpt = "；".join(fact.text for fact in facts)
                if len(excerpt) > 1850:
                    raise ValueError("complete known facts exceed the existing analysis input budget")
                news_id = opaque_id("synthetic-news-", board.board_id, story.story_id, str(stage.stage_index))
                evidence_id = opaque_id("synthetic-evidence-", board.board_id, story.story_id, str(stage.stage_index))
                snapshot = replace(
                    metric_snapshot(news_id, stage.aggregate_profile, ContentType(stage.content_type)),
                    window_start=stage.observed_at - timedelta(hours=1), window_end=stage.observed_at,
                )
                baseline = replace(snapshot, clicks=1, effective_consumptions=0, interactions=0,
                                   ctr=Decimal(1) / Decimal(snapshot.impressions)) if stage.aggregate_profile == "growth" else None
                heat = HotScoreCalculator().calculate(snapshot, baseline)
                analysis = HotNewsAnalysisInput(
                    news_id=news_id, title=story.title,
                    summary="合成新闻主线当前已知事实；不对应真实新闻。" + excerpt,
                    content_excerpt="以下仅为截至观察时间的虚构事实；不代表企业数据或生产规则。" + excerpt,
                    content_type=stage.content_type, window_start=snapshot.window_start, window_end=snapshot.window_end,
                    hot_score=float(heat.score), metrics=metrics_view(snapshot),
                    score_components=HotScoreComponents(click=float(heat.click_component),
                        consumption=float(heat.consumption_component), interaction=float(heat.interaction_component),
                        growth=float(heat.growth_component)),
                    related_news=[RelatedNewsEvidence(news_id=evidence_id, title="合成主线截至本阶段的事实包",
                        excerpt=excerpt, published_at=max(fact.available_at for fact in facts), final_score=1,
                        rerank_reasons=["synthetic_fixture_only"])],
                    analysis_policy_version="synthetic-operations-evolution-v2",
                )
                for candidate in stage.candidates:
                    case_id = opaque_id("evo-", board.board_id, story.story_id, str(stage.stage_index), candidate.candidate_key)
                    expected_analysis = ExpectedHotNewsAnalysis(
                        allowed_dominant_drivers=(stage.aggregate_profile,), required_evidence_news_ids=(evidence_id,),
                        forbidden_evidence_news_ids=(opaque_id("unprovided-", case_id),),
                        required_metric_keys=("ctr", "hot_score"), must_state_limitation=candidate.expected.must_state_limitation,
                    )
                    cases.append(OperationsEvolutionCase(
                        case_id=case_id, story_id=story_id, event_id=event_id,
                        source_story_id=story.story_id, source_board_id=board.board_id,
                        source_operator_id=board.operator_id, scope=scope,
                        split=story.split, story_title=story.title, stage_index=stage.stage_index,
                        stage_name=stage.stage_name, observed_at=stage.observed_at, prior_stage_refs=stage.prior_stage_refs,
                        known_facts=facts, candidate=EvolutionCandidate.model_validate(candidate.model_dump(exclude={"candidate_key", "expected"})),
                        expected=candidate.expected,
                        analysis_case=HotNewsEvaluationCase(case_id=case_id, description="合成主线分析契约；运营判断单独评测。" + story.title,
                            tags=("synthetic", "pending_review"), analysis_input=analysis, expected=expected_analysis),
                        metric_profile=stage.aggregate_profile, baseline=metrics_view(baseline) if baseline else None,
                    ))
    return OperationsEvolutionDataset(catalog_sha256=canonical_json_sha256(catalog), sources=catalog.sources, cases=tuple(cases))


def validate_dataset(dataset: OperationsEvolutionDataset) -> None:
    source_ids = {source.source_id for source in dataset.sources}
    if len(source_ids) != len(dataset.sources):
        raise ValueError("duplicate synthetic source IDs")
    case_ids = set()
    story_scopes, board_scopes, lineage_splits, fact_stories, news_stories, evidence_stories = {}, {}, {}, {}, {}, {}
    stages, board_stories, operator_sections = {}, {}, {}
    for case in dataset.cases:
        if case.case_id in case_ids or case.analysis_case.case_id != case.case_id:
            raise ValueError("case IDs must be unique and match analysis IDs")
        case_ids.add(case.case_id)
        expected_story = opaque_id("synthetic-story-", case.source_board_id, case.source_story_id)
        expected_event = opaque_id("synthetic-event-", case.source_board_id, case.source_story_id)
        if case.story_id != expected_story or case.event_id != expected_event:
            raise ValueError("story/event lineage identity mismatch")
        if case.scope.section_id != opaque_id("synthetic-board-", case.source_board_id):
            raise ValueError("board scope identity mismatch")
        if case.scope.user_id != opaque_id("synthetic-operator-", case.source_operator_id):
            raise ValueError("operator scope identity mismatch")
        if case.scope.tenant_id != opaque_id("synthetic-tenant-", "operations-evolution-v2"):
            raise ValueError("synthetic tenant scope identity mismatch")
        if board_scopes.setdefault(case.source_board_id, case.scope) != case.scope:
            raise ValueError("board/operator scope mismatch across stories")
        if operator_sections.setdefault(case.scope.user_id, case.scope.section_id) != case.scope.section_id:
            raise ValueError("fixture operators must be independent across boards")
        board_stories.setdefault(case.source_board_id, {})[case.story_id] = case.split
        if story_scopes.setdefault(case.story_id, case.scope) != case.scope:
            raise ValueError("actor scope mismatch within story")
        for identity in (case.story_id, case.event_id, "source:" + case.source_story_id):
            if lineage_splits.setdefault(identity, case.split) != case.split:
                raise ValueError("story/event leaks across splits")
        if any(fact.available_at > case.observed_at for fact in case.known_facts):
            raise ValueError("future evidence in persisted case")
        if len({fact.fact_id for fact in case.known_facts}) != len(case.known_facts):
            raise ValueError("duplicate fact identity")
        for fact in case.known_facts:
            if fact.source_id not in source_ids:
                raise ValueError("missing source reference")
            if fact_stories.setdefault(fact.fact_id, case.story_id) != case.story_id:
                raise ValueError("evidence fact leaks across independent stories")
        analysis = case.analysis_case.analysis_input
        if analysis.window_end != case.observed_at or analysis.window_start != case.observed_at - timedelta(hours=1):
            raise ValueError("analysis window is not the case observation window")
        expected_news = opaque_id("synthetic-news-", case.source_board_id, case.source_story_id, str(case.stage_index))
        if analysis.news_id != expected_news or news_stories.setdefault(analysis.news_id, case.story_id) != case.story_id:
            raise ValueError("news identity leaks across story/split")
        for evidence in analysis.related_news:
            if evidence.published_at is None or evidence.published_at.tzinfo is None or evidence.published_at > case.observed_at:
                raise ValueError("future or undated analysis evidence")
            if evidence_stories.setdefault(evidence.news_id, case.story_id) != case.story_id:
                raise ValueError("analysis evidence leaks across story/split")
        expected_evidence = opaque_id("synthetic-evidence-", case.source_board_id, case.source_story_id, str(case.stage_index))
        if tuple(evidence.news_id for evidence in analysis.related_news) != (expected_evidence,):
            raise ValueError("analysis evidence identity mismatch")
        excerpt = "；".join(fact.text for fact in case.known_facts)
        if (analysis.related_news[0].excerpt != excerpt
                or analysis.title != case.story_title
                or analysis.summary != "合成新闻主线当前已知事实；不对应真实新闻。" + excerpt
                or analysis.content_excerpt != "以下仅为截至观察时间的虚构事实；不代表企业数据或生产规则。" + excerpt):
            raise ValueError("analysis evidence does not match known past facts")
        if tuple(case.prior_stage_refs) != tuple(sorted(set(case.prior_stage_refs))) or any(
            index < 1 or index >= case.stage_index for index in case.prior_stage_refs
        ):
            raise ValueError("invalid past stage reference")
        if case.stage_name != STAGE_NAMES[case.stage_index - 1]:
            raise ValueError("stage name/index mismatch")
        stage_key = (case.story_id, case.stage_index)
        stage_state = (case.observed_at, case.known_facts, case.prior_stage_refs, analysis, case.metric_profile, case.baseline)
        item = stages.setdefault(stage_key, {"state": stage_state, "count": 0, "candidates": []})
        if item["state"] != stage_state:
            raise ValueError("contrast candidates disagree on current stage context")
        item["count"] += 1
        item["candidates"].append(case.candidate)
        # The prior fixture validator recomputes only authoritative aggregate metrics.
        profile_snapshot = metric_snapshot(analysis.news_id, case.metric_profile, ContentType(analysis.content_type))
        if analysis.metrics != metrics_view(profile_snapshot):
            raise ValueError("aggregate counts differ from the versioned fixture profile")
        expected_baseline = metrics_view(replace(profile_snapshot, clicks=1, effective_consumptions=0, interactions=0,
            ctr=Decimal(1) / Decimal(profile_snapshot.impressions))) if case.metric_profile == "growth" else None
        if case.baseline != expected_baseline:
            raise ValueError("baseline differs from the versioned fixture profile")
        validate_metrics(case)
        ExpectedJudgment.model_validate(case.expected.model_dump())
    for story_id in story_scopes:
        previous_date, previous_facts = None, {}
        for index in range(1, 7):
            item = stages.get((story_id, index))
            if item is None or item["count"] != 2 or item["candidates"][0] == item["candidates"][1]:
                raise ValueError("each story stage requires two distinct contrast candidates")
            observed_at, facts, *_ = item["state"]
            if previous_date is not None and observed_at <= previous_date:
                raise ValueError("stage observation dates must increase")
            current = {fact.fact_id: fact for fact in facts}
            if any(current.get(key) != value for key, value in previous_facts.items()):
                raise ValueError("past facts cannot disappear or change in later stages")
            previous_date, previous_facts = observed_at, current
    for stories in board_stories.values():
        if len(stories) != 3 or set(stories.values()) != set(SPLITS):
            raise ValueError("persisted board requires one independent story per split")


def blind_case_input(case: OperationsEvolutionCase) -> dict:
    """Explicit input allowlist; authoring keys and reference labels never reach a runner."""
    return {
        "case_id": case.case_id, "synthetic": True,
        "scope": case.scope.model_dump(mode="json"), "scope_sha256": case.scope_sha256,
        "story_id": case.story_id, "event_id": case.event_id, "story_title": case.story_title,
        "stage_index": case.stage_index, "stage_name": case.stage_name, "observed_at": case.observed_at.isoformat(),
        "history": [{"text": fact.text, "available_at": fact.available_at.isoformat(),
                     "source_url": next_source_url(case, fact)} for fact in case.known_facts],
        "candidate": case.candidate.model_dump(mode="json"),
        "analysis_input": case.analysis_case.analysis_input.model_dump(mode="json"),
    }


def next_source_url(case: OperationsEvolutionCase, fact: EvolutionFact) -> str:
    # Scenario source references are fixtures, never a claim of external news verification.
    return "synthetic://operations-evolution-v2/" + case.scope.section_id


def blind_input(dataset: OperationsEvolutionDataset | OperationsEvolutionCase, split: Split | None = None) -> dict:
    if isinstance(dataset, OperationsEvolutionCase):
        if split is not None:
            raise ValueError("single blind case input does not accept a split filter")
        return blind_case_input(dataset)
    return {"dataset_version": dataset.dataset_version, "synthetic": True,
            "cases": [blind_case_input(case) for case in dataset.cases if split is None or case.split == split]}


def analysis_projection(dataset: OperationsEvolutionDataset, split: Split | None = None, layer: Layer | None = None) -> HotNewsEvaluationDataset:
    # Two operator candidates share one immutable news observation. Run the
    # analysis evaluator once per snapshot rather than doubling its denominator.
    selected = {}
    for case in dataset.cases:
        if (split is None or case.split == split) and (layer is None or case.expected.layer == layer):
            selected.setdefault(case.analysis_case.analysis_input.news_id, case.analysis_case)
    return HotNewsEvaluationDataset(
        dataset_name=f"hot-news-operations-evolution-{split or 'all'}-{layer or 'all'}", dataset_version=dataset.dataset_version,
        description="虚构主线与程序参考预期，待人工复核；分析合同评测与运营决策/真实效果分别报告。",
        cases=tuple(selected.values()),
    )


def summary(dataset: OperationsEvolutionDataset) -> dict:
    return {
        "dataset_version": dataset.dataset_version, "synthetic": True, "annotation_status": dataset.annotation_status,
        "case_count": len(dataset.cases), "board_count": len({case.scope.section_id for case in dataset.cases}),
        "operator_count": len({case.scope.user_id for case in dataset.cases}),
        "independent_story_count": len({case.story_id for case in dataset.cases}),
        "stage_count": len({(case.story_id, case.stage_index) for case in dataset.cases}),
        "unique_news_snapshot_count": len({case.analysis_case.analysis_input.news_id for case in dataset.cases}),
        "splits": dict(sorted(Counter(case.split for case in dataset.cases).items())),
        "layers": dict(sorted(Counter(case.expected.layer for case in dataset.cases).items())),
        "decisions": dict(sorted(Counter(case.expected.decision_type for case in dataset.cases).items())),
        "categories": dict(sorted(Counter(case.expected.category for case in dataset.cases).items())),
        "boards": {name: dict(sorted(Counter(case.split for case in dataset.cases if case.scope.board_name == name).items()))
                   for name in sorted({case.scope.board_name for case in dataset.cases})},
        "limitations": ["synthetic_pending_review", "no_formal_run_or_approved_label", "not_executed",
                        "decision_reference_is_not_unique_business_truth", "no_measured_or_causal_delivery_effect",
                        "story_split_is_synthetic_template_holdout_not_real_generalization",
                        "contrast_candidates_reuse_same_stage_analysis_snapshot"],
    }


def render_casebook(dataset: OperationsEvolutionDataset, board_id: str | None = None) -> str:
    lines = ["# 新闻主线演进运营参考案例（合成 v2）", "", "全部情景、运营身份、聚合数据和预期均为程序生成，待人工复核；没有真实审批或投递。", ""]
    for case in dataset.cases:
        if board_id is not None and case.source_board_id != board_id:
            continue
        lines.extend([f"## {case.scope.board_name} · {case.story_title} · 阶段{case.stage_index} · {case.case_id}", "",
            f"模拟运营：{case.scope.operator_name}；split：{case.split}；候选层：{case.expected.layer}。",
            f"观察时间：{case.observed_at.isoformat()}；阶段：{case.stage_name}。", "", "截至当时的已知事实："])
        lines.extend(f"- {fact.available_at.isoformat()}：{fact.text}（合成来源 {fact.source_id}）" for fact in case.known_facts)
        lines.extend(["", f"候选分析主张：{case.candidate.analysis_claim}",
            f"候选人群：{json.dumps(case.candidate.audience.model_dump(mode='json'), ensure_ascii=False)}；渠道：{case.candidate.channel.value}。",
            f"候选文案：{case.candidate.notification_copy}", f"时间窗：{case.candidate.send_after.isoformat()} 至 {case.candidate.expire_at.isoformat()}。",
            f"条件：{json.dumps(case.candidate.conditions.model_dump(), ensure_ascii=False)}", "",
            f"参考决定：{case.expected.decision_type}；可接受动作：{', '.join(case.expected.allowed_decision_types)}；分类：{case.expected.category}。",
            f"必需理由：{', '.join(case.expected.required_reason_codes)}；可选理由：{', '.join(case.expected.optional_reason_codes) or '无'}。",
            f"原候选需阻断：{case.expected.must_block}；阻断预期：{', '.join(case.expected.blocking_expectations) or '无'}。",
            f"参考说明：{case.expected.explanation}", f"修订参考：{case.expected.revision or '无需修订'}", ""])
    return "\n".join(lines)


def _json_bytes(payload) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _artifact_contents(dataset: OperationsEvolutionDataset) -> dict[str, bytes]:
    artifacts = {
        "operations_cases.json": _json_bytes(dataset.model_dump(mode="json")),
        "operations_inputs.json": _json_bytes(blind_input(dataset)),
        "summary.json": _json_bytes(summary(dataset)),
        "operations_casebook.md": render_casebook(dataset).encode("utf-8"),
        "hot_news_analysis_all.json": _json_bytes(analysis_projection(dataset).model_dump(mode="json")),
    }
    board_ids = sorted({case.source_board_id for case in dataset.cases})
    counts = summary(dataset)
    index = ["# 板块运营数据与新闻主线参考", "",
             f"{counts['case_count']}条候选判断、{counts['unique_news_snapshot_count']}个新闻观察快照、{counts['independent_story_count']}条虚构主线；全部待人工复核。", "",
             "完整答案请看 operations_casebook.md；模型只读 operations_inputs*.json。", "",
             "| 板块 | 中文案例册 | 无答案评测输入 |", "|---|---|---|"]
    for board_id in board_ids:
        cases = [case for case in dataset.cases if case.source_board_id == board_id]
        artifacts[f"operations_casebook_{board_id}.md"] = render_casebook(dataset, board_id).encode("utf-8")
        artifacts[f"operations_inputs_board_{board_id}.json"] = _json_bytes({
            "dataset_version": dataset.dataset_version, "synthetic": True,
            "cases": [blind_case_input(case) for case in cases]})
        index.append(f"| {cases[0].scope.board_name} | [{len(cases)}例参考](operations_casebook_{board_id}.md) | [盲输入](operations_inputs_board_{board_id}.json) |")
    index.extend(["", "每板块3条独立主线，每主线6阶段与2个候选。development/validation/test各保留一条完整主线。",
                  "候选层是素材用途标签，层间可能复用同一主线观察；不能将三层文件当作互斥的训练/验证/测试集。",
                  f"分析投影按新闻快照去重，全量{counts['unique_news_snapshot_count']}例、每split{len(analysis_projection(dataset, split='test').cases)}例；运营评分保持{counts['case_count']}条候选固定分母。",
                  "源目录、产物和清单均版本化；当前没有真实执行结果或生产审批。", ""])
    artifacts["README.md"] = "\n".join(index).encode("utf-8")
    for split in SPLITS:
        artifacts[f"operations_inputs_{split}.json"] = _json_bytes(blind_input(dataset, split))
        if any(case.split == split for case in dataset.cases):
            artifacts[f"hot_news_analysis_{split}.json"] = _json_bytes(analysis_projection(dataset, split).model_dump(mode="json"))
    for layer in LAYERS:
        if any(case.expected.layer == layer for case in dataset.cases):
            artifacts[f"hot_news_analysis_{layer}.json"] = _json_bytes(analysis_projection(dataset, layer=layer).model_dump(mode="json"))
        for split in SPLITS:
            if any(case.split == split and case.expected.layer == layer for case in dataset.cases):
                artifacts[f"hot_news_analysis_{split}_{layer}.json"] = _json_bytes(analysis_projection(dataset, split, layer).model_dump(mode="json"))
    return artifacts


def write_artifacts(dataset: OperationsEvolutionDataset, output: Path) -> dict:
    """Preflight every output; never replace a different published fixture version."""
    from hashlib import sha256
    validate_dataset(dataset)
    output = Path(output)
    artifacts = _artifact_contents(dataset)
    manifest = {**summary(dataset), "schema_version": "operations-evolution-manifest-v2",
        "catalog_sha256": dataset.catalog_sha256,
        "artifacts": {name: {"sha256": sha256(content).hexdigest(), "size_bytes": len(content)}
                      for name, content in sorted(artifacts.items())}}
    artifacts["manifest.json"] = _json_bytes(manifest)
    expected_names = set(artifacts)
    if output.exists():
        if output.is_symlink() or not output.is_dir():
            raise ValueError("artifact output must be a real directory")
        if {path.name for path in output.iterdir()} - expected_names:
            raise ValueError("output contains stale or unrelated artifacts; use a new version directory")
        for name, content in artifacts.items():
            target = output / name
            if target.exists() and (target.is_symlink() or not target.is_file() or target.read_bytes() != content):
                raise ValueError(f"refusing to overwrite different existing artifact: {name}")
    output.mkdir(parents=True, exist_ok=True)
    for name, content in artifacts.items():
        target = output / name
        try:
            with target.open("xb") as handle:
                handle.write(content)
        except FileExistsError:
            if target.is_symlink() or target.read_bytes() != content:
                raise ValueError(f"artifact changed during conditional creation: {name}")
    return manifest


def verify_artifact_hashes(output: Path) -> None:
    from hashlib import sha256
    output = Path(output)
    manifest_path = output / "manifest.json"
    if output.is_symlink() or manifest_path.is_symlink():
        raise ValueError("artifact directory and manifest cannot be symlinks")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != "operations-evolution-manifest-v2":
        raise ValueError("unexpected manifest version")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("manifest requires artifact hashes")
    if {path.name for path in output.iterdir()} != set(artifacts) | {"manifest.json"}:
        raise ValueError("artifact inventory differs from manifest")
    for name, receipt in artifacts.items():
        if not isinstance(name, str) or Path(name).name != name or name in {".", "..", "manifest.json"}:
            raise ValueError("unsafe artifact filename")
        path = output / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("artifact must be a regular local file")
        content = path.read_bytes()
        if not isinstance(receipt, dict) or receipt.get("sha256") != sha256(content).hexdigest() or receipt.get("size_bytes") != len(content):
            raise ValueError(f"artifact hash/size mismatch: {name}")
    dataset = OperationsEvolutionDataset.model_validate_json((output / "operations_cases.json").read_bytes())
    regenerated = _artifact_contents(dataset)
    if set(regenerated) != set(artifacts):
        raise ValueError("manifest is missing required dataset projections")
    if any((output / name).read_bytes() != content for name, content in regenerated.items()):
        raise ValueError("artifact content differs from the validated dataset projection")
    if manifest.get("catalog_sha256") != dataset.catalog_sha256:
        raise ValueError("manifest/catalog lineage mismatch")
    expected_summary = summary(dataset)
    if any(manifest.get(key) != value for key, value in expected_summary.items()):
        raise ValueError("manifest summary differs from validated dataset")
