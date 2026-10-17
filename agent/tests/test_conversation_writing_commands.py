import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.conversation.receipts import CapabilityReceipt
from app.conversation.writing import ConversationWritingCommands, ConversationWritingConflict
from app.domain.job_scenario import JobScenario
from app.domain.job_status import JobStatus
from app.services.outbox import OutboxService


TENANT = str(uuid4())
USER = str(uuid4())
CONVERSATION = uuid4()
REQUEST = uuid4()
PROPOSAL = {"topic": "AI 芯片周报", "target_words": 1200, "tone": "neutral"}


class Session:
    def __init__(self, database):
        self.database = database
        self.pending = {}

    async def commit(self):
        self.database.events.append("commit")
        self.database.rows.update(self.pending)
        self.pending.clear()


class Database:
    def __init__(self):
        self.rows = {}
        self.events = []

    @asynccontextmanager
    async def session(self):
        session = Session(self)
        try:
            yield session
            await session.commit()
        finally:
            session.pending.clear()


def job_for(tenant_id, created_by, request):
    now = datetime.now(timezone.utc)
    job_id = uuid4()
    return SimpleNamespace(
        id=job_id,
        tenant_id=tenant_id,
        created_by=created_by,
        idempotency_key=request.idempotency_key,
        topic=request.topic,
        scenario=request.scenario,
        requirements=request.requirements,
        temporal_workflow_id=f"news-writing:{tenant_id}:{job_id}",
        status=JobStatus.CREATED,
        current_step=None,
        research_retries=0,
        review_rounds=0,
        progress_percent=0,
        sections_completed=0,
        sections_total=0,
        created_at=now,
        updated_at=now,
        version=1,
    )


def runtime(start_behavior=None):
    database = Database()
    requests = []

    async def create_or_get(session, *, tenant_id, created_by, request):
        requests.append((tenant_id, created_by, request))
        key = (tenant_id, request.idempotency_key)
        if key in database.rows:
            return database.rows[key], False
        stored = job_for(tenant_id, created_by, request)
        session.pending[key] = stored
        return stored, True

    async def start_job(job):
        database.events.append("start")
        assert any(row.id == job.id for row in database.rows.values())
        if start_behavior is not None:
            await start_behavior(job)

    jobs = SimpleNamespace(create_or_get=AsyncMock(side_effect=create_or_get))
    orchestrator = SimpleNamespace(
        start_job=AsyncMock(side_effect=start_job),
        get_progress=AsyncMock(side_effect=AssertionError("must not wait for Workflow progress")),
        get_result=AsyncMock(side_effect=AssertionError("must not wait for Workflow result")),
    )
    commands = ConversationWritingCommands(database=database, jobs=jobs, orchestrator=orchestrator)
    return commands, database, orchestrator, requests


async def start(commands, **overrides):
    arguments = {
        "tenant_id": TENANT,
        "user_id": USER,
        "conversation_id": CONVERSATION,
        "request_id": REQUEST,
        "proposal": PROPOSAL,
        "original_content": "请帮我写一篇 AI 芯片周报",
    }
    return await commands.start(**(arguments | overrides))


@pytest.mark.asyncio
async def test_start_commits_then_binds_receipt_before_workflow_start():
    commands, database, orchestrator, requests = runtime()
    bound = []

    async def on_bound(metadata):
        database.events.append("bound")
        assert len(database.rows) == 1
        assert orchestrator.start_job.await_count == 0
        assert metadata["workflow_accepted"] is False
        assert CapabilityReceipt.model_validate(metadata["receipt"]).entity_id == metadata["job_id"]
        bound.append(metadata)

    result = await start(commands, on_bound=on_bound)
    assert database.events[:3] == ["commit", "bound", "start"]
    assert result["workflow_accepted"] is True
    assert result["job_id"] == result["job"]["id"] == bound[0]["job_id"]
    assert result["receipt"]["version"] == "1"
    assert result["receipt"]["deep_link"] == f"/jobs?job_id={result['job_id']}"
    assert bound[0]["workflow_accepted"] is False  # the checkpoint remains a snapshot
    assert "tenant_id" not in result["job"] and "requirements" not in result["job"]
    tenant_id, created_by, request = requests[0]
    assert (tenant_id, created_by) == (UUID(TENANT), UUID(USER))
    assert request.idempotency_key == f"conversation-writing:{CONVERSATION}:{REQUEST}"
    assert len(request.idempotency_key) <= 128
    assert request.scenario == JobScenario.ASSISTED_WRITING
    assert request.requirements == {"target_words": 1200, "tone": "neutral", "brief": "请帮我写一篇 AI 芯片周报"}
    orchestrator.get_progress.assert_not_awaited()
    orchestrator.get_result.assert_not_awaited()


