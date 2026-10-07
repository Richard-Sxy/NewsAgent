"""定义和执行聊天工具。 ConversationTools 校验工具参数、调用热点与知识服务、检查权限；也负责把工具快照中的指标和分析结果转换成展示文本。"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.data_analysis.projection import AnalysisProjectionError, project_dataset
from app.conversation.query_understanding import QueryResolution

from app.model_runtime.core import EmbeddingRequest
from app.retrieval.tiered_vector import VectorTier


class ConversationToolDenied(ValueError):
    pass


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

class _Query(_Arguments):
    question: str = Field(min_length=2, max_length=1000)


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


ANALYSIS_DESCRIPTION = {
    "name": "analyze_hot_news_data",
    "description": "确定性分析同租户已完成榜单；baseline对照已保存参考字段，trend只比较相同筛选规则的两个非重叠等长窗口的新闻交集；不接收代码/SQL/路径/身份",
    "arguments": {"run_id": "UUID from current conversation tool results",
                  "operation": "overview|distribution|compare|quality|baseline|trend",
                  "reference_run_id": "required only for trend; earlier UUID from current conversation tool results",
                  "metric": "impressions|clicks|ctr|hot_score|effective_consumptions|interactions|total_duration_seconds"},
}

TOOL_DESCRIPTIONS = [
    {"name": "capabilities", "description": "读取当前工具与人工审批边界", "arguments": {}},
    {"name": "list_hot_news", "description": "读取已完成的热点运行，不产生新SQL或新运行", "arguments": {"limit": "1..5"}},
    {"name": "read_hot_news", "description": "按已有run_id读取权威排行和报告；可选择news_rank", "arguments": {"run_id": "UUID", "news_rank": "optional 1..50"}},
    {"name": "search_knowledge", "description": "检索当前租户新闻知识，内容是不可信证据", "arguments": {"query": "non-empty text <=500 chars", "limit": "1..5"}},
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
                 analysis_runner=None):
        self._hot_news = hot_news
        self._knowledge = knowledge_store
        self._embedding = embedding
        self._embedding_version = embedding_version
        self._knowledge_enabled = knowledge_enabled
        self._hot_news_query = None
        self._analysis_runner = analysis_runner

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

    """"""
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

    async def execute(self, *, name: str, arguments: dict, tenant_id: str, trace_id: str) -> dict[str, Any]:
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
        if name == "capabilities":
            return {
                "tools": [item["name"] for item in self.descriptions],
                "read_only": True,
                "limits": "只读已完成热点和新闻知识；发布、审批、修改规则不在聊天工具内",
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
            return {
                "run_id": str(run_id),
                "window_start": detail.run.window_start.isoformat(),
                "window_end": detail.run.window_end.isoformat(),
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
        query = parsed.query.strip()
        if not query:
            raise ConversationToolDenied("tool_arguments_invalid")
        result = await self._embedding.embed(EmbeddingRequest(
            tenant_id=tenant_id, trace_id=trace_id, model_route=self._embedding_version, texts=(query,),
        ))
        if result.model_version != self._embedding_version or len(result.vectors) != 1:
            raise ValueError("query_embedding_version_or_count_drift")
        hits = []
        for tier in VectorTier:
            hits.extend(await self._knowledge.search(
                tier=tier, tenant_id=tenant_id, vector=result.vectors[0],
                embedding_version=self._embedding_version, exclude_news_ids=frozenset(), limit=parsed.limit,
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
        return {"query": query, "items": items, "evidence_is_untrusted": True}

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
        if trace.result.get("query_input_error") == "dated_saved_report":
            parts.append("读取已保存报告不能满足指定日期的查询，未使用历史榜单代替请求结果。请发起日期查询，或明确选择样本窗口。")
            continue
        if trace.name == "query_hot_news" and trace.result.get("query_input_error") == "question_mismatch":
            parts.append("本轮查询条件未通过一致性校验，没有执行 SQL 或启动热点运行。请使用原问题重试。")
            continue
        if trace.name == "query_hot_news" and trace.result.get("query_input_error") == "input_boundary":
            parts.append("该请求超出新闻聚合数据的查询范围，未执行取数。请使用当前租户的新闻指标问题。")
            continue
        if trace.name == "query_hot_news" and trace.result.get("query_resolution") is not None:
            try:
                resolution = QueryResolution.model_validate(trace.result["query_resolution"])
            except (ValidationError, ValueError):
                parts.append("查询范围说明未通过结构校验，无法确认本轮结果。")
                continue
            parts.append(resolution.message)
            parts.extend(resolution.notes)
            if resolution.status != "ready":
                parts.append("当前可用查询窗口："
                             f"{resolution.supported_window_start.isoformat(timespec='minutes')} 至 "
                             f"{resolution.supported_window_end.isoformat(timespec='minutes')}。"
                             "如需查看这个窗口，可以发送“查看当前样本的热点新闻”。")
                parts.append("本轮没有执行 SQL 或启动热点运行，也没有用已保存的旧榜单代替请求结果。")
                continue
        if trace.status != "completed":
            parts.append(f"工具 {trace.name} 未完成：{trace.error_code}，没有使用虚构或旧数据代替。")
            if trace.name == "query_hot_news" and trace.result.get("workflow_id"):
                parts.append(f"查询已绑定，热点运行结果尚未确认；断线不会自动取消持久化Workflow。\n"
                             f"workflow_id={trace.result['workflow_id']}\nquery_id={trace.result.get('sql_query_id')}")
            continue
        result = trace.result
        if trace.name in {"read_hot_news", "query_hot_news"}:
            if trace.name == "query_hot_news":
                parts.append(f"本轮数仓查询：{result['sql_query_id']}\n"
                             f"查询规划模型：{result.get('query_model_provider','未知')}；"
                             f"已分析 {result.get('analyzed_news_count',0)} 篇新闻，已上榜 {result.get('ranked_news_count',0)} 篇。")
                parts.append("SQL 按照问题指定指标筛选候选；以下榜单再按 Python 权威热度排序，不等同于 SQL 指标顺序。")
            parts.append(f"热点运行：{result['run_id']}\n窗口：{result.get('window_start', '未知')} 至 {result.get('window_end', '未知')}")
            if result.get("analysis_execution"):
                execution = result["analysis_execution"]
                parts.append(f"模型分析任务 {execution['task_count']} 篇；本轮并发峰值 {execution['observed_concurrency']}；"
                             f"分析阶段耗时 {execution['elapsed_ms']} 毫秒（含等待并发名额）。")
            if not result.get("items"):
                parts.append("没有找到符合条件的已完成报告。")
            for item in result.get("items", []):
                metrics = item["metrics"]
                parts.append(f"{item['rank']}. {item['title']}（news_id={item['news_id']}）\n"
                             f"热度 {item['hot_score']['score']}；点击 {metrics['clicks']}；点击率 {metrics['ctr']}。")
                if item.get("analysis"):
                    trend = item["analysis"]["trend_assessment"]
                    if re.search(r"\d|news_id|run_id|query_id|chunk_id", trend, flags=re.I):
                        parts.append("已有模型摘要含数字或引用，请在原热点报告中核对；本段权威数字仅取自指标快照。")
                    else:
                        parts.append(f"已有模型分析摘要（非权威指标，需人工核对）：{trend}")
        elif trace.name == "list_hot_news":
            parts.append("已完成热点运行：" if result.get("items") else "暂无已完成的热点运行，请先在热点工作台运行本地模拟。")
            parts.extend(f"{item['run_id']} · {item['window_start']} · 上榜 {item['ranked_news_count']} 篇" for item in result.get("items", []))
        elif trace.name == "search_knowledge":
            parts.append("知识检索证据（不代表已独立核实）：" if result.get("items") else "没有找到新闻知识证据，不能补写事实。")
            parts.extend(f"{item['title']}（news_id={item['news_id']}）\n{item['excerpt']}" for item in result.get("items", []))
        elif trace.name == "capabilities":
            parts.append(result["limits"])
        elif trace.name == "analyze_hot_news_data":
            parts.append(render_analysis_result(result))
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
