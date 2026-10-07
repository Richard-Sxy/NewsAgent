"""
本地规则模拟推理，用于测试链路；不代表真实模型质量。
"""

from __future__ import annotations

import json
from hashlib import sha256

from app.model_runtime.core import InferenceRequest, RawInferenceResult
from app.schemas.hot_news import (
    AnalysisReason,
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
    OperationSuggestion,
)
from app.schemas.error_attribution import ErrorAttributionInput, ErrorAttributionReport
from app.schemas.research import ResearchPackage
from app.schemas.review import ReviewInput, ReviewReport
from app.schemas.writing import (
    ArticleAssemblyInput,
    ArticleDraft,
    ArticleOutline,
    ArticleSection,
    SectionWritingInput,
)
from app.knowledge.qa import QAGenerationOutput


class LocalHotNewsInference:
    async def complete(self, request: InferenceRequest) -> RawInferenceResult:
        if len(request.messages) != 2 or request.messages[1].role != "user":
            raise ValueError("local inference expects system and user messages")
        payload = json.loads(request.messages[1].content)
        if request.scene != "hot_news_analysis":
            content = self._complete_other_scene(request.scene, payload)
            digest = sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:24]
            return RawInferenceResult(
                content=content,
                request_id=f"native-local-{digest}",
                model_version=request.model_route,
                usage={"local_stub": True},
            )
        analysis_input = HotNewsAnalysisInput.model_validate(payload)
        components = analysis_input.score_components.model_dump()
        top_component = max(components, key=components.__getitem__)
        report = HotNewsAnalysisReport(
            news_id=analysis_input.news_id,
            trend_assessment=(
                f"本地模拟：热度 {analysis_input.hot_score:.4f}；"
                f"点击率 {analysis_input.metrics.ctr:.4f}。"
            ),
            dominant_driver=(
                "insufficient_data"
                if analysis_input.hot_score < 0.2
                else top_component
            ),
            attention_reasons=[
                AnalysisReason(
                    reason_type="metric",
                    statement=f"{top_component} 为输入中最大的热度分量。",
                    metric_keys=[f"{top_component}_component", "hot_score"],
                    evidence_news_ids=[],
                    confidence=1.0,
                    certainty="observed",
                )
            ],
            related_contexts=[],
            operation_suggestions=[
                OperationSuggestion(
                    action="将这条模拟新闻留在人工观察列表。",
                    rationale="本地端到端验收数据，不触发自动发布。",
                    priority="low",
                    metric_keys=["hot_score"],
                    evidence_news_ids=[],
                )
            ],
            evidence_news_ids=[],
            applied_memory_ids=[],
            limitations=["本结果由本地确定性推理替身产生，不代表真实模型判断。"],
            overall_confidence=1.0,
        )
        digest = sha256(request.idempotency_key.encode("utf-8")).hexdigest()[:24]
        return RawInferenceResult(
            content=report.model_dump_json(),
            request_id=f"native-local-{digest}",
            model_version=request.model_route,
            usage={"local_stub": True},
        )

    @staticmethod
    def _complete_other_scene(scene: str, payload: dict) -> str:
        if scene == "conversation_memory":
            from app.conversation.local import local_conversation_summary

            return local_conversation_summary(payload)
        if scene == "conversation":
            from app.conversation.local import local_conversation_plan

            return local_conversation_plan(payload)
        if scene == "query_understanding":
            from app.conversation.query_understanding import local_query_understanding

            return local_query_understanding(payload)
        if scene == "text2sql_assistant":
            from app.sql_assistant.planner import local_query_intent

            return local_query_intent(payload).model_dump_json()
        if scene == "research":
            topic = str(payload["topic"])
            result = ResearchPackage(
                job_id=str(payload["job_id"]),
                topic=topic,
                facts=[
                    {
                        "fact_id": f"F{index:03d}",
                        "claim": f"本地模拟资料 {index}：{topic}",
                        "evidence": "隔离测试夹具；不代表真实新闻事实。",
                        "source_url": f"https://example.org/newsagent-local/{index % 2 + 1}",
                        "source_title": "NewsAgent 本地模拟资料",
                        "confidence": 0.1,
                    }
                    for index in range(1, 4)
                ],
                suggested_angles=[
                    {
                        "angle_id": "A001",
                        "title": f"本地模拟选题：{topic}",
                        "rationale": "仅验证写作编排和人工审核流程。",
                        "supporting_fact_ids": ["F001", "F002"],
                    }
                ],
            )
            return result.model_dump_json()
        if scene == "outline":
            result = ArticleOutline(
                job_id=str(payload["job_id"]),
                title=f"本地模拟：{payload['research_package']['topic']}",
                angle="隔离环境流程验证",
                target_word_count=300,
                sections=[
                    {
                        "section_id": "S01",
                        "order": 1,
                        "title": "模拟资料与人工核查",
                        "purpose": "展示引用与人工审核流程",
                        "target_word_count": 300,
                        "required_fact_ids": [],
                    }
                ],
            )
            return result.model_dump_json()
        if scene in {"section", "revise"}:
            writing_input = SectionWritingInput.model_validate(payload)
            section = next(
                item for item in writing_input.outline.sections
                if item.section_id == writing_input.section_id
            )
            return ArticleSection(
                job_id=writing_input.job_id,
                section_id=writing_input.section_id,
                title=section.title,
                content="本地模拟稿件，仅用于验证 Python 写作链路；内容必须人工核查。",
                summary="本地模拟章节，等待人工审核。",
            ).model_dump_json()
        if scene == "assemble":
            assembly = ArticleAssemblyInput.model_validate(payload)
            return ArticleDraft(
                job_id=assembly.job_id,
                title=assembly.outline.title,
                content="\n\n".join(item.content for item in assembly.sections),
                section_ids=[item.section_id for item in assembly.sections],
            ).model_dump_json()
        if scene == "review":
            review = ReviewInput.model_validate(payload)
            return ReviewReport(
                job_id=review.job_id,
                review_round=review.review_round,
                decision="human_review",
                issues=[
                    {
                        "issue_id": "I001",
                        "issue_type": "unsupported_claim",
                        "severity": "high",
                        "reason": "本地模拟资料不是已核实新闻证据，必须由人工复核后处理。",
                        "suggested_action": "human_review",
                    }
                ],
                section_decisions=[],
                scores={
                    "factuality": 0, "citation": 0, "time_consistency": 0,
                    "structure": 0, "style": 0, "risk": 0,
                },
                summary="本地模拟资料不能自动通过终审，需人工核查。",
            ).model_dump_json()
        if scene == "hot_news_error_attribution":
            source = ErrorAttributionInput.model_validate(payload)
            return ErrorAttributionReport(
                dataset_id=source.dataset_id,
                dataset_sha256=source.dataset_sha256,
                attributions=tuple(
                    {
                        "case_id": case.case_id,
                        "category": "input_data_gap",
                        "rationale": "本地推理替身不能判定真实归因。",
                        "affected_asset": "none",
                        "remediation_suggestion": "提交人工分析。",
                        "confidence": 0.0,
                        "limitations": ("本地模拟结果，不代表实际归因。",),
                    }
                    for case in source.cases
                ),
            ).model_dump_json()
        if scene == "qa_generation":
            source_text = str(payload["text"])
            return QAGenerationOutput(
                pairs=(
                    {
                        "question": "这篇本地模拟报道的主题是什么？",
                        "answer": str(payload["title"]),
                        "evidence_quote": source_text[: min(len(source_text), 80)],
                    },
                )
            ).model_dump_json()
        raise ValueError(f"unsupported local scene: {scene}")
