"""Length-triggered, durable conversation compaction; original turns remain intact."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.errors import AgentOutputValidationError
from app.model_runtime.http import ModelTransportError


POLICY_VERSION = "conversation-length-v2"


def json_chars(value: Any) -> int:
    # Match NativeStructuredAgentClient's serialization, including JSON escaping.
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))


class ContextBudgetExceeded(Exception):
    pass


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    summary: str = Field(min_length=1, max_length=8000)


class ContextCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    created_at: datetime
    id: UUID

    def key(self):
        return self.created_at, self.id


class ConversationMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy_version: str = POLICY_VERSION
    summary: str = ""
    tools: list[dict[str, Any]] = Field(default_factory=list)
    through: ContextCursor | None = None
    compressed_turns: int = Field(default=0, ge=0)


class LengthBasedContext:
    def __init__(self, *, repository, model, trace_projection, max_chars=32000,
                 recent_chars=12000, summary_chars=4000, threshold_ratio=0.8,
                 max_compaction_attempts=2):
        if not 100 <= summary_chars <= 8000 or recent_chars < 100:
            raise ValueError("invalid conversation context budgets")
        if not math.isfinite(threshold_ratio) or not 0 < threshold_ratio <= 1:
            raise ValueError("context threshold ratio must be in (0, 1]")
        if type(max_compaction_attempts) is not int or not 1 <= max_compaction_attempts <= 4:
            raise ValueError("context compaction attempts must be between 1 and 4")
        if recent_chars + 2 * summary_chars + 1000 >= math.floor(max_chars * threshold_ratio):
            raise ValueError("context budget must reserve space for summary, references and current input")
        self.repository, self.model = repository, model
        self.project = trace_projection
        self.max_chars, self.recent_chars, self.summary_chars = max_chars, recent_chars, summary_chars
        self.threshold_ratio = threshold_ratio
        self.max_compaction_attempts = max_compaction_attempts
        self.memory = ConversationMemory()
        self.turns = []

    def history(self):
        prefix = ([{"summary": self.memory.summary, "tools": self.memory.tools}]
                  if self.memory.through else [])
        return prefix + [item[1] for item in self.turns]

    # 这边传入对话信息，模型名称，预算，异步回调函数。
    async def load(self, *, tenant_id, user_id, conversation_id, claim, model_ids, budget, notify, measure=None):
        self.identity = dict(tenant_id=tenant_id, user_id=user_id, conversation_id=conversation_id,
                             turn_id=claim.turn.id, request_id=claim.turn.request_id)
        self.memory = await self.repository.load_memory(tenant_id, user_id, conversation_id)
        cursor = self.memory.through
        while True:
            page = await self.repository.context_page(
                tenant_id, user_id, conversation_id, after=cursor,
                before=ContextCursor(created_at=claim.turn.created_at, id=claim.turn.id), limit=100,
            )
            for turn in page:
                cursor = ContextCursor(created_at=turn.created_at, id=turn.id)
                self.turns.append((cursor, {"user": turn.user_content,
                    "assistant": turn.assistant_content or "",
                    "tools": [self.project(trace) for trace in turn.tools]}))
                await self.fit(budget=budget, model_ids=model_ids, traces=[], notify=notify, measure=measure)
            if len(page) < 100:
                break
        await self.fit(budget=budget, model_ids=model_ids, traces=[], notify=notify, measure=measure)
        return self.history()
    
    async def fit(self, *, budget, model_ids, traces, notify, measure=None):
        """检查上下文信息，太长则压缩前面上文，保留最近的对话记录；检查合格以后先保存，再加载到上下文当中"""
        measure = measure or json_chars
        current_size = measure(self.history()) # 没有输入长度计算函数就选择 json_chars
        threshold = math.floor(budget * self.threshold_ratio) # 临界值，参考压缩率。
        if current_size <= threshold:
            return self.history()
        # 传入历史对话为空，如果原问题就超过预算了，那么直接抛出异常。
        # fixed_size() 表示没有历史会话的当前信息。
        fixed_size = measure([])
        if fixed_size > budget:
            # 当前问题，工具调用，系统提示词不能被压缩。
            raise ContextBudgetExceeded()
        if not self.turns:
            if current_size <= budget:
                return self.history()
            raise ContextBudgetExceeded()
        # 根据长度保留最近一轮的对话，而不是固定保留多少轮。至少压缩最近的一轮，即使他很长也要压缩。
        # 一个 turn 就是一次用户输入。len表示对话轮数。
        keep = len(self.turns)
        target = min(self.recent_chars, max(0, threshold - fixed_size - 2 * self.summary_chars - 500))
        # 取-keep的位置，取对话信息，因为[0]元素是一个bool值。
        while keep > 0 and measure([item[1] for item in self.turns[-keep:]]) - fixed_size > target:
            keep -= 1
        keep = min(keep, len(self.turns) - 1)
        # 输出到前端的信息。
        await notify("phase", {"phase": "memory_compaction", "message": "上下文接近长度预算，正在压缩较早对话。"})
        for attempt in range(self.max_compaction_attempts):
            count = len(self.turns) - keep
            older = self.turns[:count]
            summary = await self._summarize([item[1] for item in older], model_ids)
            memory = ConversationMemory(summary=summary, through=older[-1][0],
                compressed_turns=self.memory.compressed_turns + count,
                tools=self._references([*self.memory.tools, *(tool for _, turn in older for tool in turn["tools"])]))
            candidate = [{"summary": memory.summary, "tools": memory.tools}] + [item[1] for item in self.turns[count:]]
            candidate_size = measure(candidate)
            if candidate_size >= current_size:
                raise AgentOutputValidationError("conversation compaction did not reduce context", raw_content="")
            if candidate_size <= budget:
                # Check the complete replacement before CAS. A failed/cancelled
                # save leaves both the existing projection and this buffer intact.
                await self.repository.save_memory(**self.identity, expected_through=self.memory.through,
                                                  memory=memory, tools=traces, model_request_ids=model_ids)
                self.memory = memory
                del self.turns[:count]
                return self.history()
            if keep == 0:
                break
            # Retry with a larger complete head; the final attempt retains no
            # tail. Intermediate over-budget candidates are never published.
            keep = 0 if attempt == self.max_compaction_attempts - 2 else keep // 2
        raise ContextBudgetExceeded()

    def _references(self, tools):
        # IDs and news/window references come only from stored tool outputs,
        # never from model summaries. Recent distinct snapshots win the budget.
        retained, seen = [], set()
        for tool in reversed(tools):
            if tool.get("status") != "completed" or tool.get("name") not in {
                "list_hot_news", "read_hot_news", "query_hot_news", "analyze_hot_news_data"
            }:
                continue
            item = deepcopy(tool)
            item["arguments"] = {}
            def source(row):
                return {key: row[key] for key in ("run_id", "window_start", "window_end") if key in row}
            if item["name"] == "list_hot_news":
                item["result"] = {"items": [source(row) for row in item["result"].get("items", [])]}
            elif item["name"] == "analyze_hot_news_data":
                original = item["result"].get("source", {})
                item["result"] = {"source": {**source(original), "reference": source(original.get("reference", {}))}}
            if item["name"] in {"read_hot_news", "query_hot_news"}:
                # Long titles do not displace trustworthy identities.
                item["result"]["items"] = [{**row, "title": row.get("title", "")[:120]}
                                           for row in item["result"].get("items", [])]
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key in seen:
                continue
            seen.add(key)
            if json_chars([item]) > self.summary_chars:
                # Keep as many ranked/linked references as fit; list snapshots
                # still retain their run identities when the full list is large.
                rows = item["result"].pop("items", [])
                item["result"]["items"] = []
                for row in rows:
                    item["result"]["items"].append(row)
                    if json_chars([item]) > self.summary_chars:
                        item["result"]["items"].pop()
                        break
            if json_chars([item, *retained]) <= self.summary_chars:
                retained.insert(0, item)
        return retained

    async def _summarize(self, turns, model_ids):
        summary = self.memory.summary
        # Tool snapshots are handled separately by deterministic code. Text is
        # split into bounded fragments even for a single very long old answer.
        text = "\n".join(json.dumps({"user": turn["user"], "assistant": turn["assistant"]},
                                  ensure_ascii=False) for turn in turns)
        while text:
            width = min(len(text), max(100, (self.max_chars - 2 * self.summary_chars - 1000) // 2))
            payload = {"previous_summary": summary, "conversation_fragment": text[:width],
                       "max_summary_chars": self.summary_chars}
            while self.model.input_chars(mode="conversation_memory", payload=payload,
                                         output_type=SummaryOutput) > self.max_chars and width > 1:
                width //= 2
                payload["conversation_fragment"] = text[:width]
            if self.model.input_chars(mode="conversation_memory", payload=payload,
                                      output_type=SummaryOutput) > self.max_chars:
                raise ContextBudgetExceeded()
            for attempt in range(2):
                try:
                    result = await self.model.run_structured(app_id="python:conversation_memory",
                        mode="conversation_memory", payload=payload, output_type=SummaryOutput)
                    break
                except ModelTransportError as exc:
                    if not exc.retryable or attempt == 1:
                        raise
                    if exc.request_id:
                        model_ids.append(exc.request_id)
            if result.request_id:
                model_ids.append(result.request_id)
            summary = result.value.summary
            if len(summary) > self.summary_chars or json_chars(summary) > self.summary_chars + 2:
                raise AgentOutputValidationError("conversation summary exceeds configured budget", raw_content="")
            text = text[width:]
        return summary
