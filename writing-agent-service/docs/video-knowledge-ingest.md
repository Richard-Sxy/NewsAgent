# 视频内容 → 语音转写 / 画面概括 → 统一文本 → 知识库入库

> 状态：**仓库侧已实现，离线测试全绿，并用本地 faster-whisper 真实跑通过端到端链路。**
> 企业 ASR 网关、企业模型网关与内容中心媒资字段仍待联调。
> 本链路没有接入任何真实企业地址、凭据或数据，不能表述为已上线。

## 一、这套东西解决什么

视频新闻在内容契约里只有 `title / summary / body` 三个文本字段，`body` 通常为空。
直接入库会产出"标题 + URL"的空壳文档，污染向量库，并让热点分析与关联重排同时退化。

本链路做四件事：

1. **拿到"说了什么"**：用 ASR 把视频口播转成文本（这是新闻信息量最大的部分）。
2. **拿到"拍到了什么"**：用企业多模态模型（混元）把画面概括成 2–4 句中文。
3. 按**成本分级**选择文本来源，通道不可用时自动降级而不是失败。
4. 通过**内容充分性闸门**，不达门槛的条目跳过而非写入空壳。

## 二、为什么是 ASR + 多模态，而不是二选一

两者看到的不是同一件事，不能互相替代：

| 通道 | 拿到什么 | 拿不到什么 |
|---|---|---|
| **ASR / 语音转写** | 主播说了什么、事件要素、数字、结论 | 画面信息（现场画面、字幕板、图表） |
| **多模态视频理解** | 画面拍到了什么（抽帧后理解） | **听不到声音** —— 音频轨不被转写 |

所以新闻视频的默认策略是**优先 ASR**（口播本身已覆盖核心信息），
拿到足够文本就不再调画面模型，避免为同一段视频付两次费用。
需要更丰富时打开 `VIDEO_FUSE_VISUAL_SUMMARY=true` 做互补融合。

> 注意：混元多模态视频理解（`hunyuan-turbos-vision-video`）是**抽帧理解画面**，
> 它**不做语音转写**。指望它"顺便听到主播说了什么"是不成立的。

## 三、混元 ASR 的真实能力边界（重要）

查腾讯云官方文档核实，**混元 ASR（Hy-ASR-3.0-preview，2026-08-04 开放内测）当前只提供实时语音识别**：

- 接口形式：`实时语音识别（WebSocket）`，`engine_model_type=Hy-ASR-3.0-preview`
- **单次音频必须 ≤ 1 分钟**
- **输入必须是 16kHz 单声道 PCM**
- 不支持话者分离、VAD、词汇替换、噪音阈值；上下文与热词尚未开放

也就是说，**它不能直接吃一条完整的新闻视频**。硬用就得先切片、转 PCM、再逐段流式送，
工程复杂度与成本都不成比例。

长视频有两条更合适的路：

| 通道 | 形态 | 为什么合适 |
|---|---|---|
| 腾讯云**语音识别 · 录音文件识别**（`CreateRecTask`） | 异步任务 + 轮询/回调 | **音频格式白名单含 `mp4` / `flv` / `m4a`**，可直接把视频地址丢进去，服务端自己抽音轨；URL 方式最长 5 小时、单文件 1GB |
| 腾讯云 **MPS · 智能字幕**（`ProcessMedia`） | 异步任务 | 底层就是 Hy ASR 3.0 preview，输出字幕，适合要时间戳的场景 |

**所以本仓库的参考实现选了 `CreateRecTask`**，不是混元 ASR 实时接口。
真实通道由企业侧决定，本项目只定义 Port。

### 三·一 `CreateRecTask` 的官方事实（逐条核对官方文档）

| 事实 | 内容 |
|---|---|
| 接口 | `asr.tencentcloudapi.com`，`Version=2019-06-14` |
| 任务模型 | 异步：`CreateRecTask` 提交 → `DescribeTaskStatus` 轮询，或 `CallbackUrl` 回调 |
| URL 限制 | 音频时长 ≤ 5 小时，文件 ≤ 1GB，**需公网浏览器可下载** |
| 格式白名单 | `wav` `mp3` `m4a` `flv` `mp4` `wma` `3gp` `amr` `aac` `ogg-opus` `flac` |
| `ResTextFormat` | `0` 基础 / `1` +词级时间戳 / `2` +标点 / `3` +标点且按标点分段（字幕场景） |
| 引擎枚举 | 大模型 2.0：`16k_zh_en_2.0`；1.0：`16k_zh_en` `16k_en_large` `16k_multi_lang`；通用：`16k_zh` `16k_yue` `16k_zh-PY` `16k_zh-TW` …（**大小写敏感**） |
| 引擎 × 格式限制 | `16k_ja` `16k_ko` `16k_multi_lang` 等 **只支持 `ResTextFormat=0`** |
| 热词 | `HotwordId`（控制台热词表）或 `HotwordList`（临时，格式 `词\|权重`，≤30 字符，最多 128 个） |
| 限频 | `CreateRecTask` 20 次/秒；`DescribeTaskStatus` 50 次/秒 |
| 结果有效期 | 识别结果与 `TaskId` 均保留 **24 小时** |

