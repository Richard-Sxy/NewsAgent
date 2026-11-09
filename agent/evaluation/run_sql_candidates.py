"""Offline SQL-candidate contract checks, never SQL execution or model-quality scoring.

Built-in cases are synthetic development probes with no human-reviewed labels.
The model sees confirmed intent and approved metadata, never parameter values.
"""
from __future__ import annotations

import argparse
import asyncio
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from time import monotonic
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import Text2SqlGuardError
from app.schemas.sql_assistant import SqlAssistantIntent
from app.sql_assistant.candidate import (
    SqlCandidateConfig, SqlCandidateError, SqlCandidateResponse,
    build_sql_candidate_generator, load_sql_candidate_config,
)
from app.sql_assistant.candidate_validation import digest, validate_candidate, validator_identity
from app.sql_assistant.guard import SqlAssistantGuard
from app.sql_assistant.planner import compile_query
from app.sql_assistant.scenarios import SqlScenario


EXPERIMENT = "sql-candidate-contract-v1"
FIXED_NOW = "2026-10-03T12:00:00+08:00"
WINDOW_START = "2026-10-03T10:00:00+08:00"
WINDOW_END = "2026-10-03T11:00:00+08:00"
FIXTURE_TENANT = "11111111-1111-4111-8111-111111111111"
_NOT_EVALUATED = ["natural_language_understanding", "database_result_equivalence",
                  "hot_news_end_to_end", "enterprise_quality", "production_acceptance"]


class CandidateCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    split: Literal["development"] = "development"
    review_status: Literal["synthetic_not_human_reviewed"] = "synthetic_not_human_reviewed"
    intent: SqlAssistantIntent


def development_cases() -> list[CandidateCase]:
    values = [
        ("ctr-article", "ctr", "desc", "article", "科技", 10),
        ("clicks-article", "clicks", "desc", "article", "科技", 10),
        ("ctr-video", "ctr", "desc", "video", None, 5),
        ("impressions-asc", "impressions", "asc", None, "社会", 3),
        ("hot-score", "hot_score", "desc", None, None, 20),
        ("interactions", "interactions", "desc", None, "财经", 5),
    ]
    return [CandidateCase(id=name, intent=SqlAssistantIntent(
        sort_by=metric, sort_direction=direction, content_type=content,
        category=category, row_limit=limit, explanation="Synthetic confirmed intent; no model-understanding claim.",
    )) for name, metric, direction, content, category, limit in values]


def development_scenario() -> SqlScenario:
    return SqlScenario(
        id="candidate-contract-development", name="Synthetic candidate contracts",
        description="Confirmed intents for offline contract checks, not reviewed quality data.",
        allowed_sort_metrics=("clicks", "ctr", "impressions", "interactions", "hot_score"),
        allowed_content_types=("article", "video"),
        allowed_categories=("科技", "财经", "体育", "社会"),
        default_sort="clicks", default_limit=5, max_limit=100,
        sample_questions=("合成开发案例，不用于自然语言理解评分",),
    )


def _schema() -> tuple[str, dict]:
    # Metadata helpers only: never construct Database or a warehouse client.
    from app.sql_assistant.warehouse import get_text2sql_schema, verify_schema_contract, warehouse_contract
    verify_schema_contract("classic-v1")
    contract = warehouse_contract("classic-v1")
    ddl = get_text2sql_schema("classic-v1").render_ddl()
    return ddl, {"version": contract.version, "document_sha256": contract.sha256,
                 "ddl_sha256": sha256(ddl.encode()).hexdigest(), "dataset_profile": "classic-v1"}


def _params(intent, tenant_id):
    result = {"tenant_id": tenant_id, "window_start": WINDOW_START,
              "window_end": WINDOW_END, "row_limit": intent.row_limit}
    for name in ("content_type", "category"):
        if getattr(intent, name) is not None:
            result[name] = getattr(intent, name)
    return result


def _safe_metadata(metadata) -> dict:
    if not isinstance(metadata, dict):
        return {}
    result = {}
    usage = metadata.get("usage", {})
    if isinstance(usage, dict):
        result["usage"] = {k: v for k, v in usage.items()
            if k in {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"}
            and type(v) is int and v >= 0}
    if type(metadata.get("attempts")) is int and 0 <= metadata["attempts"] <= 1:
        result["attempts"] = metadata["attempts"]
    for key in ("model_request_id", "model_version"):
        value = metadata.get(key)
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,191}", value):
            result[key] = value
    return result


def _summary(rows):
    total = len(rows)
    passed = sum(row["status"] == "passed" for row in rows)
    usage = {}
    for row in rows:
        for key, value in row.get("usage", {}).items():
            usage[key] = usage.get(key, 0) + value
    known = sum(bool(row.get("usage")) for row in rows)
    return {"total": total, "passed": passed, "not_passed": total - passed,
            "failed": sum(row["status"] == "failed" for row in rows),
            "unsupported": sum(row["status"] == "unsupported" for row in rows),
            "not_run": sum(row["status"] == "not_run" for row in rows),
            "contract_pass_rate": passed / total if total else None,
            "usage": usage, "usage_known_cases": known,
            "usage_missing_cases": total - known}


