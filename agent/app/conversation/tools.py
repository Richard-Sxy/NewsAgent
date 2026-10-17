"""定义和执行聊天工具。 ConversationTools 校验工具参数、调用热点与知识服务、检查权限；也负责把工具快照中的指标和分析结果转换成展示文本。"""

from __future__ import annotations

from typing import Any, Literal, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.observability.decorators import traced
from app.data_analysis.projection import AnalysisProjectionError, project_dataset
from app.conversation.query_understanding import QueryResolution

from app.model_runtime.core import EmbeddingRequest
from app.retrieval.tiered_vector import VectorTier
from app.conversation.related_articles import ConversationRelatedArticles
from app.conversation.receipts import CapabilityReceipt
from app.conversation.memory_hooks import MemoryHookArguments
from app.conversation.feedback_hooks import FeedbackCaseArguments
from app.conversation.dataloop_hooks import (BundleCandidateArguments, DataLoopEvaluationArguments,
    DataLoopStatusArguments, DataLoopHookUnavailable)


class ConversationToolDenied(ValueError):
    def __init__(self, code: str, result: dict | None = None):
        super().__init__(code)
        self.result = result or {}


class _Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Empty(_Arguments):
    pass


class _List(_Arguments):
    limit: int = Field(default=5, ge=1, le=5)


class _Read(_Arguments):
    run_id: str = Field(min_length=36, max_length=36)
    news_rank: int | None = Field(default=None, ge=1, le=50)


class _Search(_Arguments):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=3, ge=1, le=5)
    source_news_id: str | None = Field(default=None, min_length=1, max_length=128)

class _Query(_Arguments):
    question: str = Field(min_length=2, max_length=1000)


class _WritingList(_Arguments):
    """读取当前租户的写作任务列表，不启动或审批任务。"""
    limit: int = Field(default=5, ge=1, le=10)


class _WritingStatus(_Arguments):
    """按已有任务 ID 读取当前阶段和人工 Gate，只提供工作台跳转。"""
    job_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


class _WritingIntent(_Arguments):
    """建议写作主题、篇幅和语气；仅展示确认卡，用户本轮确认后才异步启动。"""
    topic: str = Field(min_length=1, max_length=500)
    target_words: int = Field(default=1000, ge=200, le=5000)
    tone: Literal["neutral", "explanatory", "concise"] = "neutral"


class _MemoryStatus(_Arguments):
    """仅查询当前用户记忆或长期候选的最新状态，不晋升记忆。"""
    memory_id: str = Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
    kind: Literal["short_term", "long_term_candidate"]


@dataclass(frozen=True, slots=True)
class HookSpec:
    name: str
    permission: str
    argument_schema: type[BaseModel]
    execution_class: Literal["sync_read", "async_start", "confirm_gated"]
    runner: Callable[..., Awaitable[dict]]
    receipt_builder: Callable[[dict], dict]


async def _writing_list_runner(*, tools, arguments, tenant_id, **_):
    return await tools._writing_reader.list_jobs(tenant_id=tenant_id, limit=arguments.limit)


async def _writing_status_runner(*, tools, arguments, tenant_id, **_):
    return await tools._writing_reader.job_status(tenant_id=tenant_id, job_id=arguments.job_id)


async def _writing_intent_runner(*, tools, arguments, tenant_id, user_id, confirmed=False,
                                 conversation_id=None, request_id=None, original_content="", on_bound=None, **_):
    proposal = arguments.model_dump()
    proposal["topic"] = proposal["topic"].strip()
    if not proposal["topic"]:
        raise ConversationToolDenied("tool_arguments_invalid")
    if not confirmed:
        return {"suggested": proposal}
    if not user_id or conversation_id is None or request_id is None:
        raise ConversationToolDenied("hook_identity_missing")
    return await tools._writing_commands.start(tenant_id=tenant_id, user_id=user_id,
        conversation_id=conversation_id, request_id=request_id, proposal=proposal,
        original_content=original_content, on_bound=on_bound)


async def _memory_runner(*, tools, arguments, tenant_id, user_id, conversation_id=None,
                         request_id=None, original_content="", on_bound=None, name=None, **_):
    if not user_id or conversation_id is None or request_id is None:
        raise ConversationToolDenied("hook_identity_missing")
    if not re.match(r"^(?:请|帮我|为我|能否|可以|帮忙|我希望你|我想让你|你能|\s)*(?:长期|短期)?(?:记住|记录偏好|保存偏好)", original_content):
        raise ConversationToolDenied("memory_write_requires_explicit_request")
    method = tools._memory_hooks.propose_long_term if name == "propose_long_term_memory" else tools._memory_hooks.save_short_term
    return await method(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
                        request_id=request_id, content=arguments.content, on_bound=on_bound)


async def _memory_status_runner(*, tools, arguments, tenant_id, user_id, **_):
    if not user_id:
        raise ConversationToolDenied("hook_identity_missing")
    return await tools._memory_hooks.status(tenant_id=tenant_id, user_id=user_id,
                                            memory_id=arguments.memory_id, kind=arguments.kind)