### 三·二 两个容易踩的坑

**坑一：`AudioDuration` 的单位是「秒」，不是毫秒。**

老资料里出现过"毫秒"的说法，但官方**数据结构页明确标注 `AudioDuration` 为 Float、单位「秒」**
（示例值 `1.2`、`2.38`）。早期实现按毫秒除了 1000，会把所有时长缩小一千倍——
更麻烦的是这种错误不会报错，只会让下游统计静默失真。当前实现按秒解析，
并在该字段缺失时退回 `ResultDetail` 的 `EndMs`（单位毫秒，文档明确）兜底。
`tests/test_tencent_asr_adapter.py` 里有一条断言专门防这个回归。

**坑二：`TaskId` 不是业务唯一 ID。**

官方明确 `TaskId` 数据类型为 `uint64`、**有效期 24 小时、不同日期可能出现重复**。
因此它只用于溯源（写入 `meta.source_version = "task-<id>"`），
业务的唯一键仍然是 `news_id`。

### 三·三 任务编排：为什么把提交与查询拆开

`CreateRecTask` 是**长时异步任务**：官方口径是"1 小时音频 1–3 分钟完成"，
最长 3 小时出结果。如果在单次 RPC 调用里"提交并轮询到完成"，会有两个真问题：

1. 调用方（Temporal Activity / HTTP 请求）的超时通常远小于识别耗时，
   **超时重试会重复提交任务、重复计费**；
2. 轮询期间占住连接与并发额度，批量入库时吞吐会崩。

所以 `TencentCloudAsrRpc` 提供两个**原子操作**：

```python
task_id  = await rpc.submit_transcription_task(context=..., request=...)   # 秒级返回
snapshot = await rpc.describe_transcription_task(task_id, request_id=...)  # 查一次
```

生产编排应"提交一次、把 `TaskId` 当幂等凭据持久化、之后按间隔查一次"。
`invoke_audio_transcription()` 只是把两者串起来并轮询到终态的**便捷入口**，
适合联调与短视频，长音频不建议使用。

配置了 `VIDEO_ASR_CALLBACK_URL` 时会带上 `CallbackUrl`，可完全省掉轮询。

### 三·四 腾讯内部接入的鉴权形态

`TencentCloudAsrRpc` 把"如何证明身份"抽成可插拔的 `AsrRequestSigner`，
请求体构造与响应解析完全复用：

| `VIDEO_ASR_AUTH_MODE` | 签名实现 | 适用 | 必需配置 |
|---|---|---|---|
| `tc3` | `Tc3RequestSigner` | 公网云 API | `VIDEO_ASR_SECRET_ID` / `VIDEO_ASR_SECRET_KEY` |
| `gateway` | `StaticHeaderSigner` | 内网网关注入身份头 | `VIDEO_ASR_ENDPOINT` + `VIDEO_ASR_GATEWAY_HEADERS` |
| `none` | `NoAuthSigner` | 内网免签 | `VIDEO_ASR_ENDPOINT` |

三个模式都缺项即抛错（fail-closed）。注意 **签名里的 `host` 由 endpoint 推导**，
所以换内网域名时签名会跟着变——这一点在早期实现里是硬编码公网域名，属于真实缺陷，已修。

TC3 的 `canonicalRequest` 拼接格式用官方文档示例值做过复算校验：
`build_canonical_request()` 的输出哈希与官方 `HashedCanonicalRequest` 逐位一致
（见 `tests/test_tencent_asr_adapter.py::test_canonical_request_reproduces_official_document_hash`）。
另有 `test_signature_covers_the_exact_bytes_that_are_sent` 保证"签名覆盖的 payload
== 实际发出的 body"。

## 四、统一输出契约（核心）

这是"统一入库"的关键：**无论来源是图文正文、语音转写、画面概括还是内容中心摘要，
最终都渲染进同一套骨架**，检索侧不需要为不同来源写不同解析逻辑。

