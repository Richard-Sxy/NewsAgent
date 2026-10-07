"""Fixed synthetic enterprise edge cases through the real isolated analysis Port.

Run from agent/: python -m examples.enterprise_analysis_contracts --all.
No business Settings, environment file, warehouse mutation, arbitrary input file,
model, enterprise endpoint or automatic retry is used. Rejections are evidence
of the input gate; they are never represented as a successfully started worker.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from typing import Any

from app.analytics.entities import ContentType
from app.analytics.metric_source import HotNewsMetricQuery
from app.analytics.metrics import NewsMetricSnapshot
from app.data_analysis.acceptance import run_aggregate_acceptance
from app.data_analysis.runner import AnalysisExecutionError, AnalysisRunner, _json_bytes


CONTRACT_VERSION = "enterprise-analysis-contracts-v1"
_CURRENT_RUN = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_REFERENCE_RUN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_TENANT = "11111111-1111-4111-8111-111111111111"
_LIMITATIONS = (
    "Fixed synthetic aggregate fixtures; no enterprise endpoint or raw user events are used.",
    "These checks establish analysis/aggregate response contracts, not warehouse freshness, watermark, enterprise tenant isolation, model quality or production acceptance.",
    "Missing baselines and repeated-click fixtures use the aggregate contract; the frozen v1 warehouse uses a baseline inner join and requires clicks <= impressions.",
    "UV is not added across news or windows. Two-window changes do not establish causation or a continuing trend.",
    "Rejected cases stop at the runner input gate and have no worker execution or enforced OS-limit evidence.",
)


@dataclass(frozen=True)
class _Scenario:
    id: str
    label: str
    request: dict
    checks: dict[str, Any]
    error_code: str | None = None
    port_status: str | None = None
    port_code: str | None = None
    forbidden_paths: tuple[str, ...] = ()


def _row(news_id: str = "sample", rank: int = 1, **changes: Any) -> dict:
    value = {
        "news_id": news_id, "rank": rank, "content_type": "article",
        "impressions": 100, "clicks": 10, "unique_users": 7,
        "total_duration_seconds": 100, "effective_consumptions": 5,
        "interactions": 1, "ctr": "0.1", "hot_score": "0.5",
    }
    value.update(changes)
    return value


def _baseline(**changes: Any) -> dict:
    value = {
        "sample_count": 3, "reference_version": CONTRACT_VERSION,
        "impressions": "100", "clicks": "10", "ctr": "0.1",
        "total_duration_seconds": "100", "effective_consumptions": "5",
        "interactions": "1",
    }
    value.update(changes)
    return value


def _request(rows: list[dict], operation: str = "overview", metric: str = "ctr") -> dict:
    extended = operation in {"baseline", "trend"}
    dataset = {
        "run_id": _CURRENT_RUN,
        "window_start": "2026-10-03T10:00:00+08:00",
        "window_end": "2026-10-03T11:00:00+08:00",
        "bundle_version": CONTRACT_VERSION, "rows": rows,
    }
    if extended:
        dataset["provenance"] = {
            "workflow_version": "synthetic-workflow-v1",
            "selection_scope_sha256": "ab" * 32,
        }
    return {"schema_version": "2.0" if extended else "1.0",
            "operation": operation, "metric": metric, "dataset": dataset}


def _trend(current: list[dict] | None = None, reference: list[dict] | None = None,
           metric: str = "clicks") -> dict:
    value = _request(current if current is not None else [_row()], "trend", metric)
    prior = _request(reference if reference is not None else [_row()], "trend", metric)["dataset"]
    prior.update(run_id=_REFERENCE_RUN, window_start="2026-10-03T09:00:00+08:00",
                 window_end="2026-10-03T10:00:00+08:00")
    value["reference_dataset"] = prior
    return value


def _scenarios() -> tuple[_Scenario, ...]:
    # Requests are freshly allocated, and can never be supplied by a model or
    # loaded from a path. Expected values are explicit, independent arithmetic.
    cases = [
        _Scenario("missing-baseline", "冷启动：完全无参考基线",
                  _request([_row("cold-start", baseline=None)], "baseline", "clicks"),
                  {"analysis.compared_count": 0, "analysis.missing_baseline_news_ids": ["cold-start"],
                   "analysis.items.0.status": "missing_baseline", "analysis.items.0.reference_value": None,
                   "analysis.items.0.absolute_change": None, "analysis.items.0.relative_change": None}),
        _Scenario("partial-baseline-metric", "参考基线缺少消费时长",
                  _request([_row(baseline=_baseline(total_duration_seconds=None))],
                           "baseline", "total_duration_seconds"),
                  {"analysis.compared_count": 0, "analysis.missing_metric_news_ids": ["sample"],
                   "analysis.items.0.status": "missing_metric", "analysis.items.0.current_value": "100",
                   "analysis.items.0.reference_value": None, "analysis.items.0.relative_change": None}),
        _Scenario("zero-baseline", "基线点击为零：相对增长不可计算",
                  _request([_row(baseline=_baseline(clicks="0", ctr="0", total_duration_seconds="0",
                                                   effective_consumptions="0", interactions="0"))], "baseline", "clicks"),
                  {"analysis.items.0.status": "compared", "analysis.items.0.reference_value": "0",
                   "analysis.items.0.absolute_change": "10", "analysis.items.0.relative_change": None}),
        _Scenario("zero-exposure-all", "全体零曝光：CTR未知",
                  _request([_row("zero", impressions=0, clicks=0, unique_users=0,
                                total_duration_seconds=0, effective_consumptions=0,
                                interactions=0, ctr="0")]),
                  {"analysis.weighted_ctr": None, "analysis.totals.impressions": 0,
                   "analysis.totals.clicks": 0}, forbidden_paths=("analysis.totals.unique_users",)),
        _Scenario("zero-clicks-with-exposure", "有曝光无点击：有效CTR降至零",
                  _trend([_row(clicks=0, unique_users=0, total_duration_seconds=0,
                               effective_consumptions=0, interactions=0, ctr="0")], metric="ctr"),
                  {"analysis.aggregate.current_value": "0", "analysis.aggregate.reference_value": "0.1",
                   "analysis.aggregate.absolute_change": "-0.1", "analysis.aggregate.relative_change": "-1"}),
        _Scenario("rounded-ctr", "CTR四位小数正常舍入",
                  _request([_row(impressions=3, clicks=1, unique_users=1, total_duration_seconds=10,
                                 effective_consumptions=1, interactions=0, ctr="0.3333")], "quality"),
                  {"analysis.ctr_mismatch_news_ids": [], "analysis.clicks_above_impressions_news_ids": []},
                  port_status="local_contract_passed"),
        _Scenario("ctr-mismatch", "CTR不一致：质量提示与取数合同拒绝",
                  _request([_row(impressions=3, clicks=1, unique_users=1, total_duration_seconds=10,
                                 effective_consumptions=1, interactions=0, ctr="0.3334")], "quality"),
                  {"analysis.ctr_mismatch_news_ids": ["sample"], "analysis.zero_impression_news_ids": []},
                  port_status="contract_failed", port_code="aggregate_ctr_inconsistent"),
        _Scenario("repeated-clicks", "重复点击：核对事件口径",
                  _request([_row(impressions=2, clicks=3, unique_users=2, total_duration_seconds=30,
                                 effective_consumptions=2, interactions=1, ctr="1.5")], "quality"),
                  {"analysis.ctr_mismatch_news_ids": [], "analysis.clicks_above_impressions_news_ids": ["sample"]},
                  port_status="local_contract_passed"),
        _Scenario("empty-data", "空快照：不补造分布",
                  _request([], "distribution"),
                  {"source.row_count": 0, "analysis.count": 0, "analysis.mean": None,
                   "analysis.median": None, "analysis.p90": None}),
        _Scenario("weighted-ctr-mix", "大曝光低CTR与小曝光高CTR",
                  _request([_row("mass", 1, impressions=100000, clicks=1000, ctr="0.01"),
                            _row("niche", 2, impressions=100, clicks=40, ctr="0.4")]),
                  {"analysis.totals.impressions": 100100, "analysis.totals.clicks": 1040,
                   "analysis.weighted_ctr": "0.01038961038961038961038961038961038961038961038961038961038961039"},
                  forbidden_paths=("analysis.totals.unique_users",)),
        _Scenario("cohort-turnover", "榜单新增退出：仅比较交集",
                  _trend([_row("one", 1), _row("two", 2, clicks=5, unique_users=4, ctr="0.05"),
                          _row("new", 3, impressions=1000, clicks=999, ctr="0.999")],
                         [_row("one", 1, clicks=20, ctr="0.2"), _row("two", 2, clicks=5, unique_users=4, ctr="0.05"),
                          _row("old", 3, impressions=1000, clicks=900, ctr="0.9")]),
                  {"analysis.matched_news_count": 2, "analysis.added_news_ids": ["new"],
                   "analysis.removed_news_ids": ["old"], "analysis.aggregate.current_value": "15",
                   "analysis.aggregate.reference_value": "25", "analysis.aggregate.absolute_change": "-10",
                   "analysis.aggregate.relative_change": "-0.4"}),
    ]
    gap = _trend()
    gap["dataset"].update(window_start="2026-10-03T12:00:00+08:00",
                          window_end="2026-10-03T13:00:00+08:00")
    cases.append(_Scenario("gap-windows", "等长非连续窗口：披露间隔", gap,
                           {"analysis.gap_seconds": 7200, "analysis.aggregate.relative_change": "0"}))
    no_overlap = _trend([_row("current-only")], [_row("reference-only")])
    cases.append(_Scenario("no-overlap-cohort", "榜单无交集：拒绝趋势", no_overlap, {}, "invalid_request"))
    overlap = _trend()
    overlap["reference_dataset"].update(window_start="2026-10-03T09:30:00+08:00",
                                        window_end="2026-10-03T10:30:00+08:00")
    cases.append(_Scenario("overlapping-windows", "时间窗重叠：拒绝趋势", overlap, {}, "invalid_request"))
    unequal = _trend()
    unequal["reference_dataset"]["window_end"] = "2026-10-03T09:30:00+08:00"
    cases.append(_Scenario("unequal-windows", "窗口时长不同：拒绝趋势", unequal, {}, "invalid_request"))
    for name, label, path, replacement in (
        ("scope-mismatch", "选取口径不同：拒绝趋势", "provenance.selection_scope_sha256", "cd" * 32),
        ("bundle-mismatch", "Bundle版本不同：拒绝趋势", "bundle_version", "synthetic-bundle-v2"),
        ("workflow-mismatch", "Workflow版本不同：拒绝趋势", "provenance.workflow_version", "synthetic-workflow-v2"),
    ):
        value = _trend()
        target = value["reference_dataset"]
        fields = path.split(".")
        for field in fields[:-1]:
            target = target[field]
        target[fields[-1]] = replacement
        cases.append(_Scenario(name, label, value, {}, "invalid_request"))
    unknown = _request([_row()])
    unknown["dataset"]["rows"][0]["raw_event"] = "untrusted-unknown-field-sentinel"
    cases.append(_Scenario("unknown-field", "未知字段：白名单拒绝且不回显", unknown, {}, "invalid_request"))
    oversized = _request([_row(f"bounded-{index}", index + 1) for index in range(1001)])
    cases.append(_Scenario("oversized-row-count", "超过1000行预算：拒绝执行", oversized, {}, "row_limit"))
    return tuple(cases)


def list_scenarios() -> list[dict]:
    return [{"id": case.id, "label": case.label, "level": "analysis-contract",
             "expected_status": "rejected" if case.error_code else "accepted",
             "aggregate_port_contract": case.port_status is not None} for case in _scenarios()]


def _at(value: Any, path: str) -> Any:
    for field in path.split("."):
        value = value[int(field)] if type(value) is list else value[field]
    return value


def _has(value: dict, path: str) -> bool:
    try:
        _at(value, path)
        return True
    except (KeyError, IndexError, TypeError, ValueError):
        return False


class _ObservedRunner(AnalysisRunner):
    worker_attempted = False

    async def _execute(self, payload: bytes):
        self.worker_attempted = True
        return await super()._execute(payload)


class _AggregateFixture:
    """A fixed local aggregate source, independently checked by the Port gate."""

    def __init__(self, dataset: dict):
        self.dataset = dataset

    async def fetch_snapshots(self, query: HotNewsMetricQuery) -> list[NewsMetricSnapshot]:
        return [NewsMetricSnapshot(
            news_id=row["news_id"], content_type=ContentType(row["content_type"]),
            window_start=query.window_start, window_end=query.window_end,
            **{key: row[key] for key in ("impressions", "clicks", "unique_users",
               "total_duration_seconds", "effective_consumptions", "interactions")},
            ctr=Decimal(row["ctr"]),
        ) for row in self.dataset["rows"]]


async def _port_check(case: _Scenario) -> dict:
    dataset = case.request["dataset"]
    query = HotNewsMetricQuery(tenant_id=_TENANT,
                              window_start=datetime.fromisoformat(dataset["window_start"]),
                              window_end=datetime.fromisoformat(dataset["window_end"]))
    report = await run_aggregate_acceptance(_AggregateFixture(dataset), query)
    code = next((check["code"] for check in report["checks"] if check["status"] == "failed"), None)
    expected = {"status": case.port_status, "error_code": case.port_code}
    actual = {"status": report["status"], "error_code": code}
    return {"level": "aggregate-port-contract", "expected": expected, "actual": actual,
            "passed": actual == expected, "evidence": report["evidence"],
            "tenant_isolation": "not_verified"}


async def run_scenarios(*, scenario_ids: tuple[str, ...] | None = None,
                        backend: str = "process") -> dict:
    """Execute approved cases once and compare explicit expected facts.

    Only process/docker are accepted by AnalysisRunner. Numerical outputs must
    traverse the fixed worker and its full output recomputation gate. Negative
    requests traverse runner.run(), without mocking a successful result.
    """
    scenarios = _scenarios()
    known = {case.id for case in scenarios}
    if scenario_ids is not None:
        if not scenario_ids or len(set(scenario_ids)) != len(scenario_ids) or not set(scenario_ids) <= known:
            raise ValueError("scenario_selection_invalid")
        selected = set(scenario_ids)
        scenarios = tuple(case for case in scenarios if case.id in selected)
    runner = _ObservedRunner(backend=backend, max_rows=1000, max_input_bytes=1_048_576)
    cases = []
    for case in scenarios:
        runner.worker_attempted = False
        expected = {"status": "rejected" if case.error_code else "accepted",
                    "error_code": case.error_code, "checks": case.checks,
                    "absent_paths": list(case.forbidden_paths)}
        actual = {"status": "failed", "error_code": None, "checks": {}, "absent_paths": []}
        evidence = {"input_sha256": hashlib.sha256(_json_bytes(case.request)).hexdigest(),
                    "output_sha256": None}
        execution = {"runner_backend": backend, "backend": None, "worker_attempted": False,
                     "os_resource_limits_enforced": None}
        try:
            result = await runner.run(case.request)
            actual["status"] = "accepted"
            actual["checks"] = {path: _at(result, path) for path in case.checks}
            actual["absent_paths"] = [path for path in case.forbidden_paths if not _has(result, path)]
            core = {key: value for key, value in result.items() if key != "execution"}
            evidence["output_sha256"] = hashlib.sha256(_json_bytes(core)).hexdigest()
            execution = {"runner_backend": backend, "backend": result["execution"]["backend"],
                         "worker_attempted": runner.worker_attempted,
                         "os_resource_limits_enforced": result["execution"]["limits"]["os_resource_limits_enforced"],
                         "input_sha256": result["execution"]["input_sha256"],
                         "elapsed_ms": result["execution"]["elapsed_ms"],
                         "limits": result["execution"]["limits"]}
        except AnalysisExecutionError as exc:
            actual.update(status="rejected", error_code=exc.error_code)
            execution["worker_attempted"] = runner.worker_attempted
            # Hash only the fixed safe error envelope, not exception text/input.
            evidence["output_sha256"] = hashlib.sha256(_json_bytes({"status": "rejected",
                                                                    "error_code": exc.error_code})).hexdigest()
        except Exception:
            # Unexpected exceptions produce a failed case without private text.
            actual.update(status="failed", error_code="contract_check_failed")
            execution["worker_attempted"] = runner.worker_attempted
        port = await _port_check(case) if case.port_status is not None else None
        passed = actual == expected and (port is None or port["passed"])
        cases.append({"id": case.id, "label": case.label, "level": "analysis-contract",
                      "synthetic": True, "status": "passed" if passed else "failed", "passed": passed,
                      "expected": expected, "actual": actual, "evidence": evidence,
                      "execution": execution, "aggregate_port_contract": port})
    passed_count = sum(case["passed"] for case in cases)
    return {"contract_version": CONTRACT_VERSION, "synthetic": True,
            "status": "verified" if passed_count == len(cases) else "failed",
            "counts": {"total": len(cases), "passed": passed_count, "failed": len(cases) - passed_count},
            "runner_configuration": runner.description(), "cases": cases,
            "limitations": list(_LIMITATIONS)}


def main() -> None:
    import asyncio

    parser = argparse.ArgumentParser(description="Run fixed synthetic analysis contract scenarios.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--list", action="store_true")
    selection.add_argument("--scenario", choices=[case["id"] for case in list_scenarios()])
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--backend", choices=("process", "docker"), default="process")
    arguments = parser.parse_args()
    if arguments.list:
        report = {"contract_version": CONTRACT_VERSION, "synthetic": True, "scenarios": list_scenarios()}
    else:
        selected = (arguments.scenario,) if arguments.scenario else None
        report = asyncio.run(run_scenarios(scenario_ids=selected, backend=arguments.backend))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    if report.get("status") == "failed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
