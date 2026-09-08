"""热点分析大模型模块的输入构造、业务校验与统一调用入口。"""
from dataclasses import dataclass
from datetime import datetime, timezone

from app.analytics.hot_news_enrichment import EnrichedHotNews
from app.clients.fastgpt import AgentResult
from app.domain.errors import AgentOutputValidationError
from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    HotNewsMetrics,
    HotScoreComponents,
    PromptMemoryContext,
    RelatedNewsEvidence,
)
from app.services.agents.hot_news import HotNewsAnalysisAgentRunner


class HotNewsAnalysisInputBuilder:
    """把确定性排行和可信证据裁剪为大模型输入。"""

    def __init__(self, *, policy_version: str = "hot-news-analysis-v1") -> None:
        if not policy_version.strip():
            raise ValueError("policy_version cannot be empty")
        self.policy_version = policy_version

    def build(
        self,
        item: EnrichedHotNews,
        *,
        memory_context: PromptMemoryContext | None = None,
    ) -> HotNewsAnalysisInput:
        """构造不包含原始用户明细的热点分析上下文。

        约束：
        1. 没有 ``item.content`` 时拒绝分析。
        2. 从 ``item.ranking.current`` 复制窗口和权威指标。
        3. 从 ``item.ranking.hot_score`` 复制总分及四个分量。
        4. 从 ``item.content`` 复制标题、摘要和内容类型。
        5. 只传前 5 条 ``item.related_news``；正文片段最长 2000 字符。
        6. 证据缺少 ``news_id`` 时跳过，不能生成临时 ID。
        7. 不传 ``user_id``、原始行为记录、密钥或完整网页。
        """

        if item.content is None:
            raise ValueError("hot news content is required for model analysis")

        current = item.ranking.current
        score = item.ranking.hot_score
        content = item.content
        content.validate()

        if content.news_id != current.news_id:
            raise ValueError(
                "content news_id does not match ranking news_id: "
                f"{content.news_id!r} != {current.news_id!r}"
            )

        related_news = [
            RelatedNewsEvidence(
                news_id=related.news.news_id,
                title=related.news.title,
                excerpt=(related.news.text or "")[:2000],
                source_url=related.news.source_url,
                published_at=related.news.publish_time,
                final_score=related.final_score,
                rerank_reasons=list(related.reasons),
            )
            for related in item.related_news[:5]
            if related.news.news_id
        ]

        return HotNewsAnalysisInput(
            news_id=current.news_id,
            title=content.title,
            summary=(content.summary or "")[:2000],
            content_excerpt=(content.body or "")[:2000],
            content_type=current.content_type,
            window_start=current.window_start,
            window_end=current.window_end,
            hot_score=float(score.score),
            metrics=HotNewsMetrics(
                impressions=current.impressions,
                clicks=current.clicks,
                ctr=float(current.ctr),
                unique_users=current.unique_users,
                effective_consumptions=current.effective_consumptions,
                interactions=current.interactions,
            ),
            score_components=HotScoreComponents(
                click=float(score.click_component),
                consumption=float(score.consumption_component),
                interaction=float(score.interaction_component),
                growth=float(score.growth_component),
            ),
            related_news=related_news,
            analysis_policy_version=self.policy_version,
            memory_context=memory_context,
        )


