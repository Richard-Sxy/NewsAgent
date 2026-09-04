import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.clients.fastgpt import AgentResult
from app.domain.errors import FastGPTTimeoutError
from app.domain.errors import AgentOutputValidationError
from app.domain.job_status import JobStatus
from app.domain.execution import ArtifactType, StepType
from app.schemas.research import ResearchPackage
from app.services.execution import PreparedExecution
from app.services.news_step_handler import NewsStepHandler
from app.workflows.contracts import StepCommand


class FakeDatabase:
    @asynccontextmanager
    async def session(self):
        yield AsyncMock()


def build_handler():
    dependencies = {
        "database": FakeDatabase(),
        "artifact_store": AsyncMock(),
        "checkpoint_service": AsyncMock(),
        "execution_service": AsyncMock(),
        "artifact_pipeline": AsyncMock(),
        "research_runner": AsyncMock(),
        "writer_runner": AsyncMock(),
        "reviewer_runner": AsyncMock(),
        "research_app_id": "research-app",
        "writer_app_id": "writer-app",
        "reviewer_app_id": "reviewer-app",
    }
    return NewsStepHandler(**dependencies), dependencies


def supported_research_package(job_id: str = "job-1") -> ResearchPackage:
    return ResearchPackage.model_validate(
        {
            "job_id": job_id,
            "topic": "近期科技新闻",
            "facts": [
                {
                    "fact_id": "F001",
                    "claim": "事实一",
                    "evidence": "原文证据一",
                    "source_url": "https://example.com/source-1",
                    "source_title": "来源一",
                    "published_at": "2026-08-30T10:00:00+08:00",
                    "confidence": 0.9,
                },
                {
                    "fact_id": "F002",
                    "claim": "事实二",
                    "evidence": "原文证据二",
                    "source_url": "https://example.com/source-2",
                    "source_title": "来源二",
                    "published_at": "2026-08-30T11:00:00+08:00",
                    "confidence": 0.9,
                },
                {
                    "fact_id": "F003",
                    "claim": "事实三",
                    "evidence": "原文证据三",
                    "source_url": "https://example.com/source-1",
                    "source_title": "来源一",
                    "published_at": "2026-08-30T12:00:00+08:00",
                    "confidence": 0.85,
                },
            ],
            "timeline": [
                {
                    "event_id": "T001",
                    "occurred_at": "2026-08-30T12:00:00+08:00",
                    "description": "有事实支撑的事件",
                    "supporting_fact_ids": ["F001"],
                }
            ],
            "suggested_angles": [
                {
                    "angle_id": "A001",
                    "title": "有证据的选题角度",
                    "rationale": "由多条事实支持",
                    "supporting_fact_ids": ["F001", "F002"],
                }
            ],
        }
    )


def test_section_revise_routes_through_reassembly_before_review() -> None:
    handler, _ = build_handler()
    policy = handler.policies[StepType.SECTION_REVISE]

    assert policy.default_target_status == JobStatus.ASSEMBLING
    assert policy.next_step == StepType.ASSEMBLE


@pytest.mark.asyncio
async def test_research_step_runs_agent_and_persists_checkpoint() -> None:
    handler, dependencies = build_handler()
    prepared = PreparedExecution(uuid.uuid4(), uuid.uuid4(), 1)
    dependencies["execution_service"].prepare.return_value = prepared
    job_id = str(uuid.uuid4())
    dependencies["research_runner"].run.return_value = AgentResult(
        value=supported_research_package(job_id),
        request_id="req-1",
        usage={"prompt_tokens": 10, "completion_tokens": 5},
        raw_content="{}",
    )
    dependencies["artifact_pipeline"].persist_success.return_value = SimpleNamespace(
        storage_uri="s3://bucket/research.json",
        content_sha256="a" * 64,
    )
    command = StepCommand(
        tenant_id=str(uuid.uuid4()),
        job_id=job_id,
        step_type="research",
        step_key="research_package_v1",
        inputs={"topic": "主题", "requirements": {"style": "news"}},
    )

    outcome = await handler(command)

    dependencies["research_runner"].run.assert_awaited_once_with(
        job_id=command.job_id,
        topic="主题",
        requirements={"style": "news"},
        previous_package=None,
        revision_instruction=None,
    )
    persisted = dependencies["artifact_pipeline"].persist_success.await_args.kwargs
    assert persisted["target_status"] == JobStatus.RESEARCH_REVIEW
    assert persisted["step_id"] == prepared.step_id
    assert outcome.logical_key == "research_package_v1"
    assert outcome.content_sha256 == "a" * 64


@pytest.mark.asyncio
async def test_retryable_agent_failure_is_written_to_checkpoint() -> None:
    handler, dependencies = build_handler()
    prepared = PreparedExecution(uuid.uuid4(), uuid.uuid4(), 1)
    dependencies["execution_service"].prepare.return_value = prepared
    dependencies["research_runner"].run.side_effect = FastGPTTimeoutError("timeout")
    command = StepCommand(
        tenant_id=str(uuid.uuid4()),
        job_id=str(uuid.uuid4()),
        step_type="research",
        step_key="research_package_v1",
        inputs={"topic": "主题", "requirements": {}},
    )

    with pytest.raises(FastGPTTimeoutError):
        await handler(command)

    failure = dependencies["checkpoint_service"].commit_failure.await_args.args[1]
    assert failure.step_id == prepared.step_id
    assert failure.retryable is True
    assert failure.error_code == "FASTGPTTIMEOUTERROR"
    dependencies["artifact_pipeline"].persist_success.assert_not_awaited()


