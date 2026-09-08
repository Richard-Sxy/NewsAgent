"""将完整 ResolvedMemoryContext 裁剪为模型可见的 Prompt Memory。"""

import json
from uuid import UUID

from app.schemas.hot_news import PromptMemoryContext, PromptMemoryItem
from app.schemas.user_memory import ResolvedMemoryContext


class MemoryPromptInputBuilder:
    """把解析后的 Memory 转换成确定性、大小受控的模型输入。"""

    def __init__(
        self,
        *,
        policy_version: str = "memory-prompt-v1",
        max_items: int = 20,
        max_value_chars: int = 1000,
    ) -> None:
        policy_version = policy_version.strip()
        if not policy_version:
            raise ValueError("policy_version cannot be empty")
        if not 1 <= max_items <= 20:
            raise ValueError("max_items must be between 1 and 20")
        if max_value_chars <= 0:
            raise ValueError("max_value_chars must be greater than 0")

        self.policy_version = policy_version
        self.max_items = max_items
        self.max_value_chars = max_value_chars

    def build(
        self,
        context: ResolvedMemoryContext,
    ) -> PromptMemoryContext:
        """只复制模型所需字段；来源和覆盖链保留在审计快照中。"""

        prompt_items: list[PromptMemoryItem] = []
        omitted_memory_ids: list[UUID] = []

        for resolved_item in context.memories:
            encoded_value = json.dumps(
                resolved_item.content.value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(encoded_value) > self.max_value_chars:
                omitted_memory_ids.append(
                    resolved_item.selected_memory_id
                )
                continue
            if len(prompt_items) >= self.max_items:
                omitted_memory_ids.append(
                    resolved_item.selected_memory_id
                )
                continue

            prompt_items.append(
                PromptMemoryItem(
                    memory_id=resolved_item.selected_memory_id,
                    memory_key=resolved_item.memory_key,
                    memory_kind=resolved_item.content.kind,
                    selected_tier=resolved_item.selected_tier,
                    origin=resolved_item.origin,
                    summary=resolved_item.content.summary,
                    value=resolved_item.content.value,
                    confidence=resolved_item.confidence,
                    version=resolved_item.version,
                )
            )

        return PromptMemoryContext(
            resolver_policy_version=self.policy_version,
            resolved_at=context.resolved_at,
            items=tuple(prompt_items),
            omitted_memory_ids=tuple(omitted_memory_ids),
        )
