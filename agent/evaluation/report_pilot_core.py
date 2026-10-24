"""Bounded first-pass evaluation using native writing Runners, without Temporal.

Importing this module never loads application settings or credentials. Only the
explicit client context can construct an HTTP client; there is no CLI or default
network execution here. Reviewer self-scores are not independent quality scores.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from time import monotonic
from typing import Any
from uuid import uuid4

from pydantic import HttpUrl, TypeAdapter

from app.domain.execution import StepType
from app.model_runtime.agent_client import NativeStructuredAgentClient, model_request_context
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.http import ModelTransportError, OpenAICompatibleInferenceClient
from app.model_runtime.result import AgentResult
from app.modules.writing.agents import ResearchAgentRunner, ReviewerAgentRunner, WriterAgentRunner
from app.modules.writing.contracts.review import ReviewInput
from app.modules.writing.contracts.writing import ArticleAssemblyInput, SectionWritingInput
from app.modules.writing.news_step_handler import NewsStepHandler
from app.workflows.contracts import StepCommand


class BudgetExceeded(RuntimeError):
    """A stable code, never a provider's exception message."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ReportStageError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass
class RunBudget:
    """Share one instance across cases; attempts and deadline never reset."""

    max_calls: int
    max_wall_seconds: float
    max_sections: int = 2
    attempts: int = field(default=0, init=False)
    started_at: float = field(default_factory=monotonic, init=False)

    def __post_init__(self) -> None:
        if type(self.max_calls) is not int or self.max_calls < 0:
            raise ValueError("max_calls must be a nonnegative integer")
        if (type(self.max_wall_seconds) not in (int, float)
                or not math.isfinite(self.max_wall_seconds) or self.max_wall_seconds <= 0):
            raise ValueError("max_wall_seconds must be finite and positive")
        if type(self.max_sections) is not int or not 1 <= self.max_sections <= 20:
            raise ValueError("max_sections must be an integer between one and twenty")

    @property
    def remaining_seconds(self) -> float:
        return self.max_wall_seconds - (monotonic() - self.started_at)

    def reserve(self) -> float:
        remaining = self.remaining_seconds
        if remaining <= 0:
            raise BudgetExceeded("wall_budget_exhausted")
        if self.attempts >= self.max_calls:
            raise BudgetExceeded("call_budget_exhausted")
        self.attempts += 1
        return remaining


@asynccontextmanager
async def open_report_client(
    *, config_path: Path, api_key: str,
) -> AsyncIterator[NativeStructuredAgentClient]:
    """Construct an explicitly configured inference client; no inference or env read."""
    config = load_model_runtime_config(config_path)
    for scene in ("research", "outline", "section", "revise", "assemble", "review"):
        config.agent_scene(scene)
    if config.inference.provider != "openai_compatible":
        raise ValueError("report evaluation requires an explicit HTTP inference configuration")
    inference = OpenAICompatibleInferenceClient(config.inference, api_key=api_key)
    try:
        yield NativeStructuredAgentClient(
            StructuredInferenceService(inference=inference, prompts=config.prompt_registry()),
            config, timeout_seconds=config.inference.timeout_seconds,
        )
    finally:
        await inference.close()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _write_new(path: Path, value: Any) -> str:
    encoded = _json_bytes(value)
    with path.open("xb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256(encoded).hexdigest()


def _append_attempt(path: Path, value: dict[str, Any]) -> None:
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    with path.open("ab") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def _safe_identifier(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,191}", value):
        return value
    return None


def _safe_usage(value: Any) -> dict[str, int]:
    allowed = ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens",
               "output_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")
    return {key: value[key] for key in allowed
            if isinstance(value, dict) and type(value.get(key)) is int and value[key] >= 0}


def _raw_identity(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"raw_sha256": None, "raw_bytes": None}
    encoded = value.encode("utf-8")
    return {"raw_sha256": sha256(encoded).hexdigest(), "raw_bytes": len(encoded)}


def _url(value: Any) -> str:
    return str(TypeAdapter(HttpUrl).validate_python(value))


