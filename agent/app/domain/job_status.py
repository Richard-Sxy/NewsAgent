"""定义任务状态。 JobStatus 包含创建、研究、提纲、写作、审核、返工、终稿批准、发布、失败和取消等状态。"""

from enum import Enum


class JobStatus(str, Enum):
    """新闻写作任务在显式状态机中的业务状态。"""

    CREATED = "created"
    RESEARCHING = "researching"
    RESEARCH_REVIEW = "research_review"
    RESEARCH_COMPLETED = "research_completed"
    OUTLINING = "outlining"
    OUTLINE_REVIEW = "outline_review"
    DRAFTING = "drafting"
    ASSEMBLING = "assembling"
    REVIEWING = "reviewing"
    REVISING = "revising"
    FINAL_REVIEW = "final_review"
    WAITING_HUMAN = "waiting_human"
    FINAL_APPROVED = "final_approved"
    PUBLISHED = "published"
    FAILED = "failed"
    CANCELLED = "cancelled"