```text
# {标题}
内容类型: 视频新闻 | 图文新闻
发布时间: <ISO 8601>
来源: <source_url>
视频时长: <N> 秒              ← 仅视频
文本来源: 语音转写 + 画面概括   ← 多来源用 " + " 连接
内容置信度: 高 | 低（已降级）

## 正文          ← 图文正文（BODY）
## 语音转写      ← ASR / 字幕（TRANSCRIPT）
## 画面概括      ← 多模态模型（VIDEO_SUMMARY）
## 内容摘要      ← 内容中心摘要（SUMMARY）
```

章节**顺序固定**，有哪段渲染哪段。`ContentTextualization` 上：

- `text_source` — 主来源（决定 metadata 里的粗粒度标记，供检索降权）
- `sources` — 本次实际生效的全部来源，按固定顺序
- `effective_sources` — 空时回退为 `(text_source,)`
- `is_fused` — 是否由多路来源融合而成

metadata 中对应新增 `text_sources`（逗号分隔）与 `text_source_count`。

实测输出（本地 faster-whisper，见第八节）：

```text
# 寒潮来袭北方多地气温骤降

内容类型: 视频新闻
发布时间: 2026-09-10T20:30:00+08:00
来源: https://news.qq.com/omn/20260910V00002
视频时长: 20 秒
文本来源: 语音转写 + 内容中心摘要
内容置信度: 高

## 语音转写
同讯新闻今日要闻,受墙冷空气影响,北方多地气温皱降,气象部门发布韩朝蓝色预警。...

## 内容摘要
中央气象台发布寒潮蓝色预警，北方多地气温骤降。
```

## 五、完整调用链

```text
上游入口
  RpcNewsContentRepository.batch_get_by_news_ids          （已有，不改）
  → NewsContent(content_type=VIDEO)

编排
  ContentIngestService.ingest(tenant_id, contents)
    ├─ VideoAssetResolver.resolve(news_id)                → VideoAsset
    │    （企业媒资/内容中心 RPC；缺省为 None，走降级）
    ├─ VideoContentTextualizer.textualize(...)            → ContentTextualization
    │    ├─ 视频：TRANSCRIPT → VIDEO_SUMMARY → SUMMARY → METADATA（可叠加）
    │    │    ├─ AudioTranscriber.transcribe(...)          ← 说了什么
    │    │    │    └─ RpcAudioTranscriber                  （领域 → ASR 契约）
    │    │    │         └─ AudioTranscriptionGatewayRpc    （企业 ASR Port）
    │    │    │              └─ TencentCloudAsrRpc         （腾讯云录音文件识别，联调）
    │    │    └─ VideoUnderstandingModel.summarize_video(...)  ← 拍到了什么
    │    │         └─ RpcVideoUnderstandingModel           （领域 → 网关契约）
    │    │              └─ MultimodalModelGatewayRpc       （企业统一网关 Port）
    │    │                   └─ HunyuanVisionVideoRpc      （混元协议参考实现）
    │    └─ 图文：BODY → SUMMARY → METADATA
    ├─ KnowledgeDocumentBuilder.build(...)                → KnowledgeDocument
    └─ KnowledgeBaseWriter.upsert_documents(...)          → IngestReport
         └─ FastGPTKnowledgeWriter                        （FastGPT 数据集）

下游输出
  FastGPT Collection（trainingType=chunk） + 扁平 metadata
```

降级链细节：`_resolve_transcript` 先看内容中心是否已给字幕，没有才调 ASR；
ASR 超时/失败只是记一条 `degrade_reason` 并继续往下走，**不会打断入库**。

## 六、文件与职责

