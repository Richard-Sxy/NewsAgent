"""Persist scoped conversations and idempotent message turns.

Revision ID: 20261004_0018
Revises: 20261002_0017
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261004_0018"
down_revision = "20261002_0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("title", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint("tenant_id", "user_id", "id", name="uq_conversations_owner_id"),
    )
    op.create_index("ix_conversations_owner_updated", "conversations", ["tenant_id", "user_id", "updated_at"])
    op.create_table(
        "conversation_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_content", sa.String(4000), nullable=False),
        sa.Column("assistant_content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("tools", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("model_request_ids", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("runtime_metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_conversation_turns"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.user_id", "conversations.id"],
            name="fk_conversation_turns_owner_conversation", ondelete="CASCADE",
        ),
        sa.UniqueConstraint("conversation_id", "request_id", name="uq_conversation_turns_request"),
        sa.CheckConstraint("status IN ('processing', 'completed', 'failed')", name="status_valid"),
        sa.CheckConstraint(
            "(status = 'processing' AND completed_at IS NULL) OR "
            "(status IN ('completed', 'failed') AND completed_at IS NOT NULL)",
            name="completion_valid",
        ),
    )
    op.create_index(
        "ix_conversation_turns_owner_created", "conversation_turns",
        ["tenant_id", "user_id", "conversation_id", "created_at"],
    )
    op.create_index(
        "uq_conversation_turns_processing", "conversation_turns", ["conversation_id"],
        unique=True, postgresql_where=sa.text("status = 'processing'"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversation_turns_processing", table_name="conversation_turns")
    op.drop_index("ix_conversation_turns_owner_created", table_name="conversation_turns")
    op.drop_table("conversation_turns")
    op.drop_index("ix_conversations_owner_updated", table_name="conversations")
    op.drop_table("conversations")
