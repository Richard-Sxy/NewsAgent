import pytest

from app.domain.job_status import JobStatus
from app.domain.transition import (
    ALLOWED_TRANSITIONS,
    InvalidJobTransition,
    can_transition,
    validate_transition,
)


def test_all_job_statuses_have_transition_definition() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.CREATED, JobStatus.RESEARCHING),
        (JobStatus.RESEARCHING, JobStatus.RESEARCH_REVIEW),
        (JobStatus.RESEARCH_REVIEW, JobStatus.RESEARCH_COMPLETED),
        (JobStatus.RESEARCH_REVIEW, JobStatus.OUTLINING),
        (JobStatus.OUTLINING, JobStatus.OUTLINE_REVIEW),
        (JobStatus.OUTLINE_REVIEW, JobStatus.DRAFTING),
        (JobStatus.DRAFTING, JobStatus.ASSEMBLING),
        (JobStatus.ASSEMBLING, JobStatus.REVIEWING),
        (JobStatus.REVIEWING, JobStatus.FINAL_REVIEW),
        (JobStatus.REVIEWING, JobStatus.REVISING),
        (JobStatus.REVIEWING, JobStatus.RESEARCHING),
        (JobStatus.REVISING, JobStatus.ASSEMBLING),
        (JobStatus.REVISING, JobStatus.REVIEWING),
        (JobStatus.FINAL_REVIEW, JobStatus.FINAL_APPROVED),
        (JobStatus.FINAL_APPROVED, JobStatus.PUBLISHED),
    ],
)
def test_readme_workflow_transitions_are_allowed(
    current: JobStatus,
    target: JobStatus,
) -> None:
    validate_transition(current, target)


def test_waiting_human_can_resume_at_supported_stage() -> None:
    assert can_transition(
        JobStatus.WAITING_HUMAN,
        JobStatus.REVIEWING,
    )


def test_waiting_human_can_be_synchronized_to_failed_after_activity_error() -> None:
    assert can_transition(JobStatus.WAITING_HUMAN, JobStatus.FAILED)


@pytest.mark.parametrize(
    "terminal_status",
    [JobStatus.RESEARCH_COMPLETED, JobStatus.PUBLISHED, JobStatus.CANCELLED],
)
def test_terminal_status_has_no_outgoing_transition(
    terminal_status: JobStatus,
) -> None:
    assert ALLOWED_TRANSITIONS[terminal_status] == set()


def test_created_cannot_skip_directly_to_drafting() -> None:
    with pytest.raises(
        InvalidJobTransition,
        match="created.*drafting",
    ):
        validate_transition(
            JobStatus.CREATED,
            JobStatus.DRAFTING,
        )
