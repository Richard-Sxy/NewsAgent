"""Bounded, process-local reuse of tenant-owned writing/evaluation reads.

Every caller rechecks its HookSpec permissions and deployment enablement before
joining an in-flight read or touching the cache. User-owned memory is excluded.
Only these existing tenant-scoped READ projections can cross user/window reads;
no credential, request identity or model output is used as cache authority.
"""

import asyncio
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import time

from pydantic import ValidationError

from app.conversation.tools import ConversationToolDenied, hook_permitted


RECEIPT_READ_TTLS = {"writing_job_status": 3.0, "evaluation_status": 15.0}


class ReceiptReadUnavailable(TimeoutError):
    """The bounded reader is closed or has exhausted its in-flight budget."""


@dataclass(frozen=True)
class _CachedSnapshot:
    expires_at: float
    result: dict


class ReceiptReadCoordinator:
    def __init__(self, *, max_entries: int = 256, max_inflight: int = 128, clock=time.monotonic):
        if type(max_entries) is not int or type(max_inflight) is not int or min(max_entries, max_inflight) < 1:
            raise ValueError("receipt read limits must be positive integers")
        self.max_entries, self.max_inflight, self.clock = max_entries, max_inflight, clock
        self._cache: OrderedDict[tuple[str, str, str], _CachedSnapshot] = OrderedDict()
        self._inflight: dict[tuple[str, str, str], asyncio.Task] = {}
        self._closed = False

    async def read(self, *, tools, tenant_id: str, user_id: str, name: str,
                   arguments: dict, permissions: frozenset[str]) -> dict:
        spec = tools.hook_spec(name)
        if spec is None or name not in RECEIPT_READ_TTLS or spec.execution_class != "sync_read":
            raise ConversationToolDenied("hook_not_enabled")
        if not hook_permitted(spec, permissions):
            raise ConversationToolDenied("hook_permission_denied")
        if not tools._hook_enabled(spec):
            raise ConversationToolDenied("hook_not_enabled")
        try:
            parsed = spec.argument_schema.model_validate(arguments)
        except ValidationError as exc:
            raise ConversationToolDenied("tool_arguments_invalid") from exc
        if self._closed:
            raise ReceiptReadUnavailable("receipt reader is closed")
        entity_id = parsed.job_id if name == "writing_job_status" else parsed.workflow_id
        key = (tenant_id, name, entity_id)
        cached = self._cache.get(key)
        if cached is not None:
            if cached.expires_at > self.clock():
                self._cache.move_to_end(key)
                return deepcopy(cached.result)
            self._cache.pop(key)
        task = self._inflight.get(key)
        if task is None:
            if len(self._inflight) >= self.max_inflight:
                raise ReceiptReadUnavailable("receipt in-flight budget exhausted")
            task = asyncio.create_task(self._load(key, tools=tools, user_id=user_id,
                arguments=parsed.model_dump(), permissions=permissions), name="conversation-receipt-read")
            self._inflight[key] = task
            task.add_done_callback(self._consume_exception)
        # A browser disconnect may abandon its waiter, never the shared read.
        return deepcopy(await asyncio.shield(task))

    @staticmethod
    def _consume_exception(task):
        if not task.cancelled():
            task.exception()  # retrieve failures even when every waiter disconnects

    @staticmethod
    def _cacheable(result):
        if not isinstance(result, dict) or result.get("not_found") or result.get("error_code"):
            return False
        admission = result.get("admission")
        return not isinstance(admission, dict) or not admission.get("error_code")

    async def _load(self, key, *, tools, user_id, arguments, permissions):
        try:
            async with asyncio.timeout(10):
                result = await tools.execute(name=key[1], arguments=arguments, tenant_id=key[0],
                    user_id=user_id, trace_id="receipt-refresh", permissions=permissions)
            if self._cacheable(result) and not self._closed:
                self._cache[key] = _CachedSnapshot(self.clock() + RECEIPT_READ_TTLS[key[1]], deepcopy(result))
                self._cache.move_to_end(key)
                while len(self._cache) > self.max_entries:
                    self._cache.popitem(last=False)
            return result
        finally:
            self._inflight.pop(key, None)

    async def close(self):
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._cache.clear()