| 文件 | 层 | 职责 |
|---|---|---|
| `app/analytics/audio_transcription.py` | 领域 | `AudioTranscriber` Port、`AudioTranscription`、`TranscriptSegment`、`InMemoryAudioTranscriber` |
| `app/analytics/video_textualization.py` | 领域服务 | 统一骨架渲染、降级链、prompt 版本化、多来源融合、质量闸门 |
| `app/clients/enterprise/audio_transcription.py` | 企业契约 | ASR RPC 强类型请求/响应 + `AudioTranscriptionGatewayRpc` Protocol |
| `app/clients/enterprise/tencent_asr.py` | 企业适配 | `TencentCloudAsrRpc`（腾讯云录音文件识别 + TC3 签名）+ `RpcAudioTranscriber`（错误翻译） |
| `app/clients/enterprise/multimodal_gateway.py` | 企业契约 | 视频理解 RPC 强类型请求/响应 + `MultimodalModelGatewayRpc` Protocol |
| `app/clients/enterprise/hunyuan_video.py` | 企业适配 | `HunyuanVisionVideoRpc`（混元协议）+ `RpcVideoUnderstandingModel`（错误翻译） |
| `app/clients/local_whisper.py` | 本地适配 | `LocalWhisperTranscriber`（faster-whisper 离线参考实现，可选依赖） |
| `app/knowledge/document.py` | 领域对象 | `KnowledgeDocument` / `IngestReport` / `KnowledgeBaseWriter` Port |
| `app/knowledge/ingest.py` | 编排 | `ContentIngestService`、`ContentIngestPolicy`、充分性闸门 |
| `app/clients/knowledge_writer.py` | 外部适配 | `FastGPTKnowledgeWriter`（幂等写入 + metadata） |
| `app/knowledge/bootstrap.py` | 装配 | `VideoIngestSettings` + `create_video_ingest_runtime` |
| `tools/video_ingest_demo.py` | 工具 | 端到端演示：一条视频 → 统一文本 → 入库 |

## 七、核心类与函数

**领域侧**

- `VideoContentTextualizer.textualize(tenant_id, content, video_asset)` — 唯一入口。
- `VideoTextualizationPolicy` — 可版本化策略：`fps`、`min_text_chars`、
  `enable_video_understanding`、`enable_transcript`、`enable_audio_transcription`、
  `fuse_visual_summary`、`asr_language`、`asr_hotwords`、`prompt`。
- `_order_sections` / `_render_text` — 统一骨架的排序与渲染，图文与视频共用。
- `sanitize_model_text(value)` — 剔除角色标记 / HTML 标签 / 代码块，折叠空白。
  **这不是完整的 Prompt Injection 防护**，只是保证模型与 ASR 输出以"数据"身份进库。
- `AudioTranscriptionUnavailableError(retryable=...)` / `VideoSummaryUnavailableError` —
  领域降级信号，都带原始可重试语义。

**适配侧**

- `RpcAudioTranscriber.transcribe(...)` — 幂等键由 `news_id + language + media_url` 派生，
  与 `request_id` 无关，Temporal 重试可安全复用同一次转写。
- `TencentCloudAsrRpc` — `CreateRecTask` 提交 + `DescribeTaskStatus` 轮询；
  `build_tc3_authorization(...)` 是纯函数，可离线单测。
  **凭据一律由调用方注入，不读环境变量、不落日志。**
- `LocalWhisperTranscriber` — 只接受本地路径 / `file://`，拒绝远程 URL；
  `faster_whisper` 懒加载，未安装时抛可降级异常而不是 import 期崩溃。
- `FastGPTKnowledgeWriter.upsert_documents(...)` — 先按 `document_id` 查同名 Collection，
  存在则跳过；单条失败返回 `FAILED` 而不中断整批。

## 八、端到端演练（已真实跑通）

```bash
cd writing-agent-service

# 1) 造一条带中文口播的测试视频（需要系统 TTS 与 ffmpeg）
say -v Tingting -o /tmp/speech.aiff "腾讯新闻今日要闻。受强冷空气影响，北方多地气温骤降……"
ffmpeg -y -f lavfi -i "color=c=0x1a3a5c:s=1280x720:d=20" -i /tmp/speech.aiff \
  -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 128k /tmp/news_clip.mp4

# 2) 跑完整链路（ASR 走本地 faster-whisper，不访问任何云服务）
python tools/video_ingest_demo.py \
  --video /tmp/news_clip.mp4 \
  --news-id 20260910V00002 \
  --title "寒潮来袭北方多地气温骤降" \
  --asr local --whisper-model small
```

实测结果：`small` 模型在 CPU 上约 7 秒完成 20 秒音频的转写，
产出上面第四节的统一文档，`skipped=0 / failed=0`，
metadata 带 `asr_model=faster-whisper`、`asr_model_version=small/int8`、
`audio_duration_seconds=19.97`、`text_sources=transcript,summary`。

需要注意：本地 `small` 模型对合成语音有错字（"腾讯"→"同讯"、"骤降"→"皱降"），
**这是模型精度问题，不是链路问题**；生产走云侧 ASR 或把 `--whisper-model` 调大即可。

## 九、配置项

