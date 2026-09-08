from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.hot_news import (
    HotNewsAnalysisInput,
    HotNewsAnalysisReport,
)

class HotNewsAnalysisMemory(BaseModel):
    """一篇热点新闻经过校验的历史分析记忆"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    tenant_id: str
    news_id: str
    rank: int

    production_bundle_version: str
    workflow_version: str
    payload_schema_version: Literal["2.0"]

    analysis_input: HotNewsAnalysisInput
    analysis_report: HotNewsAnalysisReport

    fastgpt_request_id: str | None = None
    usage: dict[str, Any]

    captured_at: datetime
    validated_at: datetime
    completed_at: datetime
