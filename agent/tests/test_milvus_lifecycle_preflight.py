"""Read-only startup contracts catch configuration errors before consuming jobs."""
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.clients.enterprise.milvus_vector import MilvusTieredVectorIndex
from app.retrieval.tiered_vector import ChunkVectorRecord, VectorTier


def schema():
    string_fields = ("chunk_id", "tenant_id", "news_id", "embedding_version", "title", "excerpt", "source_url")
    fields = [{"name": key, "type": 21, "params": {"max_length": 65535},
               "is_primary": key == "chunk_id"} for key in string_fields]
    fields += [{"name": key, "type": 5, "params": {}}
               for key in ("content_version", "chunk_index", "publish_time_epoch")]
    fields.append({"name": "vector", "type": 101, "params": {"dim": 2}})
    return {"auto_id": False, "fields": fields}


class Client:
    def __init__(self):
        self.schema = schema()
        self.index = {"field_name": "vector", "metric_type": "IP", "state": "Finished", "index_type": "HNSW"}
        self.loaded = "Loaded"
        self.calls = []
    def describe_collection(self, **kw):
        self.calls.append(("describe_collection", kw))
        return deepcopy(self.schema)
    def list_indexes(self, **kw):
        self.calls.append(("list_indexes", kw))
        return ["vector_idx"]
    def describe_index(self, **kw):
        self.calls.append(("describe_index", kw))
        return dict(self.index)
    def get_load_state(self, **kw):
        self.calls.append(("get_load_state", kw))
        return {"state": self.loaded}
    def upsert(self, **kw):
        self.calls.append(("upsert", kw))
        return {"upsert_count": len(kw["data"])}


def build(client, *, search_params=None):
    return MilvusTieredVectorIndex(client, collections={t: f"test_{t.value}" for t in VectorTier},
        timeout_seconds=2, metric_type="IP", consistency_level="Strong", search_params=search_params)


@pytest.mark.asyncio
@pytest.mark.parametrize("loaded", ["Loaded", 3, SimpleNamespace(name="Loaded")])
async def test_three_tier_preflight_is_read_only_and_passes_timeouts(loaded):
    client = Client()
    client.loaded = loaded
    result = await build(client).validate_collections(expected_dimensions=2)
    assert set(result) == {"hot", "warm", "cold"}
    assert len(client.calls) == 12
    assert all(kw["timeout"] == 2 for _, kw in client.calls)
    assert all(name in {"describe_collection", "list_indexes", "describe_index", "get_load_state"}
               for name, _ in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("issue", ["missing_field", "auto_id", "dimension", "type", "metric", "building", "unloaded", "short_id"])
async def test_incompatible_schema_index_or_load_blocks_startup(issue):
    client = Client()
    if issue == "missing_field": client.schema["fields"].pop(0)
    if issue == "auto_id": client.schema["auto_id"] = True
    if issue == "dimension": client.schema["fields"][-1]["params"]["dim"] = 3
    if issue == "type": client.schema["fields"][2]["type"] = 5
    if issue == "metric": client.index["metric_type"] = "COSINE"
    if issue == "building": client.index["state"] = "InProgress"
    if issue == "unloaded": client.loaded = "NotLoad"
    if issue == "short_id": client.schema["fields"][0]["params"]["max_length"] = 32
    with pytest.raises(RuntimeError, match="hot lifecycle preflight"):
        await build(client).validate_collections(expected_dimensions=2)


@pytest.mark.asyncio
async def test_sdk_exception_text_is_never_propagated():
    client = Client()
    def broken(**kwargs):
        raise RuntimeError("token=private-endpoint-secret")
    client.describe_collection = broken
    with pytest.raises(RuntimeError) as exc:
        await build(client).validate_collections(expected_dimensions=2)
    assert "secret" not in str(exc.value)
    assert "describe_collection" in str(exc.value)


@pytest.mark.asyncio
async def test_utf8_capacity_checked_before_write_using_validated_schema():
    client = Client()
    next(f for f in client.schema["fields"] if f["name"] == "title")["params"]["max_length"] = 4
    index = build(client)
    await index.validate_collections(expected_dimensions=2)
    record = ChunkVectorRecord(chunk_id="a"*64, tenant_id="tenant", news_id="news", content_version=1,
        chunk_index=0, embedding_version="model", title="中文", excerpt="text", source_url=None,
        publish_time=datetime(2026, 10, 16, tzinfo=UTC), vector=(1., 0.))
    with pytest.raises(ValueError, match="capacity"):
        await index.upsert(tier=VectorTier.HOT, records=[record])
    assert not any(name == "upsert" for name, _ in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"ef": None}, {"ef": True}, {"ef": "96"}, {"ef": 5.0}, {"ef": 4}])