`VideoIngestSettings`（`app/knowledge/bootstrap.py`），与主 `Settings` 解耦，默认 **fail-closed**：

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `VIDEO_INGEST_ENABLED` | `false` | 未显式打开时装配函数直接抛错 |
| `VIDEO_UNDERSTANDING_ENABLED` | `true` | 关掉则不做画面概括 |
| `VIDEO_MODEL_BASE_URL` / `VIDEO_MODEL_API_KEY` | `""` | 企业统一模型网关 |
| `VIDEO_MODEL_ROUTE` | `hunyuan-turbos-vision-video-20250728` | 模型路由（版本化） |
| `VIDEO_SUMMARY_FPS` | `1.0` | 抽帧率，成本旋钮 |
| `VIDEO_SUMMARY_MAX_OUTPUT_CHARS` | `600` | 概括输出上限 |
| `VIDEO_FUSE_VISUAL_SUMMARY` | `false` | 是否让画面概括与语音转写叠加（费用翻倍） |
| `VIDEO_ASR_ENABLED` | `false` | 语音转写总开关 |
| `VIDEO_ASR_ROUTE` | `tencent-asr-16k-zh-en` | ASR 引擎路由 |
| `VIDEO_ASR_LANGUAGE` | `zh` | 语言；中文新闻建议 `16k_zh_en` 普方英引擎 |
| `VIDEO_ASR_HOTWORDS` | `[]` | JSON 数组，热词提升专有名词识别率（单热词 ≤30 字符、最多 128 个，本地先校验） |
| `VIDEO_ASR_AUTH_MODE` | `tc3` | 鉴权形态：`tc3`=公网密钥 / `gateway`=内网注入头 / `none`=内网免签 |
| `VIDEO_ASR_SECRET_ID` / `VIDEO_ASR_SECRET_KEY` | `""` | `tc3` 模式必需，由 Secret 注入 |
| `VIDEO_ASR_REGION` | `""` | 腾讯云地域；留空则用默认 |
| `VIDEO_ASR_ENDPOINT` | `""` | 内网 / 自定义域名；`gateway` 与 `none` 模式必需 |
| `VIDEO_ASR_GATEWAY_HEADERS` | `{}` | `gateway` 模式的身份头（JSON 对象），由 Secret 注入 |
| `VIDEO_ASR_CALLBACK_URL` | `""` | 配置后走回调模式，可省掉轮询 |
| `VIDEO_ASR_STRICT_MEDIA_FORMAT` | `true` | 扩展名不在官方白名单时直接拒绝（无后缀的内网地址仍放行） |
| `VIDEO_ASR_POLL_INTERVAL_SECONDS` | `3.0` | 便捷入口的轮询间隔（生产建议改用两段式编排） |
| `VIDEO_ASR_MAX_POLL_ATTEMPTS` | `100` | 便捷入口的最大轮询次数 |
| `VIDEO_ASR_TIMEOUT_MS` | `120000` | 单次 HTTP 调用超时 |
| `VIDEO_TEXT_POLICY_VERSION` | `video-text-v2` | 策略版本，纳入 Production Bundle |
| `VIDEO_TEXT_MIN_CHARS` | `40` | 语义充分性门槛（ASR 与概括共用） |
| `VIDEO_INGEST_SKIP_INSUFFICIENT` | `true` | 不充分时跳过而非入库 |

## 十、测试入口

```bash
cd writing-agent-service
mkdir -p /tmp/pytest_na   # 沙箱下需要可写的 basetemp，否则 pytest 建临时目录会报错
./.venv/bin/python -m pytest tests/ -q --basetemp=/tmp/pytest_na --tb=no

# 单文件
./.venv/bin/python -m pytest tests/test_audio_transcription.py -q         # 12 passed
./.venv/bin/python -m pytest tests/test_tencent_asr_adapter.py -q         # 32 passed
./.venv/bin/python -m pytest tests/test_asr_bootstrap.py -q               # 9 passed
./.venv/bin/python -m pytest tests/test_video_textualization.py -q        # 13 passed
./.venv/bin/python -m pytest tests/test_video_knowledge_ingest.py -q      # 11 passed
./.venv/bin/python -m pytest tests/test_hunyuan_video_adapter.py -q       # 9 passed
```

全部离线：ASR 用 `InMemoryAudioTranscriber` / Fake，模型用 `FakeVideoModel`，
HTTP 用 `httpx.MockTransport`，不访问真实网络、不使用真实凭据。

覆盖点：ASR 接入与降级、质量闸门回退、多来源融合与章节顺序、
图文与视频骨架一致性、低置信度标记、租户上下文传递、
超时/鉴权/限流/5xx/`InternalError` 映射、`tenant_id` 透传与参数验证。

