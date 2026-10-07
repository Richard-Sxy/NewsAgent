"""The isolated Python model runtime must reach the human review gate."""

from pathlib import Path

import pytest

from app.model_runtime.agent_client import NativeStructuredAgentClient, model_request_context
from app.model_runtime.config_file import load_model_runtime_config
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.factory import build_model_runtime_ports
from app.schemas.review import ReviewInput
from app.schemas.writing import ArticleAssemblyInput, SectionWritingInput
from app.services.agents import ResearchAgentRunner, ReviewerAgentRunner, WriterAgentRunner


@pytest.mark.asyncio
async def test_local_writing_chain_reaches_evidence_based_human_review() -> None:
    config_path = Path(__file__).resolve().parents[1] / "deploy/model-runtime.local.yml"
    config = load_model_runtime_config(config_path)
    ports = build_model_runtime_ports(config, environment="e2e")
    client = NativeStructuredAgentClient(
        StructuredInferenceService(inference=ports.inference, prompts=ports.prompts),
        config,
    )
    research_runner = ResearchAgentRunner(client, "python:research")
    writer_runner = WriterAgentRunner(client, "python:writer")
    reviewer_runner = ReviewerAgentRunner(client, "python:reviewer")
    try:
        with model_request_context(tenant_id="tenant-local", trace_id="writing-test"):
            research = (
                await research_runner.run(
                    job_id="job-local", topic="本地验证", requirements={}
                )
            ).value
            outline = (
                await writer_runner.create_outline(
                    job_id="job-local", research_package=research, requirements={}
                )
            ).value
            section = (
                await writer_runner.write_section(
                    SectionWritingInput(
                        job_id="job-local",
                        section_id=outline.sections[0].section_id,
                        outline=outline,
                        research_package=research,
                    )
                )
            ).value
            draft = (
                await writer_runner.assemble(
                    ArticleAssemblyInput(
                        job_id="job-local", outline=outline, sections=[section]
                    )
                )
            ).value
            review = (
                await reviewer_runner.run(
                    ReviewInput(
                        job_id="job-local",
                        review_round=1,
                        research_package=research,
                        outline=outline,
                        sections=[section],
                        draft=draft,
                    )
                )
            ).value
    finally:
        await ports.close()

    assert review.decision == "human_review"
    assert review.issues
    assert review.issues[0].suggested_action == "human_review"
    assert draft.section_ids == [section.section_id]
