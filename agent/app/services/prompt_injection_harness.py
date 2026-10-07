"""用于运营演示的 Prompt Injection / Text2SQL dry-run 安全 Harness。

该模块故意不连接数仓、不调用模型、不执行工具副作用。它把当前项目的
不可信输入边界、SQL AST Guard、租户/时间范围检查和执行闸门串成一条可观察链路，
供前端交互测试使用。生产查询仍由 Text2SqlNewsMetricSource 负责。
"""

from __future__ import annotations

import re
from hashlib import sha256
from typing import Iterable
from uuid import UUID

from sqlglot.errors import SqlglotError

from app.analytics.sql_guard import SqlGuard, SqlGuardPolicy
from app.domain.errors import Text2SqlGuardError
from app.sql_assistant.input_boundary import (
    SqlAssistantQuestionError, normalize_question, question_signals,
)
from app.schemas.security_test import (
    PromptInjectionTestRequest,
    PromptInjectionTestResponse,
    SecurityStageResult,
)

_ALLOWED_TABLE = "dw.news_behavior_aggregate"
_ALLOWED_COLUMNS = frozenset(
    {
        "news_id",
        "content_type",
        "tenant_id",
        "event_time",
        "impressions",
        "clicks",
        "unique_users",
        "total_duration_seconds",
        "effective_consumptions",
        "interactions",
    }
)
_REQUIRED_PLACEHOLDERS = frozenset({"tenant_id", "window_start", "window_end"})
_ALLOWED_PLACEHOLDERS = _REQUIRED_PLACEHOLDERS | {"row_limit", "content_type"}

_DIRECT_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", re.compile(r"(?:忽略|无视|忘记).{0,16}(?:规则|系统|提示|之前)")),
    ("cross_tenant_request", re.compile(r"(?:其他|别的|跨|所有).{0,8}租户")),
    ("privileged_action_request", re.compile(r"(?:删除|修改|导出|发布|执行命令|调用工具)")),
)
_INDIRECT_SIGNAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("embedded_instruction", re.compile(r"(?:把这段|以下内容).{0,20}(?:系统指令|当作指令|调用 SQL)")),
    ("instruction_override_in_content", re.compile(r"(?:忽略|无视).{0,16}(?:规则|系统|提示)")),
)


def evaluate_prompt_injection_test(
    request: PromptInjectionTestRequest,
    *,
    tenant_id: UUID,
) -> PromptInjectionTestResponse:
    """执行一次不落库、不执行 SQL 的安全链路演练。"""

    stages: list[SecurityStageResult] = [
        SecurityStageResult(
            name="input_boundary",
            status="passed",
            detail="运营问题、新闻正文和候选 SQL 均标记为不可信数据",
            evidence=("prompt=untrusted", "news=untrusted", "sql=candidate_only"),
        )
    ]
    try:
        operator_text = normalize_question(request.operator_prompt, max_chars=5000)
        direct_signals = tuple(dict.fromkeys((
            *question_signals(operator_text),
            *_signals(operator_text, _DIRECT_SIGNAL_PATTERNS),
        )))
    except SqlAssistantQuestionError:
        direct_signals = ("obfuscated_input",)
    try:
        news_text = normalize_question(request.untrusted_news_content, max_chars=12000) if request.untrusted_news_content else ""
        indirect_signals = _signals(news_text, _INDIRECT_SIGNAL_PATTERNS)
        if "instruction_override" in question_signals(news_text):
            indirect_signals = tuple(dict.fromkeys((*indirect_signals, "instruction_override_in_content")))
    except SqlAssistantQuestionError:
        # External evidence never acquires tool authority, even when it contains
        # hidden text. Preserve the distinction between data warnings and a
        # rejected operator request.
        indirect_signals = ("obfuscated_input_in_content",)

    if direct_signals:
        stages.append(
            SecurityStageResult(
                name="prompt_screening",
                status="blocked",
                detail="运营问题包含越权或改变系统规则的意图，拒绝继续扩大权限",
                evidence=direct_signals,
            )
        )
    elif indirect_signals:
        stages.append(
            SecurityStageResult(
                name="prompt_screening",
                status="warning",
                detail="新闻正文包含疑似指令，但正文只作为隔离证据，不改变工具权限",
                evidence=indirect_signals,
            )
        )
    else:
        stages.append(
            SecurityStageResult(
                name="prompt_screening",
                status="passed",
                detail="未发现演练规则中的直接或间接注入信号",
            )
        )

    normalized_sql: str | None = None
    guard_error: str | None = None
    sql_hash: str | None = None
    try:
        guard = SqlGuard(
            SqlGuardPolicy(
                allowed_tables=frozenset({_ALLOWED_TABLE}),
                allowed_columns=_ALLOWED_COLUMNS,
                tenant_column="tenant_id",
                window_column="event_time",
                required_placeholders=_REQUIRED_PLACEHOLDERS,
                allowed_placeholders=_ALLOWED_PLACEHOLDERS,
                max_rows=1000,
                dialect="postgres",
            )
        )
        normalized_sql = guard.validate(request.candidate_sql)
        sql_hash = sha256(normalized_sql.encode("utf-8")).hexdigest()
        stages.append(
            SecurityStageResult(
                name="sql_ast_guard",
                status="passed",
                detail="单条只读 SELECT、表列白名单、参数和 LIMIT 均通过",
                evidence=("tenant_scope=present", "time_scope=present", "read_only=true"),
            )
        )
    except (Text2SqlGuardError, SqlglotError, ValueError) as exc:
        guard_error = str(exc)
        stages.append(
            SecurityStageResult(
                name="sql_ast_guard",
                status="blocked",
                detail="候选 SQL 未通过确定性护栏，不能进入数仓",
                evidence=(guard_error,),
            )
        )

    blocked = bool(direct_signals or guard_error)
    if blocked:
        stages.append(
            SecurityStageResult(
                name="execution_gate",
                status="blocked",
                detail="安全闸门阻断；未向数仓发送 SQL，也未调用任何写工具",
                evidence=("sql_executed=false",),
            )
        )
    else:
        stages.append(
            SecurityStageResult(
                name="execution_gate",
                status="passed",
                detail="只读演练通过；当前为 dry-run，未实际执行 SQL",
                evidence=("sql_executed=false", "requires_read_only_warehouse=true"),
            )
        )

    return PromptInjectionTestResponse(
        scenario=request.scenario,
        tenant_id=str(tenant_id),
        decision="blocked" if blocked else "allow_read_only",
        risk_level=("high" if blocked else "medium" if indirect_signals else "low"),
        sql_executed=False,
        sql_hash=sql_hash,
        normalized_sql=normalized_sql,
        direct_injection_signals=direct_signals,
        indirect_injection_signals=indirect_signals,
        guard_error=guard_error,
        stages=tuple(stages),
    )


def _signals(
    content: str,
    patterns: Iterable[tuple[str, re.Pattern[str]]],
) -> tuple[str, ...]:
    return tuple(name for name, pattern in patterns if pattern.search(content))

