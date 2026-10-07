"""企业统一模型网关的多模态（视频理解）调用契约。

与 ``model_gateway.py`` 的文本结构化契约并列：文本侧继续走
``StructuredModelGatewayRpc``，视频侧走本文件的 ``MultimodalModelGatewayRpc``。

契约只描述"企业模型网关能接受什么、会返回什么"，不包含业务重试策略，
也不假设网关内部用的是混元、Qwen-VL 还是其他厂商模型。真实网关如何路由到
``hunyuan-turbos-vision-video`` 由企业侧决定，本项目只负责把领域语义翻译成
网关请求，并对返回值做第一层严格校验。
"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.clients.enterprise.common import (
    NonBlank128,
    NonBlank256,
    RpcCallContext,
    RpcResponseMeta,
)


class VideoUnderstandingCallRequest(BaseModel):
    """提交给企业模型网关的版本化视频理解请求。

    约定：

    - ``video_url`` 必须是可以被模型网关直接访问的地址。企业内网地址需要由网关
      或企业侧做代理/转存，本项目不下载、不转存、不存储视频本体。
    - ``fps`` 是抽帧率，也是成本与粒度总开关。值越低，模型看到的画面越少、
      token 越省，产出的就是概览级摘要，符合"分类聚合"目标。
    - ``prompt_version`` 与 ``prompt`` 必须成对出现，保证 Prompt 可版本化。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scene: Literal["video_news_summarization"] = "video_news_summarization"
    model_route: NonBlank128
    video_url: NonBlank256
    prompt: str = Field(min_length=1, max_length=8_000)
    prompt_version: NonBlank128
    fps: float = Field(default=1.0, gt=0, le=10)
    max_output_chars: int = Field(default=600, ge=50, le=4_000)
    idempotency_key: NonBlank256


class VideoUnderstandingCallUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class VideoUnderstandingCallResponse(BaseModel):
    """网关返回的视频概括。

    模型输出仍是不可信数据，必须继续经过领域层长度/质量校验后才会进入知识库。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(min_length=1, max_length=8_000)
    model_name: NonBlank128
    model_version: NonBlank128
    finish_reason: NonBlank128 | None = None
    usage: VideoUnderstandingCallUsage
    meta: RpcResponseMeta


class MultimodalModelGatewayRpc(Protocol):
    """企业统一模型服务的多模态 RPC Port，不包含业务重试策略。"""

    async def invoke_video_understanding(
        self,
        *,
        context: RpcCallContext,
        request: VideoUnderstandingCallRequest,
    ) -> VideoUnderstandingCallResponse: ...
