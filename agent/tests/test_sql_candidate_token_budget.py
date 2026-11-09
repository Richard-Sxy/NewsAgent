"""Per-request model token cap reaches the HTTP contract without changing defaults."""
from dataclasses import replace
import json
import httpx
import pytest
from app.model_runtime.http import OpenAICompatibleInferenceClient
from tests.test_model_runtime_config_and_http import endpoint, inference_request


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,key", [({}, "max_tokens"), ({"max_tokens": 10000}, "max_tokens"),
    ({"max_completion_tokens": 10000}, "max_completion_tokens"),
    ({"max_tokens": 10000, "max_completion_tokens": 10000}, "max_completion_tokens")])
async def test_request_budget_overrides_endpoint_default(extra, key):
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "model-v1", "choices": [{"message": {"content": "{}"}}]})
    config = endpoint(path="chat/completions", model="model-v1").model_copy(update={"extra_body": extra})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAICompatibleInferenceClient(config, api_key="fixture", client=http)
        await client.complete(replace(inference_request(), max_output_tokens=256))
        await client.complete(inference_request())
    assert seen[0][key] == 256
    assert ("max_tokens" if key == "max_completion_tokens" else "max_completion_tokens") not in seen[0]
    assert all(seen[1][name] == value for name, value in extra.items())
    if not extra:
        assert "max_tokens" not in seen[1]


@pytest.mark.parametrize("value", [True, 0, -1, 2.5, 32769])
def test_invalid_per_request_token_budget_rejected(value):
    with pytest.raises(ValueError, match="token limit"):
        replace(inference_request(), max_output_tokens=value)
