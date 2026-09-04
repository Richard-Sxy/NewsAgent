from app.domain.job_status import JobStatus

class InvalidJobTransition(ValueError):
    """允许的任务状态转移。"""

ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.CREATED: {
        JobStatus.RESEARCHING,
        JobStatus.CANCELLED,
    },
    JobStatus.RESEARCHING: {
        JobStatus.RESEARCH_REVIEW,
        JobStatus.WAITING_HUMAN,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.RESEARCH_REVIEW: {
        JobStatus.RESEARCHING,
        JobStatus.RESEARCH_COMPLETED,
        JobStatus.OUTLINING,
        JobStatus.WAITING_HUMAN,
        JobStatus.CANCELLED,
    },
    JobStatus.RESEARCH_COMPLETED: set(),
    JobStatus.OUTLINING: {
        JobStatus.OUTLINE_REVIEW,
        JobStatus.WAITING_HUMAN,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.OUTLINE_REVIEW: {
        JobStatus.OUTLINING,
        JobStatus.DRAFTING,
        JobStatus.WAITING_HUMAN,
        JobStatus.CANCELLED,
    },
    JobStatus.DRAFTING: {
        JobStatus.ASSEMBLING,
        JobStatus.FAILED,
        JobStatus.WAITING_HUMAN,
        JobStatus.CANCELLED,
    },
    JobStatus.ASSEMBLING: {
        JobStatus.REVIEWING,
        JobStatus.WAITING_HUMAN,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.REVIEWING: {
        JobStatus.FINAL_REVIEW,
        JobStatus.REVISING,
        JobStatus.RESEARCHING,
        JobStatus.WAITING_HUMAN,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.REVISING: {
        JobStatus.ASSEMBLING,
        JobStatus.REVIEWING,
        JobStatus.FAILED,
        JobStatus.WAITING_HUMAN,
        JobStatus.CANCELLED,
    },
    JobStatus.FINAL_REVIEW: {
        JobStatus.FINAL_APPROVED,
        JobStatus.REVISING,
        JobStatus.WAITING_HUMAN,
        JobStatus.CANCELLED,
    },
    JobStatus.WAITING_HUMAN: {
        JobStatus.RESEARCHING,
        JobStatus.OUTLINING,
        JobStatus.DRAFTING,
        JobStatus.REVIEWING,
        JobStatus.FINAL_REVIEW,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.FINAL_APPROVED: {
        JobStatus.PUBLISHED,
        JobStatus.CANCELLED,
    },
    JobStatus.PUBLISHED: set(),
    JobStatus.FAILED: {
        JobStatus.RESEARCHING,
        JobStatus.OUTLINING,
        JobStatus.DRAFTING,
        JobStatus.ASSEMBLING,
        JobStatus.REVIEWING,
        JobStatus.REVISING,
        JobStatus.CANCELLED,
    },
    JobStatus.CANCELLED: set(),
}

def can_transition(
    current: JobStatus,
    target: JobStatus,
) -> bool:
    """判断任务是否允许从当前状态迁移到目标状态。"""
    return target in ALLOWED_TRANSITIONS[current]

def validate_transition(
    current: JobStatus,
    target: JobStatus,
) -> None:
    """校验状态迁移，非法时抛出明确异常。"""
    if can_transition(current, target):
        return

    raise InvalidJobTransition(
        f"不允许任务从 {current.value} 迁移到 {target.value}"
    )
