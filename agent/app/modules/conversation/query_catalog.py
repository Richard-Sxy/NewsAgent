"""Opt-in, versioned context for query understanding; never an execution grant.

The catalog is development material, not reviewed gold or production approval.
Online and offline callers share the payload builder so their inputs can match.
"""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from app.modules.conversation.query_understanding import QueryPolicy, QueryUnderstanding
from app.schemas.sql_assistant import SqlAssistantIntent
from app.sql_assistant.input_boundary import normalize_question


CATALOG_VERSION = "news-query-context-v1"
CATALOG_SHA256 = "c9d29328fa833d4545a99be0b3cc882fe31b4bec9528c170c18ca89fa85a4c09"
CATALOG_PATH = Path(__file__).with_name("query_catalog_v1.json")
MAX_CONTEXT_CHARS = 12000


def _encoded(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def _load_catalog() -> dict:
    with CATALOG_PATH.open("rb") as handle:
        raw = handle.read(32769)
    if len(raw) > 32768 or sha256(raw).hexdigest() != CATALOG_SHA256:
        raise ValueError("query catalog asset identity mismatch")
    catalog = json.loads(raw)
    if catalog["version"] != CATALOG_VERSION:
        raise ValueError("query catalog version mismatch")
    return catalog


def query_context_identity() -> dict:
    """Local source identity; does not inspect model endpoints or credentials."""
    catalog = _load_catalog()
    return {
        "version": CATALOG_VERSION,
        "asset_sha256": CATALOG_SHA256,
        "builder_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "review_status": catalog["review_status"],
        "production_approved": False,
    }


def build_query_context(policy: QueryPolicy, *, max_chars: int = MAX_CONTEXT_CHARS) -> dict:
    """Project development assets through this request's server-owned scope."""
    if type(max_chars) is not int or not 0 < max_chars <= MAX_CONTEXT_CHARS:
        raise ValueError("query context requires a bounded character budget")
    catalog = _load_catalog()
    scenario = policy.scenario
    metrics = {name: catalog["metrics"][name] for name in scenario.allowed_sort_metrics}
    category = scenario.allowed_categories[0] if len(scenario.allowed_categories) == 1 else None
    content = scenario.allowed_content_types[0] if len(scenario.allowed_content_types) == 1 else None
    examples = []
    for template in catalog["example_templates"]:
        kind = template["kind"]
        if kind == "ready" and "project_computed" not in policy.allowed_ranking_sources:
            continue
        fields = {}
        if kind == "ready":
            fields["intent"] = SqlAssistantIntent(
                sort_by=scenario.default_sort, sort_direction="desc",
                category=category, content_type=content, row_limit=policy.default_limit,
                explanation="开发示例：采用当前服务端范围和默认指标。",
            )
        elif kind == "clarify":
            fields["unsupported_conditions"] = ["ambiguous_date_reference"]
        else:
            fields.update(time_basis="publication",
                          unsupported_conditions=["publication_time"])
        proposal = QueryUnderstanding(status=kind, **fields)
        question = template["question"].format(
            category=category or "",
            content={None: "", "article": "图文", "video": "视频"}[content],
            metric=metrics[scenario.default_sort]["label"], limit=policy.default_limit,
        )
        examples.append({
            "id": template["id"], "split": "development",
            "review_status": catalog["review_status"], "question": question,
            "proposal": proposal.model_dump(mode="json"),
        })
    scope = {
        "scenario_id": scenario.id, "result_mode": scenario.result_mode,
        "allowed_sort_metrics": list(scenario.allowed_sort_metrics),
        "allowed_categories": list(scenario.allowed_categories),
        "allowed_content_types": list(scenario.allowed_content_types),
        # The resolver currently only implements project_computed even if an
        # experimental Policy advertises another source.
        "allowed_ranking_sources": [s for s in policy.allowed_ranking_sources
                                    if s == "project_computed"],
        "default_sort": scenario.default_sort, "default_limit": policy.default_limit,
        "max_limit": min(policy.max_limit, scenario.max_limit),
        "time_basis": "behavior", "timezone": policy.timezone,
        "execution_window": "one_complete_hour",
        "cross_hour_uv": "unsupported",
        "publication_time": "unsupported",
        "missing_data": "unavailable_not_zero",
    }
    result = {
        **query_context_identity(),
        "policy_sha256": sha256(_encoded(policy.model_dump(mode="json")).encode()).hexdigest(),
        "capabilities": scope, "metrics": metrics,
        "semantics": catalog["semantics"], "examples": examples,
    }
    result["context_sha256"] = sha256(_encoded(result).encode()).hexdigest()
    if len(_encoded(result)) > max_chars:
        # Never silently drop a safety or time rule to fit an input budget.
        raise ValueError("query context exceeds character budget")
    return result


def build_query_understanding_payload(
    question: str, policy: QueryPolicy, clock: datetime, *,
    query_context_enabled: bool = False,
) -> dict:
    """Use one normalized question, approved Policy and frozen clock everywhere."""
    if type(query_context_enabled) is not bool:
        raise ValueError("query context opt-in must be boolean")
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("query clock must be timezone aware")
    clock = clock.astimezone(ZoneInfo(policy.timezone))
    payload = {
        "question": normalize_question(question), "policy": policy.model_dump(mode="json"),
        "temporal_context": {
            "server_now": clock.isoformat(), "timezone": policy.timezone,
            "date_without_year": "current_year",
            "day_without_month": "current_year_and_month", "hour_convention": "24_hour",
        },
    }
    if query_context_enabled:
        payload["query_context"] = build_query_context(policy)
    return payload