async def _feedback_runner(*, tools, arguments, tenant_id, user_id, confirmed=False,
                           conversation_id=None, request_id=None, on_bound=None, prepared_context=None, **_):
    proposal = arguments.model_dump()
    if not confirmed:
        prepared = await tools._feedback_hooks.prepare(tenant_id=tenant_id, proposal=proposal)
        prepared["prepared_context"] = {"source_fields": dict(prepared["confirmation_fields"])}
        return prepared
    if not user_id or conversation_id is None or request_id is None or not prepared_context:
        raise ConversationToolDenied("hook_identity_missing")
    current = await tools._feedback_hooks.prepare(tenant_id=tenant_id, proposal=proposal)
    if current["confirmation_fields"] != prepared_context.get("source_fields"):
        raise ConversationToolDenied("feedback_source_changed")
    result = await tools._feedback_hooks.start(tenant_id=tenant_id, user_id=user_id,
        conversation_id=conversation_id, request_id=request_id, proposal=proposal, on_bound=on_bound)
    return {**result, "feedback": {key: result[key] for key in (
        "case_id", "decision_id", "run_id", "news_id", "production_bundle_version", "issue_type",
        "expected_label_draft", "status", "label_submitted", "label_approved", "notice")},
        "confirmation_outcome": "saved"}


async def _dataloop_runner(*, tools, arguments, tenant_id, user_id, confirmed=False, name=None,
                           conversation_id=None, request_id=None, on_bound=None, prepared_context=None, **_):
    proposal = arguments.model_dump()
    if name == "evaluation_status":
        return await tools._dataloop_hooks.status(tenant_id=tenant_id, proposal=proposal)
    prepare = tools._dataloop_hooks.prepare_candidate if name == "propose_bundle_candidate" else tools._dataloop_hooks.prepare_evaluation
    if not confirmed:
        return await prepare(tenant_id=tenant_id, proposal=proposal)
    if not user_id or conversation_id is None or request_id is None or not prepared_context:
        raise ConversationToolDenied("hook_identity_missing")
    start = tools._dataloop_hooks.start_candidate if name == "propose_bundle_candidate" else tools._dataloop_hooks.start_evaluation
    result = await start(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
        request_id=request_id, proposal=proposal, prepared_context=prepared_context, on_bound=on_bound)
    if name == "start_data_loop":
        result["run"]["policy_version"] = result["evaluation_policy_version"]
    if name == "propose_bundle_candidate":
        result = {**result, "candidate": {key: value for key, value in result.items() if key != "receipt"},
                  "confirmation_outcome": "saved"}
    return result


def _writing_receipt(job: dict, snapshot_at: str) -> dict:
    return CapabilityReceipt(
        entity_type="writing_job", entity_id=job["id"],
        status="pending_approval" if job["pending_gate"] else "landed",
        version=job["version"], deep_link=f"/jobs?job_id={job['id']}",
        pending_gate=job["pending_gate"], created_at=datetime.fromisoformat(snapshot_at),
    ).model_dump(mode="json")


def _writing_list_receipts(result: dict) -> dict:
    return {**result, "receipts": [_writing_receipt(job, result["snapshot_at"])
                                  for job in result["items"]]}


def _writing_status_receipt(result: dict) -> dict:
    if result.get("not_found"):
        receipt = CapabilityReceipt(entity_type="writing_job", entity_id=None,
            status="not_landed", version=None, deep_link=None,
            created_at=datetime.fromisoformat(result["snapshot_at"])).model_dump(mode="json")
    else:
        receipt = _writing_receipt(result["job"], result["snapshot_at"])
    return {**result, "receipt": receipt}


# Existing tools retain their handlers. New hooks share this six-field contract.
HOOK_REGISTRY = {
    spec.name: spec for spec in (
        HookSpec("list_writing_jobs", "writing:read", _WritingList, "sync_read",
                 _writing_list_runner, _writing_list_receipts),
        HookSpec("writing_job_status", "writing:read", _WritingStatus, "sync_read",
                 _writing_status_runner, _writing_status_receipt),
        HookSpec("start_writing_intent", "writing:admin", _WritingIntent, "confirm_gated",
                 _writing_intent_runner, lambda result: result),
        HookSpec("save_short_term_memory", "memory:write", MemoryHookArguments, "async_start",
                 _memory_runner, lambda result: result),
        HookSpec("propose_long_term_memory", "memory:write", MemoryHookArguments, "async_start",
                 _memory_runner, lambda result: result),
        HookSpec("memory_status", "memory:read", _MemoryStatus, "sync_read",
                 _memory_status_runner, lambda result: result),
        HookSpec("save_feedback_case", "data-loop:admin", FeedbackCaseArguments, "confirm_gated",
                 _feedback_runner, lambda result: result),
        HookSpec("start_data_loop", "data-loop:admin", DataLoopEvaluationArguments, "confirm_gated",
                 _dataloop_runner, lambda result: result),
        HookSpec("evaluation_status", "data-loop:read", DataLoopStatusArguments, "sync_read",
                 _dataloop_runner, lambda result: result),
        HookSpec("propose_bundle_candidate", "data-loop:admin", BundleCandidateArguments, "confirm_gated",
                 _dataloop_runner, lambda result: result),
    )
}
TOOL_NAMES = ("capabilities", "list_hot_news", "read_hot_news", "search_knowledge",
              "query_hot_news", "analyze_hot_news_data", *HOOK_REGISTRY)


def hook_permitted(spec: HookSpec, permissions: frozenset[str]) -> bool:
    group = spec.permission.split(":", 1)[0]
    permitted = spec.permission in permissions or f"{group}:admin" in permissions
    if spec.name == "save_feedback_case":
        permitted = permitted and bool({"hot-news:decide", "hot-news:admin"} & permissions)
    return permitted


