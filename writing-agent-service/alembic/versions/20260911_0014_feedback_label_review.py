"""Persist auditable approve and request-changes label reviews.

Revision ID: 20260911_0014
Revises: 20260910_0013
"""

from alembic import op
import sqlalchemy as sa


revision = "20260911_0014"
down_revision = "20260910_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "feedback_labels", sa.Column("reviewed_by", sa.String(128))
    )
    op.add_column(
        "feedback_labels",
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "feedback_labels", sa.Column("review_reason", sa.Text())
    )
    op.add_column(
        "feedback_labels",
        sa.Column("review_idempotency_key", sa.String(160)),
    )
    op.execute(
        """
        UPDATE feedback_labels
        SET reviewed_by = approved_by,
            reviewed_at = approved_at,
            review_reason = 'Approved before review-reason auditing was enabled',
            review_idempotency_key = approval_idempotency_key
        WHERE approval_status IN ('approved', 'superseded')
          AND approved_by IS NOT NULL
        """
    )
    op.create_unique_constraint(
        "uq_feedback_labels_tenant_review_idempotency_key",
        "feedback_labels",
        ["tenant_id", "review_idempotency_key"],
    )
    op.create_check_constraint(
        op.f("ck_feedback_labels_review_metadata_valid"),
        "feedback_labels",
        "(approval_status IN ('approved', 'rejected') "
        "AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL "
        "AND review_reason IS NOT NULL AND review_idempotency_key IS NOT NULL) "
        "OR (approval_status = 'pending' AND reviewed_by IS NULL "
        "AND reviewed_at IS NULL AND review_reason IS NULL "
        "AND review_idempotency_key IS NULL) OR approval_status = 'superseded'",
    )
    op.create_check_constraint(
        op.f("ck_feedback_labels_review_order_valid"),
        "feedback_labels",
        "reviewed_at IS NULL OR reviewed_at >= labeled_at",
    )
    op.create_check_constraint(
        op.f("ck_feedback_labels_review_separation_valid"),
        "feedback_labels",
        "reviewed_by IS NULL OR reviewed_by <> labeled_by",
    )
    op.create_check_constraint(
        op.f("ck_feedback_labels_review_approval_consistent"),
        "feedback_labels",
        "approval_status <> 'approved' OR "
        "(reviewed_by = approved_by AND reviewed_at = approved_at "
        "AND review_idempotency_key = approval_idempotency_key)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_feedback_labels_review_approval_consistent"),
        "feedback_labels",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_feedback_labels_review_separation_valid"),
        "feedback_labels",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_feedback_labels_review_order_valid"),
        "feedback_labels",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_feedback_labels_review_metadata_valid"),
        "feedback_labels",
        type_="check",
    )
    op.drop_constraint(
        "uq_feedback_labels_tenant_review_idempotency_key",
        "feedback_labels",
        type_="unique",
    )
    op.drop_column("feedback_labels", "review_idempotency_key")
    op.drop_column("feedback_labels", "review_reason")
    op.drop_column("feedback_labels", "reviewed_at")
    op.drop_column("feedback_labels", "reviewed_by")
