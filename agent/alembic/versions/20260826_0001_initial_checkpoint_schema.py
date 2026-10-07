"""Create durable news-writing checkpoint schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260826_0001"
down_revision = None
branch_labels = None
depends_on = None

job_status = postgresql.ENUM(
    "created", "researching", "research_review", "outlining", "outline_review",
    "drafting", "assembling", "reviewing", "revising", "final_review",
    "waiting_human", "final_approved", "published", "failed", "cancelled",
    name="writing_job_status", create_type=False,
)
step_type = postgresql.ENUM(
    "research", "outline", "section_draft", "assemble", "review",
    "section_revise", "finalize", name="writing_step_type", create_type=False,
)
execution_status = postgresql.ENUM(
    "pending", "running", "succeeded", "failed", "cancelled",
    name="writing_execution_status", create_type=False,
)
artifact_type = postgresql.ENUM(
    "research_package", "outline", "section", "draft", "review_report",
    "final_article", name="writing_artifact_type", create_type=False,
)
agent_type = postgresql.ENUM(
    "research", "writer", "reviewer", "supervisor",
    name="writing_agent_type", create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (job_status, step_type, execution_status, artifact_type, agent_type):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "writing_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("temporal_workflow_id", sa.String(255), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("requirements", postgresql.JSONB(), nullable=False),
        sa.Column("status", job_status, nullable=False),
        sa.Column("current_step", step_type, nullable=True),
        sa.Column("research_retries", sa.Integer(), nullable=False),
        sa.Column("review_rounds", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("research_retries BETWEEN 0 AND 2", name="research_retries_limit"),
        sa.CheckConstraint("review_rounds BETWEEN 0 AND 3", name="review_rounds_limit"),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_writing_jobs"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_writing_jobs_tenant_idempotency_key"),
        sa.UniqueConstraint("tenant_id", "temporal_workflow_id", name="uq_writing_jobs_tenant_temporal_workflow_id"),
    )
    op.create_index("ix_writing_jobs_tenant_status_updated", "writing_jobs", ["tenant_id", "status", "updated_at"])

    op.create_table(
        "writing_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_type", step_type, nullable=False),
        sa.Column("step_key", sa.String(160), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", execution_status, nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("attempt >= 1", name="attempt_positive"),
        sa.ForeignKeyConstraint(["job_id"], ["writing_jobs.id"], name="fk_writing_steps_job_id_writing_jobs", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name="pk_writing_steps"),
        sa.UniqueConstraint("job_id", "step_key", "attempt", name="uq_writing_steps_job_key_attempt"),
    )
    op.create_index("ix_writing_steps_job_status", "writing_steps", ["job_id", "status"])

    op.create_table(
        "writing_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("artifact_type", artifact_type, nullable=False),
        sa.Column("logical_key", sa.String(160), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("storage_uri", sa.String(2048), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("content_size", sa.BigInteger(), nullable=False),
        sa.Column("artifact_metadata", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("version >= 1", name="version_positive"),
        sa.CheckConstraint("content_size >= 0", name="content_size_nonnegative"),
        sa.ForeignKeyConstraint(["job_id"], ["writing_jobs.id"], name="fk_writing_artifacts_job_id_writing_jobs", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["writing_steps.id"], name="fk_writing_artifacts_step_id_writing_steps", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_writing_artifacts"),
        sa.UniqueConstraint("job_id", "logical_key", "version", name="uq_writing_artifacts_job_key_version"),
    )
    op.create_index("ix_writing_artifacts_job_type", "writing_artifacts", ["job_id", "artifact_type"])

    op.create_table(
        "agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("step_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_type", agent_type, nullable=False),
        sa.Column("status", execution_status, nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("fastgpt_app_id", sa.String(255), nullable=False),
        sa.Column("fastgpt_request_id", sa.String(255), nullable=True),
        sa.Column("workflow_version", sa.String(120), nullable=True),
        sa.Column("input_artifact_ids", postgresql.JSONB(), nullable=False),
        sa.Column("output_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_amount", sa.Numeric(18, 6), nullable=True),
        sa.Column("error_code", sa.String(120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["writing_jobs.id"], name="fk_agent_runs_job_id_writing_jobs", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["step_id"], ["writing_steps.id"], name="fk_agent_runs_step_id_writing_steps", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["output_artifact_id"], ["writing_artifacts.id"], name="fk_agent_runs_output_artifact_id_writing_artifacts", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name="pk_agent_runs"),
        sa.UniqueConstraint("step_id", "idempotency_key", name="uq_agent_runs_step_idempotency_key"),
    )
    op.create_index("ix_agent_runs_job_status", "agent_runs", ["job_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_job_status", table_name="agent_runs")
    op.drop_table("agent_runs")
    op.drop_index("ix_writing_artifacts_job_type", table_name="writing_artifacts")
    op.drop_table("writing_artifacts")
    op.drop_index("ix_writing_steps_job_status", table_name="writing_steps")
    op.drop_table("writing_steps")
    op.drop_index("ix_writing_jobs_tenant_status_updated", table_name="writing_jobs")
    op.drop_table("writing_jobs")
    bind = op.get_bind()
    for enum in (agent_type, artifact_type, execution_status, step_type, job_status):
        enum.drop(bind, checkfirst=True)
