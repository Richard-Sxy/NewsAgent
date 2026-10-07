"""Opt-in migration/compaction acceptance in a disposable PostgreSQL database.

Only localhost:5432/conversation_memory_test is accepted. Start a NEW postgres
container and run this test sharing its network namespace; never use a demo or
enterprise database. Requires an initially empty database. No existing tables
are reset. See docs/conversation-memory.md for the exact command.
"""

import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.conversation.service import ConversationAgentService
from app.db.base import Base
from app.db.session import Database
from app.repositories.conversation import PostgresConversationRepository
from tests.test_conversation_agent import TENANT, USER, chat, runtime
from tests.test_conversation_memory import seed


URL = os.environ.get("CONVERSATION_MEMORY_TEST_DATABASE_URL", "")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not URL, reason="requires disposable PostgreSQL")]


def migration(name, operations):
    spec = importlib.util.spec_from_file_location(name, Path("alembic/versions") / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = operations
    return module


@pytest.mark.asyncio
async def test_real_postgres_upgrade_compact_reload_and_rebuild_after_downgrade():
    url = make_url(URL)
    assert url.drivername == "postgresql+psycopg"
    assert (url.host, url.port, url.database) == ("127.0.0.1", 5432, "conversation_memory_test")
    assert not url.query
    engine = create_engine(url)
    sentinel = uuid4()
    try:
        with engine.begin() as connection:
            assert not inspect(connection).get_table_names(), "use a fresh disposable database"
            operations = Operations(MigrationContext.configure(connection, opts={"target_metadata": Base.metadata}))
            original = migration("20261004_0018_conversations.py", operations)
            added = migration("20261006_0019_conversation_memory.py", operations)
            original.upgrade()
            connection.execute(text("INSERT INTO conversations(id,tenant_id,user_id,title) VALUES (:id,:tenant,:user,:title)"),
                               dict(id=sentinel, tenant=TENANT, user=USER, title="迁移前历史"))
            added.upgrade()
            assert connection.execute(text("SELECT context_memory FROM conversations WHERE id=:id"),
                                      dict(id=sentinel)).scalar_one() == {}

        database = Database(Settings.model_construct(database_url=URL))
        try:
            repo = PostgresConversationRepository(database)
            base, _, inference, tools = runtime()
            service = ConversationAgentService(repository=repo, model=base._model, tools=tools,
                context_max_chars=10000, context_recent_chars=1200, context_summary_chars=500)
            conversation = await repo.create(TENANT, USER, "真实数据库压缩")
            await seed(repo, conversation, [("我偏好中文", "已确认", [])] +
                       [("历史" * 800, "答复" * 500, []) for _ in range(9)])
            response = await chat(service, conversation, "你好")
            assert response.status == "completed"
            memory = await repo.load_memory(TENANT, USER, conversation.id)
            assert memory.through and memory.compressed_turns > 0 and "我偏好中文" in memory.summary
            # Load through a newly constructed repository/session, not a cache.
            assert await PostgresConversationRepository(database).load_memory(TENANT, USER, conversation.id) == memory
            assert len((await repo.get(TENANT, USER, conversation.id)).turns) == 11
            before = len(inference.calls)
            await chat(service, conversation, "你好")
            assert all(request.scene == "conversation" for request in inference.calls[before:])
        finally:
            await database.close()

        with engine.begin() as connection:
            operations = Operations(MigrationContext.configure(connection))
            added = migration("20261006_0019_conversation_memory.py", operations)
            count = connection.execute(text("SELECT count(*) FROM conversation_turns")).scalar_one()
            added.downgrade()
            assert connection.execute(text("SELECT count(*) FROM conversation_turns")).scalar_one() == count
            added.upgrade()
            assert connection.execute(text("SELECT context_memory FROM conversations WHERE id=:id"),
                                      dict(id=conversation.id)).scalar_one() == {}
    finally:
        engine.dispose()
