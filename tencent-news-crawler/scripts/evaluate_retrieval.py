"""评测 FastGPT 新闻知识库的召回效果。"""

import argparse
import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import httpx

from config import settings


DEFAULT_QUESTIONS = Path("tests/retrieval_questions.json")
DEFAULT_OUTPUT = Path("data/retrieval_evaluation_results.json")
VALID_SEARCH_MODES = ("embedding", "fullTextRecall", "mixedRecall")


def load_questions(path: str | Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as file:
        questions = json.load(file)

    if not isinstance(questions, list) or not questions:
        raise ValueError("召回测评集必须是非空 JSON 数组。")

    required = {"id", "question", "expected_collection_ids"}
    ids: set[str] = set()
    for index, item in enumerate(questions, start=1):
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(f"第 {index} 道题缺少必要字段。")
        if not isinstance(item["question"], str) or not item["question"].strip():
            raise ValueError(f"第 {index} 道题的问题不能为空。")
        expected = item["expected_collection_ids"]
        if not isinstance(expected, list) or not expected:
            raise ValueError(f"第 {index} 道题缺少期望 collection id。")
        if item["id"] in ids:
            raise ValueError(f"测评题 id 重复：{item['id']}")
        ids.add(item["id"])
    return questions


def search_fastgpt(
    question: str,
    *,
    search_mode: str,
    token_limit: int,
    similarity: float | None,
    using_rerank: bool,
    using_query_rewrite: bool = False,
    query_rewrite_model: str | None = None,
) -> list[dict]:
    """调用知识库搜索测试接口并返回按相关性排序的切片。"""
    if not settings.fastgpt_api_key:
        raise ValueError("请先配置 FASTGPT_API_KEY。")
    if not settings.fastgpt_dataset_id:
        raise ValueError("请先配置 FASTGPT_DATASET_ID。")

    payload: dict = {
        "datasetId": settings.fastgpt_dataset_id,
        "text": question,
        "queryImageUrls": [],
        "limit": token_limit,
        "searchMode": search_mode,
        "usingReRank": using_rerank,
        "datasetSearchUsingQueryRewrite": using_query_rewrite,
    }
    if similarity is not None:
        payload["similarity"] = similarity
    if query_rewrite_model:
        payload["datasetSearchQueryRewriteModel"] = query_rewrite_model

    response = httpx.post(
        settings.fastgpt_base_url.rstrip("/")
        + "/api/core/dataset/searchTest",
        headers={
            "Authorization": f"Bearer {settings.fastgpt_api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=settings.fastgpt_timeout,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"FastGPT HTTP {response.status_code}: {response.text[:1000]}"
        ) from exc

    body = response.json()
    if body.get("code") not in (None, 200):
        raise RuntimeError(body.get("message") or "FastGPT 搜索测试失败。")
    data = body.get("data", body)
    results = data.get("list") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise RuntimeError("FastGPT 搜索响应缺少 list 数组。")
    return results


def rank_unique_collections(results: list[dict]) -> tuple[list[str], list[dict]]:
    """切片级结果转为新闻 collection 级排名，保留每篇新闻的首个切片。"""
    collection_ids: list[str] = []
    summaries: list[dict] = []
    seen: set[str] = set()
    for result in results:
        collection_id = str(result.get("collectionId", "")).strip()
        if not collection_id or collection_id in seen:
            continue
        seen.add(collection_id)
        collection_ids.append(collection_id)
        summaries.append(
            {
                "rank": len(collection_ids),
                "collection_id": collection_id,
                "data_id": str(result.get("id", "")),
                "source_name": result.get("sourceName", ""),
                "chunk_index": result.get("chunkIndex"),
                "text": str(result.get("q", ""))[:300],
                "scores": result.get("score", []),
            }
        )
    return collection_ids, summaries


def score_result(expected_ids: list[str], ranked_ids: list[str], ks: tuple[int, ...]) -> dict:
    expected = {str(item) for item in expected_ids}
    first_rank = next(
        (index for index, item in enumerate(ranked_ids, start=1) if item in expected),
        None,
    )
    return {
        "first_relevant_rank": first_rank,
        "reciprocal_rank": round(1 / first_rank, 6) if first_rank else 0.0,
        "hits": {f"recall_at_{k}": bool(expected.intersection(ranked_ids[:k])) for k in ks},
    }


def evaluate_retrieval(
    questions: list[dict],
    search: Callable[[str], list[dict]],
    ks: tuple[int, ...] = (1, 3, 5),
    request_interval: float = 0,
) -> dict:
    results: list[dict] = []
    for index, item in enumerate(questions):
        try:
            raw_results = search(item["question"])
            ranked_ids, result_summaries = rank_unique_collections(raw_results)
            score = score_result(item["expected_collection_ids"], ranked_ids, ks)
            results.append(
                {
                    **item,
                    **score,
                    "returned_collection_count": len(ranked_ids),
                    "retrieved": result_summaries,
                    "error": None,
                }
            )
        except Exception as exc:
            results.append(
                {
                    **item,
                    "first_relevant_rank": None,
                    "reciprocal_rank": 0.0,
                    "hits": {f"recall_at_{k}": False for k in ks},
                    "returned_collection_count": 0,
                    "retrieved": [],
                    "error": str(exc),
                }
            )
        if request_interval > 0 and index < len(questions) - 1:
            time.sleep(request_interval)

    total = len(results)
    errors = sum(bool(item["error"]) for item in results)
    metrics = {
        f"recall_at_{k}": round(
            sum(item["hits"][f"recall_at_{k}"] for item in results) / total,
            4,
        )
        for k in ks
    }
    metrics["mrr"] = round(
        sum(item["reciprocal_rank"] for item in results) / total,
        4,
    )
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "summary": {
            "total": total,
            "completed": total - errors,
            "errors": errors,
            **metrics,
        },
        "items": results,
    }


def parse_ks(value: str) -> tuple[int, ...]:
    try:
        ks = tuple(sorted({int(item.strip()) for item in value.split(",")}))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--ks 应为逗号分隔的正整数") from exc
    if not ks or any(item < 1 for item in ks):
        raise argparse.ArgumentTypeError("--ks 应为逗号分隔的正整数")
    return ks

# 这边是
def main() -> None:
    parser = argparse.ArgumentParser(description="评测 FastGPT 新闻知识库召回")
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, help="只运行前 N 道题")
    parser.add_argument(
        "--question-ids",
        help="只运行指定题目 ID，多个 ID 使用逗号分隔",
    )
    parser.add_argument("--ks", type=parse_ks, default=(1, 3, 5))
    parser.add_argument("--search-mode", choices=VALID_SEARCH_MODES, default="mixedRecall")
    parser.add_argument("--token-limit", type=int, default=5000)
    parser.add_argument("--similarity", type=float)
    parser.add_argument("--using-rerank", action="store_true")
    parser.add_argument("--using-query-rewrite", action="store_true")
    parser.add_argument("--query-rewrite-model")
    parser.add_argument("--request-interval", type=float, default=0.2)
    args = parser.parse_args()

    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.token_limit < 1:
        parser.error("--token-limit 必须大于 0")
    if args.request_interval < 0:
        parser.error("--request-interval 不能小于 0")

    questions = load_questions(args.questions)
    if args.question_ids:
        selected_ids = {
            item.strip() for item in args.question_ids.split(",") if item.strip()
        }
        questions = [item for item in questions if item["id"] in selected_ids]
        if not questions:
            parser.error("--question-ids 没有匹配到任何题目")
    if args.limit is not None:
        questions = questions[: args.limit]

    def search(question: str) -> list[dict]:
        return search_fastgpt(
            question,
            search_mode=args.search_mode,
            token_limit=args.token_limit,
            similarity=args.similarity,
            using_rerank=args.using_rerank,
            using_query_rewrite=args.using_query_rewrite,
            query_rewrite_model=args.query_rewrite_model,
        )

    report = evaluate_retrieval(
        questions,
        search=search,
        ks=args.ks,
        request_interval=args.request_interval,
    )
    report["config"] = {
        "dataset_id": settings.fastgpt_dataset_id,
        "search_mode": args.search_mode,
        "token_limit": args.token_limit,
        "similarity": args.similarity,
        "using_rerank": args.using_rerank,
        "using_query_rewrite": args.using_query_rewrite,
        "query_rewrite_model": args.query_rewrite_model,
        "ks": list(args.ks),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"详细结果：{output}")
    if report["summary"]["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
