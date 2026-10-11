"""Conversation input DDL parity and opt-in disposable PostgreSQL acceptance."""

import importlib.util
import os
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.schema import CreateColumn, CreateIndex
from sqlalchemy.dialects import postgresql

from app.db.base import Base
from app.models.conversation import ConversationInputRecord, ConversationTurnRecord


MIGRATION = "20261010_0022_conversation_inputs.py"
DATABASE_URL = os.environ.get("CONVERSATION_INPUTS_TEST_DATABASE_URL", "")


def load_migration(name=MIGRATION, operations=None):
    path = Path(__file__).parents[1] / "alembic/versions" / name
    spec = importlib.util.spec_from_file_location(name.removesuffix(".py"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if operations is not None:
        module.op = operations
    return module


def migration_sql(direction):
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={
        "as_sql": True, "output_buffer": output, "target_metadata": Base.metadata,
    })
    module = load_migration(operations=Operations(context))
    getattr(module, direction)()
    return module, " ".join(output.getvalue().split())


def test_upgrade_adds_missing_inputs_contract_without_rewriting_turn_history():
    module, sql = migration_sql("upgrade")
    assert module.revision == "20261010_0022" and module.down_revision == "20261010_0021"
    owner = next(constraint for constraint in ConversationTurnRecord.__table__.constraints
                 if constraint.name == "uq_conversation_turns_owner_id")
    owner_clause = f"ADD CONSTRAINT {owner.name} UNIQUE ({', '.join(owner.columns.keys())})"
    assert owner_clause in sql
    assert sql.index(owner_clause) < sql.index("CREATE TABLE conversation_inputs")

    table = ConversationInputRecord.__table__
    dialect = postgresql.dialect()
    for column in table.columns:
        assert " ".join(str(CreateColumn(column).compile(dialect=dialect)).split()) in sql
    for constraint in table.constraints:
        assert f"CONSTRAINT {constraint.name}" in sql
        if isinstance(constraint, CheckConstraint):
            assert f"CHECK ({constraint.sqltext})" in sql
        elif isinstance(constraint, UniqueConstraint):
            assert f"UNIQUE ({', '.join(constraint.columns.keys())})" in sql
        elif isinstance(constraint, ForeignKeyConstraint):
            assert constraint.ondelete == "CASCADE"
            assert "FOREIGN KEY(tenant_id, user_id, conversation_id, turn_id)" in sql
            assert "REFERENCES conversation_turns (tenant_id, user_id, conversation_id, id) ON DELETE CASCADE" in sql
    for index in table.indexes:
        assert " ".join(str(CreateIndex(index).compile(dialect=dialect)).split()) in sql
    assert "CASADE" not in sql
    # "DELETE FROM" keeps the DML ban while allowing the FK's "ON DELETE CASCADE".
    assert "UPDATE " not in sql and "DELETE FROM" not in sql and "DROP " not in sql


def test_downgrade_preserves_saved_input_history_and_removes_dependencies_in_order():
    _, sql = migration_sql("downgrade")
    assert "IF EXISTS (SELECT 1 FROM conversation_inputs)" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP INDEX") < sql.index("DROP TABLE conversation_inputs")
    assert sql.index("DROP TABLE conversation_inputs") < sql.index("DROP CONSTRAINT uq_conversation_turns_owner_id")
    assert "UPDATE " not in sql and "DELETE " not in sql


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason="requires fresh disposable conversation_inputs_test PostgreSQL")
def test_postgres_upgrade_owner_isolation_checks_and_safe_downgrade():
    url = make_url(DATABASE_URL)
    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database) == ("127.0.0.1", 5432, "conversation_inputs_test")
    assert not url.query
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            assert not inspect(connection).get_table_names(), "requires an empty disposable database"
            operations = Operations(MigrationContext.configure(connection, opts={"target_metadata": Base.metadata}))
            original = load_migration("20261004_0018_conversations.py", operations)
            added = load_migration(operations=operations)
            original.upgrade()
            conversation_id, turn_id = uuid4(), uuid4()
            connection.execute(text(
                "INSERT INTO conversations(id,tenant_id,user_id,title) VALUES (:id,'test-tenant','test-user','历史会话')"
            ), {"id": conversation_id})
            connection.execute(text(
                "INSERT INTO conversation_turns(id,tenant_id,user_id,conversation_id,request_id,user_content,status,completed_at) "
                "VALUES (:id,'test-tenant','test-user',:conversation,:request,'迁移前的问题','completed',now())"
            ), {"id": turn_id, "conversation": conversation_id, "request": uuid4()})
            added.upgrade()
            assert connection.execute(text("SELECT user_content FROM conversation_turns WHERE id=:id"),
                                      {"id": turn_id}).scalar_one() == "迁移前的问题"
            inspector = inspect(connection)
            foreign_key = inspector.get_foreign_keys("conversation_inputs")[0]
            assert foreign_key["constrained_columns"] == ["tenant_id", "user_id", "conversation_id", "turn_id"]
            assert foreign_key["referred_columns"] == ["tenant_id", "user_id", "conversation_id", "id"]
            assert foreign_key["options"]["ondelete"] == "CASCADE"
            assert {item["name"] for item in inspector.get_check_constraints("conversation_inputs")} == {
                constraint.name for constraint in ConversationInputRecord.__table__.constraints
                if isinstance(constraint, CheckConstraint)
            }

            insert = text(
                "INSERT INTO conversation_inputs(id,tenant_id,user_id,conversation_id,turn_id,request_id,seq,content,status,applied_at,error_code) "
                "VALUES (:id,:tenant_id,:user_id,:conversation_id,:turn_id,:request_id,:seq,:content,:status,:applied_at,:error_code)"
            )
            base = dict(id=uuid4(), tenant_id="test-tenant", user_id="test-user", conversation_id=conversation_id,
                        turn_id=turn_id, request_id=uuid4(), seq=1, content="补充问题", status="pending",
                        applied_at=None, error_code=None)
            connection.execute(insert, base)
            connection.execute(insert, {**base, "id": uuid4(), "request_id": uuid4(), "seq": 2,
                                        "status": "applied", "applied_at": datetime.now(timezone.utc)})
            connection.execute(insert, {**base, "id": uuid4(), "request_id": uuid4(), "seq": 3,
                                        "status": "failed", "error_code": "interrupted"})
            for changed, expected_constraint in [
                ({"tenant_id": "other-tenant"}, "fk_conversation_inputs_owner_turn"),
                ({"user_id": "other-user"}, "fk_conversation_inputs_owner_turn"),
                ({"conversation_id": uuid4()}, "fk_conversation_inputs_owner_turn"),
                ({"turn_id": uuid4()}, "fk_conversation_inputs_owner_turn"),
                ({"seq": 0}, "ck_conversation_inputs_seq_positive"),
                ({"content": ""}, "ck_conversation_inputs_content_length_valid"),
                ({"status": "completed"}, "ck_conversation_inputs_status_valid"),
                ({"applied_at": datetime.now(timezone.utc)}, "ck_conversation_inputs_application_valid"),
                ({"status": "applied"}, "ck_conversation_inputs_application_valid"),
                ({"status": "failed"}, "ck_conversation_inputs_error_valid"),
                ({"error_code": "unexpected"}, "ck_conversation_inputs_error_valid"),
                ({"seq": 1}, "uq_conversation_inputs_turn_seq"),
                ({"request_id": base["request_id"]}, "uq_conversation_inputs_request"),
            ]:
                with pytest.raises(DBAPIError) as caught:
                    with connection.begin_nested():
                        connection.execute(insert, {**base, "id": uuid4(), "request_id": uuid4(), "seq": 10, **changed})
                assert caught.value.orig.diag.constraint_name == expected_constraint
            with pytest.raises(DBAPIError):
                with connection.begin_nested():
                    connection.execute(insert, {**base, "id": uuid4(), "request_id": uuid4(), "seq": 10, "content": "字" * 4001})

            with pytest.raises(DBAPIError, match="downgrade would lose conversation history"):
                with connection.begin_nested():
                    added.downgrade()
            assert connection.execute(text("SELECT count(*) FROM conversation_inputs")).scalar_one() == 3

            # Verify cascading deletion and empty-table downgrade in a savepoint;
            # rolling it back preserves the historical sentinel and input rows.
            savepoint = connection.begin_nested()
            connection.execute(text("DELETE FROM conversation_turns WHERE id=:id"), {"id": turn_id})
            assert connection.execute(text("SELECT count(*) FROM conversation_inputs")).scalar_one() == 0
            added.downgrade()
            assert "conversation_inputs" not in inspect(connection).get_table_names()
            assert all(item["name"] != "uq_conversation_turns_owner_id"
                       for item in inspect(connection).get_unique_constraints("conversation_turns"))
            added.upgrade()
            assert "conversation_inputs" in inspect(connection).get_table_names()
            savepoint.rollback()
            assert connection.execute(text("SELECT count(*) FROM conversation_inputs")).scalar_one() == 3
            assert connection.execute(text("SELECT count(*) FROM conversation_turns")).scalar_one() == 1
    finally:
        engine.dispose()
