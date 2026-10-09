"""
读取 YAML，检查接口地址、模型名、Prompt 版本和场景绑定。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.model_runtime.core import PromptRegistry, PromptSpec


class PromptConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scene: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=128)
    system_prompt: str = Field(min_length=1, max_length=20000)


class AgentSceneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scene: str = Field(min_length=1, max_length=128)
    prompt_version: str = Field(min_length=1, max_length=128)
    model_route: str = Field(min_length=1, max_length=128)


class EndpointConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["local", "openai_compatible"]
    model_routes: tuple[str, ...] = Field(min_length=1)
    url: HttpUrl | None = None
    api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    response_format: Literal["none", "json_object", "json_schema"] = "none"
    extra_body: dict = Field(default_factory=dict)
    local_embedding_dimensions: int = Field(default=32, ge=4, le=4096)
    expected_embedding_dimensions: int | None = Field(default=None, ge=1, le=65536)
    max_batch_size: int = Field(default=128, ge=1, le=2048)

    @model_validator(mode="after")
    def require_http_connection(self) -> Self:
        if any(not route.strip() for route in self.model_routes):
            raise ValueError("model routes cannot be blank")
        if len(self.model_routes) != len(set(self.model_routes)):
            raise ValueError("model routes cannot contain duplicates")
        if self.provider == "openai_compatible":
            if self.url is None or not self.api_key_env:
                raise ValueError("HTTP model endpoint requires url and api_key_env")
        elif self.url is not None or self.api_key_env is not None:
            raise ValueError("local model provider cannot declare remote connection")
        return self

"""模型执行的配置"""
class ModelRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    prompts: tuple[PromptConfig, ...] = Field(min_length=1)
    agent_scenes: tuple[AgentSceneConfig, ...] = ()
    inference: EndpointConfig
    embedding: EndpointConfig

    @model_validator(mode="after")
    def validate_agent_scenes(self) -> Self:
        registered = {(item.scene, item.version) for item in self.prompts}
        if len(registered) != len(self.prompts):
            raise ValueError("prompt scene/version registrations must be unique")
        names = [item.scene for item in self.agent_scenes]
        if len(names) != len(set(names)):
            raise ValueError("agent scenes must be unique")
        for item in self.agent_scenes:
            if (item.scene, item.prompt_version) not in registered:
                raise ValueError(f"agent scene lacks approved prompt: {item.scene}")
            if item.model_route not in self.inference.model_routes:
                raise ValueError(f"agent scene uses unapproved model: {item.scene}")
        return self

    def agent_scene(self, scene: str) -> AgentSceneConfig:
        for item in self.agent_scenes:
            if item.scene == scene:
                return item
        raise ValueError(f"agent scene is not configured: {scene}")

    def prompt_registry(self) -> PromptRegistry:
        return PromptRegistry(
            PromptSpec(item.scene, item.version, item.system_prompt)
            for item in self.prompts
        )


def load_model_runtime_config(path: str | Path) -> ModelRuntimeConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"model runtime config does not exist: {config_path}")
    if config_path.stat().st_size > 1_048_576:
        raise ValueError("model runtime config exceeds 1 MiB")
    try:
        decoded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("model runtime YAML cannot be read") from exc
    return ModelRuntimeConfig.model_validate(decoded)


def validate_runtime_environment(config: ModelRuntimeConfig, *, environment: str) -> None:
    if environment != "e2e" and (
        config.inference.provider == "local" or config.embedding.provider == "local"
    ):
        raise ValueError("local model providers are only allowed in e2e")
