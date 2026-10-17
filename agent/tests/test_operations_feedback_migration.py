"""Compile the actual migration with PostgreSQL naming and audit-preserving rollback."""

import importlib.util
from io import StringIO
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.base import Base
from app.modules.data_loop.persistence.analysis_feedback import AnalysisFeedbackCaseRecord
from app.modules.hot_news.persistence.hot_news_decision import HotNewsDecision


def migration_sql(direction):
    path = Path(__file__).parents[1] / "alembic/versions/20261010_0021_operations_feedback.py"
    spec = importlib.util.spec_from_file_location("operations_feedback_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output, "target_metadata": Base.metadata,
    })
    module.op = Operations(context)
    getattr(module, direction)()
    return module, output.getvalue()


def test_upgrade_preserves_existing_records_and_matches_orm_constraints():
    module, sql = migration_sql("upgrade")
    assert module.down_revision == "20261007_0020"
    assert "ADD COLUMN operator_feedback JSONB" in sql
    for model, name in (
        (HotNewsDecision, "ck_hot_news_decisions_operator_feedback_object"),
        (AnalysisFeedbackCaseRecord, "ck_feedback_cases_source_type_valid"),
    ):
        assert name in {constraint.name for constraint in model.__table__.constraints}
        assert f"ADD CONSTRAINT {name}" in sql
    assert "operator_accepted" in sql and "operator_deferred" in sql
    assert "UPDATE " not in sql and "DELETE " not in sql


def test_downgrade_refuses_audit_loss_before_removing_contract():
    _, sql = migration_sql("downgrade")
    guard = sql.index("RAISE EXCEPTION")
    assert "WHERE source_type IN ('operator_accepted', 'operator_deferred')" in sql
    assert "WHERE operator_feedback IS NOT NULL" in sql
    assert guard < sql.index("DROP CONSTRAINT") < sql.index("DROP COLUMN operator_feedback")
    assert "UPDATE " not in sql and "DELETE " not in sql
