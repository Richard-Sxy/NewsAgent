"""
聚合数据接口验收。调用 NewsMetricSource 一次，检查指标结构、时间窗口、数据范围，生成带证据哈希的验收报告。用于管理员或离线诊断。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
import hashlib
import json
import math
from typing import Literal
from uuid import UUID

from app.analytics.entities import ContentType
from app.analytics.metric_source import HotNewsMetricQuery, NewsMetricSource
from app.analytics.metrics import BASE_METRIC_KEYS, NewsMetricSnapshot


AcceptanceMode = Literal["synthetic_fixture", "adapter_contract"]
SCHEMA_VERSION = "1.0"
PORT_CONTRACT_VERSION = "NewsMetricSource.fetch_snapshots/v1"
MAX_ROWS = 1_000
MAX_INTEGER = 2**63 - 1
CTR_TOLERANCE = Decimal("0.00005")
_CONTEXT = Context(prec=64, rounding=ROUND_HALF_EVEN)
_MODES = frozenset({"synthetic_fixture", "adapter_contract"})
_ROW_ERROR_CODES = frozenset({
    "window_invalid", "aggregate_list_required", "aggregate_row_budget_exceeded",
    "aggregate_snapshot_type_required", "aggregate_news_identity_invalid",
    "aggregate_content_type_scope_mismatch", "aggregate_window_scope_mismatch",
    "aggregate_identity_duplicate", "aggregate_count_invalid", "aggregate_ctr_invalid",
    "aggregate_ctr_inconsistent",
})
_LIMITATIONS = (
    "Only the aggregate response contract for this one request was checked; production acceptance is not established.",
    "NewsMetricSnapshot has no response tenant field. Forwarding tenant_id does not prove tenant isolation; an enterprise gateway/IDL and independent two-tenant fixtures are required.",
    "UV is not summed across news or windows. No raw user/event records, news identifiers or metric values are retained in this report.",
    "Timeout cancellation is cooperative; the approved adapter must enforce its own transport deadline. No automatic retry is performed.",
)


class AggregateContractError(ValueError):
    """Contains only a fixed, non-sensitive diagnostic code."""


def _utc(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise AggregateContractError("window_invalid")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AcceptanceRequest:
    """Trusted administrator scope and bounded validation settings."""

    tenant_id: str
    window_start: datetime
    window_end: datetime
    content_types: frozenset[ContentType] = frozenset()
    max_rows: int = 200
    timeout_seconds: float = 3.0
    mode: AcceptanceMode = "synthetic_fixture"

    def validate(self) -> None:
        if type(self.tenant_id) is not str or len(self.tenant_id) != 36:
            raise AggregateContractError("tenant_scope_invalid")
        try:
            UUID(self.tenant_id)
        except ValueError as exc:
            raise AggregateContractError("tenant_scope_invalid") from exc
        if _utc(self.window_start) >= _utc(self.window_end):
            raise AggregateContractError("window_invalid")
        if type(self.content_types) is not frozenset or any(
            type(item) is not ContentType for item in self.content_types
        ):
            raise AggregateContractError("content_type_scope_invalid")
        if type(self.max_rows) is not int or not 1 <= self.max_rows <= MAX_ROWS:
            raise AggregateContractError("row_budget_invalid")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not 0.1 <= self.timeout_seconds <= 30
            or not math.isfinite(self.timeout_seconds)
        ):
            raise AggregateContractError("timeout_budget_invalid")
        if type(self.mode) is not str or self.mode not in _MODES:
            raise AggregateContractError("acceptance_mode_invalid")

    def to_query(self) -> HotNewsMetricQuery:
        self.validate()
        return HotNewsMetricQuery(
            tenant_id=self.tenant_id,
            window_start=self.window_start,
            window_end=self.window_end,
            content_types=self.content_types,
            ranking_limit=self.max_rows,
        )


def _json_sha256(value: dict) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _validate_rows(rows: object, request: AcceptanceRequest) -> list[dict]:
    # Exact types deliberately reject raw events, dictionaries, and subclasses
    # carrying extra user fields. Never call str()/repr() on a rejected object.
    if type(rows) is not list:
        raise AggregateContractError("aggregate_list_required")
    if len(rows) > request.max_rows:
        raise AggregateContractError("aggregate_row_budget_exceeded")
    scope_types = request.content_types or frozenset(ContentType)
    seen: set[tuple[str, ContentType]] = set()
    canonical_rows = []
    start, end = _utc(request.window_start), _utc(request.window_end)
    for row in rows:
        if type(row) is not NewsMetricSnapshot:
            raise AggregateContractError("aggregate_snapshot_type_required")
        if (
            type(row.news_id) is not str or not row.news_id.strip() or len(row.news_id) > 128
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in row.news_id)
        ):
            raise AggregateContractError("aggregate_news_identity_invalid")
        if type(row.content_type) is not ContentType or row.content_type not in scope_types:
            raise AggregateContractError("aggregate_content_type_scope_mismatch")
        if _utc(row.window_start) != start or _utc(row.window_end) != end:
            raise AggregateContractError("aggregate_window_scope_mismatch")
        identity = (row.news_id, row.content_type)
        if identity in seen:
            raise AggregateContractError("aggregate_identity_duplicate")
        seen.add(identity)
        canonical = {"news_id": row.news_id, "content_type": row.content_type.value}
        for field in sorted(BASE_METRIC_KEYS):
            value = getattr(row, field)
            if type(value) is not int or not 0 <= value <= MAX_INTEGER:
                raise AggregateContractError("aggregate_count_invalid")
            canonical[field] = value
        if type(row.ctr) is not Decimal or not row.ctr.is_finite() or row.ctr < 0:
            raise AggregateContractError("aggregate_ctr_invalid")
        digits = row.ctr.as_tuple()
        if len(digits.digits) > 64 or not -64 <= digits.exponent <= 20 or row.ctr > MAX_INTEGER:
            raise AggregateContractError("aggregate_ctr_invalid")
        with localcontext(_CONTEXT):
            expected_ctr = Decimal(row.clicks) / Decimal(row.impressions) if row.impressions else Decimal(0)
            if abs(row.ctr - expected_ctr) > CTR_TOLERANCE:
                raise AggregateContractError("aggregate_ctr_inconsistent")
        canonical["ctr"] = _decimal_text(row.ctr)
        canonical_rows.append(canonical)
    canonical_rows.sort(key=lambda row: (row["news_id"], row["content_type"]))
    return canonical_rows


def _base_report(mode: object) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "port_contract_version": PORT_CONTRACT_VERSION,
        "acceptance_kind": mode if type(mode) is str and mode in _MODES else "invalid",
        "status": "contract_failed",
        "counts": {"returned_row_count": None, "validated_row_count": 0, "content_type_rows": {}},
        "checks": [],
        "evidence": {"tenant_scope_sha256": None, "aggregate_snapshot_sha256": None},
        "limitations": list(_LIMITATIONS),
    }


async def run_aggregate_acceptance(
    source: NewsMetricSource,
    query: HotNewsMetricQuery,
    *,
    max_rows: int = 200,
    timeout_seconds: float = 3.0,
    mode: AcceptanceMode = "synthetic_fixture",
) -> dict:
    """Fetch once, validate aggregate data, and return a JSON-safe evidence report.

    Source exception messages and rejected values never enter the report. The
    source is dependency-injected by trusted administrator code, not a model.
    Empty responses satisfy shape validation but are explicitly non-conclusive.
    """
    report = _base_report(mode)
    try:
        if type(query) is not HotNewsMetricQuery:
            raise AggregateContractError("query_type_invalid")
        request = AcceptanceRequest(
            tenant_id=query.tenant_id, window_start=query.window_start,
            window_end=query.window_end, content_types=query.content_types,
            max_rows=max_rows, timeout_seconds=timeout_seconds, mode=mode,
        )
        request.validate()
        query.validate()
        if (
            type(query.ranking_limit) is not int or not 1 <= query.ranking_limit <= MAX_ROWS
            or type(query.requested_metrics) is not frozenset
            or not query.requested_metrics <= (BASE_METRIC_KEYS | {"ctr"})
        ):
            raise AggregateContractError("query_metric_scope_invalid")
    except Exception:
        report["checks"].append({"name": "request_scope", "status": "failed", "code": "request_invalid"})
        return report

    report["checks"].append({"name": "request_scope", "status": "passed", "code": None})
    tenant_digest = _json_sha256({"namespace": "newsagent.aggregate-acceptance.tenant.v1", "tenant_id": str(UUID(request.tenant_id))})
    report["evidence"] = {
        "tenant_scope_sha256": tenant_digest,
        "aggregate_snapshot_sha256": None,
        "window_start": _utc(request.window_start).isoformat(),
        "window_end": _utc(request.window_end).isoformat(),
        "content_types": sorted(item.value for item in request.content_types or frozenset(ContentType)),
    }
    try:
        rows = await asyncio.wait_for(source.fetch_snapshots(query), timeout=request.timeout_seconds)
    except TimeoutError:
        report["checks"].append({"name": "source_fetch", "status": "failed", "code": "source_timeout"})
        return report
    except Exception:
        report["checks"].append({"name": "source_fetch", "status": "failed", "code": "source_failure"})
        return report
    report["checks"].append({"name": "source_fetch", "status": "passed", "code": None})
    if type(rows) is list:
        report["counts"]["returned_row_count"] = len(rows)
    try:
        canonical_rows = _validate_rows(rows, request)
    except AggregateContractError as exc:
        # A response datetime can carry custom tzinfo code. Never echo its
        # exception even if it happens to use our exception class.
        code = exc.args[0] if type(exc) is AggregateContractError and len(exc.args) == 1 else None
        if type(code) is not str or code not in _ROW_ERROR_CODES:
            code = "aggregate_validation_failed"
        report["checks"].append({"name": "aggregate_contract", "status": "failed", "code": code})
        return report
    except Exception:
        report["checks"].append({"name": "aggregate_contract", "status": "failed", "code": "aggregate_validation_failed"})
        return report

    report["counts"]["validated_row_count"] = len(canonical_rows)
    report["counts"]["content_type_rows"] = {
        content_type.value: sum(row["content_type"] == content_type.value for row in canonical_rows)
        for content_type in sorted(request.content_types or frozenset(ContentType), key=lambda item: item.value)
    }
    report["evidence"]["aggregate_snapshot_sha256"] = _json_sha256({
        "port_contract_version": PORT_CONTRACT_VERSION,
        "scope": report["evidence"], "rows": canonical_rows,
    })
    report["checks"].extend([
        {"name": "aggregate_contract", "status": "passed", "code": None},
        {"name": "tenant_isolation", "status": "not_verified", "code": "response_has_no_tenant_field"},
    ])
    report["status"] = "local_contract_passed" if mode == "synthetic_fixture" else "contract_passed"
    if not canonical_rows:
        report["limitations"].append("The response was empty; metric, identity and tenant behavior for actual rows remains unverified.")
    if mode == "synthetic_fixture":
        report["limitations"].append("The built-in synthetic fixture is local control-flow evidence; no enterprise endpoint was contacted.")
    return report
