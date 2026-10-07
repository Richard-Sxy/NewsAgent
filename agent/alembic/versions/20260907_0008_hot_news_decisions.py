"""Create hot news decisions.

Revision ID: 20260907_0008
Revises: 20260905_0007
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260907_0008"
down_revision = "20260905_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hot_news_decisions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("news_id", sa.String(128), nullable=False),
        sa.Column("decision_type", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "correction_payload",
            postgresql.JSONB(),
            nullable=False,
        ),
        sa.Column("operator_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column(
            "supersedes_decision_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "decision_type IN "
            "('accepted', 'rejected', 'deferred', 'corrected')",
            name="ck_hot_news_decisions_valid_decision_type",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["analysis_runs.id"],
            name="fk_hot_news_decisions_run_id_analysis_runs",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_decision_id"],
            ["hot_news_decisions.id"],
            name=(
                "fk_hot_news_decisions_supersedes_decision_id_"
                "hot_news_decisions"
            ),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "id",
            name="pk_hot_news_decisions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_hot_news_decisions_tenant_idempotency_key",
        ),
    )

    op.create_index(
        "ix_hot_news_decisions_tenant_news_created",
        "hot_news_decisions",
        ["tenant_id", "news_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_hot_news_decisions_tenant_news_created",
        table_name="hot_news_decisions",
    )
    op.drop_table("hot_news_decisions")