def test_review_outcome_contains_only_rewrite_sections() -> None:
    value = {
        "decision": "rewrite",
        "section_decisions": [
            {"section_id": "S01", "decision": "approve"},
            {"section_id": "S02", "decision": "rewrite"},
        ],
    }
    outcome = NewsStepHandler._outcome(
        "s3://bucket/review.json",
        "b" * 64,
        StepCommand("tenant", "job", "review", "review_round_1"),
        value,
    )

    assert outcome.decision == "rewrite"
    assert outcome.rewrite_section_ids == ["S02"]


def test_agent_output_cannot_cross_job_boundary() -> None:
    result = AgentResult(
        value=ResearchPackage(job_id="another-job", topic="主题"),
        request_id="req-1",
        usage={},
        raw_content="{}",
    )
    command = StepCommand(
        tenant_id="tenant",
        job_id="expected-job",
        step_type="research",
        step_key="research-v1",
    )

    with pytest.raises(AgentOutputValidationError, match="job_id"):
        NewsStepHandler._validate_agent_result(command, result)


def test_research_package_without_facts_is_rejected_with_all_reasons() -> None:
    package = ResearchPackage.model_validate(
        {
            "job_id": "job-1",
            "topic": "近期科技新闻",
            "timeline": [
                {
                    "event_id": "T001",
                    "occurred_at": "2026-08-30T12:00:00+08:00",
                    "description": "没有事实支撑的事件",
                    "supporting_fact_ids": [],
                }
            ],
            "suggested_angles": [
                {
                    "angle_id": "A001",
                    "title": "没有事实支撑的角度",
                    "rationale": "仅根据模型推测生成",
                    "supporting_fact_ids": [],
                }
            ],
        }
    )
    result = AgentResult(package, "request-1", {}, "{}")
    command = StepCommand("tenant-1", "job-1", "research", "research-v1")

    with pytest.raises(AgentOutputValidationError) as error:
        NewsStepHandler._validate_agent_result(command, result)

    message = str(error.value)
    assert "可追溯事实不足：0 < 3" in message
    assert "独立来源不足：0 < 2" in message
    assert "时间线缺少事实支撑：T001" in message
    assert "选题角度缺少事实支撑：A001" in message


def test_supported_research_package_passes_quality_gate() -> None:
    package = supported_research_package()
    result = AgentResult(package, "request-2", {}, package.model_dump_json())
    command = StepCommand("tenant-1", "job-1", "research", "research-v1")

    NewsStepHandler._validate_agent_result(command, result)


@pytest.mark.asyncio
async def test_failed_research_quality_gate_is_not_persisted() -> None:
    handler, dependencies = build_handler()
    dependencies["execution_service"].prepare.return_value = PreparedExecution(
        uuid.uuid4(), uuid.uuid4(), 1
    )
    job_id = str(uuid.uuid4())
    dependencies["research_runner"].run.return_value = AgentResult(
        ResearchPackage(job_id=job_id, topic="主题"), "req-low-quality", {}, "{}"
    )
    command = StepCommand(
        str(uuid.uuid4()), job_id, "research", "research_package_v1"
    )

    with pytest.raises(AgentOutputValidationError, match="可追溯事实不足"):
        await handler(command)

    dependencies["artifact_pipeline"].persist_success.assert_not_awaited()
    failure = dependencies["checkpoint_service"].commit_failure.await_args.args[1]
    assert failure.retryable is False


@pytest.mark.asyncio
async def test_successful_step_replay_skips_agent_and_advances_job() -> None:
    handler, dependencies = build_handler()
    artifact = SimpleNamespace(
        storage_uri="s3://bucket/research.json",
        content_sha256="a" * 64,
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research_package_v1",
    )
    dependencies["execution_service"].prepare.return_value = PreparedExecution(
        uuid.uuid4(), uuid.uuid4(), 1, artifact
    )
    job_id = str(uuid.uuid4())
    dependencies["artifact_store"].get_json.return_value = {
        **supported_research_package(job_id).model_dump(mode="json"),
    }
    command = StepCommand(
        tenant_id=str(uuid.uuid4()),
        job_id=job_id,
        step_type="research",
        step_key="research_package_v1",
    )

    outcome = await handler(command)

    assert outcome.artifact_uri == artifact.storage_uri
    dependencies["research_runner"].run.assert_not_awaited()
    dependencies["artifact_pipeline"].persist_success.assert_not_awaited()
    advanced = dependencies["execution_service"].advance_replayed_job.await_args.kwargs
    assert advanced["target_status"] == JobStatus.RESEARCH_REVIEW


@pytest.mark.asyncio
async def test_replayed_low_quality_research_artifact_is_rejected() -> None:
    handler, dependencies = build_handler()
    artifact = SimpleNamespace(
        storage_uri="s3://bucket/research.json",
        content_sha256="a" * 64,
        artifact_type=ArtifactType.RESEARCH_PACKAGE,
        logical_key="research_package_v1",
    )
    dependencies["execution_service"].prepare.return_value = PreparedExecution(
        uuid.uuid4(), uuid.uuid4(), 1, artifact
    )
    job_id = str(uuid.uuid4())
    dependencies["artifact_store"].get_json.return_value = {
        "job_id": job_id,
        "topic": "主题",
        "facts": [],
        "timeline": [],
        "conflicts": [],
        "evidence_gaps": [],
        "suggested_angles": [],
    }
    command = StepCommand(
        str(uuid.uuid4()), job_id, "research", "research_package_v1"
    )

    with pytest.raises(AgentOutputValidationError, match="可追溯事实不足"):
        await handler(command)

    dependencies["execution_service"].advance_replayed_job.assert_not_awaited()
