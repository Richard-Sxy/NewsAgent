"""Versioned presentation extraction keeps query permissions and old payloads."""

from pathlib import Path

import pytest

from app.conversation.query_understanding import QueryUnderstanding
from app.model_runtime.config_file import load_model_runtime_config


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
CONFIGS = (
    ("model-runtime.local.yml", "native"),
    ("model-runtime.compatible.example.yml", "compatible"),
)


@pytest.mark.parametrize(("filename", "prefix"), CONFIGS)
def test_query_understanding_binds_v4_and_retains_registered_v1_v2(filename, prefix):
    config = load_model_runtime_config(DEPLOY / filename)
    binding = config.agent_scene("query_understanding")
    assert binding.prompt_version == f"{prefix}-query-understanding-v4"
    assert binding.model_route in config.inference.model_routes
    registry = config.prompt_registry()
    for version in (1, 2, 4):
        prompt = registry.resolve(
            scene="query_understanding", version=f"{prefix}-query-understanding-v{version}"
        )
        assert prompt.scene == "query_understanding"
        assert prompt.version == f"{prefix}-query-understanding-v{version}"
        assert prompt.system_prompt
    # Updating query understanding must not activate another scene's old prompt.
    assert config.agent_scene("conversation").prompt_version == f"{prefix}-conversation-v5"
    assert config.agent_scene("text2sql_assistant").prompt_version == f"{prefix}-text2sql-v1"


@pytest.mark.parametrize(("filename", "prefix"), CONFIGS)
def test_v2_prompt_separates_response_style_from_data_requirements(filename, prefix):
    config = load_model_runtime_config(DEPLOY / filename)
    prompt = config.prompt_registry().resolve(
        scene="query_understanding", version=f"{prefix}-query-understanding-v2"
    ).system_prompt
    assert "response_style只能为list或summary，默认list" in prompt
    for phrase in ("总结", "概括", "梳理", "简要介绍", "呈现请求", "不是数据排除"):
        assert phrase in prompt
    assert "展示不要太长、不用展开" in prompt
    assert "time_expression=today和response_style=summary" in prompt
    assert "日期、来源、阈值、正文过滤及其他数据条件必须完整保留" in prompt
    assert "有歧义输出clarify，未支持条件输出unsupported" in prompt
    assert "unsupported_conditions" in prompt
    assert "不输出SQL、身份、真实窗口" in prompt
    assert "Python核对用户原问和批准策略" in prompt
    assert "不把样本说成今日实时数据" in prompt


def test_v1_shaped_response_remains_parseable_with_list_default():
    # A cached v1 proposal predates response_style. Its data semantics must still
    # parse without pretending the old model requested a summary.
    old_payload = {
        "status": "ready",
        "intent": {"sort_by": "clicks", "row_limit": 5, "explanation": "按点击量排行"},
        "time_expression": "sample",
    }
    proposal = QueryUnderstanding.model_validate(old_payload)
    assert proposal.response_style == "list"
    assert proposal.intent.sort_by == "clicks"
    assert proposal.intent.row_limit == 5
    assert QueryUnderstanding.model_validate_json(proposal.model_dump_json()) == proposal


@pytest.mark.parametrize(("filename", "prefix"), CONFIGS)
def test_v4_prompt_uses_server_clock_and_preserves_natural_time_and_count(filename, prefix):
    config = load_model_runtime_config(DEPLOY / filename)
    prompt = config.prompt_registry().resolve(
        scene="query_understanding", version=f"{prefix}-query-understanding-v4"
    ).system_prompt
    for phrase in ("temporal_context.server_now", "唯一当前时间", "10月9号", "昨天", "早上八点到九点",
                   "只有几号按当前年月", "不能为了命中数据范围", "明确前N条必须保留N", "不输出SQL",
                   "Python会独立核对", "模型输出不是执行授权"):
        assert phrase in prompt
