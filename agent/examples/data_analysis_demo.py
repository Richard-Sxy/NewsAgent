"""Prepare and verify six analysis operations through the isolated local HTTP API.

Run from agent/: python -m examples.data_analysis_demo [--prepare-only].
Only published synthetic demo credentials are used. No .env, database write,
enterprise endpoint, model configuration change or automatic submit retry occurs.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from ipaddress import ip_address
import json
import re
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from app.data_analysis.engine import validate_request
from app.data_analysis.projection import project_dataset
from app.data_analysis.runner import AnalysisRunner, _json_bytes, _reject_constant, _unique_object
from app.schemas.conversation import ConversationTurnView, ConversationView
from app.schemas.hot_news_api import HotNewsRunDetailResponse


DEFAULT_BASE_URL = "http://127.0.0.1:28010"
DEMO_TENANT = "11111111-1111-4111-8111-111111111111"
DEMO_USER = "22222222-2222-4222-8222-222222222222"
DEMO_HEADERS = {
    "Authorization": "Bearer newsagent-native-local-token-not-for-production",
    "X-Tenant-ID": DEMO_TENANT,
    "X-User-ID": DEMO_USER,
    "X-Hot-News-Roles": "hot-news:read,hot-news:admin",
}
QUESTION = "查询点击量最高的前12条新闻"
REFERENCE_START = "2026-10-03T22:00:00+08:00"
CURRENT_START = "2026-10-03T23:00:00+08:00"
CURRENT_END = "2026-10-04T00:00:00+08:00"
OPERATIONS = (
    ("overview", "ctr", "统计当前热点指标概况"),
    ("distribution", "ctr", "统计点击率分布"),
    ("compare", "impressions", "比较曝光最高与最低新闻"),
    ("quality", "ctr", "统计热点数据质量检查"),
    ("baseline", "clicks", "比较当前热点点击量与基线"),
    ("trend", "ctr", "统计两个窗口点击率趋势"),
)
_MAX_RESPONSE_BYTES = 1_048_576
_HTTP_TIMEOUT_SECONDS = 150
_TOTAL_TIMEOUT_SECONDS = 900
_SAMPLE_START = datetime.fromisoformat("2026-10-03T00:00:00+08:00")
_SAMPLE_END = datetime.fromisoformat("2026-10-04T00:00:00+08:00")
_SCENARIO_FIELDS = frozenset({"id", "label", "description", "question", "reference_start",
                              "current_start", "current_end", "row_limit", "expected_signals"})
_SCALE_FIELDS = ("news_per_tenant", "news_count", "metric_row_count", "baseline_row_count",
                 "total_tenants", "total_news_count", "total_metric_row_count",
                 "total_baseline_row_count", "hours_per_news")
_SCALED_SCENARIO_IDS = frozenset({"steady", "breaking", "fatigue", "funnel", "content-mix",
                                "low-volume", "zero-baseline", "ranking-churn", "recovery-gap", "precision"})


class DemoError(ValueError):
    """Safe fixed diagnostics, without transport bodies or credential values."""


@dataclass(frozen=True)
class DemoScenario:
    id: str
    label: str
    description: str
    question: str
    reference_start: str
    current_start: str
    current_end: str
    row_limit: int
    expected_signals: list[str]

    @property
    def reference_end(self) -> str:
        return (datetime.fromisoformat(self.reference_start) + timedelta(hours=1)).isoformat()

    def report(self) -> dict:
        return asdict(self)


def _catalog(config: dict) -> tuple[dict, tuple[DemoScenario, ...]]:
    """Validate the approved synthetic catalog before any workflow submission.

    Catalog text is metadata, never code or a request to alter permissions. The
    selected question is used unchanged in both SQL runs; their saved traces
    must still pass the ordinary projection and trend provenance contract.
    """
    dataset = config.get("dataset")
    if (type(dataset) is not dict or type(dataset.get("dataset_profile")) is not str
            or dataset["dataset_profile"] not in {"enterprise-v1", "enterprise-v2", "public-headlines-v3", "timeline-v4"}):
        raise DemoError("demo_requires_enterprise_dataset_profile")
    try:
        public = dataset["dataset_profile"] == "public-headlines-v3"
        scaled = dataset["dataset_profile"] in {"enterprise-v2", "public-headlines-v3", "timeline-v4"}
        timeline = dataset["dataset_profile"] == "timeline-v4"
        hours = 720 if timeline else 24
        sample_start = datetime.fromisoformat("2026-09-10T00:00:00+08:00") if timeline else _SAMPLE_START
        sample_end = datetime.fromisoformat("2026-10-10T00:00:00+08:00") if timeline else _SAMPLE_END
        if timeline and (dataset.get("window_start") != sample_start.isoformat() or dataset.get("window_end") != sample_end.isoformat()):
            raise ValueError()
        version, digest = dataset["dataset_version"], dataset["dataset_sha256"]
        if (type(version) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", version)
                or type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError()
        identity = {key: dataset[key] for key in ("dataset_profile", "dataset_version", "dataset_sha256")}
        if scaled:
            news_count = dataset["news_per_tenant"]
            if type(news_count) is not int or news_count not in ({120, 1200} if public else {120, 1200, 12000}):
                raise ValueError()
            counts = {"news_per_tenant": news_count, "news_count": news_count,
                      "metric_row_count": news_count * hours, "baseline_row_count": news_count * hours,
                      "total_tenants": 2, "total_news_count": news_count * 2,
                      "total_metric_row_count": news_count * hours * 2, "total_baseline_row_count": news_count * hours * 2,
                      "hours_per_news": hours}
            if any(type(dataset.get(key)) is not int or dataset[key] != value for key, value in counts.items()):
                raise ValueError()
            identity.update(counts)
            schema, schema_digest = dataset["schema_version"], dataset["schema_sha256"]
            if (schema != ("news-warehouse-v4" if timeline else "news-warehouse-v3" if public else "news-warehouse-v2") or type(schema_digest) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", schema_digest)
                    or config.get("schema_version") != schema or config.get("schema_sha256") != schema_digest):
                raise ValueError()
            identity.update(schema_version=schema, schema_sha256=schema_digest)
            if public:
                catalog_count = dataset["headline_catalog_count"]
                catalog_sha256 = dataset["headline_catalog_sha256"]
                if (type(catalog_count) is not int or not news_count <= catalog_count <= 1200
                        or type(catalog_sha256) is not str or not re.fullmatch(r"[0-9a-f]{64}", catalog_sha256)):
                    raise ValueError()
                identity.update(headline_catalog_count=catalog_count, headline_catalog_sha256=catalog_sha256)
                for key in ("headline_date_start", "headline_date_end"):
                    value = dataset[key]
                    if type(value) is not str or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
                        raise ValueError()
                    datetime.strptime(value, "%Y-%m-%d")
                    identity[key] = value
                if datetime.fromisoformat(identity["headline_date_start"]) > datetime.fromisoformat(identity["headline_date_end"]):
                    raise ValueError()
        elif "schema_version" in config or "schema_sha256" in config:
            schema, schema_digest = config.get("schema_version"), config.get("schema_sha256")
            if (schema != "news-warehouse-v1" or type(schema_digest) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", schema_digest)):
                raise ValueError()
            identity.update(schema_version=schema, schema_sha256=schema_digest)
        if not scaled:
            # Old v1 APIs did not publish the complete scale manifest.
            expected_counts = {"news_per_tenant": 12, "news_count": 12, "metric_row_count": 288,
                               "baseline_row_count": 288, "total_tenants": 2, "total_news_count": 24,
                               "total_metric_row_count": 576, "total_baseline_row_count": 576, "hours_per_news": hours}
            for key in _SCALE_FIELDS:
                if key in dataset:
                    if type(dataset[key]) is not int or dataset[key] != expected_counts[key]:
                        raise ValueError()
                    identity[key] = dataset[key]
        entries = dataset["enterprise_scenarios"]
        if type(entries) is not list or not 1 <= len(entries) <= 24:
            raise ValueError()
        scenarios, ids = [], set()
        for entry in entries:
            if type(entry) is not dict or set(entry) != _SCENARIO_FIELDS:
                raise ValueError()
            identifier = entry["id"]
            if (type(identifier) is not str or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", identifier)
                    or identifier in ids):
                raise ValueError()
            ids.add(identifier)
            for field, limit in (("label", 120), ("description", 2000), ("question", 500)):
                value = entry[field]
                if (type(value) is not str or not 1 <= len(value) <= limit
                        or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                    raise ValueError()
            dates = []
            for field in ("reference_start", "current_start", "current_end"):
                value = entry[field]
                if type(value) is not str or len(value) > 40:
                    raise ValueError()
                timestamp = datetime.fromisoformat(value)
                if (timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(hours=8)
                        or timestamp.minute or timestamp.second or timestamp.microsecond):
                    raise ValueError()
                dates.append(timestamp)
            reference, current, end = dates
            if (not sample_start <= reference or end > sample_end
                    or current - reference < timedelta(hours=1) or end - current != timedelta(hours=1)):
                raise ValueError()
            if (type(entry["row_limit"]) is not int
                    or (entry["row_limit"] != 100 if scaled else not 1 <= entry["row_limit"] <= 12)):
                raise ValueError()
            if entry["question"] != f"查询点击量最高的前{entry['row_limit']}条新闻":
                raise ValueError()
            expected = entry["expected_signals"]
            if (type(expected) is not list or len(expected) > 24
                    or any(type(value) is not str or not 1 <= len(value) <= 500
                           or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value)
                           for value in expected)):
                raise ValueError()
            scenarios.append(DemoScenario(**entry))
        expected_ids = _SCALED_SCENARIO_IDS | {"day-over-day", "week-over-week", "month-span"} if timeline else _SCALED_SCENARIO_IDS
        if scaled and ids != expected_ids:
            raise ValueError()
        return identity, tuple(scenarios)
    except Exception:
        raise DemoError("demo_enterprise_catalog_invalid") from None


def approved_base_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            type(value) is str and "\\" not in value
            and not any(character.isspace() or ord(character) < 33 for character in value)
            and parsed.scheme == "http" and parsed.hostname
            and ip_address(parsed.hostname).is_loopback
            and parsed.username is None and parsed.password is None
            and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
            and "?" not in value and "#" not in value
            and parsed.port is not None and 1 <= parsed.port <= 65535
        )
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise DemoError("demo_requires_explicit_loopback_http_api")
    return value.rstrip("/")


class DataAnalysisDemo:
    def __init__(self, *, base_url: str = DEFAULT_BASE_URL, client: httpx.AsyncClient | None = None):
        self.base_url = approved_base_url(base_url)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                                  timeout=_HTTP_TIMEOUT_SECONDS)
        self._validator = AnalysisRunner()

    async def close(self):
        if self._owns_client:
            await self._client.aclose()

    async def _json(self, method: str, path: str, *, body: dict | None = None, status: int = 200) -> dict:
        try:
            async with self._client.stream(
                method, self.base_url + path, json=body, headers=DEMO_HEADERS,
                timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False,
            ) as response:
                if response.status_code != status:
                    # Submissions are never retried, nor changed to bypass 409.
                    raise DemoError(f"demo_http_status_{response.status_code}")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > _MAX_RESPONSE_BYTES:
                        raise DemoError("demo_response_limit")
                result = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                                    object_pairs_hook=_unique_object)
                if type(result) is not dict:
                    raise DemoError("demo_response_invalid")
                return result
        except DemoError:
            raise
        except Exception:
            raise DemoError("demo_transport_or_response_invalid") from None

    async def _prepare_run(self, start: str, end: str, *, question: str = QUESTION,
                           row_limit: int = 12, identity: dict | None = None) -> HotNewsRunDetailResponse:
        response = await self._json("POST", "/api/v1/local-simulation/hot-news/run", body={
            "question": question, "scenario_id": "news-ranking",
            "window_start": start, "window_end": end,
        })
        try:
            run_id = str(UUID(response["run_id"]))
            detail = HotNewsRunDetailResponse.model_validate(await self._json(
                "GET", f"/api/v1/hot-news/runs/{run_id}",
            ))
            if (
                response["status"] != "completed" or detail.run.status != "completed"
                or str(detail.run.run_id) != run_id
                or detail.run.window_start != datetime.fromisoformat(start)
                or detail.run.window_end != datetime.fromisoformat(end)
                or detail.run.production_bundle_version != response["production_bundle_version"]
                or detail.run.idempotency_key != response["workflow_id"]
                or detail.sql_tool_trace is None
                or detail.sql_tool_trace.query_id != response["sql_query_id"]
                or detail.sql_tool_trace.preview.scenario_id != "news-ranking"
                or detail.sql_tool_trace.preview.question != question
                or detail.sql_tool_trace.preview.parameters["row_limit"] != row_limit
                or detail.run.ranked_news_count > row_limit
                or detail.run.ranked_news_count != response["ranked_news_count"]
                or detail.run.analyzed_news_count != response["analyzed_news_count"]
                or identity is not None and "schema_version" in identity and (
                    detail.sql_tool_trace.preview.schema_version != identity["schema_version"]
                    or detail.sql_tool_trace.preview.schema_sha256 != identity["schema_sha256"])
            ):
                raise DemoError("demo_run_binding_mismatch")
            return detail
        except DemoError:
            raise
        except Exception:
            raise DemoError("demo_run_snapshot_invalid") from None

    def _request(self, current, reference, operation, metric):
        extended = operation in {"baseline", "trend"}
        request = {"schema_version": "2.0" if extended else "1.0", "operation": operation,
                   "metric": metric, "dataset": project_dataset(
                       current, run_id=str(current.run.run_id), tenant_id=DEMO_TENANT,
                       extended=extended, include_baseline=operation == "baseline",
                   )}
        if operation == "trend":
            request["reference_dataset"] = project_dataset(
                reference, run_id=str(reference.run.run_id), tenant_id=DEMO_TENANT, extended=True,
            )
        return validate_request(request)

    async def _send(self, conversation_id: str, content: str) -> ConversationTurnView:
        request_id = uuid4()
        response = await self._json("POST", f"/api/v1/conversations/{conversation_id}/messages", body={
            "request_id": str(request_id), "content": content,
        })
        try:
            turn = ConversationTurnView.model_validate(response)
            if turn.request_id != request_id or turn.user_content != content or turn.status != "completed":
                raise DemoError("demo_conversation_turn_failed")
            return turn
        except DemoError:
            raise
        except Exception:
            raise DemoError("demo_conversation_turn_invalid") from None

    async def list_scenarios(self) -> dict:
        """Only GET configuration; never create a workflow or conversation."""
        identity, scenarios = _catalog(await self._json("GET", "/api/v1/local-simulation/hot-news/sql-config"))
        return {"status": "listed", "synthetic": True, "dataset": identity,
                "scenarios": [scenario.report() for scenario in scenarios]}

    async def run(self, *, prepare_only: bool = False, scenario_id: str | None = None) -> dict:
        try:
            async with asyncio.timeout(_TOTAL_TIMEOUT_SECONDS):
                runtime = await self._json("GET", "/api/v1/conversations/runtime")
                analysis = runtime.get("data_analysis")
                if (runtime.get("model_provider") != "local" or runtime.get("query_enabled") is not True
                        or type(analysis) is not dict
                        or analysis.get("name") != "analyze_hot_news_data"
                        or analysis.get("execution_backend") not in {"process", "docker", "service"}
                        or not {operation for operation, _metric, _message in OPERATIONS}
                        <= set(analysis.get("arguments", {}).get("operation", "").split("|"))):
                    raise DemoError("demo_requires_local_query_and_analysis_runtime")
                selected, identity = None, None
                if scenario_id is not None:
                    identity, scenarios = _catalog(await self._json("GET", "/api/v1/local-simulation/hot-news/sql-config"))
                    selected = next((item for item in scenarios if item.id == scenario_id), None)
                    if selected is None:
                        raise DemoError("demo_scenario_unknown")
                    refreshed_identity, refreshed_scenarios = _catalog(await self._json(
                        "GET", "/api/v1/local-simulation/hot-news/sql-config"))
                    refreshed = next((item for item in refreshed_scenarios if item.id == scenario_id), None)
                    if refreshed_identity != identity or refreshed != selected:
                        raise DemoError("demo_scenario_dataset_changed")
                question = QUESTION if selected is None else selected.question
                row_limit = 12 if selected is None else selected.row_limit
                reference_start = REFERENCE_START if selected is None else selected.reference_start
                reference_end = CURRENT_START if selected is None else selected.reference_end
                current_start = CURRENT_START if selected is None else selected.current_start
                current_end = CURRENT_END if selected is None else selected.current_end
                reference = await self._prepare_run(reference_start, reference_end, question=question,
                                                    row_limit=row_limit, identity=identity)
                current = await self._prepare_run(current_start, current_end, question=question,
                                                  row_limit=row_limit, identity=identity)
                trend_request = self._request(current, reference, "trend", "ctr")
                scope = trend_request["dataset"]["provenance"]["selection_scope_sha256"]
                # Projection and the v2 input contract prove compatibility; a
                # guessed date or overlapping news IDs cannot prove the scope.
                created = await self._json("POST", "/api/v1/conversations", body={
                    "title": f"data-analysis-demo-{selected.id + '-' if selected else ''}{uuid4()}",
                }, status=201)
                conversation_id = str(ConversationView.model_validate(created).id)
                source_turns = []
                for detail in (reference, current):
                    run_id = str(detail.run.run_id)
                    turn = await self._send(conversation_id, f"读取热点运行 {run_id}")
                    if (len(turn.tools) != 1 or turn.tools[0].name != "read_hot_news"
                            or turn.tools[0].status != "completed"
                            or turn.tools[0].arguments != {"run_id": run_id}
                            or turn.tools[0].result.get("run_id") != run_id
                            or turn.tools[0].result.get("not_found")
                            or not turn.tools[0].result.get("items")):
                        raise DemoError("demo_source_not_read_in_conversation")
                    source_turns.append(str(turn.id))
                evidence = []
                if not prepare_only:
                    for operation, metric, message in OPERATIONS:
                        turn = await self._send(conversation_id, message)
                        expected_arguments = {"run_id": str(current.run.run_id), "operation": operation, "metric": metric}
                        if operation == "trend":
                            expected_arguments["reference_run_id"] = str(reference.run.run_id)
                        if (len(turn.tools) != 1 or turn.tools[0].name != "analyze_hot_news_data"
                                or turn.tools[0].status != "completed"
                                or turn.tools[0].arguments != expected_arguments):
                            raise DemoError("demo_analysis_tool_mismatch")
                        result = dict(turn.tools[0].result)
                        execution = result.pop("execution", None)
                        request = self._request(current, reference, operation, metric)
                        self._validator._validate_output(_json_bytes(result), request)
                        expected_transport = analysis.get("execution_backend")
                        if (type(execution) is not dict
                                or execution.get("input_sha256") != sha256(_json_bytes(request)).hexdigest()
                                or execution.get("backend") not in {"process", "docker"}
                                or execution.get("transport") not in {None, "service"}
                                or type(execution.get("elapsed_ms")) is not int or execution["elapsed_ms"] < 0
                                or type(execution.get("limits")) is not dict
                                or type(execution["limits"].get("os_resource_limits_enforced")) is not bool
                                or expected_transport == "service" and execution.get("transport") != "service"
                                or expected_transport in {"process", "docker"} and execution.get("backend") != expected_transport):
                            raise DemoError("demo_execution_evidence_mismatch")
                        execution_evidence = {key: execution[key] for key in ("backend", "elapsed_ms", "input_sha256")}
                        execution_evidence["transport"] = execution.get("transport")
                        execution_evidence["os_resource_limits_enforced"] = execution["limits"]["os_resource_limits_enforced"]
                        evidence.append({"operation": operation, "metric": metric, "turn_id": str(turn.id),
                                         "source": result["source"], "analysis": result["analysis"],
                                         "execution": execution_evidence})
                report = {
                    "status": "prepared" if prepare_only else "verified", "synthetic": True,
                    "conversation_id": conversation_id,
                    "current_run_id": str(current.run.run_id), "reference_run_id": str(reference.run.run_id),
                    "current_row_count": current.run.ranked_news_count,
                    "reference_row_count": reference.run.ranked_news_count,
                    "selection_scope_sha256": scope,
                    "current_window": [current_start, current_end],
                    "reference_window": [reference_start, reference_end],
                    "source_turn_ids": source_turns, "operations": evidence,
                }
                if selected is not None:
                    report.update(dataset=identity, scenario=selected.report())
                return report
        except DemoError:
            raise
        except Exception:
            raise DemoError("demo_verification_failed") from None


async def prepare_demo(*, base_url: str = DEFAULT_BASE_URL, client: httpx.AsyncClient | None = None,
                       prepare_only: bool = False, scenario_id: str | None = None) -> dict:
    demo = DataAnalysisDemo(base_url=base_url, client=client)
    try:
        return await demo.run(prepare_only=prepare_only, scenario_id=scenario_id)
    finally:
        await demo.close()


async def list_demo_scenarios(*, base_url: str = DEFAULT_BASE_URL,
                              client: httpx.AsyncClient | None = None) -> dict:
    demo = DataAnalysisDemo(base_url=base_url, client=client)
    try:
        return await demo.list_scenarios()
    finally:
        await demo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare the isolated synthetic data-analysis demo through HTTP.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--prepare-only", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--list-scenarios", action="store_true", help="Only read the enterprise synthetic catalog.")
    selection.add_argument("--scenario", help="Run one approved enterprise synthetic scenario ID.")
    arguments = parser.parse_args()
    if arguments.list_scenarios and arguments.prepare_only:
        parser.error("--prepare-only cannot be combined with --list-scenarios")
    try:
        if arguments.list_scenarios:
            result = asyncio.run(list_demo_scenarios(base_url=arguments.base_url))
        else:
            result = asyncio.run(prepare_demo(base_url=arguments.base_url, prepare_only=arguments.prepare_only,
                                             scenario_id=arguments.scenario))
    except DemoError as exc:
        print(json.dumps({"status": "failed", "error_code": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
