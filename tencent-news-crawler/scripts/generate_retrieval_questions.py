"""从已训练的 QA 数据生成新闻知识库召回测评题。

该脚本只读取 SQLite 与本机 FastGPT MongoDB，不调用大模型，也不修改知识库。
输出中的 expected_collection_ids 指向原新闻知识库的集合，可用于计算 Recall@K。
"""

import argparse
import json
import os
import random
import re
import sqlite3
import subprocess
from collections import defaultdict
from pathlib import Path

from config import settings


def load_article_mapping(db_path: str) -> dict[str, dict]:
    """按 QA collection id 建立 QA 与原新闻之间的映射。"""
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                qa.qa_collection_id,
                ingest.collection_id,
                ingest.url,
                ingest.title,
                COALESCE(ingest.topic, '') AS topic,
                ingest.publish_time
            FROM news_qa_records AS qa
            JOIN news_ingest_records AS ingest ON ingest.url = qa.url
            WHERE qa.status = 'submitted'
              AND ingest.status = 'success'
              AND qa.qa_collection_id IS NOT NULL
              AND ingest.collection_id IS NOT NULL
            """
        ).fetchall()
    return {row["qa_collection_id"]: dict(row) for row in rows}


def load_qa_data(dataset_id: str) -> list[dict]:
    """通过本机 MongoDB 容器只读获取 QA 数据。"""
    container = os.getenv("FASTGPT_MONGO_CONTAINER", "fastgpt-mongo")
    user = os.getenv("FASTGPT_MONGO_USER", "myusername")
    password = os.getenv("FASTGPT_MONGO_PASSWORD", "mypassword")
    database = os.getenv("FASTGPT_MONGO_DATABASE", "fastgpt")
    mongo_uri = (
        f"mongodb://{user}:{password}@127.0.0.1:27017/"
        f"{database}?authSource=admin"
    )
    javascript = f"""
