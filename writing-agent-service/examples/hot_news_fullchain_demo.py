"""热点分析全链路离线联调脚本。

把「行为数据 → 指标 → 基线 → 热度分 → 排行 → 证据富化 → 模型分析 → 交叉校验」
整条链路跑通，并把模型看到什么、输出了什么完整打印出来。

不需要 Temporal、PostgreSQL、Redis、MinIO：行为数据、基线、新闻内容全部使用
``examples/data/hot_news_scenario.json`` 的内存装配。

Runner 可插拔：

* 默认使用 ``OfflineHotNewsAnalysisRunner``，用确定性规则生成一份合规报告，
  用于验证链路与校验器。**它不是模型输出**。
* 配置好 ``FASTGPT_BASE_URL`` / ``FASTGPT_API_KEY`` / ``FASTGPT_HOT_NEWS_APP_ID``
  后自动切换为真实的 ``HotNewsAnalysisAgentRunner``，此时打印的就是真实模型建议。

运行::

    python -m examples.hot_news_fullchain_demo            # 离线占位 Runner
    python -m examples.hot_news_fullchain_demo --raw      # 额外打印原始 JSON

注意：本仓库 ``.env`` 里曾经写成 ``FASTGPT_DATASET_URL``，而 ``app/config.py``
期望的是 ``FASTGPT_BASE_URL``。真实联调前请确认键名，否则会出现
``FASTGPT_BASE_URL is required``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

from dotenv import load_dotenv

from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.news_content import NewsContent, NewsContentRepository
from app.clients.fastgpt import AgentResult
from app.clients.knowledge_base import RelatedNews, RelatedNewsSearchQuery
from app.schemas.hot_news import (
    AnalysisReason,
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    OperationSuggestion,
    RelatedNewsContext,
)
from app.services.hot_news_analysis import (
    HotNewsAnalysisInputBuilder,
    HotNewsAnalysisService,
    HotNewsAnalysisValidator,
)
from app.services.hot_news_orchestration import (
    HotNewsOrchestrationService,
    HotNewsRunRequest,
)
from examples.hot_news_demo import load_scenario
from examples.hot_news_e2e_support import (
    E2E_TENANT_ID,
    build_hot_news_e2e_dependencies,
)

WORKFLOW_VERSION = "offline-fullchain-demo"

_COMPONENT_LABEL = {
    "click": "点击",
    "consumption": "有效消费",
    "interaction": "互动",
    "growth": "增长",
}

_NON_WHITESPACE = re.compile(r"\s+")
_ASCII_TOKEN = re.compile(r"[a-z0-9]{2,}")


# --------------------------------------------------------------------------- #
# 离线知识检索桩
# --------------------------------------------------------------------------- #
def _terms(text: str) -> frozenset[str]:
    """把标题/摘要压成字符二元组集合，用于场景内的粗召回。"""

    normalized = _NON_WHITESPACE.sub("", text).lower()
    grams = {
        normalized[index : index + 2]
        for index in range(len(normalized) - 1)
    }
    return frozenset(grams | set(_ASCII_TOKEN.findall(normalized)))


class OfflineScenarioKnowledgeSearchClient:
    """知识库检索的离线替换件，只在同一场景内的新闻之间做关键词召回。

    遵守 ``KnowledgeSearchClient`` Protocol，不发起任何网络请求，也不读取
    企业真实数据。生产请替换为 ``FastGPTKnowledgeSearchClient``。
    """

    def __init__(
        self,
        content_repository: NewsContentRepository,
        candidate_news_ids: Sequence[str],
        *,
        tenant_id: str,
        min_overlap: int = 2,
    ) -> None:
        self._content_repository = content_repository
        self._candidate_news_ids = tuple(candidate_news_ids)
        self._tenant_id = tenant_id
        self._min_overlap = min_overlap
        self._contents: dict[str, NewsContent] | None = None

    async def _load_contents(self) -> Mapping[str, NewsContent]:
        if self._contents is None:
            self._contents = await self._content_repository.batch_get_by_news_ids(
                tenant_id=self._tenant_id,
                news_ids=list(self._candidate_news_ids),
            )
        return self._contents

    async def batch_search_related_news(
        self,
        queries: tuple[RelatedNewsSearchQuery, ...],
        *,
        tenant_id: str,
    ) -> dict[str, list[RelatedNews]]:
        contents = await self._load_contents()
        results: dict[str, list[RelatedNews]] = {}
        for query in queries:
            query.validate()
            results[query.query_id] = self._recall(query, contents)
        return results

    def _recall(
        self,
        query: RelatedNewsSearchQuery,
        contents: Mapping[str, NewsContent],
    ) -> list[RelatedNews]:
        excluded = set(query.exclude_news_ids)
        excluded.add(query.source_news_id)
        source_terms = _terms(query.query_text)

        scored: list[tuple[int, str, NewsContent]] = []
        for news_id in self._candidate_news_ids:
            if news_id in excluded:
                continue
            content = contents.get(news_id)
            if content is None:
                continue
            overlap = len(source_terms & _terms(f"{content.title} {content.summary}"))
            if overlap < self._min_overlap:
                continue
            scored.append((overlap, news_id, content))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            RelatedNews(
                collection_id=f"offline-{news_id}",
                news_id=news_id,
                title=content.title,
                text=content.summary[:1000],
                source_url=content.source_url,
                publish_time=(
                    content.publish_time.isoformat()
                    if content.publish_time is not None
                    else None
                ),
                score=float(overlap),
            )
            for overlap, news_id, content in scored[: query.candidate_limit]
        ]


# --------------------------------------------------------------------------- #
# 离线占位 Runner
# --------------------------------------------------------------------------- #
def build_offline_report(
    analysis_input: HotNewsAnalysisInput,
) -> HotNewsAnalysisReport:
    """用确定性规则生成一份能通过全部交叉校验的报告。

    这里刻意只做「输入数值 → 结构化表达」的映射，不引入任何新事实：
    指标、分数、证据全部取自可信输入，与真实模型应当遵守的约束完全一致。
    """

    components = analysis_input.score_components.model_dump()
    top_component = max(components, key=lambda key: components[key])
    top_value = components[top_component]
    metrics = analysis_input.metrics
    evidence_ids = [item.news_id for item in analysis_input.related_news]

    dominant_driver = (
        top_component if top_component in _COMPONENT_LABEL else "insufficient_data"
    )
    if analysis_input.hot_score < 0.2:
        dominant_driver = "insufficient_data"

    attention_reasons = [
        AnalysisReason(
            reason_type="metric",
            statement=(
                f"{_COMPONENT_LABEL.get(top_component, top_component)}分量最高"
                f"（{top_value:.4f}），热点总分 {analysis_input.hot_score:.4f}；"
                f"曝光 {metrics.impressions}，点击 {metrics.clicks}，"
                f"CTR {metrics.ctr:.2%}，有效消费 {metrics.effective_consumptions}，"
                f"互动 {metrics.interactions}。"
            ),
            metric_keys=[f"{top_component}_component", "hot_score"],
            evidence_news_ids=[],
            confidence=0.9,
            certainty="observed",
        )
    ]

    if evidence_ids:
        attention_reasons.append(
            AnalysisReason(
                reason_type="evidence",
                statement=(
                    f"存在 {len(evidence_ids)} 条关联报道可作为交叉验证，"
                    f"其中《{analysis_input.related_news[0].title}》与当前事件同源。"
                ),
                metric_keys=[],
                evidence_news_ids=[evidence_ids[0]],
                confidence=0.75,
                certainty="observed",
            )
        )

    attention_reasons.append(
        AnalysisReason(
            reason_type="hypothesis",
            statement=(
                "热度上升可能来自站外渠道扩散，但当前窗口数据无法证实，"
                "需结合更长周期与渠道维度复核。"
            ),
            metric_keys=["growth_component"],
            evidence_news_ids=[],
            confidence=0.4,
            certainty="inferred",
        )
    )

    related_contexts: list[RelatedNewsContext] = []
    if evidence_ids:
        related_contexts.append(
            RelatedNewsContext(
                statement=(
                    "关联报道与当前新闻指向同一事件，可作为热度持续性的旁证，"
                    "但尚未验证是否为同一信源转载。"
                ),
                evidence_news_ids=evidence_ids[:5],
                confidence=0.65,
            )
        )

    operation_suggestions = [
        OperationSuggestion(
            action=(
                "优先复核该热点的事实来源，并在"
                f"{_COMPONENT_LABEL.get(top_component, top_component)}维度持续观察下一窗口。"
            ),
            rationale=f"{_COMPONENT_LABEL.get(top_component, top_component)}是当前热度的主导因素。",
            priority="high" if analysis_input.hot_score >= 0.6 else "medium",
            metric_keys=[f"{top_component}_component", "hot_score"],
            evidence_news_ids=evidence_ids[:3],
        ),
        OperationSuggestion(
            action="补充站外渠道与搜索侧数据后再决定是否扩大推荐权重。",
            rationale="增长分量尚不足以支撑加大分发的判断。",
            priority="low",
            metric_keys=["growth_component"],
            evidence_news_ids=[],
        ),
    ]

    limitations = [
        "结论基于单个聚合窗口的确定性指标，未做跨窗口趋势校验。",
    ]
    if not evidence_ids:
        limitations.append(
            "本次没有可用的关联报道证据，事件背景未经过交叉验证。"
        )

    overall_confidence = min(
        1.0,
        round(0.45 + 0.30 * analysis_input.hot_score + 0.10 * len(evidence_ids), 4),
    )

    return HotNewsAnalysisReport(
        news_id=analysis_input.news_id,
        trend_assessment=(
            f"热点总分 {analysis_input.hot_score:.4f}，主导驱动为"
            f"{_COMPONENT_LABEL.get(top_component, top_component)}；"
            f"窗口内 CTR {metrics.ctr:.2%}，有效消费 {metrics.effective_consumptions}。"
        ),
        dominant_driver=dominant_driver,
        attention_reasons=attention_reasons,
        related_contexts=related_contexts,
        operation_suggestions=operation_suggestions,
        evidence_news_ids=evidence_ids[:10],
        applied_memory_ids=[],
        limitations=limitations,
        overall_confidence=overall_confidence,
    )


class OfflineHotNewsAnalysisRunner:
    """离线占位 Runner，接口与 ``HotNewsAnalysisAgentRunner`` 一致。"""

    runner_name = "offline-scripted-runner"

    async def run(
        self,
        analysis_input: HotNewsAnalysisInput,
    ) -> AgentResult[HotNewsAnalysisReport]:
        report = build_offline_report(analysis_input)
        return AgentResult(
            value=report,
            request_id="offline-runner",
            usage={"mode": "offline", "source": "scripted"},
            raw_content=report.model_dump_json(),
        )


def build_runner(*, use_real: bool = False) -> tuple[Any, str]:
    """构造 Runner。

    默认使用离线占位 Runner，只有显式传入 ``use_real=True`` 时才连接真实
    FastGPT，避免误把联调流量打到不可达的地址。
    """

    if not use_real:
        return OfflineHotNewsAnalysisRunner(), "离线占位（未启用真实 FastGPT）"

    base_url = (
        os.getenv("FASTGPT_BASE_URL", "").strip()
        or os.getenv("FASTGPT_DATASET_URL", "").strip()
    )
    api_key = os.getenv("FASTGPT_API_KEY", "").strip()
    app_id = os.getenv("FASTGPT_HOT_NEWS_APP_ID", "").strip()

    if not (base_url and api_key and app_id):
        raise SystemExit(
            "--real 需要 FASTGPT_BASE_URL、FASTGPT_API_KEY、FASTGPT_HOT_NEWS_APP_ID"
        )
    if not os.getenv("FASTGPT_BASE_URL", "").strip():
        print(
            "警告: 只找到 FASTGPT_DATASET_URL，而 app/config.py 期望 FASTGPT_BASE_URL。",
            flush=True,
        )

    import httpx

    from app.clients.fastgpt import FastGPTClient
    from app.services.agents.hot_news import HotNewsAnalysisAgentRunner

    client = FastGPTClient(
        SimpleNamespace(fastgpt_base_url=base_url, fastgpt_api_key=api_key),
        http_client=httpx.AsyncClient(trust_env=False),
    )
    return HotNewsAnalysisAgentRunner(client, app_id), f"真实 FastGPT（{base_url}）"


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_report(report: HotNewsAnalysisReport) -> list[str]:
    lines = [
        f"    趋势判断: {report.trend_assessment}",
        f"    主导驱动: {report.dominant_driver}    整体置信度: {report.overall_confidence}",
    ]

    if report.attention_reasons:
        lines.append("    关注原因:")
        for reason in report.attention_reasons:
            keys = ",".join(reason.metric_keys) or "-"
            evidence = ",".join(reason.evidence_news_ids) or "-"
            lines.append(
                f"      [{reason.reason_type}/{reason.certainty} "
                f"conf={reason.confidence}] {reason.statement}"
            )
            lines.append(f"        metric_keys={keys}  evidence={evidence}")

    if report.related_contexts:
        lines.append("    关联背景:")
        for context in report.related_contexts:
            lines.append(
                f"      [conf={context.confidence}] {context.statement}"
            )
            lines.append(f"        evidence={','.join(context.evidence_news_ids)}")

    if report.operation_suggestions:
        lines.append("    运营建议:")
        for suggestion in report.operation_suggestions:
            lines.append(
                f"      [{suggestion.priority}] {suggestion.action}"
            )
            lines.append(f"        理由: {suggestion.rationale}")

    if report.limitations:
        lines.append("    局限:")
        lines.extend(f"      - {item}" for item in report.limitations)

    return lines


def render_result(result: Any, runner_label: str, *, show_raw: bool) -> None:
    print("=" * 78)
    print(f"Runner: {runner_label}")
    print(
        f"窗口: {result.request.window_start.isoformat()} → "
        f"{result.request.window_end.isoformat()}"
    )
    print(f"抓取行为记录: {result.fetched_record_count} 条")
    print("=" * 78)

    for item in result.analyzed_news:
        analysis_input = item.analysis_input
        report = item.analysis.value
        print()
        print(f"#{item.rank}  {analysis_input.title}")
        print(f"  news_id: {item.news_id}    类型: {analysis_input.content_type}")
        print(
            f"  热点分: {analysis_input.hot_score:.4f}    "
            f"证据: {len(analysis_input.related_news)} 条关联报道"
        )
        print(
            "  模型输入指标: "
            f"曝光={analysis_input.metrics.impressions}, "
            f"点击={analysis_input.metrics.clicks}, "
            f"CTR={analysis_input.metrics.ctr:.2%}, "
            f"有效消费={analysis_input.metrics.effective_consumptions}, "
            f"互动={analysis_input.metrics.interactions}"
        )
        print(f"  策略版本: {analysis_input.analysis_policy_version}")
        print()
        print("  ---- 模型输出报告 ----")
        print("\n".join(render_report(report)))
        print(f"  request_id: {item.analysis.request_id}")
        print(f"  usage: {item.analysis.usage}")
        print(f"  交验通过: {item.validated_at.isoformat()}")

        if show_raw:
            print("  ---- 原始 JSON ----")
            print(
                json.dumps(
                    json.loads(report.model_dump_json()),
                    ensure_ascii=False,
                    indent=2,
                )
            )

    print()
    print("=" * 78)
    print(
        f"完成: 排行 {len(result.ranked_news)} 条，"
        f"分析 {len(result.analyzed_news)} 条，"
        f"指标快照 {len(result.metric_snapshots)} 个"
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
async def run(
    *,
    show_raw: bool = False,
    use_real: bool = False,
    min_overlap: int = 2,
) -> None:
    dependencies = build_hot_news_e2e_dependencies()
    scenario = load_scenario()
    candidate_news_ids = tuple(str(item["news_id"]) for item in scenario["news"])

    knowledge_search = OfflineScenarioKnowledgeSearchClient(
        dependencies.content_repository,
        candidate_news_ids,
        tenant_id=E2E_TENANT_ID,
        min_overlap=min_overlap,
    )
    runner, runner_label = build_runner(use_real=use_real)

    service = HotNewsOrchestrationService(
        behavior_data_source=dependencies.behavior_data_source,
        baseline_provider=dependencies.baseline_provider,
        enrichment_service=HotNewsEnrichmentService(
            content_repository=dependencies.content_repository,
            knowledge_search=knowledge_search,
        ),
        analysis_service=HotNewsAnalysisService(
            input_builder=HotNewsAnalysisInputBuilder(),
            runner=runner,
            validator=HotNewsAnalysisValidator(),
        ),
        policy=dependencies.policy,
    )

    request = HotNewsRunRequest(
        tenant_id=dependencies.tenant_id,
        window_start=dependencies.window_start,
        window_end=dependencies.window_end,
        production_bundle_version=dependencies.production_bundle_version,
        workflow_version=WORKFLOW_VERSION,
    )
    request.validate()

    result = await service.run(request)
    render_result(result, runner_label, show_raw=show_raw)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="热点分析全链路离线联调")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="额外打印模型报告的原始 JSON",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="连接真实 FastGPT（需要三个环境变量已配置且服务可达）",
    )
    parser.add_argument(
        "--min-overlap",
        type=int,
        default=2,
        help="离线检索桩的召回阈值；调成 1 可以看到「有证据」分支（默认 2）",
    )
    args = parser.parse_args()
    asyncio.run(
        run(show_raw=args.raw, use_real=args.real, min_overlap=args.min_overlap)
    )


if __name__ == "__main__":
    main()
