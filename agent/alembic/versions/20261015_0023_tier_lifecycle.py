"""Add opt-in durable news/projection lifecycle ledger and local vector substitute.

Revision ID: 20261015_0023
Revises: 20261010_0022
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261015_0023"
down_revision = "20261010_0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tier_lifecycle_news",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("news_id", sa.String(160), primary_key=True),
        sa.Column("document_id", sa.String(160), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("event_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("publish_time", sa.DateTime(timezone=True)),
        sa.Column("source_url", sa.Text()),
        sa.Column("desired_tier", sa.String(16), nullable=False),
        sa.Column("active_tier", sa.String(16)),
        sa.Column("active_job_id", sa.String(32)),
        sa.Column("current_job_id", sa.String(32), nullable=False),
        sa.Column("accepted_job_id", sa.String(32), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("hold_until", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "document_id", name="uq_tier_lifecycle_news_document"),
    )
    op.create_table(
        "tier_lifecycle_jobs",
        sa.Column("job_id", sa.String(32), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("news_id", sa.String(160), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_tier", sa.String(16), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("records", postgresql.JSONB(), nullable=False),
        sa.Column("chunk_ids", postgresql.JSONB(), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cleanup_dirty", sa.Boolean(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tier_lifecycle_jobs_news", "tier_lifecycle_jobs", ["tenant_id", "news_id"])
    op.create_index("ix_tier_lifecycle_jobs_pending", "tier_lifecycle_jobs", ["status", "job_id"])
    op.create_table(
        "tier_lifecycle_vectors",
        sa.Column("chunk_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_tier_lifecycle_vectors_scope", "tier_lifecycle_vectors", ["tenant_id", "tier"])


def downgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM tier_lifecycle_news)
               OR EXISTS (SELECT 1 FROM tier_lifecycle_jobs)
               OR EXISTS (SELECT 1 FROM tier_lifecycle_vectors) THEN
                RAISE EXCEPTION 'tier lifecycle data exists; downgrade would lose accepted work';
            END IF;
        END $$;
    """))
    op.drop_index("ix_tier_lifecycle_vectors_scope", table_name="tier_lifecycle_vectors")
    op.drop_table("tier_lifecycle_vectors")
    op.drop_index("ix_tier_lifecycle_jobs_pending", table_name="tier_lifecycle_jobs")
    op.drop_index("ix_tier_lifecycle_jobs_news", table_name="tier_lifecycle_jobs")
    op.drop_table("tier_lifecycle_jobs")
    op.drop_table("tier_lifecycle_news")