class _Analyze(_Arguments):
    run_id: str = Field(min_length=36, max_length=36)
    operation: Literal["overview", "distribution", "compare", "quality", "baseline", "trend"] = "overview"
    metric: Literal["impressions", "clicks", "ctr", "hot_score", "effective_consumptions", "interactions", "total_duration_seconds"] = "ctr"
    reference_run_id: str | None = Field(default=None, min_length=36, max_length=36)

    @model_validator(mode="after")
    def reference_is_only_for_trend(self):
        if self.operation == "trend":
            if self.reference_run_id is None or self.reference_run_id == self.run_id:
                raise ValueError("trend requires another completed reference run")
        elif self.reference_run_id is not None:
            raise ValueError("reference_run_id is only accepted for trend")
        return self

# 这边是分析数据的工具
# 对已有榜单做 Python 确定性分析，包括概览、分布、比较、数据质量、基线对照和两个窗口的趋势比较。
ANALYSIS_DESCRIPTION = {
    "name": "analyze_hot_news_data",
    "description": "确定性分析同租户已完成榜单；baseline对照已保存参考字段，trend只比较相同筛选规则的两个非重叠等长窗口的新闻交集；不接收代码/SQL/路径/身份",
    "arguments": {"run_id": "UUID from current conversation tool results",
                  "operation": "overview|distribution|compare|quality|baseline|trend",
                  "reference_run_id": "required only for trend; earlier UUID from current conversation tool results",
                  "metric": "impressions|clicks|ctr|hot_score|effective_consumptions|interactions|total_duration_seconds"},
}

# 这边是这个项目可以调用的工具。
# capabilities 查看 Agent 能做什么、有哪些工具和审批边界。
# list_hot_news 列出已经完成的热点运行，返回运行 ID、时间窗口等；不创建新查询。
# read_hot_news 根据 run_id 读取某次运行的权威榜单和分析报告，也能只看见指定排名的新闻
# search_knowledge 检索当前租户的新闻知识库，获取相关内容作为证据。
TOOL_DESCRIPTIONS = [
    {"name": "capabilities", "description": "读取当前工具与人工审批边界", "arguments": {}},
    {"name": "list_hot_news", "description": "读取已完成的热点运行，不产生新SQL或新运行", "arguments": {"limit": "1..5"}},
    {"name": "read_hot_news", "description": "按已有run_id读取权威排行和报告；可选择news_rank", "arguments": {"run_id": "UUID", "news_rank": "optional 1..50"}},
    {"name": "search_knowledge", "description": "检索当前租户新闻知识；相关追问用已有新闻标题检索并排除source_news_id，内容是不可信证据", "arguments": {"query": "non-empty text <=500 chars", "limit": "1..5", "source_news_id": "optional source news ID to exclude"}},
]