class HotNewsAnalysisValidator:
    """执行 Pydantic 无法表达的输入输出交叉校验。"""

    def validate(
        self,
        *,
        analysis_input: HotNewsAnalysisInput,
        result: AgentResult[HotNewsAnalysisReport],
    ) -> None:
        """拒绝身份、指标引用或证据引用不可信的模型结果。

        校验规则：
        1. 检查输出 ``news_id`` 与可信输入完全一致。
        2. 汇总输入允许的 related ``news_id`` 集合。
        3. 检查报告顶层及所有嵌套对象的 evidence ID 都在允许集合内。
        4. ``reason_type=metric`` 时必须至少引用一个 ``metric_key``。
        5. ``reason_type=evidence`` 时必须至少引用一个 evidence ID。
        6. ``reason_type=hypothesis`` 必须使用 ``certainty=inferred``。
        7. 输入无关联证据时，输出不得包含 related context，并且必须有 limitation。
        8. ``applied_memory_ids`` 只能引用本次实际注入模型的 Memory。
        9. 失败时抛 ``AgentOutputValidationError``，保留 raw_content/request_id。
        """

        report = result.value

        try:
            # 必须与可信输入完全一致
            self._validate_news_id(
                analysis_input=analysis_input,
                report=report,
            )
            # 模型只能声明使用本次实际注入的 Memory。
            self._validate_applied_memory_ids(
                analysis_input=analysis_input,
                report=report,
            )
            # 输入中允许引用的相关新闻 news_id
            allowed_evidence_ids = self._collect_allowed_evidence_ids(
                analysis_input
            )
            # 输入中允许引用的指标 key
            allowed_metric_keys = set(analysis_input.metrics.model_dump())
            allowed_metric_keys.add("hot_score")
            allowed_metric_keys.update(
                f"{key}_component"
                for key in analysis_input.score_components.model_dump()
            )
            # 没有相关新闻证据时限制输出
            self._validate_no_evidence_case(
                report=report,
                allowed_evidence_ids=allowed_evidence_ids,
            )
            # 检查所有 evidence ID
            self._validate_evidence_ids(
                report=report,
                allowed_evidence_ids=allowed_evidence_ids,
            )
            # 检查 attention reason
            self._validate_attention_reasons(
                report=report,
                allowed_metric_keys=allowed_metric_keys,
            )
        except ValueError as exc:
            raise AgentOutputValidationError(
                str(exc),
                raw_content=result.raw_content,
                request_id=result.request_id,
            ) from exc

    def _validate_news_id(
        self,
        *,
        analysis_input: HotNewsAnalysisInput,
        report: HotNewsAnalysisReport,
    ) -> None:
        """模型不能修改当前分析新闻的身份。"""

        if report.news_id != analysis_input.news_id:
            raise ValueError(
                "output news_id does not match trusted input: "
                f"expected={analysis_input.news_id!r}, "
                f"actual={report.news_id!r}"
            )

    def _collect_allowed_evidence_ids(
        self,
        analysis_input: HotNewsAnalysisInput,
    ) -> set[str]:
        """收集模型允许引用的相关新闻 ID。"""

        return {
            item.news_id
            for item in analysis_input.related_news
            if item.news_id
        }

    def _validate_applied_memory_ids(
        self,
        *,
        analysis_input: HotNewsAnalysisInput,
        report: HotNewsAnalysisReport,
    ) -> None:
        """拒绝重复或不在本次 Prompt 白名单中的 Memory 引用。"""

        applied_ids = report.applied_memory_ids
        if len(applied_ids) != len(set(applied_ids)):
            raise ValueError(
                "applied_memory_ids contains duplicate memory ids"
            )

        memory_context = analysis_input.memory_context
        allowed_ids = (
            {item.memory_id for item in memory_context.items}
            if memory_context is not None
            else set()
        )
        unknown_ids = set(applied_ids) - allowed_ids
        if unknown_ids:
            raise ValueError(
                "report references unknown or omitted memory ids: "
                f"{sorted(str(memory_id) for memory_id in unknown_ids)}"
            )

    def _validate_evidence_ids(
        self,
        *,
        report: HotNewsAnalysisReport,
        allowed_evidence_ids: set[str],
    ) -> None:
        """所有模型输出的 evidence ID 都必须来自输入证据。"""

        referenced_ids = self._collect_report_evidence_ids(
            report
        )

        invalid_ids = referenced_ids - allowed_evidence_ids

        if invalid_ids:
            raise ValueError(
                "report references unknown evidence news_id: "
                f"{sorted(invalid_ids)}"
            )

    def _collect_report_evidence_ids(
        self,
        report: HotNewsAnalysisReport,
    ) -> set[str]:
        """递归收集整个报告里的 evidence_news_ids。

        使用 Pydantic model_dump()，
        因此不需要手动知道每一层嵌套结构。
        """

        data = report.model_dump()

        evidence_ids: set[str] = set()

        def walk(value: object) -> None:
            if isinstance(value, dict):
                for key, child in value.items():

                    # 根据你的模型字段命名进行匹配
                    if key in {
                        "evidence_news_ids",
                        "evidence_ids",
                    }:
                        if isinstance(child, (list, tuple)):
                            for evidence_id in child:
                                if (
                                    isinstance(evidence_id, str)
                                    and evidence_id
                                ):
                                    evidence_ids.add(evidence_id)

                    else:
                        walk(child)

            elif isinstance(value, (list, tuple)):
                for child in value:
                    walk(child)

        walk(data)

        return evidence_ids

    def _validate_attention_reasons(
        self,
        *,
        report: HotNewsAnalysisReport,
        allowed_metric_keys: set[str],
    ) -> None:
        """验证每一条热点原因的证据类型。"""

        for reason in report.attention_reasons:

            # 4. metric 类型必须引用指标
            if reason.reason_type == "metric":

                if not reason.metric_keys:
                    raise ValueError(
                        "metric reason must reference "
                        "at least one metric_key"
                    )

                unknown_metric_keys = (
                    set(reason.metric_keys)
                    - allowed_metric_keys
                )

                if unknown_metric_keys:
                    raise ValueError(
                        "metric reason references unknown metrics: "
                        f"{sorted(unknown_metric_keys)}"
                    )

            # 5. evidence 类型必须引用相关新闻
            elif reason.reason_type == "evidence":

                if not reason.evidence_news_ids:
                    raise ValueError(
                        "evidence reason must reference "
                        "at least one evidence news_id"
                    )

            # 6. hypothesis 必须明确声明 inferred
            elif reason.reason_type == "hypothesis":

                if reason.certainty != "inferred":
                    raise ValueError(
                        "hypothesis reason must use "
                        "certainty='inferred'"
                    )

    def _validate_no_evidence_case(
        self,
        *,
        report: HotNewsAnalysisReport,
        allowed_evidence_ids: set[str],
    ) -> None:
        """输入完全没有相关新闻时，禁止模型伪造相关新闻背景。"""

        if allowed_evidence_ids:
            return

        # 没证据却生成相关新闻背景
        if report.related_contexts:
            raise ValueError(
                "related_contexts must be empty "
                "when no related evidence was provided"
            )

        # 必须明确告诉使用方证据不足
        if not report.limitations:
            raise ValueError(
                "report must contain at least one limitation "
                "when no related evidence was provided"
            )


