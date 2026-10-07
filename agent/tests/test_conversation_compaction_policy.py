"""Compaction admission, complete-request budgets and atomic candidate publication."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.conversation.context import (
    ContextBudgetExceeded,
    ContextCursor,
    ConversationMemory,
    LengthBasedContext,
    SummaryOutput,
    json_chars,
)
from app.domain.errors import AgentOutputValidationError


class MemoryRepository:
    """Publish only a successful save, exposing attempts separately from commits."""

    def __init__(self, memory):
        self.memory = memory.model_copy(deep=True)
        self.attempts = []
        self.saved = []
        self.error = None

    async def save_memory(self, **request):
        candidate = deepcopy(request)
        self.attempts.append(candidate)
        if self.error is not None:
            raise self.error
        assert candidate["expected_through"] == self.memory.through
        self.memory = candidate["memory"].model_copy(deep=True)
        self.saved.append(candidate)


class SummaryModel:
    """A deterministic model whose meter includes its independent request envelope."""

    def __init__(self, summary="已确认的讨论要点", *, overhead=0, behavior=None):
        self.summary = summary
        self.overhead = overhead
        self.behavior = behavior
        self.calls = []
        self.measurements = []

    def input_chars(self, *, mode, payload, output_type):
        assert mode == "conversation_memory"
        assert output_type is SummaryOutput
        size = json_chars(payload) + self.overhead
        self.measurements.append((deepcopy(payload), size))
        return size

    async def run_structured(self, *, app_id, mode, payload, output_type):
        assert app_id == "python:conversation_memory"
        assert mode == "conversation_memory"
        assert output_type is SummaryOutput
        self.calls.append(deepcopy(payload))
        summary = (await self.behavior(payload, len(self.calls))
                   if self.behavior is not None else self.summary)
        return SimpleNamespace(value=SummaryOutput(summary=summary), request_id=f"summary-{len(self.calls)}")


def cursor(index):
    return ContextCursor(created_at=datetime(2026, 10, 6, tzinfo=timezone.utc) + timedelta(seconds=index),
                         id=UUID(int=index + 2))


def read_reference():
    return {"name": "read_hot_news", "status": "completed", "arguments": {"run_id": str(UUID(int=99))},
            "result": {"run_id": str(UUID(int=99)), "not_found": False,
                       "items": [{"news_id": "trusted-news", "rank": 1, "title": "可信标题"}]}}


def context(*, texts, model=None, memory=None, references=None, **options):
    previous = memory or ConversationMemory()
    repository = MemoryRepository(previous)
    model = model or SummaryModel()
    ctx = LengthBasedContext(repository=repository, model=model, trace_projection=lambda value: value,
                             max_chars=10000, recent_chars=1200, summary_chars=500, **options)
    ctx.memory = previous.model_copy(deep=True)
    ctx.identity = dict(tenant_id="tenant", user_id="user", conversation_id=UUID(int=1),
                        turn_id=UUID(int=2), request_id=UUID(int=3))
    ctx.turns = [(cursor(index), {"user": f"完整问题 {index}", "assistant": text,
                                 "tools": deepcopy(references or []) if index == 0 else []})
                 for index, text in enumerate(texts)]
    return ctx, repository, model


async def notify(event, data):
    assert event == "phase"
    assert data["phase"] == "memory_compaction"


def request_measure(current_size, *, summary_metadata_size=0):
    """Measure current input and history together, rather than subtracting wrappers."""
    def measure(history):
        request = {"message": "当" * current_size, "history": history,
                   "tool_results": [], "available_tools": []}
        if summary_metadata_size and history and "summary" in history[0]:
            # A wire adapter may add summary-specific framing. Candidate admission
            # must use the borrowed complete-request meter, including that framing.
            request["summary_metadata"] = "元" * summary_metadata_size
        return json_chars(request)
    return measure


@pytest.mark.asyncio
async def test_soft_threshold_compacts_whole_turns_before_the_hard_limit():
    ctx, repo, model = context(texts=["背景" * 300 for _ in range(4)])
    measure = request_measure(500)
    original = deepcopy(ctx.turns)
    original_size = measure(ctx.history())
    hard_limit = original_size + 100
    assert int(hard_limit * 0.8) < original_size < hard_limit
    history = await ctx.fit(budget=hard_limit, model_ids=[], traces=[], notify=notify, measure=measure)
    assert model.calls and len(repo.saved) == 1
    assert measure(history) < original_size and measure(history) <= hard_limit
    assert ctx.turns == original[ctx.memory.compressed_turns:]
    assert repo.saved[0]["memory"].through == original[ctx.memory.compressed_turns - 1][0]
    assert original[0][1]["assistant"] == "背景" * 300


@pytest.mark.asyncio
async def test_below_soft_threshold_does_not_call_or_publish_a_summary():
    ctx, repo, model = context(texts=["短回答"])
    original = deepcopy(ctx.turns)
    history = await ctx.fit(budget=4000, model_ids=[], traces=[], notify=notify)
    assert history == [original[0][1]] and ctx.turns == original
    assert not model.calls and not repo.attempts


@pytest.mark.asyncio
async def test_uncompressible_prefix_between_soft_and_hard_limits_is_kept():
    memory = ConversationMemory(summary="已有摘要", through=cursor(-1), compressed_turns=1,
                                tools=[read_reference()])
    ctx, repo, model = context(texts=[], memory=memory)
    measure = request_measure(2400)
    size = measure(ctx.history())
    hard_limit = size + 100
    assert int(hard_limit * 0.8) < size < hard_limit
    assert await ctx.fit(budget=hard_limit, model_ids=[], traces=[], notify=notify, measure=measure) == ctx.history()
    assert ctx.memory == memory and repo.memory == memory
    assert not model.calls and not repo.attempts


@pytest.mark.asyncio
async def test_current_input_alone_over_hard_limit_stops_without_summary_work():
    ctx, repo, model = context(texts=["尚未压缩的旧消息" * 100])
    original = deepcopy(ctx.turns)
    with pytest.raises(ContextBudgetExceeded):
        await ctx.fit(budget=3000, model_ids=[], traces=[], notify=notify, measure=request_measure(4000))
    assert not model.calls and not repo.attempts and ctx.turns == original


@pytest.mark.asyncio
async def test_nonshrinking_summary_cannot_advance_memory_or_watermark():
    ctx, repo, model = context(texts=["答复"], model=SummaryModel("摘要" * 200))
    original = deepcopy(ctx.turns)
    measure = request_measure(2400)
    assert 2400 < measure(ctx.history()) < 3000
    with pytest.raises(AgentOutputValidationError):
        await ctx.fit(budget=3000, model_ids=[], traces=[], notify=notify, measure=measure)
    assert model.calls and not repo.attempts
    assert ctx.memory == repo.memory == ConversationMemory() and ctx.turns == original


@pytest.mark.asyncio
async def test_complete_candidate_including_trusted_references_must_fit_before_save():
    reference = read_reference()
    ctx, repo, model = context(texts=["旧" * 6000, "中" * 500, "近" * 180], references=[reference])
    original = deepcopy(ctx.turns)
    measure = request_measure(1000, summary_metadata_size=4000)
    initial = measure(ctx.history())
    history = await ctx.fit(budget=6000, model_ids=[], traces=[], notify=notify, measure=measure)
    assert sum(not call["previous_summary"] for call in model.calls) == 2
    assert len(repo.attempts) == len(repo.saved) == 1
    assert measure(history) <= 6000 and measure(history) < initial
    assert history[0]["tools"][0]["result"]["run_id"] == reference["result"]["run_id"]
    assert ctx.turns == original[ctx.memory.compressed_turns:]
    assert original[0][1]["tools"] == [reference]
    assert reference["arguments"]["run_id"] == str(UUID(int=99))


@pytest.mark.asyncio
async def test_exhausted_compaction_attempts_leave_every_candidate_unpublished():
    reference = read_reference()
    ctx, repo, model = context(texts=["旧" * 6000, "中" * 360, "近" * 180],
                               references=[reference], model=SummaryModel("摘要" * 100),
                               max_compaction_attempts=2)
    original = deepcopy(ctx.turns)
    original_memory = ctx.memory.model_copy(deep=True)
    with pytest.raises(ContextBudgetExceeded):
        await ctx.fit(budget=5200, model_ids=[], traces=[], notify=notify,
                      measure=request_measure(1000, summary_metadata_size=4000))
    assert sum(not call["previous_summary"] for call in model.calls) == 2 and not repo.attempts
    assert ctx.turns == original and ctx.memory == repo.memory == original_memory
    assert original[0][1]["tools"] == [reference]


@pytest.mark.asyncio
async def test_summary_request_meter_includes_its_own_envelope_when_splitting():
    model = SummaryModel(overhead=8000)
    ctx, repo, _ = context(texts=['引用"与换行\n' * 1500], model=model)
    original = deepcopy(ctx.turns)
    await ctx.fit(budget=4000, model_ids=[], traces=[], notify=notify)
    assert len(model.calls) > 1 and model.measurements
    assert all(json_chars(payload) + model.overhead <= ctx.max_chars for payload in model.calls)
    assert any(size > ctx.max_chars for _, size in model.measurements)
    assert len(repo.saved) == 1 and original[0][1]["assistant"] == '引用"与换行\n' * 1500


@pytest.mark.asyncio
async def test_cancelled_summary_keeps_existing_memory_references_and_all_turns():
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def wait_for_cancellation(payload, call):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    memory = ConversationMemory(summary="最近一次成功摘要", through=cursor(-1), compressed_turns=1,
                                tools=[read_reference()])
    ctx, repo, _ = context(texts=["新增内容" * 1000], memory=memory,
                           model=SummaryModel(behavior=wait_for_cancellation))
    original = deepcopy(ctx.turns)
    task = asyncio.create_task(ctx.fit(budget=4000, model_ids=[], traces=[], notify=notify))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and not repo.attempts
        assert ctx.memory == repo.memory == memory and ctx.turns == original
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_memory_save_does_not_publish_or_remove_original_turns():
    reference = read_reference()
    ctx, repo, _ = context(texts=["旧文本" * 2000], references=[reference])
    original = deepcopy(ctx.turns)
    original_memory = ctx.memory.model_copy(deep=True)
    repo.error = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await ctx.fit(budget=4000, model_ids=[], traces=[], notify=notify)
    assert len(repo.attempts) == 1 and not repo.saved
    assert ctx.memory == repo.memory == original_memory and ctx.turns == original
    assert original[0][1]["tools"] == [reference]


@pytest.mark.asyncio
async def test_invalid_model_summary_preserves_last_successful_projection():
    memory = ConversationMemory(summary="已有摘要", through=cursor(-1), compressed_turns=1,
                                tools=[read_reference()])
    ctx, repo, model = context(texts=["新增内容" * 1000], memory=memory, model=SummaryModel("过" * 501))
    original = deepcopy(ctx.turns)
    with pytest.raises(AgentOutputValidationError):
        await ctx.fit(budget=4000, model_ids=[], traces=[], notify=notify)
    assert model.calls and not repo.attempts
    assert ctx.memory == repo.memory == memory and ctx.turns == original
