"""定义和执行聊天工具。 ConversationPlan 校验工具参数、调用热点与知识服务、检查权限；也负责把工具快照的指标和分析结果转换成展示文本。"""

from typing import Any, Literal, Self
from pydantic import BaseModel, ConfigDict, Field, model_validator


ToolName = Literal["capabilities", "list_hot_news", "read_hot_news", "search_knowledge", "query_hot_news", "analyze_hot_news_data"]

"""模型提出一项行动：response或tool"""
class ConversationPlan(BaseModel):

    model_config = ConfigDict(extra="forbid")

    action: Literal["respond", "tool"]
    answer: str = Field(default="", max_length=8000)
    tool_name: ToolName | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_exact_action(self) -> Self:
        if self.action == "tool":
            if self.tool_name is None or self.answer.strip():
                raise ValueError("a tool action requires only a tool and arguments")
        elif self.tool_name is not None or self.arguments or not self.answer.strip():
            raise ValueError("a response requires only a non-empty answer")
        return self