@dataclass(frozen=True, slots=True)
class HotNewsAnalysisExecution:
    """一次交验通过分析，包括当时的业务输入"""

    analysis_input: HotNewsAnalysisInput
    analysis: AgentResult[HotNewsAnalysisReport]
    captured_at: datetime
    validated_at: datetime


class HotNewsAnalysisService:
    """未来供 API 或 Temporal Activity 调用的唯一热点 Agent 入口。"""

    def __init__(
        self,
        *,
        input_builder: HotNewsAnalysisInputBuilder,
        runner: HotNewsAnalysisAgentRunner,
        validator: HotNewsAnalysisValidator,
    ) -> None:
        self.input_builder = input_builder
        self.runner = runner
        self.validator = validator

    async def analyze(
        self,
        item: EnrichedHotNews,
        *,
        memory_context: PromptMemoryContext | None = None,
    ) -> AgentResult[HotNewsAnalysisReport]:
        """构造可信输入、调用模型并执行输出交叉校验。"""

        execution = await self.analyze_with_snapshot(
            item,
            memory_context=memory_context,
        )
        return execution.analysis

    async def analyze_with_snapshot(
        self,
        item: EnrichedHotNews,
        *,
        memory_context: PromptMemoryContext | None = None,
    ) -> HotNewsAnalysisExecution:
        analysis_input = self.input_builder.build(
            item,
            memory_context=memory_context,
        )
        input_snapshot = analysis_input.model_copy(deep=True)
        capture_at = datetime.now(timezone.utc)

        # 调用LLM获得结构化报告
        result = await self.runner.run(analysis_input)

        self.validator.validate(
            analysis_input=input_snapshot,
            result=result,
        )

        # 返回执行结果
        return HotNewsAnalysisExecution(
            analysis_input=input_snapshot,
            analysis=result,
            captured_at=capture_at,
            validated_at=datetime.now(timezone.utc),
        )
