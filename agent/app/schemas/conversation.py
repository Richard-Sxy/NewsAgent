"""聊天数据：创建会话、发送消息、历史轮次、工具调用记录和处理状态。"""

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


ConversationStatus = Literal["processing", "completed", "failed"]
ToolStatus = Literal["completed", "failed", "denied"]
ConversationTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
MessageContent = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]
AssistantContent = Annotated[str, StringConstraints(max_length=30000)]


class ConversationSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class CreateConversationRequest(ConversationSchema):
    title: ConversationTitle = "新对话"


class SendConversationMessageRequest(ConversationSchema):
    request_id: UUID
    content: MessageContent


class ToolTrace(ConversationSchema):
    name: str
    status: ToolStatus
    attempts: int = Field(ge=0)
    arguments: dict[str, Any]
    result: dict[str, Any]
    error_code: str | None = None


class ConversationView(ConversationSchema):
    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationTurnView(ConversationSchema):
    id: UUID
    request_id: UUID
    user_content: str
    assistant_content: AssistantContent | None = None
    status: ConversationStatus
    tools: list[ToolTrace] = Field(default_factory=list)
    model_request_ids: list[str] = Field(default_factory=list)
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class ConversationListResponse(ConversationSchema):
    items: list[ConversationView]


class ConversationDetailResponse(ConversationSchema):
    conversation: ConversationView
    turns: list[ConversationTurnView]