def _validate_case(case: dict[str, Any], budget: RunBudget) -> tuple[str, dict, set[str], int]:
    if not isinstance(case, dict):
        raise ReportStageError("invalid_case")
    for key in ("case_id", "event_group_id", "split"):
        if not isinstance(case.get(key), str) or not case[key].strip():
            raise ReportStageError("missing_case_identity")
    data = case.get("input")
    if not isinstance(data, dict) or not isinstance(data.get("topic"), str) or not data["topic"].strip():
        raise ReportStageError("missing_topic")
    requirements = data.get("requirements")
    if not isinstance(requirements, dict):
        raise ReportStageError("missing_requirements")
    documents = requirements.get("source_documents")
    if not isinstance(documents, list) or not documents:
        raise ReportStageError("missing_source_documents")
    try:
        allowed = {_url(item["url"]) for item in documents}
    except (KeyError, TypeError, ValueError):
        raise ReportStageError("invalid_source_url") from None
    if len(allowed) < 2:
        raise ReportStageError("missing_second_source")
    if any(not isinstance(item.get("content"), str) or not item["content"].strip() for item in documents):
        raise ReportStageError("empty_source_content")
    limit = requirements.get("max_sections", budget.max_sections)
    if type(limit) is not int or not 1 <= limit <= budget.max_sections:
        raise ReportStageError("case_section_budget_exceeded")
    return data["topic"], deepcopy(requirements), allowed, limit


def _source_checks(value: Any, allowed: set[str], research: Any | None = None) -> None:
    if hasattr(value, "facts"):
        if any(str(fact.source_url) not in allowed for fact in value.facts):
            raise ReportStageError("unknown_source_url")
    known_facts = {fact.fact_id: str(fact.source_url) for fact in research.facts} if research is not None else {}
    for citation in getattr(value, "citations", []):
        url = str(citation.source_url)
        if url not in allowed or known_facts.get(citation.fact_id) != url:
            raise ReportStageError("unknown_or_mismatched_citation")
    if hasattr(value, "used_fact_ids") and set(value.used_fact_ids) - set(known_facts):
        raise ReportStageError("unknown_used_fact")
    if hasattr(value, "sections"):
        if any(set(section.required_fact_ids) - set(known_facts) for section in value.sections):
            raise ReportStageError("unknown_outline_fact")
    if hasattr(value, "issues"):
        if any(set(issue.fact_ids) - set(known_facts) for issue in value.issues):
            raise ReportStageError("unknown_review_fact")