@pytest.mark.asyncio
async def test_same_request_reuses_job_and_workflow_identity():
    commands, database, orchestrator, requests = runtime()
    first = await start(commands)
    second = await start(commands, conversation_id=str(CONVERSATION), request_id=str(REQUEST))
    assert len(database.rows) == 1
    assert first["job_id"] == second["job_id"]
    assert first["workflow_id"] == second["workflow_id"]
    assert requests[0][2] == requests[1][2]
    assert orchestrator.start_job.await_args_list[0].args[0] is orchestrator.start_job.await_args_list[1].args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["topic", "scenario", "requirements"])
async def test_same_key_with_different_parameters_fails_before_second_start(field):
    commands, database, orchestrator, _ = runtime()
    await start(commands)
    stored = next(iter(database.rows.values()))
    setattr(stored, field, {"topic": "其他主题", "scenario": JobScenario.RESEARCH_PACKAGE,
                           "requirements": {"target_words": 1201}}[field])
    with pytest.raises(ConversationWritingConflict, match="writing_request_conflict"):
        await start(commands)
    assert len(database.rows) == 1
    assert orchestrator.start_job.await_count == 1


@pytest.mark.asyncio
async def test_distinct_request_or_conversation_has_distinct_business_key():
    commands, database, _, _ = runtime()
    results = [await start(commands), await start(commands, request_id=uuid4()),
               await start(commands, conversation_id=uuid4())]
    assert len(database.rows) == 3
    assert len({item["job_id"] for item in results}) == 3


@pytest.mark.asyncio
async def test_requirements_are_fixed_and_original_brief_is_bounded():
    commands, _, _, requests = runtime()
    await start(commands, proposal={"topic": "  AI 芯片周报  "}, original_content="新" * 2000)
    request = requests[0][2]
    assert request.topic == "AI 芯片周报"
    assert request.requirements == {"target_words": 1000, "tone": "neutral", "brief": "新" * 1000}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proposal",
    [
        {**PROPOSAL, "topic": " "},
        {**PROPOSAL, "topic": "新" * 501},
        {**PROPOSAL, "target_words": True},
        {**PROPOSAL, "target_words": "1200"},
        {**PROPOSAL, "target_words": 199},
        {**PROPOSAL, "target_words": 5001},
        {**PROPOSAL, "tone": "publish"},
        {**PROPOSAL, "tenant_id": str(uuid4())},
        {**PROPOSAL, "scenario": "research_package"},
        {**PROPOSAL, "requirements": {"sql": "DROP TABLE writing_jobs"}},
    ],
)
async def test_invalid_or_unapproved_parameters_cannot_create_job(proposal):
    commands, database, orchestrator, requests = runtime()
    with pytest.raises(ValidationError):
        await start(commands, proposal=proposal)
    assert not database.rows and not requests
    orchestrator.start_job.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError, ConnectionError])
async def test_start_failure_preserves_committed_row_and_binding_without_retry(failure):
    async def fail(job):
        raise failure("accepted response unavailable")

    commands, database, orchestrator, _ = runtime(fail)
    bound = AsyncMock()
    with pytest.raises(failure):
        await start(commands, on_bound=bound)
    assert len(database.rows) == 1
    assert bound.await_args.args[0]["job_id"] == str(next(iter(database.rows.values())).id)
    assert bound.await_args.args[0]["workflow_accepted"] is False
    assert orchestrator.start_job.await_count == 1


@pytest.mark.asyncio
async def test_cancellation_during_start_preserves_row_and_receipt_binding():
    entered_start = asyncio.Event()

    async def wait_for_acceptance(job):
        entered_start.set()
        await asyncio.Future()

    commands, database, orchestrator, _ = runtime(wait_for_acceptance)
    bound = AsyncMock()
    task = asyncio.create_task(start(commands, on_bound=bound))
    await asyncio.wait_for(entered_start.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(database.rows) == 1
    assert bound.await_count == 1
    assert bound.await_args.args[0]["receipt"]["status"] == "landed"
    assert orchestrator.start_job.await_count == 1
    orchestrator.get_progress.assert_not_awaited()


@pytest.mark.asyncio
async def test_binding_failure_preserves_job_without_issuing_workflow_start():
    commands, database, orchestrator, _ = runtime()
    bound = AsyncMock(side_effect=ConnectionError("checkpoint unavailable"))
    with pytest.raises(ConnectionError):
        await start(commands, on_bound=bound)
    assert len(database.rows) == 1
    orchestrator.start_job.assert_not_awaited()


def test_default_service_preserves_job_created_outbox_audit():
    commands = ConversationWritingCommands(database=Database(), orchestrator=SimpleNamespace())
    assert isinstance(commands.jobs.outbox_service, OutboxService)
