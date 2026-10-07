"""Opt-in acceptance against the isolated native-demo PostgreSQL instance.

Set CONVERSATION_TEST_DATABASE_URL explicitly after migration 20261007_0020.
Only the native/scale demo destinations listed below are accepted. Every test
uses random tenant/user/conversation/request identities; history remains for
inspection, including soft-deleted cases. No reset, hard deletion or DDL is used.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db.session import Database
from app.models.conversation import ConversationTurnRecord
from app.repositories.conversation import (
    ConversationConflict,
    ConversationNotFound,
    PostgresConversationRepository,
    TurnClaim,
)
from app.schemas.conversation import ToolTrace


DATABASE_URL = os.environ.get("CONVERSATION_TEST_DATABASE_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="set CONVERSATION_TEST_DATABASE_URL for isolated PostgreSQL acceptance",
    ),
]


def _approved_database_url() -> str:
    try:
        url = make_url(DATABASE_URL)
    except Exception:
        raise ValueError("CONVERSATION_TEST_DATABASE_URL must be a PostgreSQL URL") from None
    allowed = {
        ("127.0.0.1", 25432, "newsagent_native"),
        ("127.0.0.1", 25462, "newsagent_native"),
        ("postgres", 5432, "newsagent_native"),
    }
    if (
        url.drivername not in {"postgresql", "postgresql+psycopg"}
        or (url.host, url.port, url.database) not in allowed
        or url.query
    ):
        # Reject query arguments too: libpq host/port overrides could bypass the
        # explicit destination allowlist. Never include credential values here.
        raise ValueError(
            "conversation PostgreSQL acceptance only allows the explicit "
            "native-demo destinations and no URL query parameters"
        )
    return url.set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def _new_database() -> Database:
    # Database uses only database_url. Constructing this narrow settings value
    # bypasses BaseSettings environment/.env loading and unrelated services.
    settings = Settings.model_construct(database_url=_approved_database_url())
    return Database(settings)


@pytest_asyncio.fixture
async def database():
    value = _new_database()
    try:
        yield value
    finally:
        await value.close()


@pytest.fixture
def identity():
    return str(uuid4()), str(uuid4())


async def _conversation(repository, identity):
    tenant_id, user_id = identity
    return await repository.create(
        tenant_id, user_id, f"PostgreSQL acceptance {uuid4().hex}"
    )


async def _expire_own_turn(database, identity, conversation_id, turn_id):
    tenant_id, user_id = identity
    async with database.session() as session:
        result = await session.execute(
            update(ConversationTurnRecord)
            .where(
                ConversationTurnRecord.tenant_id == tenant_id,
                ConversationTurnRecord.user_id == user_id,
                ConversationTurnRecord.conversation_id == conversation_id,
                ConversationTurnRecord.id == turn_id,
                ConversationTurnRecord.status == "processing",
            )
            .values(created_at=datetime.now(timezone.utc) - timedelta(seconds=91))
            .returning(ConversationTurnRecord.id)
        )
        assert result.scalar_one() == turn_id


@pytest.mark.asyncio
async def test_postgres_concurrent_same_request_is_acquired_once(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    request_id = uuid4()
    ready = asyncio.Event()

    async def begin():
        await ready.wait()
        return await repository.begin_turn(
            tenant_id, user_id, conversation.id, request_id, "same request",
            runtime_metadata={"prompt_version": "postgres-acceptance-v1"},
        )

    tasks = [asyncio.create_task(begin()) for _ in range(8)]
    ready.set()
    claims = await asyncio.gather(*tasks)
    assert sum(claim.acquired for claim in claims) == 1
    assert len({claim.turn.id for claim in claims}) == 1
    detail = await repository.get(tenant_id, user_id, conversation.id)
    assert len(detail.turns) == 1
    assert detail.turns[0].runtime_metadata == {"prompt_version": "postgres-acceptance-v1"}
    await repository.finish_turn(
        tenant_id, user_id, conversation.id, claims[0].turn.id, "completed", [], []
    )


@pytest.mark.asyncio
async def test_postgres_concurrent_different_requests_cannot_both_process(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    ready = asyncio.Event()

    async def begin(content):
        await ready.wait()
        return await repository.begin_turn(
            tenant_id, user_id, conversation.id, uuid4(), content
        )

    tasks = [asyncio.create_task(begin(content)) for content in ("request A", "request B")]
    ready.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    claims = [result for result in results if isinstance(result, TurnClaim)]
    conflicts = [result for result in results if isinstance(result, ConversationConflict)]
    assert len(claims) == len(conflicts) == 1
    assert claims[0].acquired
    detail = await repository.get(tenant_id, user_id, conversation.id)
    assert len(detail.turns) == 1 and detail.turns[0].status == "processing"
    await repository.finish_turn(
        tenant_id, user_id, conversation.id, claims[0].turn.id, "completed", [], []
    )


@pytest.mark.asyncio
async def test_postgres_tenant_and_user_isolation_includes_recovery(database, identity):
    repository = PostgresConversationRepository(database)
    tenant_id, user_id = identity
    conversation = await _conversation(repository, identity)
    other_user = (tenant_id, str(uuid4()))
    other_tenant = (str(uuid4()), user_id)
    await _conversation(repository, other_user)
    await _conversation(repository, other_tenant)
    claim = await repository.begin_turn(
        tenant_id, user_id, conversation.id, uuid4(), "owned message"
    )
    await _expire_own_turn(database, identity, conversation.id, claim.turn.id)
    for tenant, user in (other_user, other_tenant):
        assert await repository.get(tenant, user, conversation.id, stale_after_seconds=90) is None
        assert conversation.id not in {item.id for item in await repository.list(tenant, user)}
        with pytest.raises(ConversationNotFound):
            await repository.begin_turn(tenant, user, conversation.id, uuid4(), "wrong owner")
        with pytest.raises(ConversationNotFound):
            await repository.finish_turn(tenant, user, conversation.id, claim.turn.id, "wrong owner", [], [])
    owned = await repository.get(tenant_id, user_id, conversation.id)
    assert owned.turns[0].status == "processing"
    assert [item.id for item in await repository.list(tenant_id, user_id)] == [conversation.id]
    await repository.finish_turn(
        tenant_id, user_id, conversation.id, claim.turn.id, "completed", [], []
    )


@pytest.mark.asyncio
async def test_postgres_get_recovers_expired_turn_and_rejects_late_finish(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    request_id = uuid4()
    old = await repository.begin_turn(
        tenant_id, user_id, conversation.id, request_id, "interrupted message"
    )
    await _expire_own_turn(database, identity, conversation.id, old.turn.id)
    detail = await repository.get(
        tenant_id, user_id, conversation.id, stale_after_seconds=90
    )
    assert detail.turns[0].status == "failed"
    assert detail.turns[0].error_code == "interrupted"
    assert detail.turns[0].completed_at is not None
    replay = await repository.begin_turn(
        tenant_id, user_id, conversation.id, request_id, "interrupted message"
    )
    assert not replay.acquired and replay.turn.id == old.turn.id
    new = await repository.begin_turn(
        tenant_id, user_id, conversation.id, uuid4(), "new message"
    )
    assert new.acquired
    with pytest.raises(ConversationConflict):
        await repository.finish_turn(
            tenant_id, user_id, conversation.id, old.turn.id, "late completion", [], []
        )
    after = await repository.get(tenant_id, user_id, conversation.id)
    assert [turn.status for turn in after.turns] == ["failed", "processing"]
    assert after.turns[0].assistant_content is None
    await repository.finish_turn(
        tenant_id, user_id, conversation.id, new.turn.id, "completed", [], []
    )


@pytest.mark.asyncio
async def test_postgres_history_survives_a_new_database_connection(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    request_id = uuid4()
    trace = ToolTrace(
        name="news.search", status="completed", attempts=1,
        arguments={"query": "科技"}, result={"news": [{"id": "acceptance", "score": 0.5}]},
    )
    claim = await repository.begin_turn(
        tenant_id, user_id, conversation.id, request_id, "persist this message",
        runtime_metadata={"scene": "conversation", "prompt_version": "postgres-acceptance-v1"},
    )
    completed = await repository.finish_turn(
        tenant_id, user_id, conversation.id, claim.turn.id, "persisted answer",
        [trace], ["acceptance-model-request"],
    )
    await database.close()
    reconnected = _new_database()
    try:
        recovered_repository = PostgresConversationRepository(reconnected)
        detail = await recovered_repository.get(tenant_id, user_id, conversation.id)
        assert detail.conversation.id == conversation.id
        assert detail.turns == [completed]
        replay = await recovered_repository.begin_turn(
            tenant_id, user_id, conversation.id, request_id, "persist this message"
        )
        assert not replay.acquired and replay.turn == completed
    finally:
        await reconnected.close()


@pytest.mark.asyncio
async def test_postgres_terminal_tool_checkpoint_survives_reconnect_and_expiry(database, identity):
    repository = PostgresConversationRepository(database)
    tenant_id, user_id = identity
    conversation = await _conversation(repository, identity)
    claim = await repository.begin_turn(tenant_id, user_id, conversation.id, uuid4(), "checkpoint before interruption")
    trace = ToolTrace(name="capabilities", status="completed", attempts=1, arguments={}, result={"approved": True})
    await repository.checkpoint_turn(tenant_id, user_id, conversation.id, claim.turn.id, claim.turn.request_id, [trace], ["checkpoint-model"])
    for tenant, user, request in ((str(uuid4()), user_id, claim.turn.request_id),
                                 (tenant_id, str(uuid4()), claim.turn.request_id),
                                 (tenant_id, user_id, uuid4())):
        with pytest.raises(ConversationNotFound):
            await repository.checkpoint_turn(tenant, user, conversation.id, claim.turn.id, request, [trace], ["checkpoint-model"])
    with pytest.raises(ConversationConflict, match="checkpoint cannot"):
        await repository.finish_turn(tenant_id, user_id, conversation.id, claim.turn.id, "short finish", [], [])
    await _expire_own_turn(database, identity, conversation.id, claim.turn.id)
    await database.close()
    reconnected = _new_database()
    try:
        restored = PostgresConversationRepository(reconnected)
        detail = await restored.get(tenant_id, user_id, conversation.id, stale_after_seconds=90)
        turn = detail.turns[0]
        assert turn.status == "failed" and turn.error_code == "interrupted"
        assert turn.tools == [trace] and turn.model_request_ids == ["checkpoint-model"]
        with pytest.raises(ConversationConflict):
            await restored.checkpoint_turn(tenant_id, user_id, conversation.id, claim.turn.id, claim.turn.request_id, [trace], ["checkpoint-model"])
        replay = await restored.begin_turn(tenant_id, user_id, conversation.id, claim.turn.request_id, claim.turn.user_content)
        assert not replay.acquired and replay.turn == turn
    finally:
        await reconnected.close()


@pytest.mark.asyncio
async def test_postgres_delete_and_begin_turn_are_serialized(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    ready = asyncio.Event()

    async def delete():
        await ready.wait()
        return await repository.delete(tenant_id, user_id, conversation.id)

    async def begin():
        await ready.wait()
        return await repository.begin_turn(tenant_id, user_id, conversation.id, uuid4(), "race with deletion")

    tasks = [asyncio.create_task(delete()), asyncio.create_task(begin())]
    ready.set()
    deleted, claimed = await asyncio.gather(*tasks, return_exceptions=True)
    if isinstance(claimed, ConversationNotFound):
        assert deleted is None
        assert await repository.get(tenant_id, user_id, conversation.id) is None
        await repository.restore(tenant_id, user_id, conversation.id)
        assert not (await repository.get(tenant_id, user_id, conversation.id)).turns
    else:
        assert isinstance(claimed, TurnClaim) and claimed.acquired
        assert isinstance(deleted, ConversationConflict)
        detail = await repository.get(tenant_id, user_id, conversation.id)
        assert len(detail.turns) == 1 and detail.turns[0].status == "processing"
        await repository.finish_turn(tenant_id, user_id, conversation.id, claimed.turn.id, "done", [], [])
    await repository.delete(tenant_id, user_id, conversation.id)


@pytest.mark.asyncio
async def test_postgres_restore_does_not_revive_stale_worker_and_keeps_history(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    claim = await repository.begin_turn(tenant_id, user_id, conversation.id, uuid4(), "expired own turn")
    trace = ToolTrace(name="capabilities", status="completed", attempts=1, arguments={}, result={"approved": True})
    await repository.checkpoint_turn(tenant_id, user_id, conversation.id, claim.turn.id,
                                    claim.turn.request_id, [trace], ["persisted-model-id"])
    await _expire_own_turn(database, identity, conversation.id, claim.turn.id)
    await repository.delete(tenant_id, user_id, conversation.id)
    await repository.delete(tenant_id, user_id, conversation.id)
    assert await repository.get(tenant_id, user_id, conversation.id) is None
    with pytest.raises(ConversationNotFound):
        await repository.finish_turn(tenant_id, user_id, conversation.id, claim.turn.id,
                                     "late deleted reply", [trace], ["persisted-model-id"])
    await repository.restore(tenant_id, user_id, conversation.id)
    detail = await repository.get(tenant_id, user_id, conversation.id)
    old = detail.turns[0]
    assert old.status == "failed" and old.error_code == "interrupted" and old.completed_at is not None
    assert old.tools == [trace] and old.model_request_ids == ["persisted-model-id"]
    for action in (
        lambda: repository.finish_turn(tenant_id, user_id, conversation.id, claim.turn.id,
                                       "late restored reply", [trace], ["persisted-model-id"]),
        lambda: repository.checkpoint_turn(tenant_id, user_id, conversation.id, claim.turn.id,
                                           claim.turn.request_id, [trace], ["persisted-model-id"]),
    ):
        with pytest.raises(ConversationConflict):
            await action()
    replay = await repository.begin_turn(tenant_id, user_id, conversation.id, claim.turn.request_id, "expired own turn")
    assert not replay.acquired and replay.turn == old
    assert await repository.restore(tenant_id, user_id, conversation.id) == detail.conversation
    fresh = await repository.begin_turn(tenant_id, user_id, conversation.id, uuid4(), "fresh turn")
    await repository.finish_turn(tenant_id, user_id, conversation.id, fresh.turn.id, "fresh reply", [], [])
    assert [turn.status for turn in (await repository.get(tenant_id, user_id, conversation.id)).turns] == ["failed", "completed"]
    await repository.delete(tenant_id, user_id, conversation.id)


@pytest.mark.asyncio
async def test_postgres_delete_and_restore_enforce_owner_before_expiry_changes(database, identity):
    repository = PostgresConversationRepository(database)
    conversation = await _conversation(repository, identity)
    tenant_id, user_id = identity
    claim = await repository.begin_turn(tenant_id, user_id, conversation.id, uuid4(), "expired scope check")
    await _expire_own_turn(database, identity, conversation.id, claim.turn.id)
    foreign_owners = ((str(uuid4()), user_id), (tenant_id, str(uuid4())))
    for tenant, user in foreign_owners:
        with pytest.raises(ConversationNotFound):
            await repository.delete(tenant, user, conversation.id)
        with pytest.raises(ConversationNotFound):
            await repository.restore(tenant, user, conversation.id)
    assert (await repository.get(tenant_id, user_id, conversation.id)).turns[0].status == "processing"
    await repository.delete(tenant_id, user_id, conversation.id)
    for tenant, user in foreign_owners:
        with pytest.raises(ConversationNotFound):
            await repository.delete(tenant, user, conversation.id)
        with pytest.raises(ConversationNotFound):
            await repository.restore(tenant, user, conversation.id)
    assert conversation.id not in {item.id for item in await repository.list(tenant_id, user_id)}
