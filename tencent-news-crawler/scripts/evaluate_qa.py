"""运行固定新闻问答评测集。

只调用 FastGPT 应用进行问答，不会修改知识库。自动规则适合回归测试，
综合题的表达质量仍需人工复核。
"""

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx

from config import settings


DEFAULT_QUESTIONS = Path("tests/evaluation_questions.json")
DEFAULT_OUTPUT = Path("data/evaluation_results.json")
REFUSAL_MARKERS = (
    "没有足够",
    "未找到",
    "无法回答",
    "无法确定",
    "无法确认",
    "无法给出",
    "无法根据",
    "没有相关",
    "暂无相关",
    "知识库中没有",
)


def load_questions(path: str | Path) -> list[dict]:
    """读取并做最低限度的题集格式校验。"""
    with Path(path).open(encoding="utf-8") as file:
        questions = json.load(file)

    if not isinstance(questions, list) or not questions:
        raise ValueError("评测集必须是非空 JSON 数组。")

    required = {"id", "category", "question", "expected_keywords", "should_refuse"}
    ids = set()
    for index, item in enumerate(questions, start=1):
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError(f"第 {index} 道题缺少必要字段。")
        if item["id"] in ids:
            raise ValueError(f"评测题 id 重复：{item['id']}")
        ids.add(item["id"])
    return questions


def ask_fastgpt(question: str) -> str:
    """调用 FastGPT v1 非流式应用对话接口。"""
    api_key = settings.fastgpt_app_api_key or settings.fastgpt_api_key
    if not api_key:
        raise ValueError(
            "请先在 .env 配置 FASTGPT_API_KEY（或 FASTGPT_APP_API_KEY）。"
        )
    if not settings.fastgpt_app_id:
        raise ValueError("请先在 .env 配置 FASTGPT_APP_ID。")

    response = httpx.post(
        settings.fastgpt_base_url.rstrip("/") + "/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "stream": False,
            "detail": False,
            "appId": settings.fastgpt_app_id,
            "messages": [{"role": "user", "content": question}],
        },
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
        raise RuntimeError(body.get("message") or "FastGPT 应用调用失败。")

    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("FastGPT 回答格式不符合预期。") from exc

    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    if not isinstance(content, str):
        raise RuntimeError("FastGPT 回答不是文本。")
    return content.strip()


def count_answer_chars(answer: str) -> int:
    """统计回答正文字符；来源 URL 不计入用户要求的摘要字数。"""
    without_urls = re.sub(r"https?://\S+", "", answer)
    return len(re.sub(r"[\s*_`#>-]", "", without_urls))


def score_answer(item: dict, answer: str) -> dict:
    """对单个回答进行确定性检查，不调用模型充当裁判。"""
    expected_keywords = item.get("expected_keywords") or []
    expected_urls = item.get("expected_urls") or []
    matched_keywords = [word for word in expected_keywords if word.lower() in answer.lower()]
    matched_urls = [url for url in expected_urls if url in answer]
    refused = any(marker in answer for marker in REFUSAL_MARKERS)
    should_refuse = bool(item["should_refuse"])
    max_chars = item.get("max_answer_chars")
    answer_chars = count_answer_chars(answer)

    keyword_pass = (
        len(matched_keywords) == len(expected_keywords)
        if expected_keywords
        else None
    )
    citation_pass = bool(matched_urls) if expected_urls else None
    refusal_pass = refused == should_refuse
    length_pass = answer_chars <= max_chars if max_chars is not None else None

    applicable = [
        result
        for result in (keyword_pass, citation_pass, refusal_pass, length_pass)
        if result is not None
    ]
    return {
        "passed": all(applicable),
        "keyword_pass": keyword_pass,
        "matched_keywords": matched_keywords,
        "citation_pass": citation_pass,
        "matched_urls": matched_urls,
        "refusal_pass": refusal_pass,
        "detected_refusal": refused,
        "length_pass": length_pass,
        "answer_chars": answer_chars,
    }


def evaluate_questions(
    questions: list[dict],
    ask: Callable[[str], str] = ask_fastgpt,
) -> dict:
    results = []
    for item in questions:
        try:
            answer = ask(item["question"])
            score = score_answer(item, answer)
            results.append({**item, "answer": answer, "error": None, **score})
        except Exception as exc:
            results.append({
                **item,
                "answer": "",
                "error": str(exc),
                "passed": False,
            })

    passed = sum(result["passed"] for result in results)
    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": round(passed / len(results), 4) if results else 0,
        },
        "items": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="评测 FastGPT 新闻问答应用")
    parser.add_argument("--questions", default=str(DEFAULT_QUESTIONS))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--limit", type=int, help="只运行前 N 道题，便于试跑")
    args = parser.parse_args()

    questions = load_questions(args.questions)
    if args.limit is not None:
        if args.limit < 1:
            parser.error("--limit 必须大于 0")
        questions = questions[:args.limit]

    report = evaluate_questions(questions)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"详细结果：{output_path}")
    if report["summary"]["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
