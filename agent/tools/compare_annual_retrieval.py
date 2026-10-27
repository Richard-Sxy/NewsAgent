"""Prepare, serve and score a frozen RRF/global fusion comparison."""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent"))

from app.modules.retrieval_lab.comparison import (
    EXPERIMENT_VERSION, REVIEW_VERSION, ComparisonReviewService, SupplementalStore,
    calculate_comparison, digest, exact_news_candidates, known_labels, load_review,
    predictions, rank_pair, read_legacy_ranking, read_supplemental_labels, sha_file, supplemental_runs,
)

DEFAULT_REVIEW = ROOT / "output/news-events-annual-20261027/human-review-v1"
DEFAULT_OUT = ROOT / "output/news-events-annual-20261027/comparison-v1"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def resolve_review_path(comp, override=None):
    """Resolve this machine's review location without changing frozen identity."""
    expected = comp["config"]["review_export_sha256"]
    if override is not None:
        candidates = [Path(override).resolve()]
    else:
        candidates = [DEFAULT_REVIEW, Path(comp["config"]["review_dir"])]
    for candidate in candidates:
        export = candidate / "human-review.export.json"
        if export.is_file() and sha_file(export) == expected:
            return candidate.resolve()
    raise ValueError("Matching frozen review not found; use score --review-dir PATH. "
                     "The export SHA-256 must match the frozen experiment.")


def score(directory, review_dir=None):
    comp = json.loads((directory / "comparison.json").read_text())
    if digest(comp["config"]) != comp["config_sha256"]:
        raise ValueError("comparison configuration drift")
    resolved_review = resolve_review_path(comp, review_dir)
    review = load_review(resolved_review)
    labels = known_labels(review)
    extra = read_supplemental_labels(directory / "supplemental.sqlite3", comp)
    if set(labels) & set(extra):
        raise ValueError("supplemental labels must not overwrite frozen judgments")
    labels.update(extra)
    for q in comp["queries"]:
        for ps in q["predictions"].values():
            for p in ps:
                label = labels.get((q["query_id"], p["news_id"]))
                if label and label["body_sha256"] != p["body_sha256"]:
                    raise ValueError("label evidence hash changed")
    report = calculate_comparison(comp, labels)
    report["input_review"] = {"directory": str(resolved_review),
                            "export_sha256": sha_file(resolved_review / "human-review.export.json")}
    write_json(directory / "results.json", report)
    lines = ["# 旧 RRF 与全局相似度排序：冻结对照", "",
        "本轮是同候选预算的融合排序消融。实际调用 LifecycleKnowledgeSearch 的两个分支；数据源是冻结的 NumPy 精确检索和不可变新闻门禁替身。",
        "不包含生产 PostgreSQL/Milvus 并发门禁、ANN 性能、语义替代或主体事件业务重排。", "",
        f"- 查询：{report['queries']}；共同 Top10 候选池：{report['required_pool_pairs']} 项。",
        f"- 当前缺少明确人工判定：{report['missing_pairs']} 项。",
        "- 原 300 项单人标注保留；补标独立保存，未判定和无法判断都不计为负例。",
        "- 旧策略读取本地示例配置的 RRF k 与层权重；三层候选按新闻去重并统一取每层20篇，只比较融合规则。",
        "- 本批查询已看过结果，作为开发对照；未标成独立 test，不报告企业 P95 或完整 Recall。", ""]
    if report["metrics"] is None:
        lines += ["**对照总体指标暂缺：共同池尚有未标候选，不能把它们当作0分，也不能丢掉未完整查询。**", "",
                  "打开 http://127.0.0.1:8879/ 完成新增候选盲标，再执行 score 子命令。"]
    else:
        lines += ["| 策略 | Precision@5 | 共同池 nDCG@5 | Precision@10 | 共同池 nDCG@10 |",
                  "| --- | ---: | ---: | ---: | ---: |"]
        for name, ms in report["metrics"].items():
            n5, n10 = ms["5"]["pooled_ndcg"], ms["10"]["pooled_ndcg"]
            lines.append(f"| {name} | {ms['5']['precision']:.2%} | {n5 if n5 is not None else 'N/A'} | {ms['10']['precision']:.2%} | {n10 if n10 is not None else 'N/A'} |")
    lines += ["", "## 调用链与复现", "",
        "prepare：冻结 review 导出和 manifest → 核对语料／模型／向量身份 → OfflineBgeProcess 编码同一查询 → exact_news_candidates 做精确检索和新闻去重 → rank_pair 调用实际 LifecycleKnowledgeSearch 的旧 RRF、新 global_similarity 分支 → 校验新策略 Top10 与原人工任务逐项一致 → 冻结 predictions 和查询向量。",
        "supplemental_runs：合并两组 Top10，按 query_id/news_id/body_sha256 复用原人工标签；新增对打乱顺序后写入独立 supplemental.sqlite3。模型不产生相关性标签。",
        "serve：ComparisonReviewService → 既有 create_app/static UI → PUT save_judgment 事务保存、修订号校验和审计；原8877标注库不写入。",
        "score：冻结原标签 + 补标库 → calculate_comparison → 固定30查询共同候选池 exp2 nDCG@5/@10 和严格Precision → results.json/report.md。任一共同池标签缺失时总体指标保持null。",
        "配置：--review-dir/--package-dir/--model-dir/--embedding-python/--legacy-config/--device/--output-dir；实际参数及源码哈希在 config.json，查询向量和源码身份见 evidence-manifest.json。",
        "入口：agent/tools/compare_annual_retrieval.py prepare|serve|score；serve仅回环8879，补标不需要GPU或模型服务。",
        "测试：agent/tests/test_retrieval_comparison.py；覆盖实际RRF分支、共同分母、缺标阻断、证据漂移、盲标隐藏与API持久化。",
        "TODO：完成新增人工标签后重新score；独立事件组与双审、源新闻业务任务、企业索引性能以及语义替代均需后续实验。",
    ]
    (directory / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "queries", "required_pool_pairs", "missing_pairs", "metrics")}, ensure_ascii=False))
    return report


