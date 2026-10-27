"""Freeze the completed single-human annual retrieval review and score its existing pool."""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "agent"))
from app.modules.retrieval_lab.store import RetrievalLabStore
from app.modules.retrieval_lab.metrics import calculate_metrics

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def mean(values):
    return statistics.mean(values) if values else None

def aggregate(rows):
    result = {"query_count": len(rows)}
    for k in (5, 10):
        metrics = [r["metrics"][str(k)] for r in rows]
        precision = [m["precision_at_k"] for m in metrics if m["precision_at_k"] is not None]
        ndcg = [m["pool_ndcg_at_k"] for m in metrics if m["pool_ndcg_at_k"] is not None]
        result[str(k)] = {
            "precision_mean": mean(precision), "precision_eligible_queries": len(precision),
            "pool_ndcg_mean": mean(ndcg), "ndcg_eligible_queries": len(ndcg),
            "ndcg_unavailable_reasons": dict(Counter(m["pool_ndcg_reason"] for m in metrics if m["pool_ndcg_reason"])),
        }
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "var/news-events-annual-20261027/retrieval-lab-v1/judgments.sqlite3")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    if any(p.name != Path(__file__).name for p in out.iterdir()):
        raise SystemExit("Output contains existing artifacts; use a new empty directory.")
    source = args.source.resolve()
    backup = out / "judgments.snapshot.sqlite3"
    with sqlite3.connect("file:" + str(source) + "?mode=ro", uri=True) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Source database integrity failed.")
        with sqlite3.connect(backup) as destination:
            connection.backup(destination)
    store = RetrievalLabStore(backup)
    export = store.export_data()
    runs = export["runs"]
    if not runs:
        raise ValueError("No frozen runs.")
    versions = {json.dumps(r["versions"], sort_keys=True) for r in runs}
    if len(versions) != 1:
        raise ValueError("Mixed corpus/model/strategy versions.")
    rows, judgments = [], []
    for run in runs:
        if len(run["candidates"]) != 10:
            raise ValueError("Expected ten candidates per frozen query.")
        if not all(n["judgment"]["status"] == "judged" for n in run["candidates"]):
            raise ValueError("Missing or unsure human judgments; do not score as negatives.")
        rows.append({
            "query_id": run["query_id"], "run_id": run["run_id"], "query": run["query"],
            "category": run.get("category"),
            "metrics": {str(k): calculate_metrics(run["candidates"], k) for k in (5, 10)},
        })
        for n in run["candidates"]:
            judgments.append({
                "schema": "retrieval-lab-single-human-qrel-v1", "query_id": run["query_id"],
                "query": run["query"], "run_id": run["run_id"], "news_id": n["news_id"],
                "title": n["title"], "body_sha256": n.get("body_sha256"),
                "original_rank": n["rank"], "versions": run["versions"],
                "judgment": n["judgment"], "review_scope": "single_human_review_not_promoted_gold",
            })
    counts = dict(Counter(j["judgment"]["label"] for j in judgments))
    summary = {
        "schema": "annual-retrieval-human-review-report-v1",
        "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(),
        "scope": "single_human_review_of_one_strategy_top10_pool",
        "task": "question_search", "formal_independent_test": False,
        "versions": runs[0]["versions"], "label_counts": counts,
        "judgment_count": len(judgments), "audit_count": len(export["judgment_audit"]),
        "overall": aggregate(rows),
        "by_category": {category: aggregate([r for r in rows if r["category"] == category]) for category in sorted({r["category"] for r in rows})},
        "recall": None, "before_after_improvement": None, "production_p95": None,
        "limitations": [
            "Only this strategy's ten returned documents per query were judged.",
            "Pool nDCG ideal order is restricted to these ten documents; it does not measure missed relevant news.",
            "Queries are discovery-based question drafts, not a source-news related-report evaluation.",
            "One human reviewer; no independent double review, adjudication, or event-group split.",
            "No historical-RRF baseline, multi-strategy pool, semantic-retirement experiment, or production performance test.",
        ],
    }
    dump(out / "human-review.export.json", export)
    dump(out / "metrics.json", summary)
    for name, values in (("per-query.jsonl", rows), ("judgments.single-human.jsonl", judgments)):
        (out / name).write_text("".join(json.dumps(v, ensure_ascii=False) + "\n" for v in values), encoding="utf-8")
    def pct(value):
        return "N/A" if value is None else f"{value * 100:.2f}%"
    lines = [
        "# 年度检索人工标注：第一版冻结报告", "",
        f"冻结时间：{summary['created_at']}", "",
        f"共 {len(runs)} 个问句查询、{len(judgments)} 项单人标注；相关 {counts.get('relevant',0)}、部分相关 {counts.get('partial',0)}、不相关 {counts.get('irrelevant',0)}。",
        "相关=2、部分相关=1、不相关=0；严格 Precision 只计相关；nDCG 增益为 2^grade-1。", "",
        "| 指标 | 结果 | 有效查询数 |", "| --- | ---: | ---: |",
    ]
    for k in (5, 10):
        m = summary["overall"][str(k)]
        lines += [
            f"| Precision@{k} | {pct(m['precision_mean'])} | {m['precision_eligible_queries']} |",
            f"| 当前 Top10 候选池 nDCG@{k} | {m['pool_ndcg_mean']:.6f} | {m['ndcg_eligible_queries']} |",
        ]
    lines += ["", "nDCG 接近 1 只说明相关候选在当前池中较靠前；不能解释为全库召回接近 100%。", "",
        "## 分板块结果", "", "| 板块 | 查询数 | Precision@5 | Precision@10 | 池内 nDCG@5 |", "| --- | ---: | ---: | ---: | ---: |"]
    for category, values in summary["by_category"].items():
        lines.append(f"| {category} | {values['query_count']} | {pct(values['5']['precision_mean'])} | {pct(values['10']['precision_mean'])} | {values['5']['pool_ndcg_mean']:.6f} |")
    lines += ["", "## 范围与后续", ""] + ["- " + value for value in [
        "当前为单策略、单人、问句检索评测，未晋升为正式独立测试金标。",
        "固定旧 RRF 与新全局相似度排序，运行相同语料和查询，合并候选后补充盲标；相同查询—新闻及证据版本可复用标签。",
        "事实覆盖替代应另设实验组；当前没有执行任何语义退役或向量替换。",
        "正式业务结论仍需源新闻关联检索任务、事件组隔离、独立复核；企业向量库 P95 需独立测量。",
    ]]
    lines += ["", "## 完整调用链与复现", "",
        "上游：原 judgments.sqlite3 → SQLite backup 一致性快照 → RetrievalLabStore.export_data 校验冻结运行哈希并导出正文、版本、标签和改标审计。",
        "转换与核心计算：calculate_metrics 分别计算 K=5/10；aggregate 对有效查询求算术均值并记录分母，再按板块汇总。",
        "依赖：既有应用 Python、项目 store/metrics 和 Python 标准库；冻结过程不请求模型或其他外部服务。",
        "下游：judgments.snapshot.sqlite3、human-review.export.json、judgments.single-human.jsonl、per-query.jsonl、metrics.json、report.md 以及 SHA256 清单。",
        "配置：--source 指定原标注库；--output 指定新的空输出目录；模型、语料、策略身份读取冻结运行，不切换工作台配置。",
        "复现入口：在 WSL 使用 var/news-events-annual-20261027/runtime/app-venv/bin/python -B 本脚本 --source 本目录/judgments.snapshot.sqlite3 --output 一个新的空目录。",
        "验证：另行读回导出与快照，核对逐标签、运行摘要、文件哈希及数据库完整性，并独立重算 Precision/nDCG；现有指标测试入口为 agent/tests/test_retrieval_lab_store.py。",
        "TODO：多策略候选池、补标、业务源新闻任务、独立复核、事件分组与性能测量；尚无可填写的前后提升数值。",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    with sqlite3.connect("file:" + str(backup) + "?mode=ro", uri=True) as frozen:
        assert frozen.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    manifest = {
        "schema": "annual-human-review-snapshot-manifest-v1", "created_at": summary["created_at"],
        "source_database": str(source), "versions": summary["versions"],
        "source_code": {str(p.relative_to(ROOT)): sha(p) for p in [
            ROOT / "agent/app/modules/retrieval_lab/store.py",
            ROOT / "agent/app/modules/retrieval_lab/metrics.py",
        ]},
        "files": {p.name: {"sha256": sha(p), "bytes": p.stat().st_size} for p in sorted(out.iterdir()) if p.is_file()},
    }
    dump(out / "manifest.json", manifest)
    print(json.dumps({"output": str(out), "counts": counts, "overall": summary["overall"]}, ensure_ascii=False))

if __name__ == "__main__":
    main()