async def evaluate_candidates(
    *, config: SqlCandidateConfig | None = None, generator=None,
    cases: list[CandidateCase] | None = None, tenant_id: str = FIXTURE_TENANT,
    max_calls: int = 100, max_seconds: float = 60,
) -> dict:
    """Inject a generator for tests; native requires no model configuration."""
    config = config or SqlCandidateConfig()
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant accounting identity required")
    if type(max_calls) is not int or not 0 <= max_calls <= 1000:
        raise ValueError("max_calls must be bounded")
    if type(max_seconds) not in {int, float} or not math.isfinite(max_seconds) or not 0 < max_seconds <= 3600:
        raise ValueError("max_seconds must be bounded")
    if config.mode == "native" and generator is not None:
        raise ValueError("native must not receive a generator")
    if config.mode != "native" and generator is None:
        raise ValueError("candidate mode requires explicit generator")
    cases = development_cases() if cases is None else [
        CandidateCase.model_validate(c.model_dump() if isinstance(c, CandidateCase) else c) for c in cases
    ]
    if not cases or len(cases) > 1000 or len({c.id for c in cases}) != len(cases):
        raise ValueError("cases must be nonempty, bounded and unique")
    scenario, guard = development_scenario(), SqlAssistantGuard(max_rows=100)
    ddl, schema = _schema()
    approved = []
    for case in cases:
        sql, params = compile_query(case.intent, scenario), _params(case.intent, tenant_id)
        guard.validate(sql, params)
        approved.append((case, sql, params))
    contract = {
        "cases": [c.model_dump(mode="json") for c in cases], "scenario": scenario.model_dump(mode="json"),
        "schema": schema, "validator": validator_identity(), "frozen_now": FIXED_NOW,
        "window_start": WINDOW_START, "window_end": WINDOW_END,
        "tenant_sha256": sha256(tenant_id.encode()).hexdigest(),
        "max_calls": max_calls, "max_seconds": max_seconds,
        "candidate_budget": config.model_dump(mode="json", exclude={"mode"}),
    }
    identity = generator.identity() if generator is not None else {"provider": "native_compiler"}
    report = {
        "experiment_kind": EXPERIMENT, "scope": "contract_only_no_database",
        "data_status": "synthetic_development_not_human_reviewed",
        "mode": config.mode, "config": config.model_dump(mode="json"),
        "contract": contract, "contract_sha256": digest(contract),
        "generator_identity": identity, "generator_identity_sha256": digest(identity),
        "runner_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        "not_evaluated": _NOT_EVALUATED, "production_acceptance": "not_approved", "cases": [],
    }
    started, calls = monotonic(), 0
    for case, native_sql, params in approved:
        before = monotonic()
        row = {"id": case.id, "status": "not_run", "reason_code": None,
               "native_sql": native_sql, "candidate_sql": None, "usage": {},
               "attempts": 0, "elapsed_ms": 0, "database_executions": 0}
        remaining = max_seconds - (monotonic() - started)
        if remaining <= 0 or (generator is not None and calls >= max_calls):
            row["reason_code"] = "wall_budget_exhausted" if remaining <= 0 else "call_budget_exhausted"
        elif generator is None:
            row["status"] = "passed"
            row["reason_code"] = "native_guard_passed"
        else:
            try:
                calls += 1
                row["attempts"] = 1
                async with asyncio.timeout(min(remaining, config.deadline_seconds)):
                    result = await generator.generate(
                        intent=case.intent, scenario=scenario, schema_ddl=ddl,
                        schema_version=schema["version"], tenant_id=tenant_id,
                        request_id=f"candidate-eval-{uuid4()}",
                    )
                row.update(_safe_metadata(result.metadata))
                checked = SqlCandidateResponse.model_validate({
                    "status": result.status, "sql": result.sql, "reason_code": result.reason_code,
                })
                if checked.status == "unsupported":
                    row.update(status="unsupported", reason_code="unsupported_contract")
                else:
                    if len(checked.sql) > config.max_output_chars:
                        raise SqlCandidateError("response_too_large")
                    row["candidate_sql"] = checked.sql
                    validate_candidate(checked.sql, approved_sql=native_sql, params=params, guard=guard)
                    row.update(status="passed", reason_code="candidate_contract_passed")
            except Text2SqlGuardError:
                row.update(status="failed", reason_code="sql_contract_rejected")
            except SqlCandidateError as exc:
                row.update(_safe_metadata(exc.metadata))
                code = exc.code if re.fullmatch(r"[a-z0-9_]{1,80}", exc.code) else "candidate_failure"
                row.update(status="failed", reason_code=code)
            except TimeoutError:
                row.update(status="failed", reason_code="deadline_exceeded")
            except Exception:
                # Exceptions may include credentials/provider bodies; never serialize them.
                row.update(status="failed", reason_code="candidate_internal_error")
        row["elapsed_ms"] = round((monotonic() - before) * 1000, 3)
        report["cases"].append(row)
    report["summary"] = {**_summary(report["cases"]), "generation_calls": calls,
                         "elapsed_ms": round((monotonic() - started) * 1000, 3),
                         "database_executions": 0}
    return report


