"""定义允许的查询场景并加载配置。规定可查询指标、栏目、内容类型、排行榜或趋势模式，以及数量上限、超时和预览有效期。"""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.sql_assistant import SqlMetric


class SqlScenario(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$", max_length=64)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    result_mode: Literal["ranking", "trend"] = "ranking"
    default_sort: SqlMetric = "clicks"
    allowed_sort_metrics: tuple[SqlMetric, ...] = Field(min_length=1)
    allowed_content_types: tuple[Literal["article", "video"], ...] = ("article", "video")
    allowed_categories: tuple[str, ...] = ("科技", "财经", "体育", "社会")
    default_limit: int = Field(default=10, ge=1, le=100)
    max_limit: int = Field(default=100, ge=1, le=1000)
    sample_questions: tuple[str, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def valid_choices(self) -> "SqlScenario":
        if self.default_sort not in self.allowed_sort_metrics:
            raise ValueError("default_sort must be in allowed_sort_metrics")
        if self.default_limit > self.max_limit:
            raise ValueError("default_limit exceeds max_limit")
        if not self.allowed_content_types or not self.allowed_categories:
            raise ValueError("scenario filters cannot be empty")
        if any(value not in {"科技", "财经", "体育", "社会"} for value in self.allowed_categories):
            raise ValueError("category does not exist in frozen schema v1")
        for choices in (self.allowed_sort_metrics, self.allowed_content_types, self.allowed_categories):
            if len(choices) != len(set(choices)):
                raise ValueError("scenario whitelist contains duplicate choices")
        return self


class SqlScenariosConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    warehouse_schema_version: Literal["news-warehouse-v1", "news-warehouse-v2", "news-warehouse-v3"]
    model_scene: Literal["text2sql_assistant"] = "text2sql_assistant"
    model_timeout_seconds: float = Field(default=15, ge=1, le=15)
    model_max_attempts: int = Field(default=2, ge=1, le=2)
    query_timeout_ms: int = Field(default=10000, ge=100, le=20000)
    preview_ttl_seconds: int = Field(default=900, ge=60, le=3600)
    scenarios: tuple[SqlScenario, ...] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_scenarios(self) -> "SqlScenariosConfig":
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate scenario id")
        return self

    def resolve(self, scenario_id: str) -> SqlScenario:
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise ValueError("查询场景不存在，请刷新场景配置")


def load_sql_scenarios(path: str | Path) -> SqlScenariosConfig:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 100_000:
        raise ValueError("Text2SQL 场景配置不存在或过大")
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError("Text2SQL 场景 YAML 格式无效") from exc
    return SqlScenariosConfig.model_validate(data)
