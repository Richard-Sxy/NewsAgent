"""Capture all four operator decisions without changing historical case hashes."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261010_0021"
down_revision = "20261007_0020"
branch_labels = None
depends_on = None

LEGACY_SOURCES = (
    "'operator_rejected', 'operator_corrected', 'validation_failed', "
    "'low_confidence', 'retrieval_error', 'post_publish_outcome'"
)
SOURCE_CONSTRAINT = "ck_feedback_cases_source_type_valid"


def upgrade() -> None:
    op.add_column(
        "hot_news_decisions",
        sa.Column("operator_feedback", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.create_check_constraint(
        "operator_feedback_object", "hot_news_decisions",
        "operator_feedback IS NULL OR jsonb_typeof(operator_feedback) = 'object'",
    )
    op.drop_constraint(op.f(SOURCE_CONSTRAINT), "feedback_cases", type_="check")
    op.create_check_constraint(
        op.f(SOURCE_CONSTRAINT), "feedback_cases",
        f"source_type IN ('operator_accepted', 'operator_deferred', {LEGACY_SOURCES})",
    )


def downgrade() -> None:
    # Losing operator judgments or rewriting frozen source references would
    # destroy audit evidence. A downgrade must fail while new evidence exists.
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM feedback_cases
                WHERE source_type IN ('operator_accepted', 'operator_deferred')
            ) OR EXISTS (
                SELECT 1 FROM hot_news_decisions WHERE operator_feedback IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'operations feedback exists; downgrade would lose audit evidence';
            END IF;
        END $$;
    """))
    op.drop_constraint(op.f(SOURCE_CONSTRAINT), "feedback_cases", type_="check")
    op.create_check_constraint(
        op.f(SOURCE_CONSTRAINT), "feedback_cases", f"source_type IN ({LEGACY_SOURCES})",
    )
    op.drop_constraint("operator_feedback_object", "hot_news_decisions", type_="check")
    op.drop_column("hot_news_decisions", "operator_feedback")
