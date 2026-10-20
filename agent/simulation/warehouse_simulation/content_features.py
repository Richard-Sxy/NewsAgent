"""Deterministic, non-model features derived from real cached article text.

These values describe the article itself (lengths, paragraph/sentence counts,
numeric tokens and repeated CJK fragments). They are not behavior metrics and
must never be presented as audience measurements.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Any

MAX_KEYWORDS = 8
MAX_KEYWORD_INPUT_CHARS = 8_000
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,}")
_DIGIT_TOKEN = re.compile(r"\d+(?:\.\d+)?%?")
_SENTENCE_END = re.compile(r"[。！？!?]+")
_PARAGRAPH_BREAK = re.compile(r"\n+")
_KEYWORD_STOP = frozenset({
    "一个", "一些", "以及", "已经", "我们", "他们", "自己", "没有", "可以", "不是",
    "这个", "那个", "这些", "那些", "因为", "所以", "但是", "如果", "还是", "就是",
    "进行", "表示", "认为", "相关", "目前", "记者", "新闻", "报道", "今天", "昨天",
    "明天", "今年", "去年", "明年", "方面", "问题", "情况", "工作", "时间", "地区",
    "中国", "其中", "通过", "对于", "关于", "由于", "以上", "以下", "同时", "此外",
})


def _keyword_top(title: str, content: str) -> list[str]:
    text = (title + "\n" + content)[:MAX_KEYWORD_INPUT_CHARS]
    counts: Counter[str] = Counter()
    for run in _CJK_RUN.findall(text):
        for size in (2, 3, 4):
            for index in range(len(run) - size + 1):
                term = run[index:index + size]
                if term in _KEYWORD_STOP:
                    continue
                counts[term] += 1
    ranked = sorted(
        ((term, count) for term, count in counts.items() if count >= 2),
        key=lambda item: (-item[1], -len(item[0]), item[0]),
    )
    selected: list[str] = []
    for term, _count in ranked:
        if any(term in existing or existing in term for existing in selected):
            continue
        selected.append(term)
        if len(selected) == MAX_KEYWORDS:
            break
    return selected


def derive_content_features(title: str, content: str) -> dict[str, Any]:
    """Return stable features; callers must pass verified real article text."""

    if not isinstance(title, str) or not title.strip():
        raise ValueError("content feature title must be nonempty text")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content feature body must be nonempty text")
    paragraphs = [part for part in _PARAGRAPH_BREAK.split(content) if part.strip()]
    return {
        "title_chars": len(title),
        "content_chars": len(content),
        "paragraph_count": len(paragraphs),
        "sentence_count": len(_SENTENCE_END.findall(content)),
        "digit_token_count": len(_DIGIT_TOKEN.findall(content)),
        "keyword_top": _keyword_top(title, content),
    }


def validate_content_features(features: Any, title: str, content: str) -> dict[str, Any]:
    """Recompute and compare; reject non-deterministic or tampered feature rows."""

    if not isinstance(features, dict):
        raise ValueError("content features must be an object")
    expected = derive_content_features(title, content)
    if set(features) != set(expected):
        raise ValueError("content feature fields differ from the frozen contract")
    for key in ("title_chars", "content_chars", "paragraph_count", "sentence_count", "digit_token_count"):
        if type(features[key]) is not int or features[key] != expected[key]:
            raise ValueError(f"content feature {key} differs from source text")
    keywords = features["keyword_top"]
    if (not isinstance(keywords, list) or len(keywords) > MAX_KEYWORDS
            or any(not isinstance(value, str) or not value for value in keywords)
            or len(keywords) != len(set(keywords))):
        raise ValueError("content feature keywords are invalid")
    if keywords != expected["keyword_top"]:
        raise ValueError("content feature keywords differ from source text")
    return expected
