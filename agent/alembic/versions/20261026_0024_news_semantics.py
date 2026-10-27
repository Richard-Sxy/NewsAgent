"""Durable semantic analyses and immutable, evidence-bound news relations."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "20261026_0024"
down_revision = "20261015_0023"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("news_semantic_analyses",
        sa.Column("analysis_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("news_id", sa.String(160), nullable=False),
        sa.Column("source_revision", sa.BigInteger(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("bundle_id", sa.String(64), nullable=False),
        sa.Column("config", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.String(32)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("next_check", sa.DateTime(timezone=True), nullable=False),
        sa.Column("features", JSONB(), nullable=False),
        sa.Column("event_id", sa.String(64)),
        sa.Column("last_error", sa.String(100)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_news_semantic_current", "news_semantic_analyses",
                    ["tenant_id", "news_id", "source_revision", "bundle_id"], unique=True)
    op.create_index("ix_news_semantic_event", "news_semantic_analyses", ["tenant_id", "event_id"])
    op.create_table("news_semantic_pairs",
        sa.Column("decision_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("bundle_id", sa.String(64), nullable=False),
        sa.Column("left_analysis_id", sa.String(64), nullable=False),
        sa.Column("right_analysis_id", sa.String(64), nullable=False),
        sa.Column("left_news_id", sa.String(160), nullable=False),
        sa.Column("right_news_id", sa.String(160), nullable=False),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_news_semantic_pairs_left", "news_semantic_pairs", ["tenant_id", "left_news_id"])
    op.create_index("ix_news_semantic_pairs_right", "news_semantic_pairs", ["tenant_id", "right_news_id"])


def downgrade():
    op.execute(sa.text("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM news_semantic_analyses) OR EXISTS (SELECT 1 FROM news_semantic_pairs) THEN
        RAISE EXCEPTION 'semantic audit data exists; refusing destructive downgrade';
      END IF;
    END $$;"""))
    op.drop_table("news_semantic_pairs")
    op.drop_table("news_semantic_analyses")
