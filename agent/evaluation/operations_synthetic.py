"""Offline, explicitly synthetic operator feedback; never creates approval records."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.analytics.entities import ContentType
from app.modules.hot_news.scoring.hot_score import HotScoreCalculator
from app.modules.hot_news.scoring.metrics import NewsMetricSnapshot
from app.modules.data_loop.evaluation.hot_news import (
    ExpectedHotNewsAnalysis, HotNewsEvaluationCase, HotNewsEvaluationDataset,
)
from app.modules.hot_news.contracts.hot_news import (
    HotNewsAnalysisInput, HotNewsMetrics, HotScoreComponents, RelatedNewsEvidence,
)
from app.modules.hot_news.contracts.hot_news_decision import DecisionType
from app.modules.data_loop.contracts.operations_feedback import OperationsFeedback, is_fresh_bad_case_eligible


VERSION = "2026-10-10.synthetic-v1"
DEFAULT_CATALOG = Path(__file__).parent / "seeds" / "operations_scenarios_v1.json"
DEFAULT_OUTPUT = Path(__file__).parent / "datasets" / "hot_news" / "operations_synthetic_v1"
Layer = Literal["golden", "fresh_bad_case", "high_risk"]
Verdict = Literal["correct", "partially_correct", "incorrect", "unassessed"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ResearchSource(StrictModel):
    source_id: str
    title: str
    url: str = Field(pattern=r"^https://")


class CandidateSuggestion(StrictModel):
    action: str = Field(min_length=1)
    audience: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    notification_copy: str = Field(alias="copy", min_length=1)
    analysis_claim: str = Field(min_length=1)


class SimulatedJudgment(StrictModel):
    decision_type: DecisionType
    operator_feedback: OperationsFeedback
    analysis_verdict: Verdict
    reason: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1)
    revision: str | None = None

    @model_validator(mode="after")
    def check_judgment(self):
        if (self.decision_type == "corrected") != bool(self.revision):
            raise ValueError("only corrected judgments require a proposed revision")
        if self.operator_feedback.category == "positive_example" and self.decision_type != "accepted":
            raise ValueError("positive examples must be accepted")
        if self.operator_feedback.category == "analysis_error" and self.analysis_verdict not in {
            "incorrect", "partially_correct",
        }:
            raise ValueError("analysis errors must explain an incorrect analysis")
        return self


class ScenarioVariant(StrictModel):
    variant_id: str = Field(pattern=r"^[a-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=500)
    facts: tuple[str, ...] = Field(min_length=3, max_length=10)
    candidate: CandidateSuggestion
    expected: SimulatedJudgment
    source_ids: tuple[str, ...] = Field(min_length=1)


class ScenarioGroup(StrictModel):
    strategy_id: str = Field(pattern=r"^OPS-(0[1-9]|1[0-2])$")
    pair_id: str = Field(pattern=r"^[a-z0-9_-]+$")
    proposed_layer: Layer
    contrast: str = Field(min_length=1)
    variants: tuple[ScenarioVariant, ...] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def check_pair(self):
        if len({v.variant_id for v in self.variants}) != len(self.variants):
            raise ValueError("variant IDs must be unique within an event group")
        if self.variants[0].model_dump(exclude={"variant_id"}) == self.variants[1].model_dump(exclude={"variant_id"}):
            raise ValueError("comparison variants must differ")
        if self.proposed_layer == "fresh_bad_case":
            for variant in self.variants:
                judgment = variant.expected
                if not is_fresh_bad_case_eligible(
                    verdict=judgment.analysis_verdict,
                    source_type=f"operator_{judgment.decision_type}",
                    source_reference={"operator_feedback": judgment.operator_feedback.model_dump()},
                ):
                    raise ValueError("business rejection/positive example cannot enter Fresh")
        return self


class ScenarioCatalog(StrictModel):
    catalog_version: Literal["operations-scenarios-v1"]
    sources: tuple[ResearchSource, ...] = Field(min_length=1)
    groups: tuple[ScenarioGroup, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_references(self):
        pairs = [g.pair_id for g in self.groups]
        sources = [s.source_id for s in self.sources]
        if len(pairs) != len(set(pairs)) or len(sources) != len(set(sources)):
            raise ValueError("event group and source IDs must be globally unique")
        for group in self.groups:
            for variant in group.variants:
                if set(variant.source_ids) - set(sources):
                    raise ValueError("unknown research source")
        return self


class SyntheticOperationsCase(StrictModel):
    case_id: str
    event_id: str
    pair_id: str
    strategy_id: str
    proposed_layer: Layer
    contrast: str
    synthetic: Literal[True] = True
    annotation_status: Literal["generated_pending_review"] = "generated_pending_review"
    delivery_status: Literal["not_executed"] = "not_executed"
    scenario: ScenarioVariant
    analysis_case: HotNewsEvaluationCase
    metric_profile: Literal["click", "consumption", "interaction", "growth"]
    baseline: HotNewsMetrics | None


class SyntheticOperationsDataset(StrictModel):
    dataset_version: str
    synthetic: Literal[True] = True
    annotation_status: Literal["generated_pending_review"] = "generated_pending_review"
    strategy_status: Literal["research_seed_not_production_policy"] = "research_seed_not_production_policy"
    catalog_sha256: str
    sources: tuple[ResearchSource, ...]
    cases: tuple[SyntheticOperationsCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def check_integrity(self):
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate synthetic case ID")
        event_layers: dict[str, str] = {}
        pair_events: dict[str, str] = {}
        known_sources = {s.source_id for s in self.sources}
        if len(known_sources) != len(self.sources):
            raise ValueError("duplicate research source ID")
        for case in self.cases:
            if case.analysis_case.case_id != case.case_id:
                raise ValueError("analysis and operations identities differ")
            old = event_layers.setdefault(case.event_id, case.proposed_layer)
            if old != case.proposed_layer:
                raise ValueError("event leaks across proposed layers")
            if pair_events.setdefault(case.pair_id, case.event_id) != case.event_id:
                raise ValueError("one comparison pair must stay in one event")
            if set(case.scenario.source_ids) - known_sources:
                raise ValueError("unknown research source in case")
            if case.proposed_layer == "fresh_bad_case":
                judgment = case.scenario.expected
                if not is_fresh_bad_case_eligible(
                    verdict=judgment.analysis_verdict,
                    source_type=f"operator_{judgment.decision_type}",
                    source_reference={"operator_feedback": judgment.operator_feedback.model_dump()},
                ):
                    raise ValueError("ineligible Fresh case")
            validate_metrics(case)
        return self


# Only aggregate counts are synthesized. These are fixture profiles, not Tencent benchmarks.
PROFILES = {
    "click": (1000, 800, 160, 8),
    "consumption": (4000, 400, 360, 18),
    "interaction": (4000, 400, 80, 80),
    "growth": (10000, 100, 10, 0),
}
START = datetime(2026, 10, 10, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 10, 1, tzinfo=timezone.utc)


def metric_snapshot(news_id: str, profile: str, content_type: ContentType) -> NewsMetricSnapshot:
    impressions, clicks, consumption, interactions = PROFILES[profile]
    return NewsMetricSnapshot(
        news_id=news_id, content_type=content_type, window_start=START, window_end=END,
        impressions=impressions, clicks=clicks, unique_users=min(impressions, clicks * 2),
        total_duration_seconds=consumption * 30, effective_consumptions=consumption,
        interactions=interactions, ctr=Decimal(clicks) / Decimal(impressions),
    )


def metrics_view(snapshot: NewsMetricSnapshot) -> HotNewsMetrics:
    return HotNewsMetrics(
        impressions=snapshot.impressions, clicks=snapshot.clicks, ctr=float(snapshot.ctr),
        unique_users=snapshot.unique_users, effective_consumptions=snapshot.effective_consumptions,
        interactions=snapshot.interactions,
    )


def validate_metrics(case: SyntheticOperationsCase) -> None:
    """Recompute persisted aggregates rather than trusting their serialized score."""
    data = case.analysis_case.analysis_input
    metrics = data.metrics
    if data.window_start.tzinfo is None or data.window_end.tzinfo is None or data.window_start >= data.window_end:
        raise ValueError("analysis window must be aware and ordered")
    def check_counts(value: HotNewsMetrics) -> None:
        if value.clicks > value.impressions or value.effective_consumptions > value.clicks or value.unique_users > value.impressions:
            raise ValueError("fixture counts must form a valid consumption funnel")
        expected_ctr = value.clicks / value.impressions if value.impressions else 0
        if abs(value.ctr - expected_ctr) > 1e-12:
            raise ValueError("CTR differs from authoritative counts")

    check_counts(metrics)
    ctr = metrics.clicks / metrics.impressions if metrics.impressions else 0
    snapshot = NewsMetricSnapshot(
        news_id=data.news_id, content_type=ContentType(data.content_type),
        window_start=data.window_start, window_end=data.window_end,
        impressions=metrics.impressions, clicks=metrics.clicks, unique_users=metrics.unique_users,
        total_duration_seconds=0, effective_consumptions=metrics.effective_consumptions,
        interactions=metrics.interactions, ctr=Decimal(str(ctr)),
    )
    baseline = None
    if case.baseline is not None:
        check_counts(case.baseline)
        baseline = replace(
            snapshot, clicks=case.baseline.clicks, effective_consumptions=case.baseline.effective_consumptions,
            interactions=case.baseline.interactions,
        )
    result = HotScoreCalculator().calculate(snapshot, baseline)
    expected = (result.click_component, result.consumption_component, result.interaction_component, result.growth_component)
    actual = (data.score_components.click, data.score_components.consumption, data.score_components.interaction, data.score_components.growth)
    if any(abs(float(x) - y) > 1e-12 for x, y in zip(expected, actual)) or abs(float(result.score) - data.hot_score) > 1e-12:
        raise ValueError("heat score/components differ from HotScoreCalculator")
    dominant = ("click", "consumption", "interaction", "growth")[max(range(4), key=lambda i: expected[i])]
    if case.metric_profile != dominant or case.analysis_case.expected.allowed_dominant_drivers != (dominant,):
        raise ValueError("fixture driver expectation differs from authoritative components")


def build_dataset(catalog: ScenarioCatalog) -> SyntheticOperationsDataset:
    cases = []
    profiles = tuple(PROFILES)
    for index, group in enumerate(catalog.groups):
        profile = profiles[index % len(profiles)]
        for variant in group.variants:
            # Public IDs carry no good/bad/action hints from authoring identifiers.
            case_id = "syn-" + sha256(f"{group.pair_id}/{variant.variant_id}".encode()).hexdigest()[:20]
            news_id = f"synthetic-{case_id}"
            content_type = ContentType.VIDEO if index % 4 == 3 else ContentType.ARTICLE
            snapshot = metric_snapshot(news_id, profile, content_type)
            baseline = replace(snapshot, clicks=1, effective_consumptions=0, interactions=0, ctr=Decimal("0.0001")) if profile == "growth" else None
            heat = HotScoreCalculator().calculate(snapshot, baseline)
            event_key = sha256(group.pair_id.encode()).hexdigest()[:20]
            evidence_id = f"synthetic-evidence-{case_id}"
            analysis = HotNewsAnalysisInput(
                news_id=news_id, title=variant.title,
                summary="合成新闻情景；不对应真实新闻。" + "；".join(variant.facts),
                content_excerpt="以下为模拟情景事实，业务设定不代表腾讯新闻内部规则。" + "；".join(variant.facts),
                content_type=content_type.value, window_start=START, window_end=END,
                hot_score=float(heat.score), metrics=metrics_view(snapshot),
                score_components=HotScoreComponents(
                    click=float(heat.click_component), consumption=float(heat.consumption_component),
                    interaction=float(heat.interaction_component), growth=float(heat.growth_component),
                ),
                related_news=[RelatedNewsEvidence(
                    news_id=evidence_id, title="合成情景事实记录（非外部新闻来源）",
                    excerpt="；".join(variant.facts), final_score=1, published_at=START,
                    rerank_reasons=["synthetic_fixture_only"],
                )], analysis_policy_version="synthetic-operations-analysis-v1",
            )
            analysis_case = HotNewsEvaluationCase(
                case_id=case_id, description="合成分析契约评测；运营判断另行评测。" + variant.title,
                tags=("synthetic", "pending_review", group.strategy_id, group.proposed_layer, profile),
                analysis_input=analysis,
                expected=ExpectedHotNewsAnalysis(
                    allowed_dominant_drivers=(profile,), required_evidence_news_ids=(evidence_id,),
                    forbidden_evidence_news_ids=(f"unprovided-{case_id}",),
                    required_metric_keys=("ctr", "hot_score"), must_state_limitation=True,
                ),
            )
            cases.append(SyntheticOperationsCase(
                case_id=case_id, event_id=f"synthetic-event-{event_key}", pair_id=group.pair_id,
                strategy_id=group.strategy_id, proposed_layer=group.proposed_layer, contrast=group.contrast,
                scenario=variant, analysis_case=analysis_case, metric_profile=profile,
                baseline=metrics_view(baseline) if baseline else None,
            ))
    return SyntheticOperationsDataset(
        dataset_version=VERSION, catalog_sha256=sha256(catalog.model_dump_json(by_alias=True).encode()).hexdigest(),
        sources=catalog.sources, cases=tuple(cases),
    )


def analysis_projection(dataset: SyntheticOperationsDataset, layer: Layer | None = None) -> HotNewsEvaluationDataset:
    return HotNewsEvaluationDataset(
        dataset_name=f"hot-news-operations-synthetic-{layer or 'all'}", dataset_version=dataset.dataset_version,
        description="全部为合成情景和程序预期，待人工复核；只评测分析Schema/驱动/证据/指标/限制，不证明运营策略效果。",
        cases=tuple(case.analysis_case for case in dataset.cases if layer is None or case.proposed_layer == layer),
    )


def summary(dataset: SyntheticOperationsDataset) -> dict:
    return {
        "dataset_version": dataset.dataset_version, "synthetic": True,
        "annotation_status": dataset.annotation_status, "case_count": len(dataset.cases),
        "independent_event_groups": len({c.event_id for c in dataset.cases}),
        "layers": dict(Counter(c.proposed_layer for c in dataset.cases)),
        "decisions": dict(Counter(c.scenario.expected.decision_type for c in dataset.cases)),
        "categories": dict(Counter(c.scenario.expected.operator_feedback.category for c in dataset.cases)),
        "strategies": dict(sorted(Counter(c.strategy_id for c in dataset.cases).items())),
    }


def write_artifacts(dataset: SyntheticOperationsDataset, output: Path) -> dict:
    for layer in ("golden", "fresh_bad_case", "high_risk"):
        if not any(case.proposed_layer == layer for case in dataset.cases) and (output / f"hot_news_analysis_{layer}.json").exists():
            raise ValueError("output contains a stale layer projection; use a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    artifacts = {"operations_cases.json": dataset.model_dump(mode="json", by_alias=True)}
    artifacts["hot_news_analysis_all.json"] = analysis_projection(dataset).model_dump(mode="json")
    for layer in ("golden", "fresh_bad_case", "high_risk"):
        if any(case.proposed_layer == layer for case in dataset.cases):
            artifacts[f"hot_news_analysis_{layer}.json"] = analysis_projection(dataset, layer).model_dump(mode="json")
    # Input-only export permits external offline predictions without including answer fields.
    artifacts["operations_inputs.json"] = {
        "dataset_version": dataset.dataset_version, "synthetic": True,
        "cases": [{
            "case_id": c.case_id, "event_id": c.event_id, "strategy_id": c.strategy_id,
            "title": c.scenario.title, "facts": c.scenario.facts,
            "candidate": c.scenario.candidate.model_dump(mode="json", by_alias=True),
            "analysis_input": c.analysis_case.analysis_input.model_dump(mode="json"),
        } for c in dataset.cases],
    }
    files = {}
    for name, value in artifacts.items():
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        (output / name).write_text(payload, encoding="utf-8")
        files[name] = {"sha256": sha256(payload.encode()).hexdigest(), "bytes": len(payload.encode())}
    casebook = render_casebook(dataset)
    (output / "operations_casebook.md").write_text(casebook, encoding="utf-8")
    files["operations_casebook.md"] = {"sha256": sha256(casebook.encode()).hexdigest(), "bytes": len(casebook.encode())}
    manifest = {**summary(dataset), "catalog_sha256": dataset.catalog_sha256, "files": files}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def render_casebook(dataset: SyntheticOperationsDataset) -> str:
    labels = {"accepted": "采纳", "rejected": "驳回", "deferred": "延后", "corrected": "修正"}
    lines = [
        "# 模拟运营反馈案例册", "", f"版本：{dataset.dataset_version}。",
        "全部为程序生成的虚构新闻情景与模拟编辑意见，待复核。研究来源支撑策略设计，不是这些新闻的来源。采纳仅表示模拟认可建议，所有案例均未执行投递。", "",
    ]
    for case in dataset.cases:
        scenario = case.scenario
        expected = scenario.expected
        lines.extend([
            f"## {case.strategy_id} · {scenario.title}", "",
            f"案例 `{case.case_id}`；事件 `{case.event_id}`；建议层 `{case.proposed_layer}`。",
            f"对照条件：{case.contrast}", "",
            "已知事实：", "", *[f"- {fact}" for fact in scenario.facts], "",
            f"候选动作：{scenario.candidate.action}",
            f"人群：{scenario.candidate.audience}；渠道：{scenario.candidate.channel}。",
            f"候选文案：{scenario.candidate.notification_copy}",
            f"候选分析主张：{scenario.candidate.analysis_claim}", "",
            f"模拟运营决定：**{labels[expected.decision_type]}**；类别 `{expected.operator_feedback.category}`；分析判断 `{expected.analysis_verdict}`。",
            f"理由码：`{'`、`'.join(expected.operator_feedback.reason_codes)}`。",
            f"简评：{expected.reason}", f"复核依据：{expected.rationale}",
        ])
        if expected.revision:
            lines.append(f"建议修订：{expected.revision}")
        lines.extend([f"研究依据：{', '.join(scenario.source_ids)}（不充当新闻事实证据）。", ""])
    lines.extend(["## 研究来源", ""])
    lines.extend(f"- {source.source_id}：[ {source.title} ]({source.url})" for source in dataset.sources)
    return "\n\n".join(lines) + "\n"


def verify_artifact_hashes(output: Path) -> None:
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["files"].items():
        if Path(name).name != name:
            raise ValueError("manifest filenames must be local basenames")
        payload = (output / name).read_bytes()
        if len(payload) != expected["bytes"] or sha256(payload).hexdigest() != expected["sha256"]:
            raise ValueError(f"artifact hash differs: {name}")


class OperatorPrediction(StrictModel):
    case_id: str
    decision_type: DecisionType
    operator_feedback: OperationsFeedback


def score_predictions(dataset: SyntheticOperationsDataset, predictions: list[OperatorPrediction]) -> dict:
    """Score simulated decisions only, with missing answers counted as failures."""
    supplied = {p.case_id: p for p in predictions}
    if len(supplied) != len(predictions):
        raise ValueError("duplicate prediction case_id")
    if set(supplied) - {c.case_id for c in dataset.cases}:
        raise ValueError("prediction contains unknown case_id")
    rows = []
    for case in dataset.cases:
        expected = case.scenario.expected
        prediction = supplied.get(case.case_id)
        action = bool(prediction and prediction.decision_type == expected.decision_type)
        category = bool(prediction and prediction.operator_feedback.category == expected.operator_feedback.category)
        reasons = bool(prediction and set(prediction.operator_feedback.reason_codes) == set(expected.operator_feedback.reason_codes))
        rows.append({
            "case_id": case.case_id, "strategy_id": case.strategy_id, "proposed_layer": case.proposed_layer,
            "missing": prediction is None, "decision_match": action, "category_match": category,
            "reason_codes_match": reasons, "passed": action and category and reasons,
        })
    count = len(rows)
    return {
        "dataset_version": dataset.dataset_version, "reference_status": "synthetic_pending_review",
        "total": count, "missing": sum(r["missing"] for r in rows),
        "decision_accuracy": sum(r["decision_match"] for r in rows) / count,
        "category_accuracy": sum(r["category_match"] for r in rows) / count,
        "reason_codes_exact_match": sum(r["reason_codes_match"] for r in rows) / count,
        "pass_rate": sum(r["passed"] for r in rows) / count,
        "cases": rows,
    }
