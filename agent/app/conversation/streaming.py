"""将处理过程推送到前端。 StreamingConversation 通过 SSE 发送处理阶段、工具开始/完成、答复片段和结束时间，同时处理心跳和断线清理。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from uuid import UUID

from anyio import CancelScope
from sqlalchemy.exc import SQLAlchemyError

from app.conversation.service import ConversationAgentService
from app.repositories.conversation import TurnClaim


EVENT_NAMES = frozenset({"accepted", "phase", "tool_started", "tool_finished", "answer_delta", "done", "error"})


def encode_event(name: str, data: dict) -> str:
    if name not in EVENT_NAMES:
        raise ValueError("unknown conversation stream event")
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return f"event: {name}\ndata: {payload}\n\n"


class StreamingConversation:
    """流式对话类"""
    def __init__(self, *, service: ConversationAgentService, claim: TurnClaim,
                 tenant_id: str, user_id: str, conversation_id: UUID,
                 heartbeat_seconds: float = 10, chunk_chars: int = 128, hot_news_query_allowed: bool = False):
        if not 0 < heartbeat_seconds <= 30 or not 1 <= chunk_chars <= 1024:
            raise ValueError("invalid conversation stream limits")
        self._service = service
        self._claim = claim
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._conversation_id = conversation_id
        self._heartbeat = heartbeat_seconds
        self._chunk_chars = chunk_chars
        self._query_allowed = hot_news_query_allowed

    async def iter_events(self) -> AsyncIterator[str]:
        # 在配置的有限条件下，可能发生的进度事件少于 40 个
        # 模型/工具预算。无论如何都要限制队列，包括终端输出。
        queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=64)
        run_started = False

        async def publish(name: str, data: dict):
        # 进度仅包含服务器控制的阶段/工具摘要。
        # 停滞的消费者不得积累无限制的生成内容。
            queue.put_nowait((name, data))

        async def run():
            nonlocal run_started
            run_started = True
            try:
                # 模型/工具循环保持其原始预算。额外的十个
                # 秒将历史记录/最终数据库持久性限制在该循环之外。
                async with asyncio.timeout(self._service.turn_timeout_seconds + 10):
                    turn = await self._service.run_turn(
                        tenant_id=self._tenant_id, user_id=self._user_id,
                        conversation_id=self._conversation_id, claim=self._claim, on_event=publish,
                        hot_news_query_allowed=self._query_allowed,
                    )
                if turn.status not in {"completed", "failed"}:
                    raise ValueError("stream result must be terminal")
                await queue.put(("_terminal", {"turn": turn.model_dump(mode="json")}))
            except asyncio.CancelledError:
                raise
            except SQLAlchemyError:
                await queue.put(("error", {"code": "storage_unavailable", "message": "本轮结果保存暂不可用，请刷新历史确认；未发送未保存的答复。"}))
            except TimeoutError:
                await queue.put(("error", {"code": "stream_timeout", "message": "本轮处理或保存超时，请刷新历史确认原请求。"}))
            except Exception:
                # Never reflect database/transport credentials or raw model data.
                await queue.put(("error", {"code": "stream_unavailable", "message": "本轮响应暂不可用，请刷新历史确认原请求。"}))

        # 构造函数/预检中没有启动任何内容。响应主体拥有
        # 该任务在连接关闭时始终等待取消。
        task = asyncio.create_task(run(), name=f"conversation-stream-{self._claim.turn.id}")
        try:
            yield encode_event("accepted", {
                "turn_id": str(self._claim.turn.id), "request_id": str(self._claim.turn.request_id),
                "replayed": not self._claim.acquired, "status": self._claim.turn.status,
            })
            while True:
                try:
                    name, data = await asyncio.wait_for(queue.get(), timeout=self._heartbeat)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if name == "_terminal":
                    turn = data["turn"]
                    answer = turn["assistant_content"] or ""
                    for index, start in enumerate(range(0, len(answer), self._chunk_chars)):
                        yield encode_event("answer_delta", {
                            "turn_id": turn["id"], "request_id": turn["request_id"], "index": index,
                            "text": answer[start:start + self._chunk_chars],
                        })
                    yield encode_event("done", data)
                    return
                yield encode_event(name, data)
                if name == "error":
                    return
        finally:
            if not task.done():
                task.cancel()
            # Starlette's older ASGI disconnect branch uses level cancellation.
            # Shield the *awaited* cleanup from that enclosing cancel scope, not
            # from our explicit task.cancel(). No untracked task is detached.
            with CancelScope(shield=True):
                with suppress(asyncio.CancelledError, Exception):
                    await task
                if not run_started:
                    # The accepted-frame send can fail before create_task gets
                    # its first timeslice. Such a claim still needs interruption
                    # even though run_turn's cancellation handler never ran.
                    await self._service.interrupt_unstarted_turn(
                        tenant_id=self._tenant_id, user_id=self._user_id,
                        conversation_id=self._conversation_id, claim=self._claim,
                    )