def save_report(report: dict, out: Path) -> dict:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    contents = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    with (out / "report.json").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(contents)
    manifest = {"experiment_kind": EXPERIMENT, "report_sha256": sha256(contents.encode()).hexdigest()}
    with (out / "manifest.json").open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(manifest, handle, sort_keys=True)
    return {"output": str(out), **report["summary"], "scope": report["scope"],
            "production_acceptance": "not_approved"}


def _read_report(path: Path) -> dict:
    path = Path(path)
    raw = (path / "report.json").read_bytes()
    if len(raw) > 2_000_000:
        raise ValueError("report too large")
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    value = json.loads(raw)
    if (manifest.get("experiment_kind") != EXPERIMENT or value.get("experiment_kind") != EXPERIMENT
            or manifest.get("report_sha256") != sha256(raw).hexdigest()
            or value.get("contract_sha256") != digest(value["contract"])
            or value.get("generator_identity_sha256") != digest(value["generator_identity"])):
        raise ValueError("candidate report identity mismatch")
    expected_ids = [item["id"] for item in value["contract"]["cases"]]
    if [item["id"] for item in value["cases"]] != expected_ids:
        raise ValueError("candidate report cases mismatch")
    if any(row["status"] not in {"passed", "failed", "unsupported", "not_run"} for row in value["cases"]):
        raise ValueError("candidate report status mismatch")
    if any(value["summary"].get(k) != v for k, v in _summary(value["cases"]).items()):
        raise ValueError("candidate report summary mismatch")
    return value


def compare_reports(baseline: Path, candidate: Path) -> dict:
    left, right = _read_report(baseline), _read_report(candidate)
    if left["contract_sha256"] != right["contract_sha256"] or left["runner_sha256"] != right["runner_sha256"]:
        raise ValueError("candidate comparison requires identical cases, schema, scope, budgets and runner")
    return {"experiment_kind": EXPERIMENT, "scope": "contract_only_no_database", "status": "comparable",
            "baseline_mode": left["mode"], "candidate_mode": right["mode"],
            "generator_identity_changed": left["generator_identity_sha256"] != right["generator_identity_sha256"],
            "comparison_variables": ["generation_mode", "generator_identity"],
            "interpretation": "Contract agreement comparison only; model/asset changes are not isolated causal effects.",
            "baseline_contract_pass_rate": left["summary"]["contract_pass_rate"],
            "candidate_contract_pass_rate": right["summary"]["contract_pass_rate"],
            "contract_pass_rate_delta": right["summary"]["contract_pass_rate"] - left["summary"]["contract_pass_rate"],
            "denominator": left["summary"]["total"], "not_evaluated": _NOT_EVALUATED,
            "production_acceptance": "not_approved"}


async def _run_cli(args):
    if args.out.exists():
        raise FileExistsError("output directory already exists")
    config = load_sql_candidate_config(str(args.candidate_config) if args.candidate_config else None)
    if config.mode == "native":
        if args.model_config is not None:
            raise ValueError("native does not load model configuration")
        return save_report(await evaluate_candidates(
            config=config, tenant_id=args.tenant_id or FIXTURE_TENANT,
            max_calls=args.max_calls, max_seconds=args.max_seconds), args.out)
    if args.model_config is None or not args.tenant_id:
        raise ValueError("candidate mode requires model config and tenant identity")
    from app.model_runtime.config_file import load_model_runtime_config
    from app.model_runtime.governance import govern_inference
    from app.model_runtime.http import OpenAICompatibleInferenceClient
    model = load_model_runtime_config(args.model_config)
    if model.inference.provider != "openai_compatible":
        raise ValueError("candidate CLI requires an explicitly configured compatible inference endpoint")
    key = os.environ.get(model.inference.api_key_env or "", "")
    if not key.strip():
        raise ValueError("model credential environment variable missing")
    inference = govern_inference(OpenAICompatibleInferenceClient(model.inference, api_key=key))
    try:
        generator = build_sql_candidate_generator(config=config, model_config=model, inference=inference)
        return save_report(await evaluate_candidates(
            config=config, generator=generator, tenant_id=args.tenant_id,
            max_calls=args.max_calls, max_seconds=args.max_seconds), args.out)
    finally:
        await inference.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--candidate-config", type=Path)
    run.add_argument("--model-config", type=Path)
    run.add_argument("--tenant-id")
    run.add_argument("--max-calls", type=int, default=100)
    run.add_argument("--max-seconds", type=float, default=60)
    compare = commands.add_parser("compare")
    compare.add_argument("--baseline", type=Path, required=True)
    compare.add_argument("--candidate", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        for name in ("candidate_config", "model_config"):
            path = getattr(args, name, None)
            if path is not None and path.name.startswith(".env"):
                raise ValueError("dotenv is not a configuration input")
        result = asyncio.run(_run_cli(args)) if args.command == "run" else compare_reports(args.baseline, args.candidate)
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "blocked", "message": "Configuration, budget or report contract failed."}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
