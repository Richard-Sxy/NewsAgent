"""Persist scoped supplementary conversation inputs without rewriting history.

Revision ID: 20261010_0022
Revises: 20261010_0021
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261010_0022"
down_revision = "20261010_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_conversation_turns_owner_id", "conversation_turns",
        ["tenant_id", "user_id", "conversation_id", "id"],
    )
    op.create_table(
        "conversation_inputs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("content", sa.String(4000), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_inputs"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "conversation_id", "turn_id"],
            ["conversation_turns.tenant_id", "conversation_turns.user_id",
             "conversation_turns.conversation_id", "conversation_turns.id"],
            name="fk_conversation_inputs_owner_turn", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("conversation_id", "request_id", name="uq_conversation_inputs_request"),
        sa.UniqueConstraint("turn_id", "seq", name="uq_conversation_inputs_turn_seq"),
        sa.CheckConstraint("seq > 0", name="seq_positive"),
        sa.CheckConstraint("char_length(content) BETWEEN 1 AND 4000", name="content_length_valid"),
        sa.CheckConstraint("status IN ('pending', 'applied', 'failed')", name="status_valid"),
        sa.CheckConstraint(
            "(status = 'pending' AND applied_at IS NULL) OR "
            "(status = 'applied' AND applied_at IS NOT NULL) OR status = 'failed'",
            name="application_valid",
        ),
        sa.CheckConstraint(
            "(status = 'failed' AND error_code IS NOT NULL) OR "
            "(status IN ('pending', 'applied') AND error_code IS NULL)",
            name="error_valid",
        ),
    )
    op.create_index(
        "ix_conversation_inputs_owner_turn", "conversation_inputs",
        ["tenant_id", "user_id", "conversation_id", "turn_id"],
    )


def downgrade() -> None:
    op.execute(sa.text("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM conversation_inputs) THEN
                RAISE EXCEPTION 'conversation inputs exist; downgrade would lose conversation history';
            END IF;
        END $$;
    """))
    op.drop_index("ix_conversation_inputs_owner_turn", table_name="conversation_inputs")
    op.drop_table("conversation_inputs")
    op.drop_constraint("uq_conversation_turns_owner_id", "conversation_turns", type_="unique")
