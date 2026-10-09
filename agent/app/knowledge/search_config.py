"""Versioned YAML configuration for knowledge retrieval, separate from ingestion."""

from pathlib import Path
import re
from typing import Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.retrieval.tiered_vector import VectorTier

"""Milvus检索客户端"""
class MilvusSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str
    database: str = Field(default="default", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    token_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=5, gt=0, le=60)
    collections: dict[VectorTier, str] = Field(min_length=1)
    metric_type: Literal["IP", "COSINE"] = "IP"
    search_params: dict = Field(default_factory=lambda: {"ef": 96})

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        parsed = urlsplit(self.uri)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Milvus uri must be an HTTP(S) server address")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Milvus uri must not contain credentials, query or fragment")
        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
               for name in self.collections.values()):
            raise ValueError("invalid Milvus collection name")
        if len(set(self.collections.values())) != len(self.collections):
            raise ValueError("each configured tier must use a distinct collection")
        return self

"""知识库检索配置信息。"""
class KnowledgeSearchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    backend: Literal["postgres", "milvus"]
    milvus: MilvusSearchConfig | None = None

    @model_validator(mode="after")
    def validate_backend(self) -> Self:
        if (self.backend == "milvus") != (self.milvus is not None):
            raise ValueError("milvus configuration is required only for the milvus backend")
        return self


def load_knowledge_search_config(path: str | Path) -> KnowledgeSearchConfig:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 1_048_576:
        raise ValueError("knowledge search YAML missing or exceeds 1 MiB")
    try:
        value = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("knowledge search YAML cannot be read") from exc
    return KnowledgeSearchConfig.model_validate(value)
