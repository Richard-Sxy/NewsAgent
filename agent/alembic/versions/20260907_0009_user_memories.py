"""Create user memory persistence tables.

Revision ID: 20260907_0009
Revises: 20260907_0008
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20260907_0009"
down_revision = "20260907_0008"
branch_labels = None
depends_on = None


def _scope_columns() -> tuple[sa.Column, ...]:
    return (
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("team_id", sa.String(128), nullable=True),
        sa.Column("section_id", sa.String(128), nullable=True),
        sa.Column("role_id", sa.String(128), nullable=True),
    )


def _timestamp_columns() -> tuple[sa.Column, ...]:
    return (
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
    )

MEMORY_KIND_CHECK = (
    "memory_kind IN ('task_goal', 'pending_item', "
    "'temporary_preference', 'stable_preference', "
    "'role_responsibility', 'user_constraint')"
)
PROPOSED_MEMORY_KIND_CHECK = (
    "proposed_memory_kind IN ('task_goal', 'pending_item', "
    "'temporary_preference', 'stable_preference', "
    "'role_responsibility', 'user_constraint')"
)
NONEMPTY_SOURCE_REFS_CHECK = (
    "jsonb_typeof(source_refs) = 'array' "
    "AND jsonb_array_length(source_refs) > 0"
)


def upgrade() -> None:
    op.create_table(
        "short_term_user_memories",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        *_scope_columns(),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("memory_key", sa.String(128), nullable=False),
        sa.Column("memory_kind", sa.String(32), nullable=False),
        sa.Column("memory_value", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            MEMORY_KIND_CHECK,
            name="memory_kind_valid",
        ),
        sa.CheckConstraint(
            "origin IN ('explicit_user', 'system_inference')",
            name="origin_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'revoked')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="version_positive",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="valid_window",
        ),
        sa.CheckConstraint(
            NONEMPTY_SOURCE_REFS_CHECK,
            name="source_refs_nonempty",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_short_term_user_memories"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_short_term_user_memories_tenant_idempotency_key",
        ),
    )
    op.create_index(
        "ix_short_term_user_memories_active_lookup",
        "short_term_user_memories",
        ["tenant_id", "user_id", "task_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_short_term_user_memories_user_key",
        "short_term_user_memories",
        ["tenant_id", "user_id", "memory_key"],
    )

    op.create_table(
        "long_term_memory_candidates",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        *_scope_columns(),
        sa.Column("proposed_memory_key", sa.String(128), nullable=False),
        sa.Column("proposed_memory_kind", sa.String(32), nullable=False),
        sa.Column("proposed_value", postgresql.JSONB(), nullable=False),
        sa.Column("proposed_summary", sa.String(500), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            PROPOSED_MEMORY_KIND_CHECK,
            name="memory_kind_valid",
        ),
        sa.CheckConstraint(
            "origin IN ('explicit_user', 'trusted_identity', "
            "'system_inference')",
            name="origin_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="version_positive",
        ),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="valid_window",
        ),
        sa.CheckConstraint(
            NONEMPTY_SOURCE_REFS_CHECK,
            name="source_refs_nonempty",
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_long_term_memory_candidates"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name=(
                "uq_long_term_memory_candidates_tenant_idempotency_key"
            ),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "id",
            name="uq_long_term_candidates_tenant_user_id",
        ),
    )
    op.create_index(
        "ix_long_term_memory_candidates_pending_lookup",
        "long_term_memory_candidates",
        ["tenant_id", "user_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_long_term_memory_candidates_user_key",
        "long_term_memory_candidates",
        ["tenant_id", "user_id", "proposed_memory_key"],
    )

    op.create_table(
        "long_term_user_memories",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        *_scope_columns(),
        sa.Column("memory_key", sa.String(128), nullable=False),
        sa.Column("memory_kind", sa.String(32), nullable=False),
        sa.Column("memory_value", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        sa.Column("source_refs", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "candidate_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("confirmed_by", sa.String(128), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "supersedes_memory_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            MEMORY_KIND_CHECK,
            name="memory_kind_valid",
        ),
        sa.CheckConstraint(
            "origin IN ('explicit_user', 'trusted_identity', "
            "'approved_candidate')",
            name="origin_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'expired', 'revoked')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_range",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="version_positive",
        ),
        sa.CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_window",
        ),
        sa.CheckConstraint(
            "confirmed_at <= recorded_at",
            name="confirmation_order",
        ),
        sa.CheckConstraint(
            "supersedes_memory_id IS NULL OR supersedes_memory_id <> id",
            name="no_self_supersession",
        ),
        sa.CheckConstraint(
            "origin <> 'approved_candidate' OR candidate_id IS NOT NULL",
            name="approved_candidate_has_source",
        ),
        sa.CheckConstraint(
            NONEMPTY_SOURCE_REFS_CHECK,
            name="source_refs_nonempty",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "candidate_id"],
            [
                "long_term_memory_candidates.tenant_id",
                "long_term_memory_candidates.user_id",
                "long_term_memory_candidates.id",
            ],
            name="fk_long_term_memories_candidate_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "supersedes_memory_id"],
            [
                "long_term_user_memories.tenant_id",
                "long_term_user_memories.user_id",
                "long_term_user_memories.id",
            ],
            name="fk_long_term_memories_supersedes_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_long_term_user_memories"),
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "id",
            name="uq_long_term_memories_tenant_user_id",
        ),
    )
    op.create_index(
        "ix_long_term_user_memories_active_lookup",
        "long_term_user_memories",
        [
            "tenant_id",
            "user_id",
            "status",
            "valid_from",
            "valid_until",
        ],
    )
    op.create_index(
        "ix_long_term_user_memories_candidate",
        "long_term_user_memories",
        ["tenant_id", "candidate_id"],
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX uq_active_long_term_memory_scope_key "
            "ON long_term_user_memories ("
            "tenant_id, user_id, COALESCE(team_id, ''), "
            "COALESCE(section_id, ''), COALESCE(role_id, ''), memory_key"
            ") WHERE status = 'active'"
        )
    )

    op.create_table(
        "memory_promotion_requests",
        sa.Column(
            "id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "candidate_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("expected_candidate_version", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "supersedes_memory_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column(
            "resulting_memory_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        *_timestamp_columns(),
        sa.CheckConstraint(
            "expected_candidate_version >= 1",
            name="expected_version_positive",
        ),
        sa.CheckConstraint(
            "length(request_fingerprint) = 64",
            name="fingerprint_length",
        ),
        sa.CheckConstraint(
            "status IN ('reserved', 'completed')",
            name="status_valid",
        ),
        sa.CheckConstraint(
            "(status = 'reserved' AND resulting_memory_id IS NULL) OR "
            "(status = 'completed' AND resulting_memory_id IS NOT NULL)",
            name="result_matches_status",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "candidate_id"],
            [
                "long_term_memory_candidates.tenant_id",
                "long_term_memory_candidates.user_id",
                "long_term_memory_candidates.id",
            ],
            name="fk_memory_promotion_candidate_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "supersedes_memory_id"],
            [
                "long_term_user_memories.tenant_id",
                "long_term_user_memories.user_id",
                "long_term_user_memories.id",
            ],
            name="fk_memory_promotion_supersedes_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id", "resulting_memory_id"],
            [
                "long_term_user_memories.tenant_id",
                "long_term_user_memories.user_id",
                "long_term_user_memories.id",
            ],
            name="fk_memory_promotion_result_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_memory_promotion_requests"),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_memory_promotion_requests_tenant_idempotency_key",
        ),
    )
    op.create_index(
        "ix_memory_promotion_requests_candidate_created",
        "memory_promotion_requests",
        ["tenant_id", "candidate_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_memory_promotion_requests_candidate_created",
        table_name="memory_promotion_requests",
    )
    op.drop_table("memory_promotion_requests")

    op.drop_index(
        "uq_active_long_term_memory_scope_key",
        table_name="long_term_user_memories",
    )
    op.drop_index(
        "ix_long_term_user_memories_candidate",
        table_name="long_term_user_memories",
    )
    op.drop_index(
        "ix_long_term_user_memories_active_lookup",
        table_name="long_term_user_memories",
    )
    op.drop_table("long_term_user_memories")

    op.drop_index(
        "ix_long_term_memory_candidates_user_key",
        table_name="long_term_memory_candidates",
    )
    op.drop_index(
        "ix_long_term_memory_candidates_pending_lookup",
        table_name="long_term_memory_candidates",
    )
    op.drop_table("long_term_memory_candidates")

    op.drop_index(
        "ix_short_term_user_memories_user_key",
        table_name="short_term_user_memories",
    )
    op.drop_index(
        "ix_short_term_user_memories_active_lookup",
        table_name="short_term_user_memories",
    )
    op.drop_table("short_term_user_memories")
