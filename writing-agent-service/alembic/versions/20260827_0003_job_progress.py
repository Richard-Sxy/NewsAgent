"""Persist monotonic writing job progress."""

from alembic import op
import sqlalchemy as sa

revision = "20260827_0003"
down_revision = "20260827_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "writing_jobs",
        sa.Column("progress_percent", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "writing_jobs",
        sa.Column("sections_completed", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "writing_jobs",
        sa.Column("sections_total", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "progress_percent_range", "writing_jobs", "progress_percent BETWEEN 0 AND 100"
    )
    op.create_check_constraint(
        "sections_completed_nonnegative", "writing_jobs", "sections_completed >= 0"
    )
    op.create_check_constraint(
        "sections_total_nonnegative", "writing_jobs", "sections_total >= 0"
    )
    op.create_check_constraint(
        "sections_progress_valid", "writing_jobs", "sections_completed <= sections_total"
    )


def downgrade() -> None:
    op.drop_constraint("sections_progress_valid", "writing_jobs", type_="check")
    op.drop_constraint("sections_total_nonnegative", "writing_jobs", type_="check")
    op.drop_constraint("sections_completed_nonnegative", "writing_jobs", type_="check")
    op.drop_constraint("progress_percent_range", "writing_jobs", type_="check")
    op.drop_column("writing_jobs", "sections_total")
    op.drop_column("writing_jobs", "sections_completed")
    op.drop_column("writing_jobs", "progress_percent")