async def test_actual_hnsw_index_requires_integer_ef_covering_largest_refill(params):
    with pytest.raises(RuntimeError, match="hnsw_search_budget"):
        await build(Client(), search_params=params).validate_collections(
            expected_dimensions=2, query_max_candidates=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("index_type", ["HNSW", "HNSW_SQ", "HNSW_PQ"])
async def test_hnsw_family_accepts_ef_at_maximum_candidate_boundary(index_type):
    client = Client()
    client.index["index_type"] = index_type
    result = await build(client, search_params={"ef": 5}).validate_collections(
        expected_dimensions=2, query_max_candidates=5)
    assert result["hot"]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("index_type", ["IVF_FLAT", "AUTOINDEX"])
async def test_non_hnsw_indexes_do_not_require_ef(index_type):
    client = Client()
    client.index["index_type"] = index_type
    assert await build(client, search_params={"nprobe": 8}).validate_collections(
        expected_dimensions=2, query_max_candidates=80)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"name": "required_source", "type": 21, "params": {"max_length": 100}},
    {"name": "count", "type": 5, "nullable": False},
    {"name": "count", "type": 5, "default_value": None},
    {"name": "count", "type": 5, "default_value": {}},
    {"name": "count", "type": 5, "default_value": 0},
    {"name": "count", "type": 5, "nullable": "true"},
    {"name": "other_id", "type": 5, "auto_id": True},
    {"name": "arbitrary", "type": 5, "is_dynamic": True},
    {"name": "$meta", "type": 23, "is_dynamic": True},
])
async def test_extra_required_or_ambiguous_fields_are_rejected_before_any_write(extra):
    client = Client()
    client.schema["fields"].append(extra)
    # Dynamic-field support does not make explicit required columns optional.
    client.schema["enable_dynamic_field"] = True
    with pytest.raises(RuntimeError, match="extra_required_fields"):
        await build(client).validate_collections(expected_dimensions=2)
    assert [method for method, _ in client.calls] == ["describe_collection"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"name": "optional_note", "type": 21, "nullable": True, "params": {"max_length": 100}},
    {"name": "generated_sparse", "type": 104, "is_function_output": True},
])
async def test_nullable_and_server_generated_extra_fields_can_be_omitted(extra):
    client = Client()
    client.schema["fields"].append(extra)
    assert (await build(client).validate_collections(expected_dimensions=2))["hot"]["status"] == "ready"


@pytest.mark.asyncio
async def test_collection_dynamic_metadata_without_an_explicit_required_column_is_allowed():
    client = Client()
    client.schema["enable_dynamic_field"] = True
    assert (await build(client).validate_collections(expected_dimensions=2))["hot"]["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,value,field_type", [
    ("long_data", 0, 5), ("bool_data", False, 1), ("string_data", "", 21),
])
async def test_real_sdk_default_encodings_allow_zero_false_and_empty_string(kind, value, field_type):
    pytest.importorskip("pymilvus")
    from pymilvus.client.abstract import FieldSchema
    from pymilvus.grpc_gen import schema_pb2
    # describe_collection returns a protobuf ValueField, including false/zero.
    raw = schema_pb2.FieldSchema(name="with_default", data_type=field_type,
        default_value=schema_pb2.ValueField(**{kind: value}))
    client = Client()
    client.schema["fields"].append(FieldSchema(raw).dict())
    assert (await build(client).validate_collections(expected_dimensions=2))["hot"]["status"] == "ready"


@pytest.mark.asyncio
async def test_real_sdk_empty_default_is_not_a_default_and_provided_function_output_is_rejected():
    pytest.importorskip("pymilvus")
    from pymilvus.client.abstract import FieldSchema
    from pymilvus.grpc_gen import schema_pb2
    client = Client()
    raw = schema_pb2.FieldSchema(name="no_default", data_type=5, default_value=schema_pb2.ValueField())
    client.schema["fields"].append(FieldSchema(raw).dict())
    with pytest.raises(RuntimeError, match="extra_required_fields"):
        await build(client).validate_collections(expected_dimensions=2)
    client = Client()
    client.schema["fields"][-1]["is_function_output"] = True
    with pytest.raises(RuntimeError, match="extra_required_fields"):
        await build(client).validate_collections(expected_dimensions=2)


@pytest.mark.asyncio
async def test_unmapped_struct_array_field_is_rejected_conservatively():
    client = Client()
    client.schema["struct_array_fields"] = [{"name": "sections", "fields": []}]
    with pytest.raises(RuntimeError, match="extra_required_fields"):
        await build(client).validate_collections(expected_dimensions=2)
