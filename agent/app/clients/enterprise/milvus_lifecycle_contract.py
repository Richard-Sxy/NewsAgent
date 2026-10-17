"""Read-only Milvus lifecycle preflight; never creates or loads collections."""
import asyncio


def _can_omit_field(field):
    """Match PyMilvus full-upsert omission rules, without importing its SDK.

    PyMilvus 2.6 client.abstract.FieldSchema.dict preserves nonempty protobuf
    ValueField defaults; client.prepare.Prepare._parse_upsert_row_request skips
    function outputs and accepts nullable/defaulted input fields. An AutoID flag
    alone is not sufficient for upsert. Normal dynamic metadata is enabled at
    collection level, not an extra input column; the current SDK cannot encode
    an explicitly described dynamic field here. Unknown encodings fail closed.
    """
    if field.get("is_primary") or field.get("auto_id") or field.get("is_dynamic"):
        return False
    if field.get("is_function_output") is True or field.get("nullable") is True:
        return True
    default = field.get("default_value")
    which_oneof = getattr(default, "WhichOneof", None)
    return callable(which_oneof) and which_oneof("data") is not None


async def validate_collections(client, *, collections, expected_dimensions, metric_type, timeout,
                               search_params=None, query_max_candidates=None, rpc=None):
    call = rpc or asyncio.to_thread
    result = {}
    string_fields = {"chunk_id", "tenant_id", "news_id", "embedding_version", "title", "excerpt", "source_url"}
    integer_fields = {"content_version", "chunk_index", "publish_time_epoch"}
    for tier, name in collections.items():
        stage = "describe_collection"
        try:
            description = await call(client.describe_collection,
                collection_name=name, timeout=timeout)
            fields = {field["name"]: field for field in description["fields"]}
            stage = "schema_fields"
            if len(fields) != len(description["fields"]):
                raise ValueError("duplicate schema fields")
            stage = "primary_key"
            if description.get("auto_id") is not False or fields.get("chunk_id", {}).get("is_primary") is not True:
                raise ValueError("chunk_id must be an explicit primary key")
            if any(field.get("is_primary") for key, field in fields.items() if key != "chunk_id"):
                raise ValueError("unexpected primary key")
            # Stable protobuf enum numbers: VARCHAR=21, INT64=5, FLOAT_VECTOR=101.
            for field in string_fields:
                stage = f"field_{field}"
                if int(fields[field]["type"]) != 21 or int(fields[field]["params"]["max_length"]) < 1:
                    raise ValueError("required VARCHAR field is incompatible")
            if int(fields["chunk_id"]["params"]["max_length"]) < 64:
                raise ValueError("chunk_id capacity must fit SHA-256 projection IDs")
            for field in integer_fields:
                stage = f"field_{field}"
                if int(fields[field]["type"]) != 5:
                    raise ValueError("required INT64 field is incompatible")
            stage = "vector_dimension"
            if (int(fields["vector"]["type"]) != 101
                    or int(fields["vector"]["params"]["dim"]) != expected_dimensions):
                raise ValueError("vector type or dimension differs")
            stage = "extra_required_fields"
            supplied = string_fields | integer_fields | {"vector"}
            if any(fields[key].get("is_function_output") for key in supplied):
                raise ValueError("provided fields must not be function-generated")
            if any(not _can_omit_field(field) for key, field in fields.items() if key not in supplied):
                raise ValueError("schema requires an input field absent from lifecycle payload")
            # The lifecycle serializer never sends array-of-struct values and
            # current SDK descriptions do not expose an omission contract here.
            if description.get("struct_array_fields"):
                raise ValueError("struct array fields are unsupported lifecycle inputs")
            stage = "vector_index"
            names = await call(client.list_indexes,
                collection_name=name, field_name="vector", timeout=timeout)
            if len(names) != 1:
                raise ValueError("one vector index is required")
            index = await call(client.describe_index,
                collection_name=name, index_name=names[0], timeout=timeout)
            if index.get("field_name") != "vector" or index.get("metric_type") != metric_type:
                raise ValueError("vector index field or metric differs")
            if index.get("state") != "Finished":
                raise ValueError("vector index must be Finished")
            stage = "hnsw_search_budget"
            if str(index.get("index_type", "")).upper().startswith("HNSW"):
                ef = (search_params or {}).get("ef")
                needed = query_max_candidates if query_max_candidates is not None else 1
                if type(needed) is not int or needed < 1 or type(ef) is not int or ef < needed:
                    raise ValueError("HNSW ef must cover lifecycle maximum refill candidates")
            stage = "load_state"
            loaded = await call(client.get_load_state,
                collection_name=name, timeout=timeout)
            state = loaded.get("state")
            if getattr(state, "name", str(state)) != "Loaded" and state != 3:
                raise ValueError("collection must be Loaded")
            result[tier.value] = {"status": "ready", "dimensions": expected_dimensions,
                "metric_type": metric_type, "index_type": index.get("index_type"),
                "varchar_limits": {key: int(fields[key]["params"]["max_length"]) for key in string_fields}}
        except Exception as exc:
            # SDK messages may contain connection details. Describe the failed
            # tier and error class, keeping original text out of startup logs.
            raise RuntimeError(f"{tier.value} lifecycle preflight {stage} failed ({type(exc).__name__})") from None
    return result
