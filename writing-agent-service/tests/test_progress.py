import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.domain.execution import StepType
from app.domain.job_status import JobStatus
from app.schemas.events import ProgressEvent
from app.services.progress import (
    InvalidProgressInput,
    ProgressCalculator,
    ProgressContext,
)


def test_fixed_step_progress_weights() -> None:
    calculator = ProgressCalculator()

    assert calculator.calculate(ProgressContext(StepType.RESEARCH)).percent == 15
    assert calculator.calculate(ProgressContext(StepType.OUTLINE)).percent == 25
    assert calculator.calculate(ProgressContext(StepType.ASSEMBLE)).percent == 75
    assert calculator.calculate(ProgressContext(StepType.REVIEW)).percent == 90
    assert calculator.calculate(ProgressContext(StepType.FINALIZE)).percent == 100


def test_section_progress_uses_completed_over_total() -> None:
    progress = ProgressCalculator().calculate(
        ProgressContext(
            step=StepType.SECTION_DRAFT,
            completed_sections=3,
            total_sections=6,
        )
    )

    assert progress.completed == 3
    assert progress.total == 6
    assert progress.percent == 45


def test_progress_never_moves_backwards_during_revision() -> None:
    progress = ProgressCalculator().calculate(
        ProgressContext(
            step=StepType.ASSEMBLE,
            previous_percent=90,
        )
    )
    assert progress.percent == 90


def test_section_progress_rejects_invalid_counts() -> None:
    with pytest.raises(InvalidProgressInput):
        ProgressCalculator().calculate(
            ProgressContext(
                step=StepType.SECTION_DRAFT,
                completed_sections=4,
                total_sections=3,
            )
        )


def test_progress_event_contains_reference_but_no_body_field() -> None:
    event = ProgressEvent(
        event="step.completed",
        deduplication_key="job-1:research:1:completed",
        tenant_id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        step=StepType.RESEARCH,
        status=JobStatus.RESEARCH_REVIEW,
        progress={"completed": 1, "total": 1, "percent": 15},
        artifact={
            "logical_key": "research_package_v1",
            "version": 1,
        },
        occurred_at=datetime.now(timezone.utc),
    )

    payload = event.model_dump(mode="json")
    assert payload["artifact"]["logical_key"] == "research_package_v1"
    assert "content" not in payload
    assert "body" not in payload


def test_section_id_must_follow_contract() -> None:
    with pytest.raises(ValidationError):
        ProgressEvent(
            event="step.completed",
            deduplication_key="invalid-section",
            tenant_id=uuid.uuid4(),
            job_id=uuid.uuid4(),
            step=StepType.SECTION_DRAFT,
            section_id="section-one",
            status=JobStatus.DRAFTING,
            progress={"completed": 1, "total": 2, "percent": 45},
            occurred_at=datetime.now(timezone.utc),
        )