async def prepare(args):
    import numpy as np
    from app.modules.retrieval_lab.engine import RetrievalLabEngine, _checked_vectors
    from app.retrieval.bge_snapshot import BGE_QUERY_INSTRUCTION

    review = load_review(args.review_dir)
    legacy = read_legacy_ranking(args.legacy_config)
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / "query-vectors").mkdir()
    config = {
        "version": EXPERIMENT_VERSION, "created_at": datetime.now(timezone.utc).isoformat(),
        "review_dir": str(args.review_dir.resolve()), "review_export_sha256": sha_file(args.review_dir / "human-review.export.json"),
        "package_dir": str(args.package_dir.resolve()), "legacy_config": str(args.legacy_config.resolve()),
        "legacy_config_sha256": sha_file(args.legacy_config),
        "rrf_k": legacy["rrf_k"], "weights": legacy["weights"],
        "candidate_limit_per_tier": 20, "candidate_unit": "unique_news",
        "intent": "general", "limit": 10, "score_ks": [5, 10],
        "semantic_retirement": False, "business_reranker": False,
        "independent_test": False, "backend": "numpy_exact",
        "source_code": {str(p.relative_to(ROOT)): sha_file(p) for p in [
            ROOT / "agent/app/modules/retrieval_lab/comparison.py",
            ROOT / "agent/tools/compare_annual_retrieval.py",
            ROOT / "agent/app/modules/knowledge/tier_lifecycle_query.py",
            ROOT / "agent/app/retrieval/query_policy.py",
            ROOT / "agent/app/modules/retrieval_lab/engine.py",
        ]},
    }
    write_json(out / "config.json", config)  # Written before predictions, no parameter search.
    engine = RetrievalLabEngine(args.package_dir, args.model_dir, device=args.device,
                                embedding_python=args.embedding_python)
    engine.encoder.log_path = out / "encoder.log"
    try:
        if not (args.package_dir / "vector-index/manifest.json").is_file():
            raise ValueError("comparison requires an existing frozen real vector index")
        engine.build()
        if engine.manifest != review["manifest"]:
            raise ValueError("frozen review and corpus manifest differ")
        expected = review["runs"][0]["versions"]
        if engine.versions != expected or not engine.semantic_quality_ready:
            raise ValueError("real model/corpus/strategy identity mismatch")
        comp = {"schema": "annual-fusion-comparison-v1", "config": config, "config_sha256": digest(config),
            "versions": expected, "as_of_date": engine.as_of_date.isoformat(), "queries": []}
        for run in review["runs"]:
            if run["versions"] != expected or run["trace"]["retrieval_plan"]["intent"] != "general":
                raise ValueError("mixed identities or unsupported source query intent")
            vectors, tokens = engine.encoder.encode([BGE_QUERY_INSTRUCTION + run["query"].strip()])
            vector = _checked_vectors(vectors, 1)[0]
            np.save(out / "query-vectors" / (run["query_id"] + ".npy"), vector, allow_pickle=False)
            hits = exact_news_candidates(engine, vector)
            pair = await rank_pair(hits=hits, vector=vector, embedding_version=engine.model_version,
                query=run["query"], category=run["category"], policy=engine.policy,
                candidate_limit=20, rrf_k=config["rrf_k"], weights=config["weights"])
            preds = {name: predictions(result) for name, result in pair.items()}
            if [p["news_id"] for p in preds["global_similarity"]] != [c["news_id"] for c in run["candidates"]]:
                raise ValueError("global Top10 did not reproduce frozen human task: " + run["query_id"])
            for values in preds.values():
                for prediction in values:
                    prediction["body_sha256"] = engine.by_id[prediction["news_id"]]["body_sha256"]
            comp["queries"].append({"query_id": run["query_id"], "query": run["query"],
                "category": run["category"], "predictions": preds,
                "traces": {k: v["trace"] for k, v in pair.items()},
                "query_tokens": tokens[0], "global_reproduced": True})
            print(json.dumps({"query_id": run["query_id"], "global_reproduced": True}), flush=True)
        write_json(out / "comparison.json", comp)
        labels = known_labels(review)
        tasks, pending = supplemental_runs(comp, engine.by_id, labels)
        (out / "pending-pairs.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in pending), encoding="utf-8")
        store = SupplementalStore(out / "supplemental.sqlite3")
        store.set_manifest({**engine.manifest, "comparison_sha256": sha_file(out / "comparison.json"),
            "review_strategy": REVIEW_VERSION, "review_scope": "additional_single_human_blind_judgments"})
        for task in tasks:
            store.put_run(task)
        evidence = {"config_sha256": digest(config), "global_reproduction_queries": len(comp["queries"]),
            "source_index_manifest_sha256": sha_file(args.package_dir / "vector-index/manifest.json"),
            "comparison_sha256": sha_file(out / "comparison.json"),
            "pending_pairs": len(pending), "pending_queries": len(tasks),
            "query_vectors": {p.name: sha_file(p) for p in sorted((out / "query-vectors").glob("*.npy"))}}
        write_json(out / "evidence-manifest.json", evidence)
    finally:
        engine.close()
    score(out)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    prep.add_argument("--review-dir", type=Path, default=DEFAULT_REVIEW)
    prep.add_argument("--package-dir", type=Path, default=ROOT / "var/news-events-annual-20261027/retrieval-lab-v1")
    prep.add_argument("--model-dir", type=Path, default=ROOT / "var/knowledge-snapshots/rag-recall-demo/data/models/bge-large-zh-v1.5")
    prep.add_argument("--embedding-python", type=Path, default=ROOT / "var/news-events-annual-20261027/runtime/embedding-venv/bin/python")
    prep.add_argument("--legacy-config", type=Path, default=ROOT / "deploy/knowledge-lifecycle.local.example.yml")
    prep.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    prep.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    serving = sub.add_parser("serve")
    serving.add_argument("--directory", type=Path, default=DEFAULT_OUT)
    serving.add_argument("--port", type=int, default=8879)
    scoring = sub.add_parser("score")
    scoring.add_argument("--directory", type=Path, default=DEFAULT_OUT)
    scoring.add_argument("--review-dir", type=Path, help="relocated frozen review with identical export SHA-256")
    args = p.parse_args()
    if args.command == "prepare":
        asyncio.run(prepare(args))
    elif args.command == "score":
        score(args.directory, args.review_dir)
    else:
        if not 1024 <= args.port <= 65535:
            p.error("port must be in 1024..65535")
        import uvicorn
        from app.modules.retrieval_lab.api import create_app
        uvicorn.run(create_app(ComparisonReviewService(args.directory)), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
