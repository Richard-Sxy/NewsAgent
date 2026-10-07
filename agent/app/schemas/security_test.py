"""Prompt Injection / Text2SQL 安全演练接口契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


SecurityStageStatus = Literal["passed", "warning", "blocked", "skipped"]
SecurityDecision = Literal["allow_read_only", "blocked"]
SecurityRiskLevel = Literal["low", "medium", "high"]


class PromptInjectionTestRequest(BaseModel):
    """一次只读安全演练输入；新闻内容和模型生成 SQL 均是不可信数据。"""

    model_config = ConfigDict(extra="forbid")

    scenario: str = Field(default="manual", min_length=1, max_length=64)
    operator_prompt: str = Field(min_length=1, max_length=5000)
    untrusted_news_content: str = Field(default="", max_length=12000)
    candidate_sql: str = Field(min_length=1, max_length=20000)


class SecurityStageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=64)
    status: SecurityStageStatus
    detail: str = Field(min_length=1, max_length=500)
    evidence: tuple[str, ...] = Field(default=(), max_length=16)


class PromptInjectionTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: str
    tenant_id: str
    decision: SecurityDecision
    risk_level: SecurityRiskLevel
    sql_executed: bool
    sql_hash: str | None
    normalized_sql: str | None
    direct_injection_signals: tuple[str, ...]
    indirect_injection_signals: tuple[str, ...]
    guard_error: str | None
    stages: tuple[SecurityStageResult, ...]