async def run_report_case(
    case: dict[str, Any], client: NativeStructuredAgentClient, output_dir: Path,
    *, budget: RunBudget | None = None,
) -> dict[str, Any]:
    """Run one first pass with real Runners and business gates; never approve/retry.

    Only case.input.topic/requirements enter the model. A successful first pass
    with N chapters makes N+4 calls, ending after Reviewer even if it recommends
    revision or human review. ``completed`` denotes technical stage completion,
    never factual approval, production Workflow completion or publication.
    Necessary structured outputs persist; raw responses only contribute hashes.
    ``budget`` is shared across cases when supplied. It counts attempted calls,
    including failures, and enforces the overall deadline outside the HTTP Port.
    """
    active_budget = budget if budget is not None else RunBudget(6, 900.0, 2)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "attempts.jsonl").touch(exist_ok=False)
    case_identity = case if isinstance(case, dict) else {}
    job_id = f"report-{uuid4().hex}"
    trace: dict[str, Any] = {
        "case_id": case_identity.get("case_id"), "event_group_id": case_identity.get("event_group_id"),
        "split": case_identity.get("split"), "job_id": job_id, "status": "blocked",
        "review_decision": None, "model_calls": 0, "stages": [],
        "outputs": {"research": None, "outline": None, "sections": [], "draft": None, "review": None},
        "quality_status": "pending_human_review", "mode": "native_stages_first_pass_only",
        "automatic_revision": False, "automatic_retry": False, "human_approval": False,
    }
    planned = ["research", "outline", "sections", "assemble", "review"]
    try:
        topic, requirements, allowed_urls, max_sections = _validate_case(case, active_budget)
    except (ReportStageError, ValueError, TypeError) as exc:
        trace["error_type"] = type(exc).__name__
        trace["error_code"] = exc.code if isinstance(exc, ReportStageError) else "invalid_input"
        for stage in planned:
            trace["stages"].append({"stage": stage, "status": "not_run", "elapsed_ms": 0,
                                    "request_id": None, "usage": {}, "reason": "preflight_blocked"})
        trace.update(planned_stage_count=len(planned), completed_stage_count=0,
                     failed_stage_count=0, not_run_stage_count=len(planned))
        _write_new(output_dir / "execution.json", trace)
        return trace

    trace["input_sha256"] = sha256(_json_bytes({"topic": topic, "requirements": requirements})).hexdigest()
    trace["source_urls_sha256"] = sha256(_json_bytes(sorted(allowed_urls))).hexdigest()
    trace["max_sections"] = max_sections
    research_runner = ResearchAgentRunner(client, "python:research")
    writer_runner = WriterAgentRunner(client, "python:writer")
    reviewer_runner = ReviewerAgentRunner(client, "python:reviewer")
    outputs = trace["outputs"]
    failed = False

    async def invoke(
        stage: str, mode: str, step_type: StepType, inputs: dict[str, Any],
        call: Callable[[], Awaitable[AgentResult]], retain: Callable[[dict], None],
        validate: Callable[[Any], None],
    ) -> Any | None:
        nonlocal failed
        stage_record: dict[str, Any] = {
            "stage": stage, "status": "not_run", "elapsed_ms": 0.0,
            "request_id": None, "usage": {}, "raw_sha256": None, "raw_bytes": None,
        }
        trace["stages"].append(stage_record)
        try:
            remaining = active_budget.reserve()
        except BudgetExceeded as exc:
            stage_record["reason"] = exc.code
            trace["status"] = "blocked"
            trace["error_code"] = exc.code
            failed = True
            return None
        trace["model_calls"] += 1
        trace_id = uuid4().hex
        stage_record["trace_id"] = trace_id
        stage_record["attempt"] = 1
        # Public route/prompt identity only: never inspect or serialize credentials.
        binding = client._config.agent_scene(mode)
        stage_record.update(model_route=binding.model_route, prompt_version=binding.prompt_version)
        _append_attempt(output_dir / "attempts.jsonl", {
            "stage": stage, "trace_id": trace_id, "case_id": trace["case_id"],
            "case_attempt": trace["model_calls"], "shared_attempt": active_budget.attempts,
            "status": "attempt_started", "model_route": binding.model_route,
            "prompt_version": binding.prompt_version,
        })
        command = StepCommand(tenant_id="offline-report-pilot", job_id=job_id,
                              step_type=step_type.value, step_key=stage, inputs=inputs)
        started = monotonic()
        structured = None
        value = None
        timeout = min(remaining, client._timeout_seconds)
        try:
            # Artifact I/O is part of the shared wall budget; recheck after it.
            if active_budget.remaining_seconds <= 0:
                raise BudgetExceeded("wall_budget_exhausted")
            timeout = min(timeout, active_budget.remaining_seconds)
            with model_request_context(tenant_id="offline-report-pilot", trace_id=trace_id):
                async with asyncio.timeout(timeout):
                    result = await call()
            stage_record.update(request_id=_safe_identifier(result.request_id), usage=_safe_usage(result.usage),
                                **_raw_identity(result.raw_content))
            value = result.value
            structured = value.model_dump(mode="json")
            retain(structured)
            NewsStepHandler._validate_agent_result(command, result)
            validate(value)
            stage_record["status"] = "completed"
        except Exception as exc:
            failed = True
            stage_record["status"] = "failed"
            stage_record["error_type"] = type(exc).__name__
            stage_record["error_code"] = exc.code if isinstance(exc, (ReportStageError, BudgetExceeded)) else "stage_failure"
            stage_record["failure_stage"] = _safe_identifier(getattr(exc, "failure_stage", None))
            stage_record["failure_kind"] = _safe_identifier(getattr(exc, "failure_kind", None))
            if stage_record["raw_sha256"] is None:
                stage_record.update(**_raw_identity(getattr(exc, "raw_content", None)))
            if stage_record["request_id"] is None:
                stage_record["request_id"] = _safe_identifier(getattr(exc, "request_id", None))
            trace["status"] = "failed"
            if isinstance(exc, ModelTransportError) and (
                stage_record["failure_stage"] == "model_binding"
                or stage_record["failure_kind"] in {"http_error", "rate_limit", "connection"}
            ):
                # The attempted call remains a failed technical stage, but its
                # provider identity/dependency is not valid evidence for quality.
                trace["status"] = "blocked"
                trace["error_code"] = stage_record["error_code"] = (
                    stage_record["failure_kind"] or "model_binding_failure"
                )
                trace["failure_stage"] = stage_record["failure_stage"]
                trace["failure_kind"] = stage_record["failure_kind"]
            if isinstance(exc, BudgetExceeded) or (isinstance(exc, TimeoutError) and active_budget.remaining_seconds <= 0):
                trace["status"] = "blocked"
                trace["error_code"] = stage_record["error_code"] = "wall_budget_exhausted"
            value = None
        finally:
            stage_record["elapsed_ms"] = round((monotonic() - started) * 1000, 3)
            filename = f"{len(trace['stages']):02d}-{stage}.json"
            digest = _write_new(output_dir / filename, {**stage_record, "output": structured})
            stage_record.update(artifact=filename, sha256=digest)
        return value

    def outline_checks(value: Any, research: Any) -> None:
        if len(value.sections) > max_sections:
            raise ReportStageError("outline_section_limit_exceeded")
        _source_checks(value, allowed_urls, research)

    research = await invoke(
        "research", "research", StepType.RESEARCH, {},
        lambda: research_runner.run(job_id=job_id, topic=topic, requirements=requirements),
        lambda value: outputs.update(research=value),
        lambda value: _source_checks(value, allowed_urls),
    )
    outline = None
    sections = []
    draft = None
    if not failed:
        outline = await invoke(
            "outline", "outline", StepType.OUTLINE, {},
            lambda: writer_runner.create_outline(job_id=job_id, research_package=research, requirements=requirements),
            lambda value: outputs.update(outline=value),
            lambda value: outline_checks(value, research),
        )
    if not failed:
        ordered = sorted(outline.sections, key=lambda section: section.order)
        section_stage_names = [f"section_{index:02d}" for index in range(1, len(ordered) + 1)]
        planned = ["research", "outline", *section_stage_names, "assemble", "review"]
        for stage, section_plan in zip(section_stage_names, ordered, strict=True):
            def write_one():
                return writer_runner.write_section(SectionWritingInput(
                    job_id=job_id, section_id=section_plan.section_id, outline=outline,
                    research_package=research, requirements=requirements,
                    previous_sections_summary={section.section_id: section.summary for section in sections},
                ))
            section = await invoke(
                stage, "section", StepType.SECTION_DRAFT, {"section_id": section_plan.section_id},
                write_one, lambda value: outputs["sections"].append(value),
                lambda value: _source_checks(value, allowed_urls, research),
            )
            if failed:
                break
            sections.append(section)
    if not failed:
        section_refs = {section.section_id: {} for section in sections}
        draft = await invoke(
            "assemble", "assemble", StepType.ASSEMBLE, {"section_artifacts": section_refs},
            lambda: writer_runner.assemble(ArticleAssemblyInput(job_id=job_id, outline=outline, sections=sections)),
            lambda value: outputs.update(draft=value),
            lambda value: _source_checks(value, allowed_urls, research),
        )
    if not failed:
        def validate_review(value: Any) -> None:
            _source_checks(value, allowed_urls, research)
            section_ids = {section.section_id for section in sections}
            if any(issue.section_id is not None and issue.section_id not in section_ids for issue in value.issues):
                raise ReportStageError("unknown_review_section")

        review = await invoke(
            "review", "review", StepType.REVIEW,
            {"review_round": 1, "section_artifacts": {section.section_id: {} for section in sections}},
            lambda: reviewer_runner.run(ReviewInput(job_id=job_id, review_round=1, research_package=research,
                                                   outline=outline, sections=sections, draft=draft)),
            lambda value: outputs.update(review=value), validate_review,
        )
        if not failed:
            trace["review_decision"] = review.decision
            trace["status"] = "completed"
    present = {stage["stage"] for stage in trace["stages"]}
    for stage in planned:
        if stage not in present:
            trace["stages"].append({"stage": stage, "status": "not_run", "elapsed_ms": 0.0,
                                    "request_id": None, "usage": {}, "reason": "upstream_not_completed"})
    trace["planned_stage_count"] = len(planned)
    trace["completed_stage_count"] = sum(stage["status"] == "completed" for stage in trace["stages"])
    trace["failed_stage_count"] = sum(stage["status"] == "failed" for stage in trace["stages"])
    trace["not_run_stage_count"] = sum(stage["status"] == "not_run" for stage in trace["stages"])
    _write_new(output_dir / "execution.json", trace)
    return trace


def score_report_case(
    trace: dict[str, Any], gold: dict[str, Any], human_review: dict[str, Any],
) -> dict[str, Any]:
    """Lazy forwarding keeps independent scoring out of the inference chain."""
    from evaluation.report_pilot_scoring import score_report_case as score
    return score(trace, gold, human_review)
