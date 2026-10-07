"""保存统一结果、模型请求编号、用量和输出。"""

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel


OutputT = TypeVar("OutputT", bound=BaseModel)


@dataclass(frozen=True)
class AgentResult(Generic[OutputT]):
    value: OutputT
    request_id: str | None
    usage: dict[str, Any]
    raw_content: str
