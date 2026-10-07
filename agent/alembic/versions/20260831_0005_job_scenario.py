"""Add an operation-facing scenario to writing jobs.

Revision ID: 20260831_0005
Revises: 20260828_0004
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "20260831_0005"
down_revision = "20260828_0004"
branch_labels = None
depends_on = None

job_scenario = postgresql.ENUM(
    "research_package",
    "assisted_writing",
    name="writing_job_scenario",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    job_scenario.create(bind, checkfirst=True)
    op.add_column(
        "writing_jobs",
        sa.Column(
            "scenario",
            job_scenario,
            nullable=False,
            server_default="assisted_writing",
        ),
    )
    # 默认值只用于回填已有任务；新任务必须显式由业务层写入场景。
    op.alter_column("writing_jobs", "scenario", server_default=None)


def downgrade() -> None:
    op.drop_column("writing_jobs", "scenario")
    job_scenario.drop(op.get_bind(), checkfirst=True)
