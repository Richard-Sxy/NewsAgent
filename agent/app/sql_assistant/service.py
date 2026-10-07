"""总协调入口。 SqlAssiatantService 串起问题检查、模型调用、SQL生成、预览保存和检查执行。重点看 preview()、execute()、execute_for_hot_news()"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Callable, TYPE_CHECKING
from uuid import uuid4

from app.clients.enterprise.sql_warehouse import SqlQuery, SqlWarehouseClient
from app.model_runtime.agent_client import StructuredAgentClient, model_request_context
from app.schemas.sql_assistant import (
    SqlAssistantIntent, SqlAssistantPreview, SqlAssistantPreviewRequest,
    SqlAssistantResult, SqlAssistantStage,
)
from app.sql_assistant.audit import QuerySnapshotStore
from app.sql_assistant.guard import SqlAssistantGuard
from app.sql_assistant.planner import compile_query, screen_question, validate_intent
from app.sql_assistant.scenarios import SqlScenario, load_sql_scenarios
from app.sql_assistant.warehouse import (
    demo_dataset_info, get_text2sql_schema, verify_schema_contract,
    warehouse_contract,
    STREAMED_PROFILES, profile_news_count,
)
from app.sql_assistant.synthetic_profiles import CLASSIC_PROFILE, validate_profile

if TYPE_CHECKING:
    from app.services.hot_news_orchestration import HotNewsRunRequest
    from app.sql_assistant.hot_news_binding import HotNewsSqlBindingStore


class SqlAssistantError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


def _digest(value: dict) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _snapshot_fingerprint(snapshot: dict) -> str:
    fields = {key: snapshot[key] for key in ("preview", "tenant_id", "user_id", "scenario", "intent")}
    if "execution_policy" in snapshot:
        fields["execution_policy"] = snapshot["execution_policy"]
    if "dataset_contract" in snapshot:
        fields["dataset_contract"] = snapshot["dataset_contract"]
    return _digest(fields)


def _json_cell(value):
    if value is None or isinstance(value, (str, int, float)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        # Keep exact warehouse decimals as strings; Vue controls presentation.
        return format(value, "f")
    return str(value)


class SqlAssistantService:
    def __init__(
        self, *, agent: StructuredAgentClient, store: QuerySnapshotStore,
        warehouse_factory: Callable[[str], SqlWarehouseClient], scenarios_path: str,
        model_provider: str,
        hot_news_bindings: "HotNewsSqlBindingStore | None" = None,
        dataset_profile: str = CLASSIC_PROFILE,
        news_per_tenant: int = 1200,
    ) -> None:
        self.agent = agent
        self.store = store
        self.warehouse_factory = warehouse_factory
        self.scenarios_path = scenarios_path
        self.model_provider = model_provider
        self.hot_news_bindings = hot_news_bindings
        self.dataset_profile = validate_profile(dataset_profile)
        self.news_per_tenant = news_per_tenant
        self.contract = warehouse_contract(self.dataset_profile)
        if self.dataset_profile in STREAMED_PROFILES:
            profile_news_count(self.dataset_profile, news_per_tenant)
        # The scenario enforces its own narrower ceiling; the guard accepts the
        # frozen config's global maximum, including 168-row hourly trends.
        self.guard = SqlAssistantGuard(max_rows=1000)

    def config(self) -> dict:
        markdown = verify_schema_contract(self.dataset_profile)
        config = self._scenarios()
        return {
            "schema_version": self.contract.version, "schema_sha256": self.contract.sha256,
            "schema_markdown": markdown, "dataset": demo_dataset_info(
                self.dataset_profile, news_per_tenant=self.news_per_tenant,
            ),
            "scenarios": [scenario.model_dump(mode="json") for scenario in config.scenarios],
            "configuration_template": Path(self.scenarios_path).read_text(encoding="utf-8"),
            "model_provider": self.model_provider,
        }

    def _scenarios(self):
        config = load_sql_scenarios(self.scenarios_path)
        if config.warehouse_schema_version != self.contract.version:
            raise SqlAssistantError("场景与当前数仓格式版本不一致", 409)
        return config

    def dataset_identity(self) -> dict:
        """Freeze only the immutable scaled identity, never the full dataset."""
        dataset = demo_dataset_info(self.dataset_profile, news_per_tenant=self.news_per_tenant)
        return {key: dataset[key] for key in (
            "dataset_profile", "dataset_version", "dataset_sha256", "news_per_tenant",
        ) if key in dataset}

    """自然语言转查询意图。"""
    async def preview(self, request: SqlAssistantPreviewRequest, *, tenant_id: str, user_id: str,
                      expected_intent: SqlAssistantIntent | None = None) -> SqlAssistantPreview:
        started = monotonic()
        verify_schema_contract(self.dataset_profile)
        config = self._scenarios()
        scenario = config.resolve(request.scenario_id)
        question = screen_question(request.question)
        stages = [SqlAssistantStage(name="input_boundary", detail="身份由网关注入；问题只用于选择批准的查询指标和过滤条件")]
        query_id = str(uuid4())
        payload = {
            "question": question, "scenario": scenario.model_dump(mode="json"),
            "schema_ddl": get_text2sql_schema(self.dataset_profile).render_ddl(), "schema_version": self.contract.version,
            "window_policy": "时间范围由服务端确认；不得输出租户、SQL、时间或数据值。",
        }
        result = None
        # Retries stay here for this short interactive read workflow. The HTTP
        # adapter does not retry, so attempts cannot multiply with Temporal.
        with model_request_context(tenant_id=tenant_id, trace_id=f"sql-assistant-{query_id}"):
            for attempt in range(1, config.model_max_attempts + 1):
                try:
                    async with asyncio.timeout(config.model_timeout_seconds):
                        # 调用 run_structured() 要求输出 SqlAssistantIntent。
                        result = await self.agent.run_structured(
                            app_id="python:sql-assistant-v1", payload=payload,
                            output_type=SqlAssistantIntent, mode=config.model_scene,
                        )
                    break
                except Exception as exc:
                    retryable = isinstance(exc, TimeoutError) or bool(getattr(exc, "retryable", False))
                    if not retryable or attempt == config.model_max_attempts:
                        if retryable:
                            raise SqlAssistantError("模型暂不可用，请稍后重试；当前没有执行 SQL", 503) from exc
                        raise
                    await asyncio.sleep(0.25 * attempt)
        if result is None:
            raise SqlAssistantError("模型未生成查询计划", 502)
        intent = result.value
        validate_intent(intent, scenario)
        stages.append(SqlAssistantStage(
            name="model_planning", detail=f"{self.model_provider} 生成受限查询计划；没有接触数据库或企业真实记录",
            elapsed_ms=int((monotonic() - started) * 1000), attempts=attempt,
        ))
        params: dict[str, str | int] = {
            "tenant_id": tenant_id, "window_start": request.window_start.isoformat(),
            "window_end": request.window_end.isoformat(), "row_limit": intent.row_limit,
        }
        if intent.content_type is not None:
            params["content_type"] = intent.content_type
        if intent.category is not None:
            params["category"] = intent.category
        # A narrower scenario whitelist constrains even an omitted model filter.
        if intent.content_type is None and len(scenario.allowed_content_types) == 1:
            intent = intent.model_copy(update={"content_type": scenario.allowed_content_types[0]})
            params["content_type"] = intent.content_type
        if intent.category is None and len(scenario.allowed_categories) == 1:
            intent = intent.model_copy(update={"category": scenario.allowed_categories[0]})
            params["category"] = intent.category
        if len(scenario.allowed_categories) not in (1, 4) and intent.category is None:
            raise SqlAssistantError("当前场景请在问题中指定一个允许的栏目")
        if expected_intent is not None:
            # The upstream semantic gate is authoritative for required slots.
            # A second model must not silently change a metric or drop a filter.
            validate_intent(expected_intent, scenario)
            fields = ("sort_by", "sort_direction", "content_type", "category", "row_limit")
            if any(getattr(intent, field) != getattr(expected_intent, field) for field in fields):
                from app.conversation.query_understanding import QueryResolution, QueryResolutionError

                raise QueryResolutionError(QueryResolution(
                    status="unsupported", reason_code="query_intent_mismatch",
                    message="查询计划与已确认条件不一致，未执行取数。",
                    supported_window_start=request.window_start,
                    supported_window_end=request.window_end,
                ), request_id=result.request_id)
        sql = compile_query(intent, scenario)
        self.guard.validate(sql, params)
        stages.append(SqlAssistantStage(name="sql_guard", detail="参数化 SELECT、单视图、租户、双向时间边界、白名单与行数均通过"))
        stages.append(SqlAssistantStage(name="preview_saved", detail="预览已保存；执行仅接受 query_id，并重新校验快照"))
        preview = SqlAssistantPreview(
            query_id=query_id, question=request.question, scenario_id=scenario.id,
            sql=sql, parameters=params, sql_hash=sha256(sql.encode()).hexdigest(),
            schema_version=self.contract.version, schema_sha256=self.contract.sha256,
            explanation=f"{intent.explanation} 时间以已确认的整点窗口为准；SQL 热度仅是候选召回分，热点最终热度由 Python 规则计算。",
            model_request_id=result.request_id, model_provider=self.model_provider,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=config.preview_ttl_seconds), stages=stages,
        )
        snapshot = {
            "preview": preview.model_dump(mode="json"), "tenant_id": tenant_id,
            "user_id": user_id, "scenario": scenario.model_dump(mode="json"),
            "intent": intent.model_dump(mode="json"),
            "execution_policy": {"query_timeout_ms": config.query_timeout_ms},
        }
        if self.dataset_profile in STREAMED_PROFILES:
            snapshot["dataset_contract"] = self.dataset_identity()
        snapshot["fingerprint"] = _snapshot_fingerprint(snapshot)
        await self.store.save(snapshot)
        return preview

    async def execute(self, query_id: str, *, tenant_id: str, user_id: str) -> SqlAssistantResult:
        return await self._execute(query_id, tenant_id=tenant_id, user_id=user_id)

    async def execute_for_hot_news(self, query_id: str, *, request: "HotNewsRunRequest") -> SqlAssistantResult:
        """Execute only an atomically claimed run tool, with Temporal owning retry.

        The claim freezes authorization and config before enqueueing. UI TTL
        and later YAML edits cannot invalidate that already accepted run.
        """
        request.validate()
        if query_id != request.sql_query_id or self.hot_news_bindings is None:
            raise SqlAssistantError("热点运行没有绑定当前 SQL 工具", 409)
        await self.hot_news_bindings.require(
            run_key=request.idempotency_key, query_id=query_id,
            tenant_id=request.tenant_id, user_id=request.sql_query_user_id,
            production_bundle_version=request.production_bundle_version,
            scope_sha256=request.sql_query_scope_sha256,
            window_start=request.window_start, window_end=request.window_end,
        )
        return await self._execute(
            query_id, tenant_id=request.tenant_id, user_id=request.sql_query_user_id,
            run_request=request,
        )

    async def _execute(self, query_id: str, *, tenant_id: str, user_id: str, run_request: "HotNewsRunRequest | None" = None) -> SqlAssistantResult:
        verify_schema_contract(self.dataset_profile)
        config = self._scenarios() if run_request is None else None
        snapshot = await self.store.get(query_id, tenant_id, user_id)
        if snapshot is None:
            raise SqlAssistantError("查询预览不存在或不属于当前用户", 404)
        if snapshot.get("fingerprint") != _snapshot_fingerprint(snapshot):
            raise SqlAssistantError("查询快照完整性校验失败，请重新生成", 409)
        preview = SqlAssistantPreview.model_validate(snapshot["preview"])
        if preview.schema_version != self.contract.version or preview.schema_sha256 != self.contract.sha256:
            raise SqlAssistantError("数仓格式版本不一致，请重新生成查询", 409)
        if self.dataset_profile in STREAMED_PROFILES and snapshot.get("dataset_contract") != self.dataset_identity():
            raise SqlAssistantError("合成数据规模或版本已变化，请重新生成查询", 409)
        if run_request is None and datetime.now(timezone.utc) >= preview.expires_at:
            raise SqlAssistantError("查询预览已过期，请重新生成", 410)
        scenario = config.resolve(preview.scenario_id) if config is not None else SqlScenario.model_validate(snapshot["scenario"])
        if config is not None and scenario.model_dump(mode="json") != snapshot["scenario"]:
            raise SqlAssistantError("场景配置已变化，请重新生成预览", 409)
        intent = SqlAssistantIntent.model_validate(snapshot["intent"])
        validate_intent(intent, scenario)
        expected_sql = compile_query(intent, scenario)
        if preview.sql != expected_sql or preview.sql_hash != sha256(expected_sql.encode()).hexdigest():
            raise SqlAssistantError("SQL 与批准的查询计划不一致", 409)
        self.guard.validate(preview.sql, preview.parameters)
        expected_parameters = {
            "tenant_id": tenant_id,
            "window_start": preview.parameters["window_start"],
            "window_end": preview.parameters["window_end"],
            "row_limit": intent.row_limit,
        }
        if intent.content_type is not None:
            expected_parameters["content_type"] = intent.content_type
        if intent.category is not None:
            expected_parameters["category"] = intent.category
        if preview.parameters != expected_parameters:
            raise SqlAssistantError("绑定参数与批准的查询计划或当前租户不一致", 409)
        if run_request is not None:
            if scenario.result_mode != "ranking" or any(
                datetime.fromisoformat(str(preview.parameters[key])) != getattr(run_request, key)
                for key in ("window_start", "window_end")
            ):
                raise SqlAssistantError("SQL 工具窗口与热点运行不一致", 409)
        if snapshot.get("result") is not None:
            cached = SqlAssistantResult.model_validate(snapshot["result"])
            if (
                cached.query_id != query_id or cached.sql_hash != preview.sql_hash
                or cached.truncated or cached.row_count != len(cached.rows)
                or not 0 <= cached.row_count <= intent.row_limit
            ):
                raise SqlAssistantError("已保存结果与当前查询不一致或超出批准行数", 409)
            return cached
        params = dict(preview.parameters)
        if params["tenant_id"] != tenant_id:
            raise SqlAssistantError("租户范围校验失败", 403)
        for key in ("window_start", "window_end"):
            params[key] = datetime.fromisoformat(str(params[key]))
        warehouse = self.warehouse_factory(tenant_id)
        timeout_ms = config.query_timeout_ms if config is not None else snapshot.get("execution_policy", {}).get("query_timeout_ms")
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or not 1 <= timeout_ms <= 20000:
            raise SqlAssistantError("查询执行策略不完整", 409)
        started = monotonic()
        max_attempts = 1 if run_request is not None else 2
        for attempt in range(1, max_attempts + 1):
            try:
                async with asyncio.timeout(timeout_ms / 1000 + 1):
                    result = await warehouse.execute(SqlQuery(
                        sql=preview.sql, params=params, timeout_ms=timeout_ms,
                        max_rows=int(params["row_limit"]),
                    ))
                break
            except Exception as exc:
                retryable = isinstance(exc, TimeoutError) or bool(getattr(exc, "retryable", False))
                if not retryable or attempt == max_attempts:
                    raise SqlAssistantError("只读数据库查询失败或超时，请缩小时间范围后重试", 503, retryable=retryable) from exc
                await asyncio.sleep(0.25)
        if result.truncated or len(result.rows) > int(params["row_limit"]):
            raise SqlAssistantError("数据库结果超过已批准行数，已停止返回", 502)
        rows = [{str(key): _json_cell(value) for key, value in row.items()} for row in result.rows]
        columns = list(rows[0]) if rows else (
            ["news_id", "title", "content_type", "category", "source"] if scenario.result_mode == "ranking" else ["event_time"]
        ) + ([] if rows else ["impressions", "clicks", "effective_consumptions", "interactions", "ctr", "hot_score"])
        elapsed = int((monotonic() - started) * 1000)
        output = SqlAssistantResult(
            query_id=query_id, columns=columns, rows=rows, row_count=len(rows), elapsed_ms=elapsed,
            truncated=False, sql_hash=preview.sql_hash,
            summary=f"在本地 PostgreSQL 实际执行只读查询，返回 {len(rows)} 个候选。合成指标供热点 Agent 使用；SQL hot_score 仅是候选召回分，最终排名由 Python 热度规则计算。",
            stages=[*preview.stages,
                SqlAssistantStage(name="snapshot_recheck", detail="用户、租户、格式哈希、配置版本和 SQL 均已复核"),
                SqlAssistantStage(name="warehouse_execute", detail="PostgreSQL 只读事务执行；设置 statement_timeout", elapsed_ms=elapsed, attempts=attempt),
                SqlAssistantStage(name="result_saved", detail="查询结果与预览关联保存，重复执行返回同一已保存结果"),
            ],
        )
        await self.store.complete(query_id, tenant_id, user_id, output.model_dump(mode="json"))
        return output
