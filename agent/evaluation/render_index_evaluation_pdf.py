"""Render one Chinese page from pinned index/ANN/token coverage evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, GRAY, white, HexColor,
)


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def render(evidence: Path, output: Path) -> None:
    ann = load(evidence / "ann-report.json")
    token = load(evidence / "tokenization-audit.json")
    source = load(evidence / "source-index-verification.json")
    annotation = load(evidence / "annotation/manifest.json")
    if (ann["status"] != "completed" or source["status"] != "completed"
            or ann["package_manifest_sha256"] != token["package_manifest_sha256"]
            or ann["package_manifest_sha256"] != source["package_manifest_sha256"]
            or ann["package_manifest_sha256"] != annotation["index_manifest_sha256"]
            or annotation["status"] != "pending_human_annotation"):
        raise ValueError("diagram requires matching completed index and evaluation evidence")
    build = load(Path(ann["package"]) / "build_report.json")
    if build["package_manifest_sha256"] != ann["package_manifest_sha256"]:
        raise ValueError("build identity differs from evaluated index")
    coverage = token["new_article_body_coverage"]
    truncation = token["scope"]["new_in_this_package"]
    row = next(row for row in ann["summary"] if row["requested_ef"] == 256 and row["k"] == 20)
    accuracy = row["strata"]["all_balanced"]["article_ann_at_k"]
    latency = row["strata"]["all_balanced"]["latency"]
    output.parent.mkdir(parents=True, exist_ok=True)
    d = Diagram(output)
    d.c.setTitle("NewsAgent 索引构建、参数来源与严格评测")
    d.c.setSubject("v2快照；旧行保留+新向量追加后重建HNSW；编码覆盖与ANN实测")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / INDEX BUILD & EVALUATION / 2026-10-15", 32, 22, 10, GREEN, True)
    d.text("索引怎样建成，参数怎样得到，效果怎样验收", 32, 44, 27, INK, True)
    d.text(f"v2：{build['total_articles']:,} 篇 / {build['total_chunks']:,} 切片 · 固定包摘要 {ann['package_manifest_sha256'][:16]}… · 本轮未改索引与部署", 32, 88, 12.5, MUTED)
    d.box(32, 119, 1125, 30, PALE_AMBER)
    d.text("结论：图近似精度在本次工程样本中很高；800 字切片与 512 token 窗口存在覆盖缺口；人工语义质量尚待标注。", 45, 127, 11.2, AMBER)

    d.text("1  构建链：只追加新向量，合并后重建整张图", 32, 164, 16, GREEN, True)
    d.box(32, 198, 235, 66, PALE_GREEN, GREEN)
    d.text("已有 v1 快照", 46, 209, 14.5, GREEN, True)
    d.para(f"{build['base_articles']:,} 篇 / {build['base_chunks']:,} 片\n旧 JSONL 行与向量原样保留", 46, 234, 207, 10.8, 33)
    d.arrow(267, 230, 560, 230, GREEN)
    d.text("不重切、不重算旧向量", 309, 209, 10.8, MUTED)

    d.box(32, 283, 235, 86, PALE_BLUE, BLUE)
    d.text("8,000 篇冻结原文", 46, 295, 14.5, BLUE, True)
    d.para("校验文件 / 正文 / 来源 / 时间\n541 个已有 ID 跳过\n新增 7,459 篇 / 12,226 片", 46, 322, 209, 10.8, 49)
    d.arrow(267, 326, 286, 326, BLUE)
    d.box(295, 283, 248, 86, PALE_BLUE, BLUE)
    d.text("确定性切片与输入头", 309, 295, 14.5, BLUE, True)
    d.para("正文 800 字 / 重叠 80 字\n每片重复标题 + URL + 时间\n实际输入仍受 512 token 限制", 309, 322, 221, 10.8, 49)
    d.arrow(543, 326, 562, 326, BLUE)
    d.box(571, 283, 248, 86, PALE_BLUE, BLUE)
    d.text("本地 BGE 文档编码", 585, 295, 14.5, BLUE, True)
    d.para("MPS / FP16 / batch 8 / CLS\n无 query 前缀，输出 1024 维\n转 FP32 + L2，批次散回原行序", 585, 322, 221, 10.8, 49)
    d.arrow(695, 283, 695, 268, BLUE)
    d.box(571, 198, 248, 66, PALE_GREEN, GREEN)
    d.text("按行号合并向量矩阵", 585, 209, 14.5, GREEN, True)
    d.para("50,252 × 1024 float32\nlabel = JSONL 行号 0…50,251", 585, 234, 222, 10.8, 34)
    d.arrow(819, 230, 841, 230, GREEN)

    d.box(850, 198, 307, 171, white, GREEN)
    d.text("HNSW 建图（层级示意）", 864, 209, 14.5, GREEN, True)
    for y, xs in ((250, (904, 1058)), (282, (880, 940, 1000, 1060, 1120))):
        for left, right in zip(xs, xs[1:]):
            d.c.setStrokeColor(GRAY)
            d.c.line(left, H-y, right, H-y)
        for x in xs:
            d.c.setFillColor(PALE_GREEN)
            d.c.setStrokeColor(GREEN)
            d.c.circle(x, H-y, 5, fill=1, stroke=1)
    d.arrow(904, 255, 940, 274, GREEN)
    d.arrow(1058, 255, 1060, 274, GREEN)
    d.para("IP / M=32 / ef_construction=200\nseed=100 / threads=4\n在全部合并向量上重新插入、连边", 864, 303, 278, 10.9, 51)
    d.box(32, 386, 1125, 29, PALE_GREEN)
    d.text("chunks.jsonl ↔ embeddings.npy ↔ index.bin（同行身份） → SHA256SUMS / 回执 → 原子 rename 新包 → API / Worker 显式切换", 45, 394, 10.8, GREEN)

    d.text("2  参数来源：模型约束、工程固定值、资源选择、查询配置", 32, 435, 16, BLUE, True)
    for x, title, body in (
        (32, "模型决定", "1024 维 / CLS / 最大 512 token\n512 包含特殊 token；头部也占预算\n不可只把序列长度调到 800/1024"),
        (318, "构建器固定", "新片 800/80；M32；efc200；seed100\n容量由总行数得到：50,252\n当前未证明这些值是质量最优"),
        (604, "本机资源选择", "MPS / FP16 / batch8 / threads4\n为控制内存；落盘仍 FP32\n小数值探针不能替代排序质量评测"),
        (890, "查询 YAML", "ef256 / chunk_pool200\n实际 ef=max(配置ef,当前pool)\n池不足翻倍；ef64/128会被抬至200"),
    ):
        d.box(x, 470, 267, 96, PALE_BLUE, BLUE)
        d.text(title, x+13, 481, 13.7, BLUE, True)
        d.para(body, x+13, 507, 243, 10.6, 51)

    d.text("3  严格评测：工程结果已测，语义验收不能跳过人工标签", 32, 586, 16, INK, True)
    d.box(32, 620, 354, 144, PALE_AMBER, AMBER)
    d.text("编码输入覆盖：已发现缺口", 47, 632, 15.5, AMBER, True)
    d.para(f"新增片段 {truncation['chunks_exceeding_window_fraction']:.2%} 超过 512 token\n{coverage['articles_with_uncovered_body_intervals']:,} / {coverage['articles']:,} 篇存在正文区间缺口\n扣除切片重叠后，字符覆盖 {coverage['body_character_coverage_fraction']:.2%}\n未覆盖 {coverage['uncovered_body_characters']:,} 字符；可逐篇追溯\n这是输入区间覆盖，不是语义损失比例", 47, 663, 326, 10.9, 88)
    d.box(400, 620, 367, 144, PALE_GREEN, GREEN)
    d.text("HNSW 对精确 IP：工程探针", 415, 632, 15.5, GREEN, True)
    lower, upper = accuracy["ci95"]
    d.para(f"旧/新各48个源向量；排除源新闻所有片\n文章 ANN@20 = {accuracy['mean']:.4%}（ef256）\n条件 bootstrap 95%：{lower:.4%}…{upper:.0%}\np50 {latency['p50_ms']:.2f}ms / p95 {latency['p95_ms']:.2f}ms\n无编码/HTTP/并发；不等于用户问句质量", 415, 663, 337, 10.9, 88)
    d.box(781, 620, 376, 144, white, BLUE)
    d.text("新闻相关性：等待双人盲标", 796, 632, 15.5, BLUE, True)
    d.para(f"{annotation['query_count']} 个草稿问题 / {annotation['annotation_pairs']} 个待判断候选\n0…3级：无关 / 背景 / 有用 / 核心相关\n事件组隔离 dev / validation / test\n池内 Recall / nDCG、P@3、无答案误接收\n未判 TopK / 草稿 / 缺预测 → 阻断评分", 796, 663, 346, 10.9, 88)
    d.text("待办：人工试标校准 → token预算切片候选 → FP16/FP32与图参数对照 → validation选参 → 冻结test → 人工审核发布", 32, 783, 10.8, MUTED)
    d.text("数据：source-index-verification / tokenization-audit / ann-report；协议：agent/evaluation/news_retrieval_protocol.v1.json", 32, 807, 9.7, MUTED)
    d.c.showPage()
    d.c.save()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render(args.evidence.resolve(), args.output.resolve())
