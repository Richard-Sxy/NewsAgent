"""Add the terminal status for an approved research package.

Revision ID: 20260831_0006
Revises: 20260831_0005
"""

from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260831_0006"
down_revision = "20260831_0005"
branch_labels = None
depends_on = None

previous_job_status = postgresql.ENUM(
    "created",
    "researching",
    "research_review",
    "outlining",
    "outline_review",
    "drafting",
    "assembling",
    "reviewing",
    "revising",
    "final_review",
    "waiting_human",
    "final_approved",
    "published",
    "failed",
    "cancelled",
    name="writing_job_status",
    create_type=False,
)


def upgrade() -> None:
    op.execute(
        "ALTER TYPE writing_job_status "
        "ADD VALUE IF NOT EXISTS 'research_completed' AFTER 'research_review'"
    )


def downgrade() -> None:
    # PostgreSQL 不支持直接删除 ENUM 值；先将新状态回退为可人工继续的状态。
    op.execute(
        "UPDATE writing_jobs SET status = 'waiting_human' "
        "WHERE status = 'research_completed'"
    )
    op.execute("ALTER TYPE writing_job_status RENAME TO writing_job_status_with_research")
    previous_job_status.create(op.get_bind(), checkfirst=False)
    op.execute(
        "ALTER TABLE writing_jobs ALTER COLUMN status "
        "TYPE writing_job_status USING status::text::writing_job_status"
    )
    op.execute("DROP TYPE writing_job_status_with_research")
