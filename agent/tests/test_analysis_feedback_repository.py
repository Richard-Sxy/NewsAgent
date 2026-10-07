from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401 - 注册 Data Loop ORM
from app.db.base import Base
from app.models.analysis_feedback import AnalysisFeedbackCaseRecord
from app.repositories.analysis_feedback import (
    PostgresAnalysisFeedbackRepository,
)
from app.schemas.analysis_feedback import AnalysisFeedbackCase
from tests.test_analysis_feedback_schema import NOW, failed_case_values


class ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


def feedback_case(**overrides) -> AnalysisFeedbackCase:
    values = failed_case_values()
    values.update(
        id=UUID(int=1),
        tenant_id="tenant-1",
        status="needs_label",
        recorded_at=NOW,
        content_sha256="a" * 64,
    )
    values.update(overrides)
    return AnalysisFeedbackCase(**values)


def _constraint_names(table_name: str, kind: type) -> set[str]:
    return {
        constraint.name
        for constraint in Base.metadata.tables[table_name].constraints
        if isinstance(constraint, kind)
    }


def test_feedback_tables_and_tenant_constraints_are_registered() -> None:
    assert {
        "publication_outcomes",
        "feedback_cases",
        "feedback_labels",
    } <= set(Base.metadata.tables)
    assert "uq_feedback_cases_tenant_id" in _constraint_names(
        "feedback_cases", UniqueConstraint
    )
    assert "uq_feedback_cases_tenant_idempotency_key" in _constraint_names(
        "feedback_cases", UniqueConstraint
    )
    assert "uq_feedback_labels_case_version" in _constraint_names(
        "feedback_labels", UniqueConstraint
    )
    assert "ck_feedback_cases_source_type_valid" in _constraint_names(
        "feedback_cases", CheckConstraint
    )

    foreign_key = next(
        iter(Base.metadata.tables["feedback_labels"].foreign_key_constraints)
    )
    assert tuple(
        element.target_fullname for element in foreign_key.elements
    ) == ("feedback_cases.tenant_id", "feedback_cases.id")


def test_feedback_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for table_name in (
        "publication_outcomes",
        "feedback_cases",
        "feedback_labels",
    ):
        ddl = str(
            CreateTable(Base.metadata.tables[table_name]).compile(
                dialect=dialect
            )
        )
        assert f"CREATE TABLE {table_name}" in ddl


@pytest.mark.asyncio
async def test_case_lookup_is_always_tenant_scoped() -> None:
    session = SimpleNamespace(execute=AsyncMock(return_value=ScalarResult(None)))
    repository = PostgresAnalysisFeedbackRepository(session)

    await repository.get_case(
        tenant_id="tenant-1",
        feedback_case_id=UUID(int=1),
    )

    statement = session.execute.await_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert "tenant-1" in compiled.params.values()
    assert UUID(int=1) in compiled.params.values()
    assert "feedback_cases.tenant_id" in str(compiled)


@pytest.mark.asyncio
async def test_case_insert_uses_tenant_idempotency_constraint() -> None:
    session = SimpleNamespace(execute=AsyncMock(return_value=ScalarResult(None)))
    repository = PostgresAnalysisFeedbackRepository(session)

    inserted = await repository.insert_case(case=feedback_case())

    assert inserted is False
    statement = session.execute.await_args.args[0]
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert (
        "ON CONFLICT ON CONSTRAINT "
        "uq_feedback_cases_tenant_idempotency_key" in compiled
    )


def test_case_record_round_trip_keeps_typed_snapshots() -> None:
    case = feedback_case()
    record = AnalysisFeedbackCaseRecord(
        **PostgresAnalysisFeedbackRepository.to_case_values(case)
    )

    restored = PostgresAnalysisFeedbackRepository.to_case_domain(record)

    assert restored == case
    assert restored.analysis_input_snapshot.news_id == "news-1"
    assert not hasattr(restored, "raw_behavior_records")