`test_tencent_asr_adapter.py` 另外覆盖了官方协议事实：引擎枚举大小写、
`ResTextFormat` 与引擎的兼容性降级、热词格式校验、URL 扩展名白名单、
`AudioDuration` 单位为秒、`TaskId` 兼容字符串与 uint64、
时长缺失时用 `ResultDetail` 兜底、提交/查询分离、三种鉴权形态，
以及**用官方文档示例值复算 `canonicalRequest` 哈希**。

## 十一、待企业确认（按优先级）

1. **视频地址从哪来。** 当前 `NewsContentRow` 只有 `title / summary / body`，没有
   `video_url`。需要内容中心新增字段，或提供媒资 RPC —— 对应实现 `VideoAssetResolver`。
2. **ASR 谁来做、怎么鉴权。** 最优是内容中心直接给字幕（本项目只做清洗与入库）；
   次优是企业 ASR 网关。若直连腾讯云「录音文件识别」，还需确认：
   - 用公网密钥（`VIDEO_ASR_AUTH_MODE=tc3`）还是内网网关注入身份
     （`gateway` / `none`）；
   - 内网域名是什么（`VIDEO_ASR_ENDPOINT`），以及模型网关侧是否能访问该地址。
   **不建议**用混元 ASR 内测版处理长视频（1 分钟限制）。
3. **视频地址对 ASR 通道是否可达。** `CreateRecTask` 用 `Url` 方式要求公网可下载；
   内网地址需要企业侧代理或用 `SourceType=1` 传音频字节（≤5MB，长视频不适用）。
4. **视频的 `summary` / `body` 到底返回什么。** 返回简介 / 返回空 / 返回播放器 HTML
   三种情况处理完全不同。建议补进 `docs/enterprise-rpc-integration-checklist.md` 第 3 节。
5. **入库目标库是 FastGPT 还是企业检索平台索引。** 若是后者，另写一个
   `KnowledgeBaseWriter` 实现即可，上层文本化完全复用。

## 十二、已知未完成项（TODO）

- [ ] `FastGPTKnowledgeWriter.existing_strategy` 目前只支持 `skip`；`replace`（删除后重建）
      需要确认 `collection/delete` 的 HTTP 方法与权限模型，暂未实现 ——
      **已存在的文档不会被刷新内容**（例如换了模型版本后摘要不会更新）。
- [ ] 未接入 Temporal Activity；目前 `ContentIngestService` 只提供领域入口，
      批量入库的重试与限流仍由调用方控制。
- [ ] `TencentCloudAsrRpc` 未做真实腾讯云联调。签名算法已用官方文档示例值**复算校验**
      （`canonicalRequest` 哈希逐位一致），但尚未对真实端点发起过调用 ——
      首次联调建议先单独跑一次 `submit_transcription_task`，确认鉴权与参数被服务端接受。
- [ ] 两段式编排尚未接入 Temporal：`submit_transcription_task` /
      `describe_transcription_task` 已就绪，但还没有对应的 Activity 与 `TaskId`
      持久化，当前批量入库仍走 `invoke_audio_transcription` 的便捷轮询路径。
- [ ] `CallbackUrl` 只做了参数透传，**尚未实现接收回调的 HTTP 端点**；
      启用回调模式前需要先有回调服务与结果落库。
- [ ] 未在热点分析输入与关联重排侧接入 `ContentTextualization`。
      该对象已具备 `text_source` / `is_sufficient`，接入时应对降级文档降权。
- [ ] 未做视频媒资字段的真实联调，所有企业字段映射仍是契约假设。
- [ ] `TranscriptSegment` 已带时间戳但尚未写入 metadata；若要支持"检索结果跳转到
      视频对应秒数"，需要把 segments 单独持久化或写进文档尾部。

## 十三、验收口径

- 抓 20 条视频入库后，能按语义召回，且 metadata 带 `media_type=video`。
- ASR 与模型都不可用时同一批仍能入库，`text_source` 降级为 `summary` 或 `metadata`，
  且 `IngestReport.failed` 为空。
- 空壳条目（`is_sufficient=false`）在默认策略下计入 `skipped` 而不是 `created`。
- 同一批入库的图文与视频文档，`内容类型 / 发布时间 / 来源 / 文本来源 / 内容置信度`
  五个公共字段**全部存在**（结构统一的机器可校验口径）。
- 图文检索基线（`data/retrieval_evaluation_results.json`）不下降。