const datasetId = ObjectId({json.dumps(dataset_id)});
const rows = db.dataset_datas.find(
  {{ datasetId }},
  {{ _id: 0, collectionId: 1, q: 1, a: 1, chunkIndex: 1 }}
).toArray().map((item) => ({{
  collection_id: String(item.collectionId),
  question: item.q || '',
  answer: item.a || '',
  chunk_index: item.chunkIndex || 0
}}));
print(JSON.stringify(rows));
"""
    command = [
        "docker",
        "exec",
        container,
        "mongosh",
        mongo_uri,
        "--quiet",
        "--eval",
        javascript,
    ]
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("未找到 docker，请确认 FastGPT 的 MongoDB 运行方式。") from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout).strip()
        raise RuntimeError(f"读取 FastGPT MongoDB 失败：{message}") from exc

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MongoDB 返回内容不是有效 JSON。") from exc


GENERIC_QUESTION_PATTERNS = (
    r"何时发布了?.*财报",
    r"财报.*营收.*是多少",
    r"发布时间是什么",
    r"发生在什么时间",
)


INTENT_PATTERNS = (
    ("causal_reasoning", r"为什么|原因|驱动力|导致|背后|为何"),
    ("comparison", r"相比|与.+相比|分别|差异|变化如何|同比|环比"),
    ("time_event", r"何时|什么时候|哪一天|时间|第几分钟|截至"),
    ("numeric_fact", r"多少|几(?:个|人|次|名|家|亿元|万元|%|％)|涨幅|跌幅|占比|排名"),
    ("person_entity", r"谁|哪位|哪些人|哪名|对手是谁|由谁"),
    ("enumeration", r"哪些|哪三|哪两|具体包括|有什么|分别是什么"),
    ("outcome_status", r"结果|结局|进展|处于什么阶段|表现如何|作出什么|发布了什么"),
    ("mechanism_process", r"如何|怎样|通过什么|怎么|方式|过程"),
)


def classify_question_intent(question: str) -> str:
    """给问题标注主要语义意图，便于分层统计召回效果。"""
    for intent, pattern in INTENT_PATTERNS:
        if re.search(pattern, question):
            return intent
    return "specific_fact"


def _question_bigrams(question: str) -> set[str]:
    """生成用于近似去重的字符二元组，忽略标点与具体数字。"""
    normalized = re.sub(r"\d+(?:\.\d+)?", "#", question.lower())
    normalized = re.sub(r"[^a-z\u4e00-\u9fa5#]+", "", normalized)
    return {
        normalized[index:index + 2]
        for index in range(max(len(normalized) - 1, 0))
    }


def questions_are_similar(left: str, right: str, threshold: float = 0.60) -> bool:
    """使用 Dice 系数过滤表述高度相似的问题。"""
    left_bigrams = _question_bigrams(left)
    right_bigrams = _question_bigrams(right)
    if not left_bigrams or not right_bigrams:
        return left.strip().lower() == right.strip().lower()
    score = 2 * len(left_bigrams & right_bigrams) / (
        len(left_bigrams) + len(right_bigrams)
    )
    return score >= threshold


def specificity_score(row: dict) -> float:
    """估算问题的可区分性，优先产品名、人物、数值和组合事实。"""
    question = row.get("question", "").strip()
    answer = row.get("answer", "").strip()
    score = 0.0

    # 英文/型号通常是产品、赛事或技术专名，比单独的公司名更可定位。
    named_tokens = re.findall(r"[A-Za-z][A-Za-z0-9.+_-]{1,}", question)
    score += min(len(set(token.lower() for token in named_tokens)), 3) * 3.0
    score += min(len(set(re.findall(r"\d+(?:\.\d+)?%?", question))), 3) * 1.2

    detail_markers = ("分别", "哪些", "哪两", "哪三", "具体", "为什么", "如何", "原因", "目标")
    score += sum(marker in question for marker in detail_markers) * 1.5
    if 14 <= len(question) <= 55:
        score += 1.0
    if 12 <= len(answer) <= 180:
        score += 1.0
    if any(separator in answer for separator in ("；", "、", "%", "亿元", "万辆")):
        score += 1.0

    score -= sum(bool(re.search(pattern, question)) for pattern in GENERIC_QUESTION_PATTERNS) * 6.0
    return score


def select_specific_question(rows: list[dict], randomizer: random.Random) -> dict:
    """从同一篇新闻的 QA 中选择细节约束最多的问题。"""
    shuffled = list(rows)
    randomizer.shuffle(shuffled)
    return max(shuffled, key=specificity_score)


def build_questions(
    qa_rows: list[dict],
    article_mapping: dict[str, dict],
    limit: int,
    seed: int,
) -> list[dict]:
    """优先覆盖不同新闻，再按主题轮询抽题。"""
    randomizer = random.Random(seed)
    candidates_by_topic: dict[str, list[dict]] = defaultdict(list)

    qa_by_collection: dict[str, list[dict]] = defaultdict(list)
    for row in qa_rows:
        question = row.get("question", "").strip()
        answer = row.get("answer", "").strip()
        if question and answer and row.get("collection_id") in article_mapping:
            qa_by_collection[row["collection_id"]].append(row)

    for qa_collection_id, rows in qa_by_collection.items():
        article = article_mapping[qa_collection_id]
        # 每篇新闻只取一道，避免少数长文占满题集。
        selected = select_specific_question(rows, randomizer)
        topic = article["topic"] or "未分类"
        candidates_by_topic[topic].append(
            {
                "category": "retrieval",
                "intent": classify_question_intent(selected["question"]),
                "topic": topic,
                "question": selected["question"].strip(),
                "reference_answer": selected["answer"].strip(),
                "expected_collection_ids": [article["collection_id"]],
                "expected_urls": [article["url"]],
                "source_title": article["title"],
                "publish_time": article["publish_time"],
                "qa_collection_id": qa_collection_id,
                "specificity_score": round(specificity_score(selected), 2),
            }
        )

    for candidates in candidates_by_topic.values():
        randomizer.shuffle(candidates)

    # 每次优先选择当前使用次数最少的主题和语义意图，同时过滤近似模板。
    candidates = [
        candidate
        for topic_candidates in candidates_by_topic.values()
        for candidate in topic_candidates
    ]
    randomizer.shuffle(candidates)
    questions: list[dict] = []
    selected_topic_counts: dict[str, int] = defaultdict(int)
    selected_intent_counts: dict[str, int] = defaultdict(int)
    while candidates and len(questions) < limit:
        candidates.sort(
            key=lambda item: (
                selected_topic_counts[item["topic"]],
                selected_intent_counts[item["intent"]],
            )
        )
        selected = None
        for candidate in candidates:
            if not any(
                questions_are_similar(candidate["question"], item["question"])
                for item in questions
            ):
                selected = candidate
                break
        if selected is None:
            break
        candidates.remove(selected)
        questions.append(selected)
        selected_topic_counts[selected["topic"]] += 1
        selected_intent_counts[selected["intent"]] += 1

    for index, item in enumerate(questions, start=1):
        item["id"] = f"retrieval_{index:04d}"
    return questions


def main() -> None:
    parser = argparse.ArgumentParser(description="生成新闻召回测评问题")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument(
        "--output",
        default="tests/retrieval_questions.json",
    )
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit 必须大于 0")
    if not settings.fastgpt_qa_dataset_id:
        parser.error("请先配置 FASTGPT_QA_DATASET_ID")

    mapping = load_article_mapping(settings.ingest_db_path)
    qa_rows = load_qa_data(settings.fastgpt_qa_dataset_id)
    questions = build_questions(qa_rows, mapping, args.limit, args.seed)
    if not questions:
        raise RuntimeError("没有找到可映射到原新闻的 QA 数据。")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(questions, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    topic_counts: dict[str, int] = defaultdict(int)
    intent_counts: dict[str, int] = defaultdict(int)
    for item in questions:
        topic_counts[item["topic"]] += 1
        intent_counts[item["intent"]] += 1
    print(
        json.dumps(
            {
                "qa_data_count": len(qa_rows),
                "mapped_article_count": len(mapping),
                "generated": len(questions),
                "topics": dict(sorted(topic_counts.items())),
                "intents": dict(sorted(intent_counts.items())),
                "output": str(output),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
