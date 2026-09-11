from dataclasses import dataclass, field
from typing import Any, Literal


HumanAction = Literal[
    "approve",
    "revise",
    "research",
    "cancel",
]
ReviewAction = Literal[
    "approve",
    "rewrite",
    "research",
    "human_review",
]


@dataclass
class NewsWritingInput:
    """启动长链路所需的稳定输入；大对象只通过 Artifact URI 传递。"""

    tenant_id: str
    job_id: str
    topic: str
    requirements: dict[str, Any]
    scenario: Literal["research_package", "assisted_writing"] = "assisted_writing"
    recovery_action: Literal["research"] | None = None
    recovery_instruction: str | None = None


@dataclass
class StepCommand:
    """Workflow 发给 Activity 的幂等业务命令。"""

    tenant_id: str
    job_id: str
    step_type: str
    step_key: str
    attempt: int = 1
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepOutcome:
    """Activity 提交 checkpoint 后返回的轻量控制结果。"""

    artifact_uri: str
    content_sha256: str
    logical_key: str
    section_ids: list[str] = field(default_factory=list)
    decision: ReviewAction | None = None
    rewrite_section_ids: list[str] = field(default_factory=list)


@dataclass
class HumanDecision:
    gate: Literal["research", "outline", "review", "final"]
    action: HumanAction
    instruction: str | None = None


@dataclass
class WorkflowSnapshot:
    phase: str
    completed_steps: int
    review_round: int
    waiting_gate: str | None
    last_artifact_uri: str | None


@dataclass
class WorkflowResult:
    job_id: str
    status: Literal[
        "research_completed", "final_approved", "waiting_human", "cancelled"
    ]
    final_artifact_uri: str | None = None
    research_artifact_uri: str | None = None
    reason: str | None = None


@dataclass
class JobStateCommand:
    tenant_id: str
    job_id: str
    target_status: Literal[
        "research_completed", "waiting_human", "cancelled", "failed"
    ]
    reason: str | None = None


@dataclass(frozen=True)
class HotNewsActivityOutcome:
    """Activity 返回给 Workflow 的轻量结果。"""

    run_id: str
    idempotency_key: str
    status: Literal["completed"]
    fetched_record_count: int
    metric_snapshot_count: int
    ranked_news_count: int
    analyzed_news_count: int


@dataclass(frozen=True)
class HotNewsWindowDispatchRequest:
    """Temporal Schedule 触发的热点窗口分发请求。

    Schedule 的 action 参数是静态的，不能携带"当前时间"；分发 Workflow
    在每次触发时用 ``workflow.now()`` 计算 ``[now - window_minutes, now)``
    窗口，再以子 Workflow 启动一次有边界的热点监控运行。
    每个租户组一个 Schedule，组内只含一个租户的生产 Bundle 配置。
    """

    tenant_group: str
    tenant_id: str
    window_minutes: int
    production_bundle_version: str
    workflow_version: str = "hot-news-workflow-v1"
