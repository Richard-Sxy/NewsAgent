"""腾讯云语音识别（ASR）接入：Adapter 与协议参考实现。

分层与 ``hunyuan_video.py`` 一致：

```text
VideoContentTextualizer（领域）
→ AudioTranscriber（领域 Port）
→ RpcAudioTranscriber（Adapter：领域语义 ↔ ASR 网关契约）
→ AudioTranscriptionGatewayRpc（企业 Port）
   ├─ 企业统一 ASR 网关（生产：由企业路由到语音识别）
   └─ TencentCloudAsrRpc（参考实现：直连腾讯云「录音文件识别」）
```

为什么选「录音文件识别」（``CreateRecTask``）
-------------------------------------------

混元 ASR（``Hy-ASR-3.0-preview``）当前内测版只提供实时语音识别：单次音频必须
1 分钟以内、16k 单声道 PCM，且不支持 VAD。新闻视频普遍几分钟起步，硬用就得先
分片再流式送。而「录音文件识别」**原生接受视频容器**——音频格式白名单包含
``mp4`` / ``flv`` / ``m4a``，也就是说**直接把视频 URL 传进去，服务端自己抽音轨**，
URL 方式最长 5 小时、单文件 1GB，正好匹配"一批新闻视频离线入库"的场景。

官方事实（2026-09 逐条核对官方文档，含出处）
-------------------------------------------

==================================== ==========================================
事实                                  说明
==================================== ==========================================
接口与版本                             ``asr.tencentcloudapi.com``，``Version=2019-06-14``
任务模型                               异步：``CreateRecTask`` 提交 → ``DescribeTaskStatus``
                                      轮询，或 ``CallbackUrl`` 回调
URL 限制                               时长 ≤ 5 小时，文件 ≤ 1GB，**需公网浏览器可下载**
音频格式白名单                         wav mp3 m4a flv mp4 wma 3gp amr aac ogg-opus flac
时长字段                               ``TaskStatus.AudioDuration`` 类型 **Float，单位「秒」**
任务有效期                             识别结果与 TaskId 均为 24 小时；**TaskId 跨天会重复，
                                       不可作业务唯一 ID**
限频                                   CreateRecTask 20 次/秒，DescribeTaskStatus 50 次/秒
``ResTextFormat``                      0 基础 / 1 +词级时间戳 / 2 +标点 / 3 +标点且按标点分段
                                       （字幕场景）；部分引擎仅支持 0
热词                                   ``HotwordId``（控制台热词表）与 ``HotwordList``
                                       （临时热词，格式 ``词|权重``，≤30 字符，最多 128 个）
==================================== ==========================================

``AudioDuration`` 的单位是最容易搞反的一个：部分资料把它写成"毫秒"，但**数据结构页
明确标注 Float / 单位「秒」**（示例值 1.2、2.38）。本实现按「秒」解析，并在该字段
缺失时用 ``ResultDetail`` 的毫秒时间戳兜底，避免整条链路的时长被放缩。

任务编排：为什么要把提交与查询拆开
----------------------------------

``CreateRecTask`` 是**长时异步任务**：官方承诺 1 小时音频 1–3 分钟完成，最长 3 小时
出结果。若在单次 RPC 调用里"提交并轮询到完成"，会带来两个真问题：

1. 调用方（Temporal Activity / HTTP 请求）的超时通常远小于识别耗时，**超时重试会
   重复提交任务、重复计费**。
2. 轮询期间占住连接与并发额度，批量入库时吞吐会崩。

因此本类提供两个**原子操作**，推荐生产环境使用：

- :meth:`TencentCloudAsrRpc.submit_transcription_task` → ``TaskId``（秒级返回）
- :meth:`TencentCloudAsrRpc.describe_transcription_task` → :class:`AsrTaskSnapshot`

由编排层负责"提交一次、按间隔查询一次"，并把 ``TaskId`` 作为幂等凭据持久化。
``invoke_audio_transcription`` 只是把两者串起来的便捷实现，用于联调与短视频。

鉴权：公网 TC3 与内网网关
-------------------------

- 公网：:class:`Tc3RequestSigner`（TC3-HMAC-SHA256，需要 SecretId / SecretKey）。
- 腾讯内部 / 企业网关：通常不签发云 API 密钥，而由网关注入身份。此时用
  :class:`StaticHeaderSigner`（附内部标识头）或 :class:`NoAuthSigner`（免签）。
  签名只决定"怎么证明身份"，:meth:`build_create_task_payload` 的协议体完全复用。

凭据一律由调用方注入，不从环境变量读取、不落日志。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import hashlib
import hmac
import json
from time import time
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import urlparse

import httpx
from pydantic import ValidationError

from app.analytics.audio_transcription import (
    AudioTranscriber,
    AudioTranscription,
    AudioTranscriptionRequest,
    AudioTranscriptionUnavailableError,
    TranscriptSegment,
)
from app.clients.enterprise.audio_transcription import (
    AudioTranscriptionCallRequest,
    AudioTranscriptionCallResponse,
    AudioTranscriptionCallSegment,
    AudioTranscriptionCallUsage,
    AudioTranscriptionGatewayRpc,
)
from app.clients.enterprise.common import (
    DefaultRpcCallContextProvider,
    EnterpriseRpcAuthenticationError,
    EnterpriseRpcError,
    EnterpriseRpcRateLimitError,
    EnterpriseRpcResponseError,
    EnterpriseRpcTimeoutError,
    EnterpriseRpcUnavailableError,
    RpcCallContext,
    RpcCallContextProvider,
    RpcResponseMeta,
)

_ALGORITHM = "TC3-HMAC-SHA256"
_SERVICE = "asr"
_DEFAULT_HOST = "asr.tencentcloudapi.com"
DEFAULT_ENDPOINT = f"https://{_DEFAULT_HOST}"
_API_VERSION = "2019-06-14"
_CREATE_TASK = "CreateRecTask"
_DESCRIBE_TASK = "DescribeTaskStatus"
_PAYLOAD_CONTENT_TYPE = "application/json; charset=utf-8"

# 官方 SDK 与文档示例只签 content-type 与 host。少签几个头，出错面更小。
# 注意：X-TC-Action / X-TC-Version / X-TC-Region 经请求头传递，默认不参与签名
# （官方手写示例的 SignedHeaders 正是 content-type;host）。
_DEFAULT_SIGNED_HEADERS: tuple[str, ...] = ("content-type", "host")

# EngineModelType 大小写敏感，必须逐字对齐官方枚举。
_ENGINE_BY_LANGUAGE: dict[str, str] = {
    "zh": "16k_zh_en",
    "zh-cn": "16k_zh_en",
    "zh-hans": "16k_zh_en",
    "zh-tw": "16k_zh-TW",
    "zh-hant": "16k_zh-TW",
    "zh-py": "16k_zh-PY",
    "yue": "16k_yue",
    "en": "16k_en_large",
    "ja": "16k_ja",
    "ko": "16k_ko",
}
_DEFAULT_ENGINE = "16k_zh_en"

# 官方限制：这些引擎仅支持 ResTextFormat=0，传 1/2/3 会直接报参数错误。
_RES_TEXT_FORMAT_ZERO_ONLY_ENGINES = frozenset(
    {
        "16k_multi_lang",
        "16k_ja",
        "16k_ko",
        "16k_vi",
        "16k_ms",
        "16k_id",
        "16k_fil",
        "16k_th",
        "16k_pt",
        "16k_tr",
        "16k_ar",
        "16k_es",
        "16k_hi",
        "16k_fr",
        "16k_zh_medical",
        "16k_de",
    }
)

# 3 = 词级时间戳 + 标点 + 按标点分段，适用字幕与入库溯源。
_DEFAULT_RES_TEXT_FORMAT = 3

# CreateRecTask 官方限制。URL 方式上限 5 小时 / 1GB；
# 此处只能校验时长与扩展名，文件大小需要内容中心侧保证。
_MAX_AUDIO_SECONDS = 5 * 3600
_SUPPORTED_MEDIA_EXTENSIONS = frozenset(
    {
        "wav",
        "mp3",
        "m4a",
        "flv",
        "mp4",
        "wma",
        "3gp",
        "amr",
        "aac",
        "ogg",
        "opus",
        "flac",
    }
)

# 临时热词表（HotwordList）官方限制。
_MAX_HOTWORDS = 128
_MAX_HOTWORD_CHARS = 30
_HOTWORD_WEIGHT = 5
_HOTWORD_FORBIDDEN_CHARS = ("|", ",", "，", "｜")

# DescribeTaskStatus 的 Data.Status 取值。
_STATUS_WAITING = 0
_STATUS_RUNNING = 1
_STATUS_SUCCESS = 2
_STATUS_FAILED = 3
_STATUS_NAMES = {
    _STATUS_WAITING: "waiting",
    _STATUS_RUNNING: "doing",
    _STATUS_SUCCESS: "success",
    _STATUS_FAILED: "failed",
}


def serialize_payload(payload: Mapping[str, Any]) -> str:
    """按 TC3 签名要求的紧凑格式序列化请求体。

    必须与实际发送的字节完全一致，否则服务端会以 ``AuthFailure.SignatureFailure``
    拒绝。单独抽成函数，保证"签名用"和"发送用"是同一份实现。
    """

    return json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))


def host_of(endpoint: str) -> str:
    """从 endpoint 解析出参与签名的 host。

    签名里的 ``host`` 必须与实际请求的 Host 头一致。企业在内网接入时会把
    endpoint 换掉，这里若仍硬编码公网域名，签名必然失败。
    """

    parsed = urlparse(endpoint if "//" in endpoint else f"//{endpoint}")
    host = parsed.netloc or parsed.path
    if not host.strip():
        raise ValueError(f"cannot derive host from endpoint: {endpoint!r}")
    return host


def _sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def build_canonical_request(
    *,
    action: str,
    payload_json: str,
    host: str,
    signed_headers: Sequence[str] = _DEFAULT_SIGNED_HEADERS,
) -> str:
    """拼接 TC3 规范请求串（canonicalRequest）。

    格式固定为（query string 为空也要保留那一行空行，这是最容易漏的地方）::

        POST
        /

        content-type:application/json; charset=utf-8
        host:asr.tencentcloudapi.com
        x-tc-action:<action 的小写形式>

        content-type;host;x-tc-action
        <sha256(payload_json)>

    单独抽出来是为了能用官方文档给出的示例值直接复算校验，
    而不只是"看起来像对的"。
    """

    normalized = [name.strip().lower() for name in signed_headers]
    if not normalized:
        raise ValueError("signed_headers cannot be empty")
    available = {
        "content-type": _PAYLOAD_CONTENT_TYPE,
        "host": host,
        "x-tc-action": action.lower(),
    }
    for name in normalized:
        if name not in available:
            raise ValueError(f"unsupported signed header: {name!r}")

    canonical_headers = "".join(f"{name}:{available[name]}\n" for name in normalized)
    return "\n".join(
        [
            "POST",
            "/",
            "",
            canonical_headers,
            ";".join(normalized),
            _sha256_hex(payload_json.encode("utf-8")),
        ]
    )


def build_tc3_authorization(
    *,
    secret_id: str,
    secret_key: str,
    action: str,
    payload: Mapping[str, Any],
    region: str = "",
    host: str = _DEFAULT_HOST,
    service: str = _SERVICE,
    timestamp: int | None = None,
    signed_headers: Sequence[str] = _DEFAULT_SIGNED_HEADERS,
) -> tuple[str, int]:
    """构造腾讯云 API 3.0 的 TC3-HMAC-SHA256 ``Authorization`` 头。

    返回 ``(authorization, timestamp)``。纯函数，便于离线单测与比对官方示例。

    签名覆盖「HTTP 方法 + host + payload 哈希 + 时间戳」：

    - ``host`` 必须与实际请求的 Host 一致，故通过 :func:`host_of` 从 endpoint 推导。
    - ``action`` 默认不进入 ``SignedHeaders``（官方示例只签 ``content-type;host``），
      它由 ``X-TC-Action`` 请求头承载；``signed_headers`` 可显式扩展。
    - ``region`` 为兼容调用签名而保留；腾讯云的 Region 走 ``X-TC-Region`` 头。
    """

    if not secret_id.strip():
        raise ValueError("secret_id cannot be empty")
    if not secret_key.strip():
        raise ValueError("secret_key cannot be empty")
    if not action.strip():
        raise ValueError("action cannot be empty")

    ts = int(timestamp if timestamp is not None else time())
    date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    canonical_request = build_canonical_request(
        action=action,
        payload_json=serialize_payload(payload),
        host=host,
        signed_headers=signed_headers,
    )

    credential_scope = f"{date}/{service}/tc3_request"
    string_to_sign = "\n".join(
        [
            _ALGORITHM,
            str(ts),
            credential_scope,
            _sha256_hex(canonical_request.encode("utf-8")),
        ]
    )

    secret_date = _hmac_sha256(f"TC3{secret_key}".encode("utf-8"), date)
    secret_service = _hmac_sha256(secret_date, service)
    secret_signing = _hmac_sha256(secret_service, "tc3_request")
    signature = hmac.new(
        secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    signed_header_names = ";".join(
        name.strip().lower() for name in signed_headers
    )
    authorization = (
        f"{_ALGORITHM} Credential={secret_id}/{credential_scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    return authorization, ts


class AsrRequestSigner(Protocol):
    """为单次 API 调用生成鉴权头，返回 ``(headers, timestamp)``。

    抽象点放在"如何证明身份"上：公网走 TC3 签名，企业内网走网关注入头或免签，
    而请求体构造与响应解析完全复用同一份实现。
    """

    def sign(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> tuple[dict[str, str], int]: ...


@dataclass(frozen=True, slots=True)
class Tc3RequestSigner:
    """公网 TC3-HMAC-SHA256 签名（需要云 API 密钥）。"""

    secret_id: str
    secret_key: str
    region: str = ""

    def sign(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> tuple[dict[str, str], int]:
        if not self.secret_id.strip():
            raise ValueError("secret_id cannot be empty")
        if not self.secret_key.strip():
            raise ValueError("secret_key cannot be empty")
        authorization, ts = build_tc3_authorization(
            secret_id=self.secret_id,
            secret_key=self.secret_key,
            action=action,
            payload=payload,
            region=self.region,
            host=host_of(endpoint),
        )
        return {"Authorization": authorization}, ts


@dataclass(frozen=True, slots=True)
class StaticHeaderSigner:
    """企业 / 内部网关模式：身份由网关注入，调用方只附网关要求的标识头。

    腾讯内部接入通常不签发云 API 密钥，而是走内部鉴权。此时把网关要求的
    header（如内部票据）从 Secret 注入即可，协议体与解析逻辑完全复用。
    """

    headers: Mapping[str, str]

    def sign(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> tuple[dict[str, str], int]:
        if not self.headers:
            raise ValueError("StaticHeaderSigner requires at least one header")
        return dict(self.headers), int(time())


@dataclass(frozen=True, slots=True)
class NoAuthSigner:
    """内网免签模式；仅当企业网关自身完成鉴权时使用。"""

    def sign(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        endpoint: str,
    ) -> tuple[dict[str, str], int]:
        return {}, int(time())


@dataclass(frozen=True, slots=True)
class TencentAsrCredentials:
    """腾讯云凭据。生产环境应由企业网关注入，不落配置、不进日志。"""

    secret_id: str
    secret_key: str
    region: str = ""
    endpoint: str = f"https://{_DEFAULT_HOST}"

    def validate(self) -> None:
        if not self.secret_id.strip():
            raise ValueError("secret_id cannot be empty")
        if not self.secret_key.strip():
            raise ValueError("secret_key cannot be empty")


@dataclass(frozen=True, slots=True)
class AsrTaskSnapshot:
    """``DescribeTaskStatus`` 的一次快照。

    时间戳字段位于 ``payload["ResultDetail"]``，单位为毫秒；整体时长
    ``payload["AudioDuration"]`` 单位为**秒**（官方数据结构标注为 Float）。
    """

    task_id: int
    status: int
    request_id: str
    payload: Mapping[str, Any]

    @property
    def status_name(self) -> str:
        return _STATUS_NAMES.get(self.status, f"unknown({self.status})")

    @property
    def is_terminal(self) -> bool:
        return self.status in (_STATUS_SUCCESS, _STATUS_FAILED)

    @property
    def is_success(self) -> bool:
        return self.status == _STATUS_SUCCESS

    @property
    def error_message(self) -> str:
        return str(self.payload.get("ErrorMsg") or "").strip()


def resolve_engine(language: str) -> str:
    """把语言标签映射为官方 ``EngineModelType``。"""

    key = (language or "").strip().lower()
    return _ENGINE_BY_LANGUAGE.get(key, _DEFAULT_ENGINE)


def resolve_res_text_format(engine: str) -> int:
    """在引擎兼容范围内挑选 ``ResTextFormat``。"""

    if engine in _RES_TEXT_FORMAT_ZERO_ONLY_ENGINES:
        return 0
    return _DEFAULT_RES_TEXT_FORMAT


def build_hotword_list(hotwords: Sequence[str]) -> str:
    """构造临时热词表 ``HotwordList``。

    官方格式 ``词|权重``，单热词 ≤30 字符、权重 [1,11] 或 100、最多 128 个。
    热词本身若含分隔符会破坏整个字符串的结构，因此在这里直接拒绝，
    而不是把畸形参数发给服务端。
    """

    cleaned: list[str] = []
    for word in hotwords:
        candidate = word.strip()
        if not candidate:
            continue
        if len(candidate) > _MAX_HOTWORD_CHARS:
            raise ValueError(
                f"hotword exceeds {_MAX_HOTWORD_CHARS} characters: {candidate[:40]!r}"
            )
        for char in _HOTWORD_FORBIDDEN_CHARS:
            if char in candidate:
                raise ValueError(
                    f"hotword must not contain {char!r}: {candidate!r}"
                )
        if candidate not in cleaned:
            cleaned.append(candidate)
    if not cleaned:
        raise ValueError("hotwords must contain at least one non-blank entry")
    if len(cleaned) > _MAX_HOTWORDS:
        raise ValueError(f"at most {_MAX_HOTWORDS} hotwords are allowed")
    return ",".join(f"{word}|{_HOTWORD_WEIGHT}" for word in cleaned)


def ensure_supported_media_url(url: str) -> str:
    """对 URL 扩展名做 fail-fast 校验。

    官方白名单不含 ``avi`` / ``mkv`` / ``m3u8`` 等，交上去只会白跑一趟并计费。
    若 URL 本身没有扩展名（企业内网地址常见），则放行，不做误杀。
    """

    path = urlparse(url).path
    last_segment = path.rsplit("/", 1)[-1]
    if "." not in last_segment:
        return url
    extension = last_segment.rsplit(".", 1)[-1].lower()
    if extension in _SUPPORTED_MEDIA_EXTENSIONS:
        return url
    raise ValueError(
        f"unsupported media extension '.{extension}'; "
        f"tencent asr accepts: {', '.join(sorted(_SUPPORTED_MEDIA_EXTENSIONS))}"
    )


class TencentCloudAsrRpc:
    """腾讯云「录音文件识别」参考实现，可直接作为 ``AudioTranscriptionGatewayRpc``。

    仅用于联调与协议验证；生产环境建议替换为企业统一 ASR 网关（或由内部网关代签）。
    """

    def __init__(
        self,
        *,
        credentials: TencentAsrCredentials | None = None,
        signer: AsrRequestSigner | None = None,
        endpoint: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 60,
        poll_interval_seconds: float = 3.0,
        max_poll_attempts: int = 100,
        poll_backoff: float = 1.5,
        max_poll_interval_seconds: float = 15.0,
        callback_url: str | None = None,
        strict_media_format: bool = True,
    ) -> None:
        if credentials is None and signer is None:
            raise ValueError("either credentials or signer must be provided")
        if credentials is not None:
            credentials.validate()
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than 0")
        if max_poll_attempts <= 0:
            raise ValueError("max_poll_attempts must be greater than 0")
        if poll_backoff < 1:
            raise ValueError("poll_backoff must be at least 1")
        if max_poll_interval_seconds < poll_interval_seconds:
            raise ValueError(
                "max_poll_interval_seconds cannot be smaller than poll_interval_seconds"
            )

        self._credentials = credentials
        self._signer: AsrRequestSigner = signer or Tc3RequestSigner(
            credentials.secret_id,  # type: ignore[union-attr]
            credentials.secret_key,  # type: ignore[union-attr]
            credentials.region,  # type: ignore[union-attr]
        )
        self._region = str(getattr(self._signer, "region", "") or "")
        resolved_endpoint = endpoint or (
            credentials.endpoint
            if credentials is not None
            else f"https://{_DEFAULT_HOST}"
        )
        self._endpoint = resolved_endpoint.rstrip("/")
        # 装配期就解析 host：配置写错时立刻暴露，而不是等到首次调用。
        host_of(self._endpoint)

        self._http_client = http_client or httpx.AsyncClient()
        self._owns_client = http_client is None
        self._timeout = httpx.Timeout(timeout_seconds, connect=10)
        self._poll_interval = poll_interval_seconds
        self._max_polls = max_poll_attempts
        self._poll_backoff = poll_backoff
        self._max_poll_interval = max_poll_interval_seconds
        self._callback_url = (callback_url or "").strip() or None
        self._strict_media_format = strict_media_format

    async def close(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    # ------------------------------------------------------------------
    # 协议层：请求体构造（纯函数，便于单测与比对官方文档）
    # ------------------------------------------------------------------

    @staticmethod
    def build_create_task_payload(
        request: AudioTranscriptionCallRequest,
    ) -> dict[str, Any]:
        """构造 ``CreateRecTask`` 请求体。

        关键点：``SourceType=0`` + ``Url`` 表示"给一个公网可下载的地址"，
        腾讯云自行下载并抽音轨——因此 ``media_url`` 可以直接是 mp4。
        """

        engine = resolve_engine(request.language)
        payload: dict[str, Any] = {
            "EngineModelType": engine,
            "ChannelNum": 1,  # 16k 引擎仅支持单声道
            "ResTextFormat": resolve_res_text_format(engine),
            "SourceType": 0,
            "Url": request.media_url,
            "ConvertNumMode": 1,  # 阿拉伯数字智能转换，便于检索与统计
            "FilterDirty": 1,
            "FilterModal": 1,
            "FilterPunc": 0,  # 保留标点：入库文本需要可读性
        }
        if request.hotwords:
            payload["HotwordList"] = build_hotword_list(request.hotwords)
        return payload

    # ------------------------------------------------------------------
    # 原子操作：提交 / 查询（生产推荐）
    # ------------------------------------------------------------------

    async def submit_transcription_task(
        self,
        *,
        context: RpcCallContext,
        request: AudioTranscriptionCallRequest,
    ) -> int:
        """只提交任务并返回 ``TaskId``，秒级返回，不做任何等待。

        编排层应把 ``TaskId`` 作为幂等凭据持久化，之后用
        :meth:`describe_transcription_task` 按间隔查询，避免超时重试导致重复计费。
        """

        self._validate_request(request)
        payload = self.build_create_task_payload(request)
        if self._callback_url:
            payload["CallbackUrl"] = self._callback_url
        body = await self._call_api(
            action=_CREATE_TASK,
            payload=payload,
            request_id=context.request_id,
        )
        return _extract_task_id(body, request_id=context.request_id)

    async def describe_transcription_task(
        self,
        task_id: int,
        *,
        request_id: str,
    ) -> AsrTaskSnapshot:
        """查询一次任务状态，返回强类型快照（不抛"失败"异常）。"""

        body = await self._call_api(
            action=_DESCRIBE_TASK,
            payload={"TaskId": task_id},
            request_id=request_id,
        )
        return _build_snapshot(body, task_id=task_id, request_id=request_id)

    # ------------------------------------------------------------------
    # 便捷入口：提交 + 轮询到完成（联调 / 短视频）
    # ------------------------------------------------------------------

    async def invoke_audio_transcription(
        self,
        *,
        context: RpcCallContext,
        request: AudioTranscriptionCallRequest,
    ) -> AudioTranscriptionCallResponse:
        """提交并按退避间隔轮询到终态，解析为强类型响应。

        .. warning::
           这条路径会在单次调用内等待识别完成，**只适合联调与短视频**。
           长音频请改用 :meth:`submit_transcription_task` +
           :meth:`describe_transcription_task` 两段式编排，否则调用方超时重试
           会造成重复提交与重复计费。
        """

        task_id = await self.submit_transcription_task(
            context=context, request=request
        )
        snapshot = await self._poll_until_terminal(
            task_id, request_id=context.request_id
        )
        if not snapshot.is_success:
            raise EnterpriseRpcResponseError(
                "tencent asr task failed: "
                f"{snapshot.error_message or 'unknown error'}",
                request_id=context.request_id,
                error_code=f"AsrTask{snapshot.status_name}",
            )
        return _build_response(
            snapshot, request=request, request_id=context.request_id
        )

    async def _poll_until_terminal(
        self,
        task_id: int,
        *,
        request_id: str,
    ) -> AsrTaskSnapshot:
        interval = self._poll_interval
        for _ in range(self._max_polls):
            snapshot = await self.describe_transcription_task(
                task_id, request_id=request_id
            )
            if snapshot.is_terminal:
                return snapshot
            await asyncio.sleep(interval)
            interval = min(interval * self._poll_backoff, self._max_poll_interval)
        raise EnterpriseRpcTimeoutError(
            f"tencent asr task {task_id} did not finish within "
            f"{self._max_polls} polls",
            request_id=request_id,
        )

    # ------------------------------------------------------------------
    # 入参校验与 HTTP 调用
    # ------------------------------------------------------------------

    def _validate_request(self, request: AudioTranscriptionCallRequest) -> None:
        limit = request.max_duration_seconds
        if limit is not None and limit > _MAX_AUDIO_SECONDS:
            raise ValueError(
                f"max_duration_seconds exceeds the {_MAX_AUDIO_SECONDS}s "
                "limit of tencent asr"
            )
        if self._strict_media_format:
            ensure_supported_media_url(request.media_url)

    async def _call_api(
        self,
        *,
        action: str,
        payload: Mapping[str, Any],
        request_id: str,
    ) -> Mapping[str, Any]:
        signer_headers, ts = self._signer.sign(
            action=action, payload=payload, endpoint=self._endpoint
        )
        headers: dict[str, str] = {
            "Content-Type": _PAYLOAD_CONTENT_TYPE,
            "X-TC-Action": action,
            "X-TC-Version": _API_VERSION,
            "X-TC-Timestamp": str(ts),
        }
        if self._region:
            headers["X-TC-Region"] = self._region
        # 让 signer 的输出优先：内部网关可能需要覆盖或追加身份头。
        headers.update(signer_headers)

        try:
            response = await self._http_client.post(
                self._endpoint,
                headers=headers,
                content=serialize_payload(payload).encode("utf-8"),
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise EnterpriseRpcTimeoutError(
                f"tencent asr {action} timed out",
                request_id=request_id,
            ) from exc
        except httpx.HTTPError as exc:
            raise EnterpriseRpcUnavailableError(
                f"tencent asr {action} transport error: {exc}",
                request_id=request_id,
            ) from exc

        _raise_for_status(response, request_id=request_id, action=action)
        _raise_for_api_error(response, request_id=request_id, action=action)
        try:
            body = response.json()
        except ValueError as exc:
            raise EnterpriseRpcResponseError(
                f"tencent asr {action} returned non-JSON body",
                request_id=request_id,
            ) from exc
        if not isinstance(body, dict):
            raise EnterpriseRpcResponseError(
                f"tencent asr {action} returned unexpected payload",
                request_id=request_id,
            )
        return body


def _raise_for_status(
    response: httpx.Response,
    *,
    request_id: str,
    action: str,
) -> None:
    status = response.status_code
    if status < 400:
        return
    detail = response.text.strip()[:500]
    if status in (401, 403):
        raise EnterpriseRpcAuthenticationError(
            f"tencent asr {action} unauthorized: {detail}",
            request_id=request_id,
            error_code=str(status),
        )
    if status == 429:
        raise EnterpriseRpcRateLimitError(
            f"tencent asr {action} rate limited: {detail}",
            request_id=request_id,
            error_code=str(status),
        )
    if status >= 500:
        raise EnterpriseRpcUnavailableError(
            f"tencent asr {action} unavailable: {detail}",
            request_id=request_id,
            error_code=str(status),
        )
    raise EnterpriseRpcResponseError(
        f"tencent asr {action} rejected request: {detail}",
        request_id=request_id,
        error_code=str(status),
    )


def _raise_for_api_error(
    response: httpx.Response,
    *,
    request_id: str,
    action: str,
) -> None:
    """腾讯云把业务错误放在 HTTP 200 的 ``Response.Error`` 里。"""

    try:
        body = response.json()
    except ValueError:
        return
    if not isinstance(body, dict):
        return
    error = body.get("Response")
    if not isinstance(error, dict):
        return
    error = error.get("Error")
    if not isinstance(error, dict):
        return
    code = str(error.get("Code") or "UnknownError")
    message = str(error.get("Message") or "")
    detail = f"tencent asr {action} error {code}: {message}"
    if code.startswith("AuthFailure") or code.startswith("Unauthorized"):
        raise EnterpriseRpcAuthenticationError(
            detail, request_id=request_id, error_code=code
        )
    if code.startswith("RequestLimitExceeded"):
        raise EnterpriseRpcRateLimitError(
            detail, request_id=request_id, error_code=code
        )
    if code.startswith("InternalError"):
        # 服务端内部错误是可重试的，交给 Temporal 的退避策略处理。
        raise EnterpriseRpcUnavailableError(
            detail, request_id=request_id, error_code=code
        )
    raise EnterpriseRpcResponseError(detail, request_id=request_id, error_code=code)


def _extract_response(body: Mapping[str, Any], *, request_id: str) -> Mapping[str, Any]:
    response = body.get("Response")
    if not isinstance(response, dict):
        raise EnterpriseRpcResponseError(
            "tencent asr response is missing Response object",
            request_id=request_id,
        )
    return response


def _extract_data(body: Mapping[str, Any], *, request_id: str) -> Mapping[str, Any]:
    data = _extract_response(body, request_id=request_id).get("Data")
    if not isinstance(data, dict):
        raise EnterpriseRpcResponseError(
            "tencent asr response is missing Data object",
            request_id=request_id,
        )
    return data


def _extract_task_id(body: Mapping[str, Any], *, request_id: str) -> int:
    """读取 ``TaskId``。

    官方类型是 **uint64**，Python 侧无需担心溢出；同时兼容字符串形态，
    避免网关把大整数序列化成字符串时直接失败。
    """

    raw = _extract_data(body, request_id=request_id).get("TaskId")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    raise EnterpriseRpcResponseError(
        "tencent asr CreateRecTask did not return a valid TaskId",
        request_id=request_id,
    )


def _build_snapshot(
    body: Mapping[str, Any],
    *,
    task_id: int,
    request_id: str,
) -> AsrTaskSnapshot:
    data = _extract_data(body, request_id=request_id)
    status = data.get("Status")
    if isinstance(status, bool) or not isinstance(status, int):
        raise EnterpriseRpcResponseError(
            f"tencent asr DescribeTaskStatus returned invalid Status: {status!r}",
            request_id=request_id,
        )
    response_request_id = str(
        _extract_response(body, request_id=request_id).get("RequestId") or request_id
    )
    return AsrTaskSnapshot(
        task_id=task_id,
        status=status,
        request_id=response_request_id,
        payload=data,
    )


def _resolve_duration_seconds(
    data: Mapping[str, Any],
    segments: Sequence[AudioTranscriptionCallSegment],
) -> float | None:
    """解析音频时长。

    ``TaskStatus.AudioDuration`` 官方标注为 **Float、单位「秒」**（示例 1.2、2.38），
    因此不做任何单位换算。若该字段缺失，退回 ``ResultDetail`` 的最大 ``EndMs``
    ——后者的单位是毫秒且在文档中明确，可作为可靠兜底。
    """

    raw = data.get("AudioDuration")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    if segments:
        return max(segment.end_seconds for segment in segments)
    return None


def _build_response(
    snapshot: AsrTaskSnapshot,
    *,
    request: AudioTranscriptionCallRequest,
    request_id: str,
) -> AudioTranscriptionCallResponse:
    data = snapshot.payload
    text = str(data.get("Result") or "").strip()
    if not text:
        raise EnterpriseRpcResponseError(
            "tencent asr returned an empty transcript",
            request_id=request_id,
        )

    segments: list[AudioTranscriptionCallSegment] = []
    details = data.get("ResultDetail")
    if isinstance(details, list):
        for item in details:
            if not isinstance(item, dict):
                continue
            segment_text = str(
                item.get("FinalSentence") or item.get("SliceSentence") or ""
            ).strip()
            if not segment_text:
                continue
            start_ms = item.get("StartMs")
            end_ms = item.get("EndMs")
            segments.append(
                AudioTranscriptionCallSegment(
                    start_seconds=(
                        float(start_ms) / 1000 if isinstance(start_ms, int) else 0.0
                    ),
                    end_seconds=(
                        float(end_ms) / 1000 if isinstance(end_ms, int) else 0.0
                    ),
                    text=segment_text,
                )
            )

    duration = _resolve_duration_seconds(data, segments)
    engine = resolve_engine(request.language)

    try:
        return AudioTranscriptionCallResponse(
            text=text,
            model_name="tencent-asr-rec-task",
            model_version=engine,
            language=request.language,
            duration_seconds=duration,
            segments=tuple(segments),
            usage=AudioTranscriptionCallUsage(
                audio_seconds=duration if duration is not None else 0.0
            ),
            meta=RpcResponseMeta(
                request_id=snapshot.request_id,
                source_system="tencent-cloud-asr",
                # TaskId 仅作溯源；官方明确其 24 小时有效且跨天可能重复，
                # 业务唯一 ID 请用 news_id。
                source_version=f"task-{snapshot.task_id}",
                served_at=datetime.now(tz=timezone.utc),
            ),
        )
    except ValidationError as exc:
        raise EnterpriseRpcResponseError(
            f"tencent asr response failed contract: {exc}",
            request_id=request_id,
        ) from exc


class RpcAudioTranscriber(AudioTranscriber):
    """把领域转写请求翻译为企业 ASR 通道调用，并做严格响应校验。

    职责边界（与 ``RpcVideoUnderstandingModel`` 对齐）：

    - 生成带租户与 Trace 的可信调用上下文，并派生幂等键。
    - 把 ``EnterpriseRpcError`` 一律翻译为 ``AudioTranscriptionUnavailableError``，
      让上层走降级链而不是整体失败；同时保留 ``retryable`` 供 Temporal 判断。
    - 拒绝空白或超长输出。
    """

    def __init__(
        self,
        client: AudioTranscriptionGatewayRpc,
        *,
        model_route: str,
        context_provider: RpcCallContextProvider | None = None,
        timeout_ms: int = 120_000,
        max_transcript_chars: int = 200_000,
    ) -> None:
        if not model_route.strip():
            raise ValueError("model_route cannot be empty")
        if not 50 <= timeout_ms <= 120_000:
            raise ValueError("timeout_ms must be between 50 and 120000")
        if max_transcript_chars <= 0:
            raise ValueError("max_transcript_chars must be greater than 0")
        self._client = client
        self._model_route = model_route
        self._context_provider = (
            context_provider or DefaultRpcCallContextProvider()
        )
        self._timeout_ms = timeout_ms
        self._max_transcript_chars = max_transcript_chars

    async def transcribe(
        self,
        *,
        tenant_id: str,
        request: AudioTranscriptionRequest,
    ) -> AudioTranscription:
        if not tenant_id.strip():
            raise ValueError("tenant_id cannot be empty")
        request.validate()
        idempotency_key = self._idempotency_key(request)
        context = self._context_provider.create(
            tenant_id=tenant_id,
            operation="news-audio-transcription",
            timeout_ms=self._timeout_ms,
            idempotency_key=idempotency_key,
        )

        try:
            response = await self._client.invoke_audio_transcription(
                context=context,
                request=AudioTranscriptionCallRequest(
                    model_route=self._model_route,
                    media_url=request.media_url,
                    language=request.language,
                    hotwords=request.hotwords,
                    max_duration_seconds=request.max_duration_seconds,
                    idempotency_key=idempotency_key,
                ),
            )
        except EnterpriseRpcError as exc:
            raise AudioTranscriptionUnavailableError(
                f"{type(exc).__name__}: {exc}",
                retryable=exc.retryable,
            ) from exc

        text = _coerce_text(response, max_chars=self._max_transcript_chars)
        if not text:
            raise AudioTranscriptionUnavailableError(
                "asr gateway returned an empty transcript",
                retryable=False,
            )
        return AudioTranscription(
            text=text,
            model_name=_coerce_str(response, "model_name", "unknown"),
            model_version=_coerce_str(
                response, "model_version", _coerce_str(response, "model_name", "unknown")
            ),
            language=_coerce_str(response, "language", ""),
            duration_seconds=_coerce_float(response, "duration_seconds"),
            segments=_coerce_segments(response),
        )

    @staticmethod
    def _idempotency_key(request: AudioTranscriptionRequest) -> str:
        """同一媒资 + 同一语言 + 同一策略可安全复用同一次转写。"""

        raw = f"{request.news_id}|{request.language}|{request.media_url}"
        return raw[:256]


def _coerce_str(response: Any, name: str, default: str) -> str:
    value = getattr(response, name, None)
    if value is None and isinstance(response, Mapping):
        value = response.get(name)
    text = str(value or "").strip()
    return text or default


def _coerce_float(response: Any, name: str) -> float | None:
    value = getattr(response, name, None)
    if value is None and isinstance(response, Mapping):
        value = response.get(name)
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


def _coerce_text(response: Any, *, max_chars: int) -> str:
    """兼容 Pydantic 契约对象与映射两种返回形态。"""

    value = getattr(response, "text", None)
    if value is None and isinstance(response, Mapping):
        value = response.get("text")
    text = str(value or "").strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text


def _coerce_segments(response: Any) -> tuple[TranscriptSegment, ...]:
    raw = getattr(response, "segments", None)
    if raw is None and isinstance(response, Mapping):
        raw = response.get("segments")
    if not raw:
        return ()
    segments: list[TranscriptSegment] = []
    for item in raw:
        start = _coerce_float(item, "start_seconds") or 0.0
        end = _coerce_float(item, "end_seconds")
        text = _coerce_str(item, "text", "")
        if not text:
            continue
        segments.append(
            TranscriptSegment(
                start_seconds=start,
                end_seconds=end if end is not None else start,
                text=text,
            )
        )
    return tuple(segments)
