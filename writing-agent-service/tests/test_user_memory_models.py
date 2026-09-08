from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401 - 注册全部 ORM 模型
from app.db.base import Base


MEMORY_TABLES = {
    "short_term_user_memories",
    "long_term_memory_candidates",
    "long_term_user_memories",
    "memory_promotion_requests",
}


def _constraint_names(table_name: str, constraint_type: type) -> set[str]:
    table = Base.metadata.tables[table_name]
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, constraint_type)
    }


def _foreign_key_column_sets(
    table_name: str,
) -> set[tuple[tuple[str, ...], tuple[str, ...]]]:
    table = Base.metadata.tables[table_name]
    return {
        (
            tuple(element.parent.name for element in constraint.elements),
            tuple(element.target_fullname for element in constraint.elements),
        )
        for constraint in table.foreign_key_constraints
    }


def test_memory_tables_are_registered() -> None:
    assert MEMORY_TABLES <= set(Base.metadata.tables)


def test_short_term_memory_constraints_are_registered() -> None:
    uniques = _constraint_names(
        "short_term_user_memories", UniqueConstraint
    )
    checks = _constraint_names(
        "short_term_user_memories", CheckConstraint
    )

    assert (
        "uq_short_term_user_memories_tenant_idempotency_key" in uniques
    )
    assert {
        "ck_short_term_user_memories_memory_kind_valid",
        "ck_short_term_user_memories_origin_valid",
        "ck_short_term_user_memories_status_valid",
        "ck_short_term_user_memories_confidence_range",
        "ck_short_term_user_memories_version_positive",
        "ck_short_term_user_memories_valid_window",
        "ck_short_term_user_memories_source_refs_nonempty",
    } <= checks


def test_candidate_constraints_are_registered() -> None:
    uniques = _constraint_names(
        "long_term_memory_candidates", UniqueConstraint
    )
    checks = _constraint_names(
        "long_term_memory_candidates", CheckConstraint
    )

    assert (
        "uq_long_term_memory_candidates_tenant_idempotency_key" in uniques
    )
    assert {
        "ck_long_term_memory_candidates_memory_kind_valid",
        "ck_long_term_memory_candidates_status_valid",
        "ck_long_term_memory_candidates_valid_window",
        "ck_long_term_memory_candidates_source_refs_nonempty",
    } <= checks


def test_long_term_memory_references_and_invariants_are_registered() -> None:
    checks = _constraint_names(
        "long_term_user_memories", CheckConstraint
    )
    foreign_keys = _foreign_key_column_sets("long_term_user_memories")

    assert (
        ("tenant_id", "user_id", "candidate_id"),
        (
            "long_term_memory_candidates.tenant_id",
            "long_term_memory_candidates.user_id",
            "long_term_memory_candidates.id",
        ),
    ) in foreign_keys
    assert (
        ("tenant_id", "user_id", "supersedes_memory_id"),
        (
            "long_term_user_memories.tenant_id",
            "long_term_user_memories.user_id",
            "long_term_user_memories.id",
        ),
    ) in foreign_keys
    assert {
        "ck_long_term_user_memories_valid_window",
        "ck_long_term_user_memories_confirmation_order",
        "ck_long_term_user_memories_no_self_supersession",
        "ck_long_term_user_memories_approved_candidate_has_source",
        "ck_long_term_user_memories_source_refs_nonempty",
    } <= checks


def test_only_one_active_long_term_memory_exists_per_full_scope_key() -> None:
    table = Base.metadata.tables["long_term_user_memories"]
    index = next(
        item
        for item in table.indexes
        if item.name == "uq_active_long_term_memory_scope_key"
    )

    assert index.unique is True
    assert index.dialect_options["postgresql"]["where"] is not None
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "CREATE UNIQUE INDEX" in ddl
    assert "WHERE status = 'active'" in ddl
    assert ddl.count("coalesce") == 3


def test_promotion_request_is_idempotent_and_references_both_sides() -> None:
    uniques = _constraint_names(
        "memory_promotion_requests", UniqueConstraint
    )
    checks = _constraint_names(
        "memory_promotion_requests", CheckConstraint
    )
    foreign_keys = _foreign_key_column_sets("memory_promotion_requests")

    assert (
        "uq_memory_promotion_requests_tenant_idempotency_key" in uniques
    )
    assert (
        ("tenant_id", "user_id", "candidate_id"),
        (
            "long_term_memory_candidates.tenant_id",
            "long_term_memory_candidates.user_id",
            "long_term_memory_candidates.id",
        ),
    ) in foreign_keys
    for column_name in ("supersedes_memory_id", "resulting_memory_id"):
        assert (
            ("tenant_id", "user_id", column_name),
            (
                "long_term_user_memories.tenant_id",
                "long_term_user_memories.user_id",
                "long_term_user_memories.id",
            ),
        ) in foreign_keys
    assert {
        "ck_memory_promotion_requests_expected_version_positive",
        "ck_memory_promotion_requests_fingerprint_length",
        "ck_memory_promotion_requests_status_valid",
        "ck_memory_promotion_requests_result_matches_status",
    } <= checks
    assert Base.metadata.tables[
        "memory_promotion_requests"
    ].c.request_fingerprint.type.length == 64


def test_memory_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for table_name in MEMORY_TABLES:
        table = Base.metadata.tables[table_name]
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table_name}" in ddl
