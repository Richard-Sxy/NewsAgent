from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "alembic"
    / "versions"
    / "20260910_0013_data_loop_integrity.py"
)


class RecordingOperations:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, operation_name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((operation_name, args, kwargs))

        return record


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "data_loop_integrity_migration",
        MIGRATION_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_binds_feedback_to_the_full_run_identity() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    foreign_keys = {
        args[0]: (tuple(args[3]), tuple(args[4]))
        for name, args, _kwargs in operations.calls
        if name == "create_foreign_key"
    }
    assert foreign_keys[
        "fk_publication_outcomes_tenant_run_identity"
    ] == (
        ("tenant_id", "run_id", "run_idempotency_key"),
        ("tenant_id", "id", "idempotency_key"),
    )
    assert foreign_keys["fk_feedback_cases_tenant_run_identity"] == (
        ("tenant_id", "run_id", "run_idempotency_key"),
        ("tenant_id", "id", "idempotency_key"),
    )
    assert foreign_keys[
        "fk_hot_news_decisions_tenant_run_analysis_runs"
    ] == (("tenant_id", "run_id"), ("tenant_id", "id"))


def test_upgrade_installs_immutable_and_lineage_triggers() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.upgrade()

    executed_sql = "\n".join(
        args[0]
        for name, args, _kwargs in operations.calls
        if name == "execute"
    )
    for table_name in migration.DELETE_PROTECTED_TABLES:
        assert f"trg_{table_name}_delete_immutable" in executed_sql
    for trigger_name in (
        "trg_evaluation_datasets_update_immutable",
        "trg_evaluation_dataset_cases_update_immutable",
        "trg_evaluation_dataset_case_lineage",
        "trg_candidate_evaluation_lineage",
        "trg_configuration_candidate_approval_lineage",
        "trg_production_bundle_lineage",
        "trg_promotion_decision_lineage",
    ):
        assert trigger_name in executed_sql
    assert "existing promotion decision lineage is invalid" in executed_sql


def test_downgrade_restores_the_previous_foreign_keys() -> None:
    migration = load_migration()
    operations = RecordingOperations()
    migration.op = operations

    migration.downgrade()

    restored_foreign_keys = {
        args[0]: (tuple(args[3]), tuple(args[4]))
        for name, args, _kwargs in operations.calls
        if name == "create_foreign_key"
    }
    assert restored_foreign_keys[
        "fk_publication_outcomes_run_id_analysis_runs"
    ] == (("run_id",), ("id",))
    assert restored_foreign_keys[
        "fk_feedback_cases_run_id_analysis_runs"
    ] == (("run_id",), ("id",))
    assert restored_foreign_keys[
        "fk_hot_news_decisions_run_id_analysis_runs"
    ] == (("run_id",), ("id",))
