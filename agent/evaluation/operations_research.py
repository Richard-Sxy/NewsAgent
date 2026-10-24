"""Public-source-informed, synthetic operations fixtures kept apart from v2.

Public sources justify editorial test design. They are never evidence that the
fictional events occurred, and no reference judgment is a production approval.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, Field, model_validator

from app.analytics.entities import ContentType
from app.modules.hot_news.scoring.hot_score import HotScoreCalculator
from app.modules.data_loop.evaluation.hot_news import (
    ExpectedHotNewsAnalysis, HotNewsEvaluationCase, HotNewsEvaluationDataset,
)
from app.modules.data_loop.contracts.evaluation_dataset import canonical_json_sha256
from app.modules.hot_news.contracts.hot_news import (
    HotNewsAnalysisInput, HotNewsMetrics, HotScoreComponents, RelatedNewsEvidence,
)
from evaluation.operations_evolution import (
    CandidateScenario, EvolutionCandidate, EvolutionFact, ExpectedJudgment,
    StrictModel, SyntheticScope, opaque_id,
)
from evaluation.operations_synthetic import metric_snapshot, metrics_view, validate_metrics


VERSION = "2026-10-14.public-research-synthetic-v1"
DEFAULT_CATALOG = Path(__file__).parent / "seeds" / "operations_research_v1.json"
DEFAULT_OUTPUT = Path(__file__).parent / "datasets" / "hot_news" / "operations_research_v1"
SPLITS = ("development", "validation", "test")
Split = Literal["development", "validation", "test"]
Profile = Literal["click", "consumption", "interaction", "growth"]


class ResearchSource(StrictModel):
    source_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    url: str = Field(pattern=r"^https://[^\s]+$", max_length=2000)
    publisher: str = Field(min_length=1, max_length=300)
    published_date: str | None = Field(default=None, max_length=100)
    accessed_date: Literal["2026-10-14"] = "2026-10-14"
    source_kind: Literal["tencent_product", "official_guidance", "official_rule"]
    evidence_summary: str = Field(min_length=1, max_length=3000)
    support_boundary: str = Field(min_length=1, max_length=3000)

    @model_validator(mode="after")
    def public_source_url(self):
        parsed = urlsplit(self.url)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("research source must have a public HTTPS hostname without credentials")
        return self


class ResearchCandidate(CandidateScenario):
    manual_rubric: tuple[str, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def bounded_rubric(self):
        if any(not item.strip() or len(item) > 1000 for item in self.manual_rubric):
            raise ValueError("manual rubric entries must be nonempty and bounded")
        return self


class ResearchObservation(StrictModel):
    observation_index: Literal[1, 2]
    observation_name: str = Field(min_length=1, max_length=300)
    observed_at: AwareDatetime
    facts: tuple[EvolutionFact, ...] = Field(min_length=1, max_length=5)
    aggregate_profile: Profile
    content_type: Literal["article", "video"]
    candidates: tuple[ResearchCandidate, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def current_facts_and_contrast(self):
        if any(fact.available_at > self.observed_at for fact in self.facts):
            raise ValueError("future facts are unavailable at observation time")
        if len({fact.fact_id for fact in self.facts}) != len(self.facts):
            raise ValueError("observation contains duplicate fact IDs")
        if len({candidate.candidate_key for candidate in self.candidates}) != 2:
            raise ValueError("candidate authoring keys must differ")
        public = [_public_candidate(candidate) for candidate in self.candidates]
        if public[0] == public[1]:
            raise ValueError("contrast candidates must differ in public input")
        return self


class ResearchStory(StrictModel):
    story_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    split: Split
    design_source_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    observations: tuple[ResearchObservation, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def observation_lineage(self):
        if tuple(item.observation_index for item in self.observations) != (1, 2):
            raise ValueError("story needs observations 1 and 2 in order")
        if self.observations[0].observed_at >= self.observations[1].observed_at:
            raise ValueError("observation times must increase")
        if len(set(self.design_source_ids)) != len(self.design_source_ids):
            raise ValueError("duplicate design source reference")
        facts = {}
        for observation in self.observations:
            for fact in observation.facts:
                if fact.source_id != f"fixture-{self.story_id}":
                    raise ValueError("news facts must reference their fictional story source")
                if fact.fact_id in facts and facts[fact.fact_id] != fact:
                    raise ValueError("same fact ID cannot change its text, time or source")
                facts[fact.fact_id] = fact
        return self


class ResearchBoard(StrictModel):
    board_id: str = Field(pattern=r"^[a-z0-9_-]+$")
    board_name: str = Field(min_length=1, max_length=128)
    operator_id: str = Field(min_length=1, max_length=128)
    operator_name: str = Field(min_length=1, max_length=128)
    scope_type: Literal["operator"] = "operator"
    applicable_scenarios: tuple[str, ...] = Field(min_length=1, max_length=20)
    metric_objectives: tuple[str, ...] = Field(min_length=1, max_length=20)
    audience_summary: str = Field(min_length=1, max_length=1000)
    risk_focus: tuple[str, ...] = Field(min_length=1, max_length=20)
    stories: tuple[ResearchStory, ...] = Field(min_length=3, max_length=3)

    @model_validator(mode="after")
    def complete_split_coverage(self):
        if {story.split for story in self.stories} != set(SPLITS):
            raise ValueError("each board requires one independent story per split")
        return self


class OperationsResearchCatalog(StrictModel):
    schema_version: Literal["operations-research-catalog-v1"] = "operations-research-catalog-v1"
    dataset_version: Literal[VERSION] = VERSION
    synthetic: Literal[True] = True
    research_sources: tuple[ResearchSource, ...] = Field(min_length=1)
    boards: tuple[ResearchBoard, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def independent_research_and_news_lineage(self):
        sources = {source.source_id for source in self.research_sources}
        if len(sources) != len(self.research_sources):
            raise ValueError("duplicate research source ID")
        for field in ("board_id", "operator_id"):
            if len({getattr(board, field) for board in self.boards}) != len(self.boards):
                raise ValueError(f"duplicate {field}; fixture operators must be independent")
        stories, fact_stories = set(), {}
        for board in self.boards:
            for story in board.stories:
                if story.story_id in stories:
                    raise ValueError("story cannot cross boards or splits")
                stories.add(story.story_id)
                if set(story.design_source_ids) - sources:
                    raise ValueError("missing research source used in test design")
                for observation in story.observations:
                    for fact in observation.facts:
                        if fact_stories.setdefault(fact.fact_id, story.story_id) != story.story_id:
                            raise ValueError("fact ID leaks across independent stories")
        return self


class ResearchCase(StrictModel):
    case_id: str = Field(pattern=r"^research-[0-9a-f]{20}$")
    story_id: str = Field(pattern=r"^synthetic-story-[0-9a-f]{20}$")
    event_id: str = Field(pattern=r"^synthetic-event-[0-9a-f]{20}$")
    source_story_id: str
    source_board_id: str
    source_operator_id: str
    scope: SyntheticScope
    split: Split
    story_title: str
    stage_index: Literal[1, 2]
    stage_name: Literal["observation_1", "observation_2"]
    observation_name: str = Field(min_length=1, max_length=300)
    observed_at: AwareDatetime
    known_facts: tuple[EvolutionFact, ...] = Field(min_length=1)
    candidate: EvolutionCandidate
    expected: ExpectedJudgment
    analysis_case: HotNewsEvaluationCase
    metric_profile: Profile
    baseline: HotNewsMetrics | None
    design_source_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    manual_rubric: tuple[str, ...] = Field(min_length=1, max_length=20)
    synthetic: Literal[True] = True
    annotation_status: Literal["generated_pending_review"] = "generated_pending_review"
    delivery_status: Literal["not_executed"] = "not_executed"

    @property
    def scope_sha256(self):
        return canonical_json_sha256(self.scope)


class OperationsResearchDataset(StrictModel):
    schema_version: Literal["operations-research-dataset-v1"] = "operations-research-dataset-v1"
    dataset_version: Literal[VERSION] = VERSION
    synthetic: Literal[True] = True
    annotation_status: Literal["generated_pending_review"] = "generated_pending_review"
    strategy_status: Literal["research_seed_not_production_policy"] = "research_seed_not_production_policy"
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    research_sources: tuple[ResearchSource, ...] = Field(min_length=1)
    cases: tuple[ResearchCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validated_contract(self):
        validate_dataset(self)
        return self


def _id(prefix, *parts):
    return opaque_id(prefix, VERSION, *parts)


def _public_candidate(candidate):
    return EvolutionCandidate.model_validate(candidate.model_dump(exclude={"candidate_key", "expected", "manual_rubric"}))


def _case_id(board_id, story_id, index, candidate):
    return _id("research-", board_id, story_id, str(index), canonical_json_sha256(candidate))


def load_catalog(path: Path = DEFAULT_CATALOG) -> OperationsResearchCatalog:
    return OperationsResearchCatalog.model_validate_json(Path(path).read_bytes())


def _analysis(story_title, board_id, story_id, index, observed_at, facts, profile, content_type):
    excerpt = "；".join(fact.text for fact in facts)
    if len(excerpt) > 1850:
        raise ValueError("complete visible facts exceed the existing analysis input budget")
    news_id = _id("synthetic-news-", board_id, story_id, str(index))
    evidence_id = _id("synthetic-evidence-", board_id, story_id, str(index))
    snapshot = replace(metric_snapshot(news_id, profile, ContentType(content_type)),
                       window_start=observed_at - timedelta(hours=1), window_end=observed_at)
    baseline = replace(snapshot, clicks=1, effective_consumptions=0, interactions=0,
                       ctr=Decimal(1) / Decimal(snapshot.impressions)) if profile == "growth" else None
    heat = HotScoreCalculator().calculate(snapshot, baseline)
    analysis = HotNewsAnalysisInput(
        news_id=news_id, title=story_title,
        summary="公开研究启发的合成新闻情景；不对应真实新闻。" + excerpt,
        content_excerpt="以下仅为观察时点可见的虚构事实；公开研究来源不证明这些事实发生。" + excerpt,
        content_type=content_type, window_start=snapshot.window_start, window_end=snapshot.window_end,
        hot_score=float(heat.score), metrics=metrics_view(snapshot),
        score_components=HotScoreComponents(click=float(heat.click_component),
            consumption=float(heat.consumption_component), interaction=float(heat.interaction_component),
            growth=float(heat.growth_component)),
        related_news=[RelatedNewsEvidence(news_id=evidence_id, title="合成主线当前可见事实包",
            excerpt=excerpt, published_at=max(fact.available_at for fact in facts), final_score=1,
            rerank_reasons=["synthetic_fixture_only"])],
        analysis_policy_version="synthetic-operations-research-v1",
    )
    return analysis, metrics_view(baseline) if baseline else None


def build_dataset(catalog: OperationsResearchCatalog) -> OperationsResearchDataset:
    cases = []
    for board in catalog.boards:
        scope = SyntheticScope(
            tenant_id=_id("synthetic-tenant-"), user_id=_id("synthetic-operator-", board.operator_id),
            section_id=_id("synthetic-board-", board.board_id),
            **board.model_dump(exclude={"board_id", "operator_id", "stories"}),
        )
        for story in board.stories:
            known = {}
            for observation in story.observations:
                for fact in observation.facts:
                    known[fact.fact_id] = fact
                facts = tuple(sorted(known.values(), key=lambda fact: (fact.available_at, fact.fact_id)))
                index = observation.observation_index
                analysis, baseline = _analysis(story.title, board.board_id, story.story_id, index,
                    observation.observed_at, facts, observation.aggregate_profile, observation.content_type)
                for source_candidate in observation.candidates:
                    candidate = _public_candidate(source_candidate)
                    case_id = _case_id(board.board_id, story.story_id, index, candidate)
                    expected = ExpectedHotNewsAnalysis(
                        allowed_dominant_drivers=(observation.aggregate_profile,),
                        required_evidence_news_ids=(analysis.related_news[0].news_id,),
                        forbidden_evidence_news_ids=(_id("unprovided-", case_id),),
                        required_metric_keys=("ctr", "hot_score"),
                        must_state_limitation=source_candidate.expected.must_state_limitation,
                    )
                    cases.append(ResearchCase(
                        case_id=case_id, story_id=_id("synthetic-story-", board.board_id, story.story_id),
                        event_id=_id("synthetic-event-", board.board_id, story.story_id),
                        source_story_id=story.story_id, source_board_id=board.board_id,
                        source_operator_id=board.operator_id, scope=scope, split=story.split,
                        story_title=story.title, stage_index=index, stage_name=f"observation_{index}",
                        observation_name=observation.observation_name, observed_at=observation.observed_at,
                        known_facts=facts, candidate=candidate, expected=source_candidate.expected,
                        analysis_case=HotNewsEvaluationCase(case_id=case_id,
                            description="公开研究启发的合成分析契约；运营决定单独评分。" + story.title,
                            tags=("synthetic", "pending_review"), analysis_input=analysis, expected=expected),
                        metric_profile=observation.aggregate_profile, baseline=baseline,
                        design_source_ids=story.design_source_ids, manual_rubric=source_candidate.manual_rubric,
                    ))
    # ID order carries no authoring-key or candidate-order hint about the answer.
    return OperationsResearchDataset(catalog_sha256=canonical_json_sha256(catalog),
        research_sources=catalog.research_sources, cases=tuple(sorted(cases, key=lambda case: case.case_id)))


def validate_dataset(dataset: OperationsResearchDataset) -> None:
    source_ids = {source.source_id for source in dataset.research_sources}
    if len(source_ids) != len(dataset.research_sources):
        raise ValueError("duplicate research source ID")
    case_ids, scopes, stories, split_by_story, fact_stories, observations, operators = set(), {}, {}, {}, {}, {}, {}
    for case in dataset.cases:
        if case.case_id in case_ids or case.analysis_case.case_id != case.case_id:
            raise ValueError("case IDs must be unique and match analysis case IDs")
        case_ids.add(case.case_id)
        if case.case_id != _case_id(case.source_board_id, case.source_story_id, case.stage_index, case.candidate):
            raise ValueError("case identity differs from versioned public candidate input")
        if (case.story_id != _id("synthetic-story-", case.source_board_id, case.source_story_id)
                or case.event_id != _id("synthetic-event-", case.source_board_id, case.source_story_id)):
            raise ValueError("story/event lineage identity mismatch")
        if (case.scope.tenant_id != _id("synthetic-tenant-")
                or case.scope.user_id != _id("synthetic-operator-", case.source_operator_id)
                or case.scope.section_id != _id("synthetic-board-", case.source_board_id)):
            raise ValueError("scope identity mismatch")
        if scopes.setdefault(case.source_board_id, case.scope) != case.scope:
            raise ValueError("board scope changed between cases")
        if operators.setdefault(case.scope.user_id, case.source_board_id) != case.source_board_id:
            raise ValueError("fixture operators must be independent across boards")
        if split_by_story.setdefault(case.source_story_id, case.split) != case.split:
            raise ValueError("story leaks across splits")
        story_state = (case.source_board_id, case.scope, case.story_title, case.design_source_ids)
        if stories.setdefault(case.source_story_id, story_state) != story_state:
            raise ValueError("story identity, scope or design sources changed")
        if (len(set(case.design_source_ids)) != len(case.design_source_ids)
                or set(case.design_source_ids) - source_ids):
            raise ValueError("missing or duplicate design source reference")
        if len({fact.fact_id for fact in case.known_facts}) != len(case.known_facts):
            raise ValueError("duplicate visible fact ID")
        for fact in case.known_facts:
            if fact.available_at > case.observed_at or fact.source_id != f"fixture-{case.source_story_id}":
                raise ValueError("future or non-fictional news fact source")
            if fact_stories.setdefault(fact.fact_id, case.story_id) != case.story_id:
                raise ValueError("fact ID leaks across independent stories")
        facts = tuple(sorted(case.known_facts, key=lambda fact: (fact.available_at, fact.fact_id)))
        if facts != case.known_facts:
            raise ValueError("visible facts must be deterministically ordered")
        analysis, baseline = _analysis(case.story_title, case.source_board_id, case.source_story_id,
            case.stage_index, case.observed_at, facts, case.metric_profile,
            case.analysis_case.analysis_input.content_type)
        if case.analysis_case.analysis_input != analysis or case.baseline != baseline:
            raise ValueError("analysis input, evidence, aggregates or baseline differ from visible facts and profiles")
        expected_analysis = ExpectedHotNewsAnalysis(
            allowed_dominant_drivers=(case.metric_profile,),
            required_evidence_news_ids=(analysis.related_news[0].news_id,),
            forbidden_evidence_news_ids=(_id("unprovided-", case.case_id),),
            required_metric_keys=("ctr", "hot_score"), must_state_limitation=case.expected.must_state_limitation,
        )
        if case.analysis_case.expected != expected_analysis:
            raise ValueError("analysis reference differs from deterministic contract")
        if case.stage_name != f"observation_{case.stage_index}":
            raise ValueError("observation index/name mismatch")
        if any(not item.strip() or len(item) > 1000 for item in case.manual_rubric):
            raise ValueError("invalid manual review rubric")
        state = (case.observed_at, case.known_facts, case.observation_name, analysis, case.baseline)
        item = observations.setdefault((case.source_story_id, case.stage_index), {"state": state, "candidates": []})
        if item["state"] != state:
            raise ValueError("contrast candidates disagree on observation context")
        item["candidates"].append(case.candidate)
        validate_metrics(case)
    for story_id in stories:
        previous_date, previous_facts = None, {}
        for index in (1, 2):
            item = observations.get((story_id, index))
            if item is None or len(item["candidates"]) != 2 or item["candidates"][0] == item["candidates"][1]:
                raise ValueError("story requires two observations and two distinct candidates each")
            observed_at, facts, *_ = item["state"]
            current = {fact.fact_id: fact for fact in facts}
            if previous_date is not None and observed_at <= previous_date:
                raise ValueError("observation dates must increase")
            if any(current.get(key) != value for key, value in previous_facts.items()):
                raise ValueError("known past facts cannot disappear or change")
            previous_date, previous_facts = observed_at, current
    for board_id in scopes:
        board_stories = {story: split_by_story[story] for story, state in stories.items() if state[0] == board_id}
        if len(board_stories) != 3 or set(board_stories.values()) != set(SPLITS):
            raise ValueError("board requires one independent story in each split")


def blind_case_input(case: ResearchCase) -> dict:
    """Allowlist of visible scenario data; research design and reference labels stay out."""
    return {
        "case_id": case.case_id, "synthetic": True,
        "scope": case.scope.model_dump(mode="json"), "scope_sha256": case.scope_sha256,
        "story_id": case.story_id, "event_id": case.event_id, "story_title": case.story_title,
        "observation_index": case.stage_index, "observed_at": case.observed_at.isoformat(),
        "history": [{"text": fact.text, "available_at": fact.available_at.isoformat(),
                     "source_url": "synthetic://operations-research-v1/" + _id("fixture-", case.story_id)}
                    for fact in case.known_facts],
        "candidate": case.candidate.model_dump(mode="json"),
        "analysis_input": case.analysis_case.analysis_input.model_dump(mode="json"),
    }


def blind_input(dataset: OperationsResearchDataset, split: Split | None = None, board_id: str | None = None) -> dict:
    return {"dataset_version": dataset.dataset_version, "synthetic": True,
            "cases": [blind_case_input(case) for case in dataset.cases
                      if (split is None or case.split == split)
                      and (board_id is None or case.source_board_id == board_id)]}


def analysis_projection(dataset: OperationsResearchDataset, split: Split | None = None) -> HotNewsEvaluationDataset:
    selected = {}
    for case in dataset.cases:
        if split is None or case.split == split:
            selected.setdefault(case.analysis_case.analysis_input.news_id, case.analysis_case)
    return HotNewsEvaluationDataset(
        dataset_name=f"hot-news-operations-research-{split or 'all'}", dataset_version=dataset.dataset_version,
        description="公开研究启发的合成新闻；参考判断待人工校准，不证明运营收益。",
        cases=tuple(selected.values()),
    )


def summary(dataset: OperationsResearchDataset) -> dict:
    return {
        "dataset_version": dataset.dataset_version, "synthetic": True,
        "annotation_status": dataset.annotation_status, "case_count": len(dataset.cases),
        "board_count": len({case.source_board_id for case in dataset.cases}),
        "operator_count": len({case.scope.user_id for case in dataset.cases}),
        "independent_story_count": len({case.story_id for case in dataset.cases}),
        "unique_news_snapshot_count": len({case.analysis_case.analysis_input.news_id for case in dataset.cases}),
        "research_source_count": len(dataset.research_sources),
        "splits": dict(sorted(Counter(case.split for case in dataset.cases).items())),
        "decisions": dict(sorted(Counter(case.expected.decision_type for case in dataset.cases).items())),
        "categories": dict(sorted(Counter(case.expected.category for case in dataset.cases).items())),
        "reason_codes": dict(sorted(Counter(code for case in dataset.cases for code in case.expected.required_reason_codes).items())),
        "boards": {board_id: {"board_name": next(case.scope.board_name for case in dataset.cases if case.source_board_id == board_id),
            "case_count": sum(case.source_board_id == board_id for case in dataset.cases)}
            for board_id in sorted({case.source_board_id for case in dataset.cases})},
        "limitations": ["public_sources_inform_design_not_news_verification", "synthetic_metrics_and_news_facts",
            "generated_labels_pending_human_review", "not_executed", "no_causal_delivery_effect",
            "manual_rubric_not_automatically_scored", "template_holdout_not_real_news_generalization"],
    }


def render_casebook(dataset: OperationsResearchDataset, board_id: str | None = None) -> str:
    sources = {source.source_id: source for source in dataset.research_sources}
    lines = ["# 公开研究启发的分板块运营评测案例", "", f"版本：{dataset.dataset_version}。",
        "新闻、身份、聚合指标和参考决定均为合成，待人工复核，未投递。公开来源只支撑设计原则，不能证明虚构新闻发生。",
        "自动评分仅检查决定、类别、理由、阻断、证据和修改字段；改稿语义须按每例人工 rubric 复核。", ""]
    cases = sorted(dataset.cases, key=lambda case: (case.source_board_id, case.source_story_id, case.stage_index, case.case_id))
    for case in cases:
        if board_id is not None and case.source_board_id != board_id:
            continue
        lines.extend([f"## {case.scope.board_name} · {case.story_title} · 观察{case.stage_index} · {case.case_id}", "",
            f"split：{case.split}；模拟运营：{case.scope.operator_name}；观察情景：{case.observation_name}。",
            f"观察时间：{case.observed_at.isoformat()}。", "", "设计依据（与合成新闻事实分开）："])
        lines.extend(f"- [{sources[source_id].title}]({sources[source_id].url})：{sources[source_id].support_boundary}"
                     for source_id in case.design_source_ids)
        lines.extend(["", "截至观察时点的合成事实："])
        lines.extend(f"- {fact.available_at.isoformat()}：{fact.text}（{fact.source_id}）" for fact in case.known_facts)
        lines.extend(["", f"候选标题：{case.candidate.title}", f"分析主张：{case.candidate.analysis_claim}",
            f"文案：{case.candidate.notification_copy}",
            f"人群：{json.dumps(case.candidate.audience.model_dump(mode='json'), ensure_ascii=False)}；渠道：{case.candidate.channel.value}。",
            f"发送窗：{case.candidate.send_after.isoformat()} 至 {case.candidate.expire_at.isoformat()}。",
            f"条件：{json.dumps(case.candidate.conditions.model_dump(), ensure_ascii=False)}", "",
            f"参考决定：{case.expected.decision_type}；可接受决定：{', '.join(case.expected.allowed_decision_types)}；分类：{case.expected.category}。",
            f"必需理由：{', '.join(case.expected.required_reason_codes)}；可选理由：{', '.join(case.expected.optional_reason_codes) or '无'}。",
            f"原候选需阻断：{case.expected.must_block}；修改字段：{', '.join(case.expected.required_revision_fields) or '无'}。",
            f"参考说明：{case.expected.explanation}", f"参考改稿：{case.expected.revision or '无需修订'}", "", "人工语义 rubric："])
        lines.extend(f"- {item}" for item in case.manual_rubric)
        lines.append("")
    return "\n".join(lines)


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _artifact_contents(dataset):
    counts = summary(dataset)
    contents = {
        "operations_cases.json": _json_bytes(dataset.model_dump(mode="json")),
        "operations_inputs.json": _json_bytes(blind_input(dataset)),
        "operations_casebook.md": render_casebook(dataset).encode("utf-8"),
        "source_registry.json": _json_bytes({"dataset_version": dataset.dataset_version,
            "usage": "research_design_only_not_news_fact_evidence",
            "research_sources": [source.model_dump(mode="json") for source in dataset.research_sources]}),
        "summary.json": _json_bytes(counts),
        "hot_news_analysis_all.json": _json_bytes(analysis_projection(dataset).model_dump(mode="json")),
    }
    index = ["# 运营建议评测扩充：公开研究设计与合成情景", "", f"版本：{dataset.dataset_version}。",
        f"{counts['board_count']}板块、{counts['independent_story_count']}条主线、{counts['unique_news_snapshot_count']}个观察快照、{counts['case_count']}条候选判断。",
        "新闻事实、指标与运营身份均为合成；参考判断待人工校准，全部未投递。公开来源只解释设计依据。", "",
        "[来源登记](source_registry.json) · [全部答案与人工 rubric](operations_casebook.md) · [全部盲输入](operations_inputs.json) · [数据统计](summary.json)", "",
        "| 板块 | 参考案例册 | 无答案输入 |", "|---|---|---|"]
    for board_id, board in counts["boards"].items():
        casebook = f"operations_casebook_{board_id}.md"
        inputs = f"operations_inputs_board_{board_id}.json"
        contents[casebook] = render_casebook(dataset, board_id).encode("utf-8")
        contents[inputs] = _json_bytes(blind_input(dataset, board_id=board_id))
        index.append(f"| {board['board_name']} | [{board['case_count']}例]({casebook}) | [盲输入]({inputs}) |")
    for split in SPLITS:
        contents[f"operations_inputs_{split}.json"] = _json_bytes(blind_input(dataset, split))
        contents[f"hot_news_analysis_{split}.json"] = _json_bytes(analysis_projection(dataset, split).model_dump(mode="json"))
    index.extend(["", "每板块每split保留一条完整独立主线，每主线两个观察时点、每时点两个对照候选；不强迫真实新闻走六阶段模板。",
        "自动评分复用既有固定分母评分器；manual_rubric 不进入模型输入，也不被自动当成语义评分。",
        "板块运营仍需复核合理替代动作、理由码和改稿语义；正式标签二审、模型质量与真实运营收益另行验收。",
        "源目录、不可变产物与 manifest 均版本化；不修改生产 Prompt/Bundle，不训练或投递。", ""])
    contents["README.md"] = "\n".join(index).encode("utf-8")
    return contents


def write_artifacts(dataset: OperationsResearchDataset, output: Path = DEFAULT_OUTPUT) -> dict:
    validate_dataset(dataset)
    output = Path(output)
    contents = _artifact_contents(dataset)
    manifest = {**summary(dataset), "schema_version": "operations-research-manifest-v1",
        "catalog_sha256": dataset.catalog_sha256,
        "artifacts": {name: {"sha256": sha256(content).hexdigest(), "size_bytes": len(content)}
                      for name, content in sorted(contents.items())}}
    contents["manifest.json"] = _json_bytes(manifest)
    if output.exists() or output.is_symlink():
        if output.is_symlink() or not output.is_dir():
            raise ValueError("artifact output must be a real directory")
        if {path.name for path in output.iterdir()} - set(contents):
            raise ValueError("output has unrelated or stale files; choose a new version directory")
        for name, content in contents.items():
            target = output / name
            if target.is_symlink() or (target.exists() and (not target.is_file() or target.read_bytes() != content)):
                raise ValueError(f"refusing to overwrite different artifact: {name}")
    output.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        target = output / name
        try:
            with target.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            if target.is_symlink() or not target.is_file() or target.read_bytes() != content:
                raise ValueError(f"artifact changed during exclusive creation: {name}")
    return manifest


def verify_artifact_hashes(output: Path = DEFAULT_OUTPUT) -> None:
    output = Path(output)
    manifest_path = output / "manifest.json"
    if output.is_symlink() or manifest_path.is_symlink():
        raise ValueError("artifact directory and manifest cannot be symlinks")
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("schema_version") != "operations-research-manifest-v1":
        raise ValueError("unexpected manifest version")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("manifest requires artifact hashes")
    if {path.name for path in output.iterdir()} != set(artifacts) | {"manifest.json"}:
        raise ValueError("artifact inventory differs from manifest")
    for name, receipt in artifacts.items():
        if not isinstance(name, str) or Path(name).name != name or name in {".", "..", "manifest.json"}:
            raise ValueError("unsafe artifact filename")
        target = output / name
        if target.is_symlink() or not target.is_file():
            raise ValueError("artifact must be a regular local file")
        content = target.read_bytes()
        if not isinstance(receipt, dict) or receipt.get("sha256") != sha256(content).hexdigest() or receipt.get("size_bytes") != len(content):
            raise ValueError(f"artifact hash or size mismatch: {name}")
    dataset = OperationsResearchDataset.model_validate_json((output / "operations_cases.json").read_bytes())
    rebuilt = _artifact_contents(dataset)
    if set(rebuilt) != set(artifacts) or any((output / name).read_bytes() != content for name, content in rebuilt.items()):
        raise ValueError("artifacts differ from validated dataset projections")
    if manifest.get("catalog_sha256") != dataset.catalog_sha256:
        raise ValueError("manifest/catalog lineage mismatch")
    if any(manifest.get(key) != value for key, value in summary(dataset).items()):
        raise ValueError("manifest summary differs from validated dataset")
