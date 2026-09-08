from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import app.models  # noqa: F401 - 注册全部 ORM 模型
from app.db.base import Base


def test_all_checkpoint_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "writing_jobs",
        "writing_steps",
        "writing_artifacts",
        "agent_runs",
        "outbox_events",
        "analysis_runs",
        "hot_news_decisions",
        "short_term_user_memories",
        "long_term_memory_candidates",
        "long_term_user_memories",
        "memory_promotion_requests",
    }


def test_job_has_tenant_idempotency_and_retry_constraints() -> None:
    table = Base.metadata.tables["writing_jobs"]
    constraint_names = {constraint.name for constraint in table.constraints}
    assert "uq_writing_jobs_tenant_idempotency_key" in constraint_names
    assert "uq_writing_jobs_tenant_temporal_workflow_id" in constraint_names
    assert "ck_writing_jobs_research_retries_limit" in constraint_names
    assert "ck_writing_jobs_review_rounds_limit" in constraint_names
    assert "ck_writing_jobs_progress_percent_range" in constraint_names
    assert "ck_writing_jobs_sections_progress_valid" in constraint_names
    assert table.c.scenario.type.name == "writing_job_scenario"
    assert table.c.scenario.nullable is False


def test_step_attempt_is_idempotently_unique() -> None:
    table = Base.metadata.tables["writing_steps"]
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("job_id", "step_key", "attempt") in unique_column_sets


def test_artifact_version_is_immutable_identity() -> None:
    table = Base.metadata.tables["writing_artifacts"]
    unique_column_sets = {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert ("job_id", "logical_key", "version") in unique_column_sets
    assert table.c.content_sha256.type.length == 64


def test_all_tables_compile_for_postgresql() -> None:
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CREATE TABLE {table.name}" in ddl
