#!/usr/bin/env python3
"""端到端演示：一条视频 → 统一文本 → 知识库入库。

这个脚本的作用是**把链路真的跑一遍**，而不是只跑单测：

```text
本地视频文件
  ├─ ffprobe 探测真实元数据（时长 / 分辨率 / 是否含音轨）
  └─ LocalWhisperTranscriber 真实转写口播
       ↓
  VideoContentTextualizer（领域策略 + 降级链 + 统一骨架渲染）
       ↓
  ContentIngestService（充分性闸门）
       ↓
  InMemoryKnowledgeBaseWriter（演示写入端）
```

生产环境把 ``LocalWhisperTranscriber`` 换成 ``RpcAudioTranscriber``（企业 ASR 网关
或 ``TencentCloudAsrRpc``）、把 ``InMemoryKnowledgeBaseWriter`` 换成
``FastGPTKnowledgeWriter`` 即可，中间所有领域逻辑完全复用。

用法::

    python tools/video_ingest_demo.py --video /path/to/news.mp4 \
        --title "寒潮来袭北方多地气温骤降" \
        --news-id 20260910V00002 \
        --asr local --whisper-model small

需要 ``faster_whisper``（可选依赖）与系统 ``ffprobe``；``--asr none`` 可跳过转写，
只演示元数据降级路径。
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics.entities import ContentType  # noqa: E402
from app.analytics.news_content import NewsContent  # noqa: E402
from app.analytics.video_textualization import (  # noqa: E402
    VideoAsset,
    VideoContentTextualizer,
    VideoTextualizationPolicy,
)
from app.knowledge.document import InMemoryKnowledgeBaseWriter  # noqa: E402
from app.knowledge.ingest import (  # noqa: E402
    ContentIngestPolicy,
    ContentIngestService,
)

SHANGHAI = timezone(timedelta(hours=8))

DEFAULT_ARTICLE_BODY = (
    "中央气象台今日发布寒潮蓝色预警，受强冷空气影响，北方多地气温将下降"
    "六到十摄氏度，局地降温幅度可达十二摄氏度以上。气象部门提醒公众注意"
    "防寒保暖，减少不必要的户外活动，交通运输部门已启动应急预案。"
)


# ----------------------------------------------------------------------
# 元数据探测
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbedMedia:
    duration_seconds: float
    has_audio: bool
    has_video: bool
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None


def probe_media(path: Path) -> ProbedMedia:
    """用 ffprobe 读取容器真实信息。

    这一步对应生产链路的"媒资 RPC 提供时长与音轨信息"；这里用本地探测替代，
    好处是演示不依赖任何外部服务。
    """

    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-show_entries",
            "stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(completed.stdout or "{}")
    streams = payload.get("streams") or []

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = payload.get("format", {}).get("duration")

    return ProbedMedia(
        duration_seconds=float(duration) if duration else 0.0,
        has_audio=audio is not None,
        has_video=video is not None,
        width=video.get("width") if video else None,
        height=video.get("height") if video else None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
    )


class StaticVideoAssetResolver:
    """把预置的 ``VideoAsset`` 按 ``news_id`` 返回。

    对应生产链路的 ``VideoAssetResolver``（企业媒资 / 内容中心 RPC）。
    """

    def __init__(self, assets: dict[str, VideoAsset]) -> None:
        self._assets = assets

    async def resolve(self, *, tenant_id: str, news_id: str) -> VideoAsset | None:
        return self._assets.get(news_id)


# ----------------------------------------------------------------------
# 输出
# ----------------------------------------------------------------------


def hr(title: str = "") -> None:
    line = "─" * 78
    print(f"\n{line}")
    if title:
        print(title)
        print(line)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="视频内容 → 统一文本 → 入库 的端到端演示",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--video", required=True, help="本地视频文件路径")
    parser.add_argument("--news-id", default="demo-video-1", help="业务主键")
    parser.add_argument("--title", default="寒潮来袭北方多地气温骤降")
    parser.add_argument("--summary", default="", help="内容中心摘要（可为空）")
    parser.add_argument(
        "--source-url",
        default="",
        help="视频原始页面地址；缺省时用 file:// 本地路径占位",
    )
    parser.add_argument(
        "--publish-time",
        default="2026-09-10T20:30:00+08:00",
        help="ISO 8601 发布时间",
    )
    parser.add_argument(
        "--asr",
        choices=("local", "none"),
        default="local",
        help="语音转写通道；local 走 faster-whisper，none 演示降级路径",
    )
    parser.add_argument("--whisper-model", default="small", help="whisper 模型规格")
    parser.add_argument(
        "--min-text-chars", type=int, default=40, help="充分性闸门阈值"
    )
    parser.add_argument(
        "--article-body",
        default=DEFAULT_ARTICLE_BODY,
        help="额外渲染一条图文新闻以对比结构；传空字符串则跳过",
    )
    return parser


async def run(args: argparse.Namespace) -> int:
    video_path = Path(args.video).expanduser().resolve()
    if not video_path.is_file():
        print(f"视频文件不存在: {video_path}", file=sys.stderr)
        return 2

    publish_time = datetime.fromisoformat(args.publish_time)
    if publish_time.tzinfo is None:
        publish_time = publish_time.replace(tzinfo=SHANGHAI)

    hr("1. 媒资元数据探测（ffprobe）")
    try:
        probe = probe_media(video_path)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(f"ffprobe 探测失败: {exc}", file=sys.stderr)
        return 2
    print(f"文件      : {video_path}")
    print(f"时长      : {probe.duration_seconds:.2f} 秒")
    print(f"视频轨    : {probe.video_codec} {probe.width}x{probe.height}")
    print(f"音频轨    : {probe.audio_codec if probe.has_audio else '× 无音轨'}")
    if not probe.has_audio and args.asr == "local":
        print("→ 无音轨，ASR 必然拿不到文本，将走降级链。")

    # ----------------------------------------------------------------
    # 装配：这里对应 bootstrap.create_video_ingest_runtime 的等价物
    # ----------------------------------------------------------------
    transcriber = None
    if args.asr == "local":
        from app.clients.local_whisper import LocalWhisperTranscriber

        transcriber = LocalWhisperTranscriber(model_size=args.whisper_model)

    policy = VideoTextualizationPolicy(
        version="video-text-v2",
        model_route="hunyuan-turbos-vision-video-20250728",
        min_text_chars=args.min_text_chars,
        asr_min_text_chars=args.min_text_chars,
        # 演示环境没有多模态网关：只跑 ASR 与降级路径。
        enable_video_understanding=False,
    )
    textualizer = VideoContentTextualizer(
        policy=policy, model=None, transcriber=transcriber
    )

    writer = InMemoryKnowledgeBaseWriter()
    service = ContentIngestService(
        textualizer=textualizer,
        writer=writer,
        policy=ContentIngestPolicy(version="video-ingest-v2", skip_insufficient=True),
        video_asset_resolver=StaticVideoAssetResolver(
            {
                args.news_id: VideoAsset(
                    video_url=f"file://{video_path}",
                    duration_seconds=int(probe.duration_seconds),
                )
            }
        ),
    )

    source_url = args.source_url or f"file://{video_path}"
    video_content = NewsContent(
        news_id=args.news_id,
        title=args.title,
        summary=args.summary,
        content_type=ContentType.VIDEO,
        publish_time=publish_time,
        source_url=source_url,
    )
    article_content = (
        NewsContent(
            news_id=f"{args.news_id}-article",
            title=args.title,
            summary="",
            content_type=ContentType.ARTICLE,
            publish_time=publish_time,
            source_url=source_url,
            body=args.article_body,
        )
        if args.article_body.strip()
        else None
    )

    contents = [video_content]
    if article_content is not None:
        contents.append(article_content)

    hr("2. 文本化 + 入库（统一骨架）")
    started = time.monotonic()
    report = await service.ingest(tenant_id="tenant-demo", contents=contents)
    elapsed = time.monotonic() - started

    for outcome in report.outcomes:
        print(f"[{outcome.status.value:>7}] {outcome.document_id}"
              + (f"  （{outcome.detail}）" if outcome.detail else ""))
    print(
        f"\n汇总: 新建 {len(report.created)} / 覆盖 {len(report.updated)} / "
        f"跳过 {len(report.skipped)} / 失败 {len(report.failed)}"
    )
    print(f"耗时: {elapsed:.1f}s")

    for document in writer.documents:
        hr(f"统一入库文档 · {document.document_id}")
        print(document.text)
        print("\n--- metadata ---")
        for key in sorted(document.metadata):
            print(f"{key} = {document.metadata[key]}")

    if article_content is not None:
        hr("3. 结构统一性核对")
        video_doc = writer.get(f"news-{args.news_id}")
        article_doc = writer.get(f"news-{args.news_id}-article")
        fields = ("内容类型: ", "发布时间: ", "来源: ", "文本来源: ", "内容置信度: ")
        for label, document in (("视频", video_doc), ("图文", article_doc)):
            present = (
                [f for f in fields if f in document.text] if document else []
            )
            print(f"{label}文档公共字段 {len(present)}/{len(fields)}: "
                  f"{'一致' if len(present) == len(fields) else '缺失'}")
        if video_doc and article_doc:
            video_sections = [l for l in video_doc.text.splitlines() if l.startswith("## ")]
            article_sections = [
                l for l in article_doc.text.splitlines() if l.startswith("## ")
            ]
            print(f"视频章节: {video_sections}")
            print(f"图文章节: {article_sections}")

    hr("完成")
    print("要把这条链路接到生产，只需替换两端：")
    print("  · 转写端：LocalWhisperTranscriber → RpcAudioTranscriber"
          "（企业 ASR 网关 / TencentCloudAsrRpc）")
    print("  · 写入端：InMemoryKnowledgeBaseWriter → FastGPTKnowledgeWriter")
    print("中间的策略、降级链、统一骨架与 metadata 全部复用。")
    return 0


def main() -> int:
    args = build_arg_parser().parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