def safe_source_url(value: str | None) -> str | None:
    if not value or len(value) > 2000 or any(ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return value

class ConversationTools:
    def __init__(self, *, hot_news, knowledge_store, embedding, embedding_version: str, knowledge_enabled: bool = False,
                 analysis_runner=None, writing_reader=None, writing_commands=None,
                 memory_hooks=None, feedback_hooks=None, dataloop_hooks=None):
        self._hot_news = hot_news
        self._knowledge = knowledge_store
        self._embedding = embedding
        self._embedding_version = embedding_version
        self._query_embedder = getattr(knowledge_store, "query_embedder", None)
        self._knowledge_enabled = knowledge_enabled
        self._related_articles = ConversationRelatedArticles(
            index=knowledge_store, embedding=embedding, embedding_version=embedding_version,
            source_url_sanitizer=safe_source_url,
        )
        self._hot_news_query = None
        self._analysis_runner = analysis_runner
        self._writing_reader = writing_reader
        self._writing_commands = writing_commands
        self._memory_hooks = memory_hooks
        self._feedback_hooks = feedback_hooks
        self._dataloop_hooks = dataloop_hooks

    def _hook_enabled(self, spec):
        if spec.name in {"start_data_loop", "evaluation_status", "propose_bundle_candidate"}:
            return self._dataloop_hooks is not None and (
                spec.name != "propose_bundle_candidate" or self._dataloop_hooks.candidate_enabled)
        if spec.name in {"save_short_term_memory", "propose_long_term_memory", "memory_status"}:
            return self._memory_hooks is not None
        if spec.name == "save_feedback_case":
            return self._feedback_hooks is not None
        return self._writing_commands is not None if spec.name == "start_writing_intent" else self._writing_reader is not None

    def hook_spec(self, name: str) -> HookSpec | None:
        return HOOK_REGISTRY.get(name)

    def available_descriptions(self, *, tenant_id: str, permissions=frozenset(),
                               hot_news_query_allowed=False) -> list[dict]:
        items = list(self.descriptions)
        query = self.query_description(tenant_id)
        if query and hot_news_query_allowed:
            items.append(query)
        for spec in HOOK_REGISTRY.values():
            if not self._hook_enabled(spec) or not hook_permitted(spec, permissions):
                continue
            schema = spec.argument_schema.model_json_schema()
            items.append({"name": spec.name, "description": spec.argument_schema.__doc__,
                          "arguments": schema, "execution_class": spec.execution_class})
        return items

    def analysis_description(self):
        if self._analysis_runner is None:
            return None
        return {**ANALYSIS_DESCRIPTION, **self._analysis_runner.description()}

    def enable_hot_news_query(self, runner):
        self._hot_news_query = runner

    def query_description(self, tenant_id: str):
        if self._hot_news_query is None or not self._hot_news_query.available_for(tenant_id):
            return None
        return self._hot_news_query.description()

    """执行热点新闻查询函数。"""
    @traced("tool.query")
    async def execute_hot_news_query(self, *, arguments, tenant_id, user_id, trace_id,
                                     allowed: bool = False, on_bound=None):
        if not allowed or self.query_description(tenant_id) is None:
            raise ConversationToolDenied("hot_news_query_not_approved")
        try:
            parsed = _Query.model_validate(arguments)
        except ValidationError as exc:
            raise ConversationToolDenied("tool_arguments_invalid") from exc
        metadata = await self._hot_news_query.execute(
            question=parsed.question.strip(), tenant_id=tenant_id, user_id=user_id, on_bound=on_bound,
        )
        report = await self.execute(name="read_hot_news", arguments={"run_id": metadata["run_id"]},
                                    tenant_id=tenant_id, trace_id=trace_id)
        if report.get("not_found") or report.get("sql_query_id") != metadata["sql_query_id"]:
            raise ValueError("hot_news_query_report_mismatch")
        return {**report, **metadata}

    @property
    def descriptions(self) -> list[dict]:
        items = [item for item in TOOL_DESCRIPTIONS
                if item["name"] != "search_knowledge" or self._knowledge_enabled]
        analysis = self.analysis_description()
        return [*items, analysis] if analysis else items

    @traced("tool.execute")
    async def execute(self, *, name: str, arguments: dict, tenant_id: str, trace_id: str,
                      permissions=frozenset(), user_id: str | None = None,
                      confirmed=False, conversation_id=None, request_id=None, original_content="",
                      on_bound=None, prepared_context=None) -> dict[str, Any]:
        spec = self.hook_spec(name)
        if spec is not None:
            if not hook_permitted(spec, permissions):
                raise ConversationToolDenied("hook_permission_denied")
            if not self._hook_enabled(spec):
                raise ConversationToolDenied("hook_not_enabled")
            try:
                parsed = spec.argument_schema.model_validate(arguments)
            except ValidationError as exc:
                raise ConversationToolDenied("tool_arguments_invalid") from exc
            try:
                result = await spec.runner(tools=self, arguments=parsed, tenant_id=tenant_id,
                    user_id=user_id, trace_id=trace_id, confirmed=confirmed,
                    conversation_id=conversation_id, request_id=request_id,
                    original_content=original_content, on_bound=on_bound, name=name,
                    prepared_context=prepared_context)
            except DataLoopHookUnavailable as exc:
                raise ConversationToolDenied(exc.error_code, exc.result) from exc
            return spec.receipt_builder(result)
        schemas = {"capabilities": _Empty, "list_hot_news": _List, "read_hot_news": _Read,
                   "search_knowledge": _Search, "analyze_hot_news_data": _Analyze}
        if name not in schemas:
            raise ConversationToolDenied("tool_not_allowed")
        if name == "search_knowledge" and not self._knowledge_enabled:
            raise ConversationToolDenied("knowledge_read_not_enabled")
        if name == "analyze_hot_news_data" and self._analysis_runner is None:
            raise ConversationToolDenied("data_analysis_not_enabled")
        try:
            parsed = schemas[name].model_validate(arguments)
        except ValidationError as exc:
            raise ConversationToolDenied("tool_arguments_invalid") from exc
        if name == "capabilities":   # 这边直接返回模版。
            return {
                "tools": [item["name"] for item in self.available_descriptions(
                    tenant_id=tenant_id, permissions=permissions)],
                "read_only": not any(self._hook_enabled(hook) and hook_permitted(hook, permissions)
                    and hook.execution_class != "sync_read" for hook in HOOK_REGISTRY.values()),
                "limits": "读取获准的热点、新闻知识、写作、记忆和评测快照；写作、反馈与配置候选需本轮确认，审批、激活、回滚与发布仅在工作台执行",
                "new_sql": "新SQL仍通过热点工作台的原有Agent链路执行",
            }
        if name == "list_hot_news":
            runs = await self._hot_news.list_runs(tenant_id=tenant_id, offset=0, limit=20)
            return {"items": [
                {"run_id": str(run.run_id), "window_start": run.window_start.isoformat(),
                 "window_end": run.window_end.isoformat(), "ranked_news_count": run.ranked_news_count}
                for run in runs if run.status == "completed"
            ][:parsed.limit]}
        if name == "analyze_hot_news_data":
            return await self._analyze(parsed, tenant_id=tenant_id)
        if name == "read_hot_news":
            try:
                run_id = UUID(parsed.run_id)
            except ValueError as exc:
                raise ConversationToolDenied("tool_arguments_invalid") from exc
            detail = await self._hot_news.get_run_detail(tenant_id=tenant_id, run_id=run_id)
            if detail is None:
                return {"run_id": str(run_id), "items": [], "not_found": True}
            selected = [item for item in detail.ranked_news if parsed.news_rank is None or item.rank == parsed.news_rank]
            execution = getattr(detail, "analysis_execution", None)
            report = {
                "run_id": str(run_id),
                "window_start": detail.run.window_start.isoformat(),
                "window_end": detail.run.window_end.isoformat(),
                "ranked_news_count": getattr(detail.run, "ranked_news_count", len(detail.ranked_news)),
                "analyzed_news_count": getattr(detail.run, "analyzed_news_count",
                                               sum(item.analysis is not None for item in detail.ranked_news)),
                "sql_query_id": detail.sql_tool_trace.query_id if detail.sql_tool_trace else None,
                "analysis_execution": execution.model_dump(mode="json") if execution else None,
                "items": [
                    {"rank": item.rank, "news_id": item.news_id, "title": (item.title or item.news_id)[:200],
                     "metrics": item.metrics.model_dump(mode="json"), "hot_score": item.hot_score.model_dump(mode="json"),
                     "analysis": {"trend_assessment": item.analysis.trend_assessment[:1000],
                                  "dominant_driver": item.analysis.dominant_driver,
                                  "limitations": [value[:200] for value in item.analysis.limitations[:5]]} if item.analysis else None}
                    for item in selected[:5]
                ],
                "truncated": len(selected) > 5,
            }
            if self._knowledge_enabled and getattr(detail.run, "status", None) == "completed":
                report["related_articles"] = await self._related_articles.recall(
                    run_id=str(run_id), sources=report["items"], tenant_id=tenant_id,
                )
            return report
        query = parsed.query.strip()
        source_news_id = parsed.source_news_id.strip() if parsed.source_news_id is not None else None
        if not query or source_news_id == "":
            raise ConversationToolDenied("tool_arguments_invalid")
        lifecycle_query = getattr(self._knowledge, "lifecycle_query", None)
        if lifecycle_query is not None:
            recalled = await lifecycle_query.search(query_text=query, tenant_id=tenant_id,
                limit=parsed.limit, exclude_news_ids=(source_news_id,) if source_news_id else ())
            response = {"query": query, "items": [
                {"news_id": hit.news_id, "chunk_id": hit.chunk_id,
                 "title": hit.title[:200], "excerpt": hit.excerpt[:600],
                 "source_url": safe_source_url(hit.source_url), "content_version": hit.content_version,
                 "publish_time": hit.publish_time.isoformat() if hit.publish_time else None,
                 "embedding_version": hit.embedding_version, "tier": hit.tier.value}
                for hit in recalled["hits"]], "evidence_is_untrusted": True,
                "retrieval_backend": "tier_lifecycle", "retrieval_trace": recalled["trace"]}
            if source_news_id:
                response["source_news_id"] = source_news_id
            return response
        if self._query_embedder is not None:
            vectors = await self._query_embedder.embed_queries((query,), tenant_id=tenant_id)
            if len(vectors) != 1:
                raise ValueError("query_embedding_version_or_count_drift")
            embedding_version = self._query_embedder.version
        else:
            result = await self._embedding.embed(EmbeddingRequest(
                tenant_id=tenant_id, trace_id=trace_id, model_route=self._embedding_version, texts=(query,),
            ))
            if result.model_version != self._embedding_version or len(result.vectors) != 1:
                raise ValueError("query_embedding_version_or_count_drift")
            vectors, embedding_version = result.vectors, self._embedding_version
        hits = []
        for tier in getattr(self._knowledge, "tiers", tuple(VectorTier)):
            hits.extend(await self._knowledge.search(
                tier=tier, tenant_id=tenant_id, vector=vectors[0],
                embedding_version=embedding_version,
                exclude_news_ids=frozenset((source_news_id,)) if source_news_id else frozenset(),
                limit=parsed.limit,
            ))
        seen = set()
        items = []
        for hit in sorted(hits, key=lambda item: (-item.raw_score, item.chunk_id)):
            if hit.news_id in seen:
                continue
            seen.add(hit.news_id)
            items.append({"news_id": hit.news_id, "chunk_id": hit.chunk_id,
                          "title": hit.title[:200], "excerpt": hit.excerpt[:600],
                          "source_url": safe_source_url(hit.source_url), "content_version": hit.content_version,
                          "publish_time": hit.publish_time.isoformat() if hit.publish_time else None,
                          "embedding_version": hit.embedding_version})
            if len(items) >= parsed.limit:
                break
        response = {"query": query, "items": items, "evidence_is_untrusted": True}
        if source_news_id:
            response["source_news_id"] = source_news_id
        if getattr(self._knowledge, "snapshot_version", None):
            response.update({"retrieval_backend": "bge_snapshot",
                             "snapshot_version": self._knowledge.snapshot_version,
                             "embedding_version": embedding_version,
                             "coverage_note": "离线新闻快照的语义召回；不代表当前实时热点或发布时间筛选。"})
        return response

    async def _analyze(self, arguments: _Analyze, *, tenant_id: str) -> dict:
        try:
            run_id = UUID(arguments.run_id)
            reference_id = UUID(arguments.reference_run_id) if arguments.reference_run_id else None
        except ValueError as exc:
            raise ConversationToolDenied("tool_arguments_invalid") from exc
        detail = await self._hot_news.get_run_detail(tenant_id=tenant_id, run_id=run_id)
        extended = arguments.operation in {"baseline", "trend"}
        try:
            request = {"schema_version": "2.0" if extended else "1.0",
                       "operation": arguments.operation, "metric": arguments.metric,
                       "dataset": project_dataset(detail, run_id=str(run_id), tenant_id=tenant_id,
                                                  extended=extended, include_baseline=arguments.operation == "baseline")}
            if reference_id is not None:
                reference = await self._hot_news.get_run_detail(tenant_id=tenant_id, run_id=reference_id)
                request["reference_dataset"] = project_dataset(reference, run_id=str(reference_id),
                                                              tenant_id=tenant_id, extended=True)
        except AnalysisProjectionError as exc:
            raise ConversationToolDenied(str(exc)) from exc
        return await self._analysis_runner.run(request)


def render_tool_results(traces) -> str:
    """Numbers are formatted from tool snapshots by Python, not invented by LLM."""
    parts = []
    for trace in traces:
        response_style = "list"
        if trace.result.get("query_input_error") == "dated_saved_report":
            parts.append("读取已保存报告不能满足指定日期的查询，未使用历史榜单代替请求结果。请发起日期查询，或明确选择样本窗口。")
            continue
        if trace.name == "query_hot_news" and trace.result.get("query_input_error") == "question_mismatch":
            parts.append("本轮查询条件未通过一致性校验，没有执行 SQL 或启动热点运行。请使用原问题重试。")
            continue
        if trace.name == "query_hot_news" and trace.result.get("query_input_error") == "input_boundary":
            parts.append("该请求超出新闻聚合数据的查询范围，未执行取数。请使用当前租户的新闻指标问题。")
            continue
        if trace.name == "query_hot_news" and trace.result.get("query_input_error") == "supplement_requires_new_turn":
            parts.append("本轮补充要求已保存；当前 SQL 查询要求一条完整问题，请在下一轮合并日期、小时和筛选条件后发送。已启动的热点运行不会自动取消或重跑。")
            continue
        if trace.name == "query_hot_news" and trace.result.get("query_resolution") is not None:
            try:
                resolution = QueryResolution.model_validate(trace.result["query_resolution"])
            except (ValidationError, ValueError):
                parts.append("查询范围说明未通过结构校验，无法确认本轮结果。")
                continue
            parts.append(resolution.message)
            parts.extend(resolution.notes)
            response_style = resolution.response_style
            if resolution.status != "ready":
                coverage_start = resolution.coverage_window_start or resolution.supported_window_start
                coverage_end = resolution.coverage_window_end or resolution.supported_window_end
                parts.append("当前可用查询窗口："
                             f"{coverage_start.isoformat(timespec='minutes')} 至 "
                             f"{coverage_end.isoformat(timespec='minutes')}（结束不含），每次查询一个整点小时。"
                             "可以发送“查看当前样本的热点新闻”，或明确日期和小时。")
                parts.append("本轮没有执行 SQL 或启动热点运行，也没有用已保存的旧榜单代替请求结果。")
                continue
        if trace.status != "completed":
            reasons = {"query_workflow_failed": "热点运行失败，未取得通过校验的报告。",
                       "query_backend_unavailable": "热点查询服务暂不可用，未确认运行结果。",
                       "query_wait_timeout": "等待热点运行超时；已绑定的运行可能继续执行，请按运行引用核对状态。",
                       "query_result_unavailable": "无法读取或校验热点运行结果，当前状态尚未确认。"}
            parts.append(reasons.get(trace.error_code, f"工具 {trace.name} 未完成：{trace.error_code}，没有使用虚构或旧数据代替。"))
            if trace.result.get("reason"):
                parts.append(trace.result["reason"])
            if trace.name == "query_hot_news" and trace.result.get("workflow_id"):
                parts.append(f"查询已绑定，热点运行结果尚未确认；断线不会自动取消持久化Workflow。\n"
                             f"workflow_id={trace.result['workflow_id']}\nquery_id={trace.result.get('sql_query_id')}")
            if trace.name == "start_writing_intent" and trace.result.get("job_id"):
                parts.append(f"写作任务已保存，Workflow 接受状态尚未确认；已启动的 Workflow 不会被对话断线取消。\n"
                             f"job_id={trace.result['job_id']}\nworkflow_id={trace.result.get('workflow_id')}\n"
                             f"详情：/jobs?job_id={trace.result['job_id']}")
            continue
        result = trace.result
        if trace.name == "start_writing_intent" and result.get("confirmation"):
            if any(other is not trace and other.name == trace.name and not other.result.get("confirmation")
                   for other in traces):
                continue
            parts.append("写作参数为模型建议，尚未创建写作任务。请在本轮有效期内确认启动，或重新描述要求。")
            continue
        if trace.name == "save_feedback_case" and result.get("confirmation"):
            if any(other is not trace and other.name == trace.name and not other.result.get("confirmation") for other in traces):
                continue
            parts.append("反馈参数为模型建议，尚未保存案例；确认将同时记录该分析的驳回反馈，期望标签仅作为草稿，不提交或批准标签。")
            continue
        if trace.name in {"start_data_loop", "propose_bundle_candidate"} and result.get("confirmation"):
            if any(other is not trace and other.name == trace.name and not other.result.get("confirmation") for other in traces):
                continue
            parts.append("参数与来源快照已保存，尚未执行；请在本轮有效期内确认，生产批准、激活和回滚仍仅在评审台执行。")
            continue
        if trace.name in {"read_hot_news", "query_hot_news"}:
            if trace.name == "query_hot_news":
                provider = result.get("query_model_provider", "未知")
                planning = "Python确定性编译（已确认意图）" if provider == "python-confirmed-intent" else f"查询规划模型：{provider}"
                parts.append(f"本轮数仓查询：{result['sql_query_id']}\n"
                             f"{planning}；"
                             f"已分析 {result.get('analyzed_news_count',0)} 篇新闻，已上榜 {result.get('ranked_news_count',0)} 篇。")
                parts.append("SQL 按照问题指定指标筛选候选；以下榜单再按 Python 权威热度排序，不等同于 SQL 指标顺序。")
            parts.append(f"热点运行：{result['run_id']}\n窗口：{result.get('window_start', '未知')} 至 {result.get('window_end', '未知')}")
            if result.get("analysis_execution"):
                execution = result["analysis_execution"]
                parts.append(f"模型分析任务 {execution['task_count']} 篇；本轮并发峰值 {execution['observed_concurrency']}；"
                             f"分析阶段耗时 {execution['elapsed_ms']} 毫秒（含等待并发名额）。")
            if not result.get("items"):
                parts.append("没有找到符合条件的已完成报告。")
            if response_style == "summary" and result.get("items"):
                parts.append("热点简要总结（仅依据本次已保存的候选报告）：")
            for item in result.get("items", []):
                metrics = item["metrics"]
                if response_style == "summary":
                    parts.append(f"{item['rank']}. {item['title']}：热度 {item['hot_score']['score']}，"
                                 f"点击 {metrics['clicks']}，点击率 {metrics['ctr']}。")
                    if item.get("analysis"):
                        parts.append(f"已保存的模型摘要（需人工核对）：{item['analysis']['trend_assessment']}")
                    continue
                parts.append(f"{item['rank']}. {item['title']}（news_id={item['news_id']}）\n"
                             f"热度 {item['hot_score']['score']}；点击 {metrics['clicks']}；点击率 {metrics['ctr']}。")
                if item.get("analysis"):
                    trend = item["analysis"]["trend_assessment"]
                    parts.append(f"模型分析摘要（数字以快照为准，需人工核对）：{trend}")
            if result.get("truncated"):
                parts.append("仅展示部分候选，完整结果见本次热点运行报告。")
        elif trace.name == "list_hot_news":
            parts.append("已完成热点运行：" if result.get("items") else "暂无已完成的热点运行，请先在热点工作台运行本地模拟。")
            parts.extend(f"{item['run_id']} · {item['window_start']} · 上榜 {item['ranked_news_count']} 篇" for item in result.get("items", []))
        elif trace.name == "search_knowledge":
            if result.get("coverage_note"):
                parts.append(result["coverage_note"])
            parts.append("知识检索证据（不代表已独立核实）：" if result.get("items") else "没有找到新闻知识证据，不能补写事实。")
            parts.extend(f"{item['title']}（news_id={item['news_id']}）\n{item['excerpt']}" for item in result.get("items", []))
        elif trace.name == "capabilities":
            parts.append(result["limits"])
        elif trace.name == "analyze_hot_news_data":
            parts.append(render_analysis_result(result))
        elif trace.name in {"list_writing_jobs", "writing_job_status", "start_writing_intent"}:
            jobs = result.get("items", []) if trace.name == "list_writing_jobs" else (
                [result["job"]] if result.get("job") else [])
            if not jobs:
                parts.append("没有找到当前租户可读取的写作任务。")
            for job in jobs:
                gate = f"；等待人工 {job['pending_gate']} 审批" if job.get("pending_gate") else ""
                admission = job.get("admission") or result.get("admission")
                if admission:
                    parts.append(f"对话任务准入：{admission['state']}；队列位置 {admission.get('queue_position')}；"
                                 f"租户活跃 {admission.get('active_count')} / 预算 {admission.get('budget')}。")
                parts.append(f"写作任务：{job['topic']}\njob_id={job['id']}；版本 {job['version']}；"
                             f"状态 {job['status']}；阶段 {job['current_step'] or '无执行中的步骤'}；"
                             f"进度 {job['progress_percent']}%{gate}。\n详情：/jobs?job_id={job['id']}")
        elif trace.name in {"save_short_term_memory", "propose_long_term_memory", "memory_status"}:
            memory = result.get("memory")
            if memory:
                description = "长期候选，尚未晋升" if memory["tier"] == "long_term_candidate" and memory.get("not_promoted") else "用户记忆快照"
                parts.append(f"{description}：{memory['content']}\n实体 {memory['id']}；版本 {memory['version']}；"
                             f"状态 {memory['effective_status']}；有效期至 {memory['expires_at']}。")
            else:
                parts.append("没有找到当前用户可读取的记忆记录。")
        elif trace.name == "save_feedback_case" and result.get("feedback"):
            feedback = result["feedback"]
            parts.append(f"反馈案例已保存，等待人工标注与二审：{feedback['case_id']}\n"
                         f"关联运行 {feedback['run_id']}；新闻 {feedback['news_id']}；Bundle {feedback['production_bundle_version']}。\n"
                         f"期望标签仅草稿：{feedback['expected_label_draft']}。未提交或批准标签。\n"
                         f"详情：/data-loop?case_id={feedback['case_id']}")
        elif trace.name in {"start_data_loop", "evaluation_status"} and result.get("run"):
            run = result["run"]
            if result.get("receipt", {}).get("status") == "not_landed":
                admission = result.get("admission", {})
                parts.append(f"评测启动请求：{run['workflow_id']}；准入状态 {admission.get('state', run['phase'])}；"
                             f"队列位置 {admission.get('queue_position')}；租户活跃 {admission.get('active_count')} / "
                             f"预算 {admission.get('budget')}。业务工作流启动尚未确认，评测记录尚未生成。")
                continue
            parts.append(f"评测 Workflow：{run['workflow_id']}；阶段 {run['phase']}；"
                         f"门禁 {run.get('gate_passed')}；等待人工批准 {run.get('waiting_for_approval')}。\n"
                         f"评测记录：{run.get('evaluation_run_id') or '尚未生成'}；详情：/data-loop?workflow_id={run['workflow_id']}")
        elif trace.name == "propose_bundle_candidate" and result.get("candidate"):
            candidate = result["candidate"]
            parts.append(f"配置候选已保存：{candidate['candidate_id']}；版本 {candidate['candidate_version']}；"
                         f"状态 {candidate['status']}；未批准或激活。\n详情：/data-loop?candidate_id={candidate['candidate_id']}")
    return "\n\n".join(parts)


def render_analysis_result(result: dict) -> str:
    """Render the calculated report, retaining its scope, versions and evidence hash."""
    source, analysis = result["source"], result["analysis"]
    display = lambda value: "无法计算（空样本或分母为零）" if value is None else str(value)
    parts = [f"数据分析 · {result['operation']} · 指标 {result['metric']}",
             f"热点运行：{source['run_id']}\n窗口：{source['window_start']} 至 {source['window_end']}",
             f"分析范围：当前已上榜的 {source['row_count']} 篇新闻；不是全量数仓。"]
    if result["operation"] == "overview":
        labels = {"impressions": "曝光", "clicks": "点击", "effective_consumptions": "有效消费",
                  "interactions": "互动", "total_duration_seconds": "总消费时长（秒）"}
        parts.append("；".join(f"{labels[key]} {value}" for key, value in analysis["totals"].items()))
        parts.append(f"加权点击率（总点击 / 总曝光）：{display(analysis['weighted_ctr'])}")
    elif result["operation"] == "distribution":
        labels = {"min": "最小值", "max": "最大值", "mean": "均值", "median": "中位数", "p90": "P90（最近秩）"}
        parts.append("；".join(f"{label} {display(analysis[key])}" for key, label in labels.items()))
    elif result["operation"] == "compare":
        for key, label in (("highest", "最高"), ("lowest", "最低")):
            item = analysis[key]
            parts.append(f"{label}：news_id={item['news_id']}，值 {item['value']}" if item else f"{label}：空样本")
        parts.append(f"绝对差值：{display(analysis['absolute_difference'])}；相对变化（差值 / 最低值）：{display(analysis['relative_change'])}")
    elif result["operation"] == "quality":
        labels = {"zero_impression_news_ids": "零曝光", "ctr_mismatch_news_ids": "点击率与快照计数不一致",
                  "clicks_above_impressions_news_ids": "点击多于曝光（需核对事件口径）"}
        for key, label in labels.items():
            values = analysis[key]
            displayed = ", ".join(values[:10]) if values else "未发现"
            if len(values) > 10:
                displayed += f"（共 {len(values)} 篇，仅显示前 10 篇；完整列表见工具快照）"
            parts.append(f"{label}：{displayed}")
        parts.extend(analysis["notes"])
    elif result["operation"] == "baseline":
        parts.append(f"已保存参考值对照：可计算 {analysis['compared_count']} 篇；缺失参考 {len(analysis['missing_baseline_news_ids'])} 篇；缺失指标 {len(analysis['missing_metric_news_ids'])} 篇。")
        for item in analysis["items"][:10]:
            parts.append(f"news_id={item['news_id']}：当前 {display(item['current_value'])}；参考 {display(item['reference_value'])}；"
                         f"差值 {display(item['absolute_change'])}；相对变化 {display(item['relative_change'])}；"
                         f"参考份数 {display(item['sample_count'])}；参考版本 {item['reference_version'] or '未记录'}；状态 {item['status']}")
        if len(analysis["items"]) > 10:
            parts.append("仅显示前 10 篇，完整对照见工具快照。")
    else:
        reference = source["reference"]
        parts.append(f"参考运行：{reference['run_id']}\n参考窗口：{reference['window_start']} 至 {reference['window_end']}")
        parts.append(f"两窗口共同上榜 {analysis['matched_news_count']} 篇，新增上榜 {len(analysis['added_news_ids'])} 篇，退出榜单 {len(analysis['removed_news_ids'])} 篇；窗口间隔 {analysis['gap_seconds']} 秒。")
        aggregate = analysis["aggregate"]
        parts.append(f"仅共同新闻的 {aggregate['method']} 指标：参考 {display(aggregate['reference_value'])} → 当前 {display(aggregate['current_value'])}；"
                     f"差值 {display(aggregate['absolute_change'])}；相对变化 {display(aggregate['relative_change'])}。")
        for item in analysis["items"][:10]:
            parts.append(f"news_id={item['news_id']}：参考 {display(item['reference_value'])} → 当前 {display(item['current_value'])}；差值 {display(item['absolute_change'])}；相对变化 {display(item['relative_change'])}")
        if len(analysis["items"]) > 10:
            parts.append("仅显示前 10 篇，完整对照见工具快照。")
    parts.extend(result["limitations"])
    parts.append(f"算法：{result['algorithm_version']}；Bundle：{source['bundle_version']}；"
                 f"快照SHA-256：{source['snapshot_sha256']}；执行：{result['execution'].get('transport', result['execution']['backend'])}")
    return "\n".join(parts)
