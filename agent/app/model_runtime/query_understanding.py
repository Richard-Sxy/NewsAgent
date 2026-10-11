"""Use an existing inference endpoint with the application's approved query prompt."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from app.model_runtime.agent_client import NativeStructuredAgentClient
from app.model_runtime.config_file import AgentSceneConfig, EndpointConfig, ModelRuntimeConfig
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import ModelRuntimePorts, build_model_runtime_ports


def query_understanding_config(
    *, prompts_config: ModelRuntimeConfig, endpoint_config: ModelRuntimeConfig,
) -> ModelRuntimeConfig:
    """Keep connection credentials external; do not inherit unrelated scene prompts."""
    endpoint_binding = endpoint_config.agent_scene("query_understanding")
    return _project_query_config(prompts_config=prompts_config, inference=endpoint_config.inference,
                                 model_route=endpoint_binding.model_route)


def _project_query_config(
    *, prompts_config: ModelRuntimeConfig, inference: EndpointConfig, model_route: str,
) -> ModelRuntimeConfig:
    prompt_binding = prompts_config.agent_scene("query_understanding")
    prompt = next(p for p in prompts_config.prompts
                  if p.scene == "query_understanding" and p.version == prompt_binding.prompt_version)
    return ModelRuntimeConfig(
        schema_version=1,
        inference=inference,
        # Query understanding never calls embedding; retain the already approved
        # local embedding Port without adopting another connection's embedding.
        embedding=prompts_config.embedding,
        prompts=(prompt,),
        agent_scenes=(AgentSceneConfig(scene="query_understanding", prompt_version=prompt.version,
                                      model_route=model_route),),
    )


def load_query_understanding_endpoint_config(
    path: str | Path, *, prompts_config: ModelRuntimeConfig,
) -> ModelRuntimeConfig:
    """Project only the configured HTTP endpoint and its unique query route.

    Endpoint files may contain unrelated legacy prompt registrations. They are
    not part of this client's approval boundary and are never inherited. Actual
    credentials remain in the environment and are checked when building Ports.
    """
    config_path = Path(path)
    limit = 1_048_576
    if not config_path.is_file():
        raise ValueError("query understanding endpoint YAML cannot be read")
    try:
        with config_path.open("rb") as handle:
            contents = handle.read(limit + 1)
    except OSError:
        raise ValueError("query understanding endpoint YAML cannot be read") from None
    if len(contents) > limit:
        raise ValueError("query understanding endpoint YAML exceeds 1 MiB")
    try:
        decoded = yaml.safe_load(contents.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError):
        raise ValueError("query understanding endpoint YAML cannot be read") from None
    if not isinstance(decoded, dict) or type(decoded.get("schema_version")) is not int or decoded["schema_version"] != 1:
        raise ValueError("query understanding endpoint requires schema_version 1")
    scenes = decoded.get("agent_scenes")
    if not isinstance(scenes, list):
        raise ValueError("query understanding endpoint requires exactly one query binding")
    bindings = [item for item in scenes if isinstance(item, dict) and item.get("scene") == "query_understanding"]
    if len(bindings) != 1:
        raise ValueError("query understanding endpoint requires exactly one query binding")
    try:
        inference = EndpointConfig.model_validate(decoded.get("inference"))
        binding = AgentSceneConfig.model_validate(bindings[0])
    except ValidationError:
        # Validation errors include input values; never reflect private config.
        raise ValueError("query understanding endpoint or binding is invalid") from None
    if inference.provider != "openai_compatible":
        raise ValueError("query understanding endpoint requires openai_compatible inference")
    if not binding.prompt_version.strip():
        raise ValueError("query understanding endpoint binding version is invalid")
    if binding.model_route not in inference.model_routes:
        raise ValueError("query understanding endpoint model route is not approved")
    return _project_query_config(prompts_config=prompts_config, inference=inference,
                                 model_route=binding.model_route)


def build_query_understanding_client(
    *, prompts_config: ModelRuntimeConfig, endpoint_config: ModelRuntimeConfig, environment: str,
) -> tuple[NativeStructuredAgentClient, ModelRuntimePorts, dict]:
    config = query_understanding_config(prompts_config=prompts_config, endpoint_config=endpoint_config)
    ports = build_model_runtime_ports(config, environment=environment)
    client = NativeStructuredAgentClient(
        StructuredInferenceService(inference=ports.inference, prompts=ports.prompts), config,
        timeout_seconds=config.inference.timeout_seconds,
    )
    metadata = {"provider": config.inference.provider,
                "model_route": config.agent_scene("query_understanding").model_route,
                "prompt_version": config.agent_scene("query_understanding").prompt_version}
    return client, ports, metadata
