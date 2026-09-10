# 实测样例：一条视频 → 统一文本 → 入库

> 这份文件是 `tools/video_ingest_demo.py` 的**真实运行输出**，不是手写示意。
> 环境：macOS + ffmpeg + macOS `say`(Tingting 中文音色) + 本地 faster-whisper `small`(int8/CPU)。
> 全程不访问任何云服务、不使用任何企业凭据。

## 输入素材

用系统 TTS 合成了一条 20 秒的中文新闻口播，再与纯色画面合成为 mp4：

```bash
say -v Tingting -o /tmp/speech.aiff \
  "腾讯新闻今日要闻。受强冷空气影响，北方多地气温骤降，气象部门发布寒潮蓝色预警。\
专家提醒公众注意防寒保暖，减少户外活动时间。交通运输部门已启动应急预案，确保道路通行安全。"

ffmpeg -y -f lavfi -i "color=c=0x1a3a5c:s=1280x720:d=20" -i /tmp/speech.aiff \
  -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac -b:a 128k /tmp/news_clip.mp4
```

## 步骤 1：媒资元数据探测（ffprobe）

```text
文件      : /private/tmp/na_video/news_clip.mp4
时长      : 20.00 秒
视频轨    : h264 1280x720
音频轨    : aac
```

## 步骤 2：语音转写（ASR）

`LocalWhisperTranscriber` + faster-whisper `small`，CPU int8，约 4.7 秒完成：

```text
同讯新闻今日要闻,受墙冷空气影响,北方多地气温皱降,气象部门发布韩朝蓝色预警。
专家提醒公众注意防寒保暖,减少户外活动时间。交通运输部门已启动应急预案,确保道路通行安全。
```

`duration=19.97s`，`language=zh`，`language_probability=1.0`。

> **注意错字**：`腾讯`→`同讯`、`强冷`→`墙冷`、`骤降`→`皱降`、`寒潮`→`韩朝`。
> 这是 `small` 模型对合成语音的精度问题，**不是链路问题**。生产走云侧 ASR
> （腾讯云 `CreateRecTask` 的 `16k_zh_en` 普方英大模型引擎）或换更大模型即可显著改善。

## 步骤 3：入库结果

```text
[created] news-20260910V00002
[created] news-20260910V00002-article
汇总: 新建 2 / 覆盖 0 / 跳过 0 / 失败 0
```

## 步骤 4：统一入库文档

### 4.1 视频新闻

```text
# 寒潮来袭北方多地气温骤降

内容类型: 视频新闻
发布时间: 2026-09-10T20:30:00+08:00
来源: https://news.qq.com/omn/20260910V00002
视频时长: 20 秒
文本来源: 语音转写 + 内容中心摘要
内容置信度: 高

## 语音转写
同讯新闻今日要闻,受墙冷空气影响,北方多地气温皱降,气象部门发布韩朝蓝色预警。专家提醒公众注意防寒保暖,减少户外活动时间。交通运输部门已启动应急预案,确保道路通行安全。

## 内容摘要
中央气象台发布寒潮蓝色预警，北方多地气温骤降。
```

metadata：

```text
asr_model = faster-whisper
asr_model_version = small/int8
audio_duration_seconds = 19.97
is_low_confidence = false
media_type = video
news_id = 20260910V00002
original_title = 寒潮来袭北方多地气温骤降
publish_time = 2026-09-10T20:30:00+08:00
source = writing-agent-service
source_url = https://news.qq.com/omn/20260910V00002
text_source = transcript
text_source_count = 2
text_sources = transcript,summary
textualization_policy_version = video-text-v2
```

### 4.2 图文新闻（用于结构对比）

```text
# 寒潮来袭北方多地气温骤降

内容类型: 图文新闻
发布时间: 2026-09-10T20:30:00+08:00
来源: https://news.qq.com/omn/20260910V00002
文本来源: 图文正文
内容置信度: 高

## 正文
中央气象台今日发布寒潮蓝色预警，受强冷空气影响，北方多地气温将下降六到十摄氏度，局地降温幅度可达十二摄氏度以上。气象部门提醒公众注意防寒保暖，减少不必要的户外活动，交通运输部门已启动应急预案。
```

metadata：

```text
is_low_confidence = false
media_type = article
news_id = 20260910V00002-article
text_source = body
text_source_count = 1
text_sources = body
textualization_policy_version = video-text-v2
...
```

## 步骤 5：结构统一性核对

```text
视频文档公共字段 5/5: 一致
图文文档公共字段 5/5: 一致
视频章节: ['## 语音转写', '## 内容摘要']
图文章节: ['## 正文']
```

两条文档的头部字段完全一致（`内容类型 / 发布时间 / 来源 / 文本来源 / 内容置信度`），
差异只在"视频多一行 `视频时长`"与"渲染了哪些章节"。**这就是统一入库的机器可校验口径。**

## 附：降级路径实测（无音轨视频）

用 `testsrc` 造一条无音轨视频，ASR 必然失败，验证不会因此中断入库：

```text
音频轨    : × 无音轨
[skipped] news-demo-silent  （insufficient text: source=summary, content_chars=18, doc_chars=164;
          audio transcription unavailable: local whisper transcription failed: IndexError: tuple index out of range）
汇总: 新建 0 / 覆盖 0 / 跳过 1 / 失败 0
```

要点：

- ASR 抛异常被翻译成 `AudioTranscriptionUnavailableError`，**只记 `degrade_reason` 并继续**，
  没有让整条入库失败（`failed=0`）。
- 落到 `summary` 来源后只有 18 个有效字符，未达 40 字门槛，被**充分性闸门跳过**，
  而不是作为空壳写进向量库。
- 日志里区分了 `content_chars=18`（有效内容）与 `doc_chars=164`（含头部元信息的整篇文档），
  避免误判"164 字为什么还算不充分"。

## 复现命令

```bash
cd writing-agent-service

# 正常路径
PYTHONPATH=$PWD python tools/video_ingest_demo.py \
  --video /tmp/news_clip.mp4 \
  --news-id 20260910V00002 \
  --title "寒潮来袭北方多地气温骤降" \
  --summary "中央气象台发布寒潮蓝色预警，北方多地气温骤降。" \
  --source-url "https://news.qq.com/omn/20260910V00002" \
  --asr local --whisper-model small

# 降级路径
PYTHONPATH=$PWD python tools/video_ingest_demo.py \
  --video /tmp/silent.mp4 --news-id demo-silent \
  --title "无音轨视频降级演示" --summary "这条视频没有音轨。" --asr local
```

`--asr local` 需要额外安装 `faster-whisper`（可选依赖）；`--asr none` 则完全不需要，
只演示元数据与摘要的降级路径。
