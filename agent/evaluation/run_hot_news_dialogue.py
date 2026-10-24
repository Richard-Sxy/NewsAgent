"""Frozen dialogue evaluation against the isolated loopback conversation API.

Default execution validates fixtures only. --run sends each message once, keeps
the persisted conversations, and compares query evidence with the saved run.
This verifies the local rule Port and integration, never enterprise model quality.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import time
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


PREFIX = "/api/v1/conversations"
DEFAULT_DATASET = Path(__file__).parent / "datasets/hot_news/hot_news_dialogue_v1/cases.json"
DEFAULT_BASE_URL = "http://127.0.0.1:5175"
LOCAL_TOKEN = "newsagent-native-local-token-not-for-production"
LOCAL_TENANT = "11111111-1111-4111-8111-111111111111"
LOCAL_USER = "22222222-2222-4222-8222-222222222222"
MAX_CASES = 40
MAX_TURNS = 60
TOOLS = {"query_hot_news", "list_hot_news", "read_hot_news", "capabilities"}


class Fixture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expectation(Fixture):
    kind: Literal["query", "read", "reject", "clarify", "refuse", "pending"]
    allowed_tools: list[str] = Field(default_factory=list, max_length=3)
    intent: dict[str, Any] = Field(default_factory=dict)
    window_start: AwareDatetime | None = None
    window_end: AwareDatetime | None = None
    resolution_codes: list[str] = Field(default_factory=list)
    same_run_from_turn: int | None = Field(default=None, ge=0, le=5)
    response_any: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def bounded_expectation(self):
        if not set(self.allowed_tools) <= TOOLS:
            raise ValueError("fixture tools exceed the dialogue suite")
        if (self.window_start is None) != (self.window_end is None):
            raise ValueError("both expected bounds are required")
        if self.window_start is not None and self.window_end - self.window_start != timedelta(hours=1):
            raise ValueError("query fixture must use one complete hour")
        if not set(self.intent) <= {"sort_by", "sort_direction", "content_type", "category", "row_limit"}:
            raise ValueError("only approved intent fields may be evaluated")
        return self


class DialogueTurn(Fixture):
    user: str = Field(min_length=1, max_length=1000)
    expect: Expectation


class DialogueCase(Fixture):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    title: str = Field(min_length=1, max_length=100)
    tags: list[str] = Field(min_length=1, max_length=8)
    support: Literal["implemented", "boundary", "pending"]
    turns: list[DialogueTurn] = Field(min_length=1, max_length=6)


class DialogueDataset(Fixture):
    schema_version: Literal[1]
    dataset_name: str
    dataset_version: str
    description: str
    data_kind: Literal["synthetic"]
    dataset_profile: Literal["timeline-v4"]
    timezone: Literal["Asia/Shanghai"]
    coverage_start: AwareDatetime
    coverage_end: AwareDatetime
    sample_as_of: Literal["2026-10-09"]
    cases: list[DialogueCase] = Field(min_length=1, max_length=MAX_CASES)

    @model_validator(mode="after")
    def frozen_range(self):
        if self.coverage_end - self.coverage_start != timedelta(days=30):
            raise ValueError("fixtures must freeze exactly thirty days")
        if self.coverage_start.isoformat() != "2026-09-10T00:00:00+08:00" or self.coverage_end.isoformat() != "2026-10-10T00:00:00+08:00":
            raise ValueError("fixture coverage must remain frozen through 2026-10-09")
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        if sum(len(case.turns) for case in self.cases) > MAX_TURNS:
            raise ValueError("dialogue suite exceeds the bounded request budget")
        for case in self.cases:
            for index, turn in enumerate(case.turns):
                expected = turn.expect
                if expected.same_run_from_turn is not None and expected.same_run_from_turn >= index:
                    raise ValueError("run references must point to an earlier turn")
                if expected.window_start is not None and not (
                    self.coverage_start <= expected.window_start < expected.window_end <= self.coverage_end
                ):
                    raise ValueError("successful query expectation is outside frozen coverage")
        return self


def load_dataset(path: Path = DEFAULT_DATASET) -> tuple[DialogueDataset, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    manifest = path.parent / "manifest.json"
    if manifest.exists():
        frozen = json.loads(manifest.read_text(encoding="utf-8"))
        if frozen.get("cases_file") == path.name and frozen.get("sha256") != digest:
            raise ValueError("frozen dataset SHA-256 does not match its manifest")
    return DialogueDataset.model_validate_json(raw), digest


def loopback_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise ValueError("base URL must be an HTTP loopback origin without credentials or a path")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid loopback origin") from exc
    if not loopback or port is None:
        raise ValueError("only an explicit loopback port may be used")
    return value.rstrip("/")


def _date(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.utcoffset() is not None else None
    except ValueError:
        return None


def _valid_uuid(value: Any) -> bool:
    try:
        UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def referenced_run(turn: dict) -> str | None:
    for trace in reversed(turn.get("tools", [])):
        if trace.get("status") == "completed" and trace.get("name") in {"query_hot_news", "read_hot_news"}:
            result = trace.get("result", {})
            if not result.get("not_found") and _valid_uuid(result.get("run_id")):
                return result["run_id"]
    return None


def report_items(detail: dict) -> list[dict]:
    """Match the public read_hot_news projection, including bounded text fields."""
    rows = []
    for item in detail.get("ranked_news", [])[:5]:
        analysis = item.get("analysis")
        rows.append({"rank": item.get("rank"), "news_id": item.get("news_id"),
                     "title": (item.get("title") or item.get("news_id", ""))[:200],
                     "metrics": item.get("metrics"), "hot_score": item.get("hot_score"),
                     "analysis": {"trend_assessment": analysis["trend_assessment"][:1000],
                                  "dominant_driver": analysis["dominant_driver"],
                                  "limitations": [value[:200] for value in analysis.get("limitations", [])[:5]]}
                     if analysis else None})
    return rows


def evaluate_turn(expected: Expectation, turn: dict, *, dataset: DialogueDataset,
                  previous_turns: list[dict] | None = None, run_details: dict[str, dict] | None = None) -> dict:
    """Grade bounded tool evidence, never an LLM judge or a substring-only success."""
    failures: list[str] = []
    previous_turns = previous_turns or []
    run_details = run_details or {}
    traces = turn.get("tools", [])
    assistant = turn.get("assistant_content") or ""
    if turn.get("status") != "completed" or turn.get("error_code"):
        failures.append("conversation_turn_not_completed")
    if not assistant:
        failures.append("empty_assistant_response")
    if len(traces) > 3 or any(trace.get("name") not in expected.allowed_tools for trace in traces):
        failures.append("unnecessary_or_forbidden_tool")
    query_traces = [trace for trace in traces if trace.get("name") == "query_hot_news"]
    if len(query_traces) > 1 or any(trace.get("attempts", 0) > 1 for trace in query_traces):
        failures.append("query_submission_repeated")
    if expected.response_any and not any(value in assistant for value in expected.response_any):
        failures.append("missing_boundary_explanation")

    if expected.kind == "query":
        successful = [trace for trace in query_traces if trace.get("status") == "completed"]
        if len(successful) != 1:
            failures.append("query_not_completed")
        else:
            result = successful[0].get("result", {})
            run_id = result.get("run_id")
            resolution = result.get("query_resolution", {})
            if resolution.get("status") != "ready":
                failures.append("query_resolution_not_ready")
            if not _valid_uuid(run_id) or not result.get("sql_query_id") or not result.get("workflow_id"):
                failures.append("missing_persisted_query_binding")
            provider = result.get("query_model_provider")
            if provider not in {"local", "python-confirmed-intent"}:
                failures.append("query_provider_is_not_local")
            if provider == "python-confirmed-intent" and (
                result.get("query_model_request_id") is not None or not result.get("understanding_model_request_id")
            ):
                failures.append("confirmed_intent_provenance_invalid")
            start, end = _date(result.get("window_start")), _date(result.get("window_end"))
            if not start or not end or end - start != timedelta(hours=1) or not (
                dataset.coverage_start <= start < end <= dataset.coverage_end
            ):
                failures.append("query_outside_frozen_single_hour")
            if expected.window_start is not None and (start, end) != (expected.window_start, expected.window_end):
                failures.append("query_requested_window_changed")
            for field, wanted in expected.intent.items():
                if resolution.get("intent", {}).get(field) != wanted:
                    failures.append(f"query_intent_changed:{field}")
                if field in {"category", "content_type", "row_limit"} and result.get("parameters", {}).get(field) != wanted:
                    failures.append(f"query_parameter_changed:{field}")
            if (_date(result.get("parameters", {}).get("window_start")),
                _date(result.get("parameters", {}).get("window_end"))) != (start, end):
                failures.append("sql_parameters_window_mismatch")
            items = result.get("items", [])
            if not items:
                failures.append("query_report_is_empty")
            for item in items:
                metrics = item.get("metrics", {})
                if (_date(metrics.get("window_start")), _date(metrics.get("window_end"))) != (start, end):
                    failures.append("ranked_item_window_mismatch")
                if expected.intent.get("content_type") and metrics.get("content_type") != expected.intent["content_type"]:
                    failures.append("ranked_item_content_type_mismatch")
            detail = run_details.get(str(run_id))
            if detail is None:
                failures.append("authoritative_run_not_verified")
            elif (detail.get("run", {}).get("status") != "completed"
                  or detail.get("run", {}).get("run_id") != run_id
                  or (detail.get("sql_tool_trace") or {}).get("query_id") != result.get("sql_query_id")
                  or report_items(detail) != items
                  or (_date(detail.get("run", {}).get("window_start")),
                      _date(detail.get("run", {}).get("window_end"))) != (start, end)):
                failures.append("authoritative_run_query_or_snapshot_mismatch")
            if detail is not None:
                preview = (detail.get("sql_tool_trace") or {}).get("preview", {})
                if preview.get("sql") != result.get("sql") or preview.get("parameters") != result.get("parameters"):
                    failures.append("authoritative_sql_plan_mismatch")
            if str(run_id) not in assistant or str(result.get("sql_query_id")) not in assistant:
                failures.append("assistant_omits_evidence_reference")

    elif expected.kind == "read":
        reads = [trace for trace in traces if trace.get("name") == "read_hot_news" and trace.get("status") == "completed"]
        if len(reads) != 1 or reads[0].get("result", {}).get("not_found") or not reads[0].get("result", {}).get("items"):
            failures.append("saved_report_not_read")
        if expected.same_run_from_turn is not None:
            prior = previous_turns[expected.same_run_from_turn] if len(previous_turns) > expected.same_run_from_turn else {}
            if not referenced_run(prior) or referenced_run(turn) != referenced_run(prior):
                failures.append("followup_run_reference_changed")
        if referenced_run(turn) and referenced_run(turn) not in assistant:
            failures.append("assistant_omits_run_reference")
        if len(reads) == 1:
            result = reads[0].get("result", {})
            detail = run_details.get(str(result.get("run_id")))
            if detail is None:
                failures.append("authoritative_read_run_not_verified")
            else:
                selected = report_items(detail)
                news_rank = reads[0].get("arguments", {}).get("news_rank")
                if news_rank is not None:
                    selected = [item for item in selected if item["rank"] == news_rank]
                if detail.get("run", {}).get("status") != "completed" or selected != result.get("items"):
                    failures.append("saved_read_snapshot_mismatch")

    elif expected.kind in {"reject", "clarify"}:
        if any(trace.get("status") == "completed" for trace in traces):
            failures.append("rejected_query_used_saved_or_new_data")
        resolutions = [trace.get("result", {}).get("query_resolution", {}) for trace in query_traces]
        allowed_status = {"clarify"} if expected.kind == "clarify" else {"unsupported", "clarify"}
        if len(resolutions) != 1 or resolutions[0].get("status") not in allowed_status:
            failures.append("expected_query_boundary_missing")
        elif expected.resolution_codes and resolutions[0].get("reason_code") not in expected.resolution_codes:
            failures.append("query_boundary_reason_changed")
        if any(trace.get("result", {}).get(key) for trace in traces for key in ("run_id", "workflow_id", "sql_query_id")):
            failures.append("rejected_query_created_binding")

    elif expected.kind in {"refuse", "pending"}:
        if traces:
            failures.append("unexpected_tool_on_refusal_or_pending_input")
    return {"verdict": "failed" if failures else "expected_pending" if expected.kind == "pending" else "passed",
            "failures": sorted(set(failures)), "referenced_run_id": referenced_run(turn)}


def summarize(cases: list[dict]) -> dict:
    counts = Counter(case["verdict"] for case in cases)
    attempts = [turn for case in cases for turn in case["turns"]]
    return {"case_count": len(cases), "passed": counts["passed"], "failed": counts["failed"],
            "expected_pending": counts["expected_pending"], "blocked": counts["blocked"],
            "turn_count": len(attempts), "attempted_turn_count": len(attempts),
            "completed_response_count": sum(turn.get("response") is not None for turn in attempts),
            "completed_query_count": sum(trace.get("name") == "query_hot_news" and trace.get("status") == "completed"
                                         for turn in attempts for trace in (turn.get("response") or {}).get("tools", [])),
            "meaning": "turn_count与attempted_turn_count记录消息提交尝试；completed_response_count记录已返回结构化轮次。passed含正确澄清和拒绝；只有completed_query_count表示完成取数。expected_pending不计通过。"}


async def run_evaluation(dataset: DialogueDataset, *, base_url: str = DEFAULT_BASE_URL,
                         selected_ids: list[str] | None = None, timeout: float = 180,
                         direct_api_identity: bool = False, transport=None) -> dict:
    base_url = loopback_url(base_url)
    if not 1 <= timeout <= 240:
        raise ValueError("per-request timeout must be between 1 and 240 seconds")
    selection = set(selected_ids or [])
    if selection - {case.id for case in dataset.cases}:
        raise ValueError("unknown dialogue case selection")
    cases = [case for case in dataset.cases if not selection or case.id in selection]
    eval_id = str(uuid4())
    headers = {"Content-Type": "application/json"}
    if direct_api_identity:
        headers.update({"Authorization": f"Bearer {LOCAL_TOKEN}", "X-Tenant-ID": LOCAL_TENANT,
                        "X-User-ID": LOCAL_USER, "X-Hot-News-Roles": "hot-news:admin"})
    report = {"evaluation_id": eval_id, "created_at": datetime.now(timezone.utc).isoformat(),
              "base_url": base_url, "dataset_name": dataset.dataset_name,
              "dataset_version": dataset.dataset_version, "sample_as_of": dataset.sample_as_of,
              "scope": "isolated_synthetic_local_rule_port", "runtime": None, "cases": []}
    async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=timeout, transport=transport,
                                 trust_env=False, follow_redirects=False) as client:
        runtime_response = await client.get(f"{PREFIX}/runtime")
        runtime_response.raise_for_status()
        runtime = runtime_response.json()
        report["runtime"] = runtime
        if runtime.get("model_provider") != "local" or runtime.get("query_enabled") is not True:
            raise ValueError("runtime must explicitly report local rule inference and enabled query permission; no messages were submitted")
        details: dict[str, dict] = {}
        for case in cases:
            record = {"id": case.id, "title": case.title, "tags": case.tags,
                      "support": case.support, "conversation_id": None, "turns": [], "verdict": "blocked"}
            report["cases"].append(record)
            current_attempt = None
            try:
                response = await client.post(PREFIX, json={"title": f"评测 {eval_id[:8]} {case.id}"[:100]})
                response.raise_for_status()
                conversation = response.json()["id"]
                if not _valid_uuid(conversation):
                    raise ValueError("API returned an invalid conversation UUID")
                record["conversation_id"] = conversation
                previous: list[dict] = []
                for index, fixture in enumerate(case.turns):
                    request_id = str(uuid4())
                    started = time.monotonic()
                    current_attempt = {"index": index, "request_id": request_id, "user": fixture.user,
                                       "expected": fixture.expect.model_dump(mode="json"), "response": None,
                                       "elapsed_seconds": None, "verdict": "blocked", "failures": [],
                                       "referenced_run_id": None}
                    record["turns"].append(current_attempt)
                    # Submit exactly once. A timeout is recorded; never repeat a command
                    # or poll until a previously unknown command starts another workflow.
                    response = await client.post(f"{PREFIX}/{conversation}/messages",
                                                 json={"request_id": request_id, "content": fixture.user})
                    response.raise_for_status()
                    turn = response.json()
                    current_attempt["response"] = turn
                    for trace in turn.get("tools", []):
                        run_id = trace.get("result", {}).get("run_id")
                        if trace.get("name") in {"query_hot_news", "read_hot_news"} and trace.get("status") == "completed" and _valid_uuid(run_id) and run_id not in details:
                            proof = await client.get(f"/api/v1/hot-news/runs/{run_id}")
                            if proof.status_code == 200:
                                details[run_id] = proof.json()
                    grade = evaluate_turn(fixture.expect, turn, dataset=dataset,
                                          previous_turns=previous, run_details=details)
                    if turn.get("request_id") != request_id or turn.get("user_content") != fixture.user:
                        grade = {**grade, "verdict": "failed", "failures": [*grade["failures"], "response_request_or_user_mismatch"]}
                    current_attempt.update(elapsed_seconds=round(time.monotonic() - started, 3), **grade)
                    previous.append(turn)
                    current_attempt = None
                verdicts = {turn["verdict"] for turn in record["turns"]}
                record["verdict"] = "failed" if "failed" in verdicts else "expected_pending" if "expected_pending" in verdicts else "passed"
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                # Do not include headers, raw HTML or transport exception strings.
                record["error"] = {"type": type(exc).__name__,
                                   "http_status": exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None,
                                   "submissions_retried": False}
                if current_attempt is not None:
                    current_attempt.update(error=dict(record["error"]),
                                           elapsed_seconds=round(time.monotonic() - started, 3))
                record["verdict"] = "blocked"
    report["summary"] = summarize(report["cases"])
    return report


async def main_async(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--run", action="store_true", help="显式执行有限HTTP对话；默认仅校验冻结评测集")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--direct-api-identity", action="store_true", help="直连本地模拟API时发送既有合成身份；默认由前端代理注入")
    parser.add_argument("--case", action="append", dest="case_ids", help="只运行指定case ID，可重复")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    dataset, digest = load_dataset(args.dataset)
    if not args.run:
        print(json.dumps({"status": "dataset_valid", "dataset_name": dataset.dataset_name,
                          "dataset_sha256": digest, "case_count": len(dataset.cases),
                          "turn_count": sum(len(case.turns) for case in dataset.cases),
                          "coverage_start": dataset.coverage_start.isoformat(),
                          "coverage_end": dataset.coverage_end.isoformat()}, ensure_ascii=False, indent=2))
        return 0
    report = await run_evaluation(dataset, base_url=args.base_url, selected_ids=args.case_ids,
                                  timeout=args.timeout, direct_api_identity=args.direct_api_identity)
    report["dataset_sha256"] = digest
    target = args.output or Path(__file__).parent / "results" / f"hot-news-dialogue-{report['evaluation_id']}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report_path": str(target.resolve()), **report["summary"]}, ensure_ascii=False, indent=2))
    return 1 if report["summary"]["failed"] or report["summary"]["blocked"] else 0


def main() -> None:
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
