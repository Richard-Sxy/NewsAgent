"""Persist Python-owned news documents and vector projections.

Revision ID: 20261002_0016
Revises: 20260912_0015
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "20261002_0016"
down_revision = "20260912_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "native_knowledge_documents",
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(160), nullable=False),
        sa.Column("news_id", sa.String(160), nullable=False),
        sa.Column("content_version", sa.Integer, nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("embedding_version", sa.String(128), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("metadata", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("content_version > 0", name="ck_native_knowledge_document_version"),
        sa.PrimaryKeyConstraint("tenant_id", "document_id"),
    )
    op.create_table(
        "native_knowledge_chunks",
        sa.Column("chunk_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("document_id", sa.String(160), nullable=False),
        sa.Column("news_id", sa.String(160), nullable=False),
        sa.Column("content_version", sa.Integer, nullable=False),
        sa.Column("chunk_index", sa.Integer, nullable=False),
        sa.Column("embedding_version", sa.String(128), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("excerpt", sa.Text, nullable=False),
        sa.Column("source_url", sa.Text, nullable=True),
        sa.Column("publish_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("vector", postgresql.JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["native_knowledge_documents.tenant_id", "native_knowledge_documents.document_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "document_id", "chunk_index"),
    )
    op.create_index(
        "ix_native_knowledge_chunks_search",
        "native_knowledge_chunks",
        ["tenant_id", "tier", "embedding_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_native_knowledge_chunks_search", table_name="native_knowledge_chunks")
    op.drop_table("native_knowledge_chunks")
    op.drop_table("native_knowledge_documents")
