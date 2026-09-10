#!/usr/bin/env python3
"""FastGPT OpenAI-compatible 接口的本地 Mock 服务。

用途：在真实 FastGPT 不可用（或还没配 App ID）时，让 writing-agent-service 的
Temporal 工作流能完整跑完 Research -> Writer -> Reviewer，用于验证**本项目自身**
的链路是否通畅。它不是 FastGPT 的替代品，产出全是假内容。

工作原理：
    app/clients/fastgpt.py 的 run_structured() 会把目标模型的 JSON Schema
    放在请求的 variables.output_schema 里。本服务读取该 Schema，据此生成一份
    **能通过 Pydantic 校验**的假数据，再按 FastGPT 的响应格式包回去。

只依赖标准库。启动：
    python3 tools/mock_fastgpt.py --port 3000
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_DEPTH = 6
DEFAULT_ARRAY_LEN = 3

_LOWER = "abcdefghijklmnopqrstuvwxyz"
_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_DIGITS = "0123456789"


def _esc_char(token: str, rng: random.Random) -> str:
    """把正则转义记号转成一个具体字符。"""
    if token == "d":
        return rng.choice(_DIGITS)
    if token == "D":
        return rng.choice(_LOWER)
    if token == "w":
        return rng.choice(_LOWER + _DIGITS + "_")
    if token == "W":
        return rng.choice("!@#$%^&*-_")
    if token == "s":
        return " "
    if token == "S":
        return rng.choice(_LOWER)
    if token in ("b", "B"):
        return ""
    return token


def _parse_char_class(body: str, rng: random.Random) -> list[str]:
    chars: list[str] = []
    i = 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            piece = _esc_char(body[i + 1], rng)
            if piece:
                chars.append(piece)
            i += 2
            continue
        if i + 2 < len(body) and body[i + 1] == "-":
            for code in range(ord(body[i]), ord(body[i + 2]) + 1):
                chars.append(chr(code))
            i += 3
            continue
        chars.append(body[i])
        i += 1
    return chars or ["a"]


def _parse_atom(pattern: str, i: int, rng: random.Random) -> tuple[str, int]:
    """解析一个原子单元，返回 (生成的文本, 下一个下标)。"""
    ch = pattern[i]
    if ch == "(":
        depth = 0
        j = i
        while j < len(pattern):
            if pattern[j] == "(":
                depth += 1
            elif pattern[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = pattern[i + 1 : j]
        branch = inner.split("|")[0]
        return _generate_from_pattern(branch, rng, depth=1), j + 1
    if ch == "[":
        end = pattern.find("]", i)
        if end == -1:
            return ch, i + 1
        negate = pattern[i + 1 : i + 2] == "^"
        body = pattern[i + 2 : end] if negate else pattern[i + 1 : end]
        chars = _parse_char_class(body, rng)
        if negate:
            pool = [c for c in _LOWER + _DIGITS if c not in chars] or ["a"]
            return rng.choice(pool), end + 1
        return rng.choice(chars), end + 1
    if ch == "\\" and i + 1 < len(pattern):
        return _esc_char(pattern[i + 1], rng), i + 2
    if ch == ".":
        return rng.choice(_LOWER), i + 1
    return ch, i + 1


def _parse_quantifier(pattern: str, i: int) -> tuple[int | None, int]:
    """解析量词，返回 (最小重复次数, 下一个下标)。没有量词则返回 None。"""
    if i >= len(pattern):
        return None, i
    ch = pattern[i]
    if ch == "*":
        return 0, i + 1
    if ch == "+":
        return 1, i + 1
    if ch == "?":
        return 0, i + 1
    if ch == "{":
        match = re.match(r"\{(\d+)(?:,(\d*))?\}", pattern[i:])
        if match:
            return int(match.group(1)), i + match.end()
    return None, i


def _generate_from_pattern(pattern: str, rng: random.Random, depth: int = 0) -> str:
    """按正则反向生成一个能匹配它的字符串。处理不了时返回空串，由调用方回退。"""
    if not pattern or depth > 4:
        return ""
    text = pattern
    if text.startswith("^"):
        text = text[1:]
    if text.endswith("$") and not text.endswith("\\$"):
        text = text[:-1]

    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] in "*+?":
            i += 1
            continue
        if text[i] in "|)":
            break
        piece, i = _parse_atom(text, i, rng)
        minimum, i = _parse_quantifier(text, i)
        # 次数为 0 的量词也至少生成一个，避免产出空串触发 minLength 校验失败
        times = minimum if minimum else 1
        out.append(piece * times)
    return "".join(out)


# 命中字段名的关键词 -> 生成更像真实内容的假值
_TEXT_TEMPLATES: list[tuple[tuple[str, ...], str]] = [
    (("topic", "title", "subject"), "示例选题：本地链路验证"),
    (("angle", "headline", "hook"), "从数据变化切入，先给结论再补证据"),
    (("summary", "abstract", "brief"), "这是 mock 生成的摘要，用于验证字段透传是否完整。"),
    (("content", "body", "text", "paragraph"), "这是 mock 生成的正文段落。"
     "它包含足够的长度以通过常见的 minLength 校验，并且不携带任何真实信息。"),
    (("reason", "rationale", "why"), "mock 给出的理由：用于占位，不代表真实判断。"),
    (("instruction", "suggestion", "advice"), "mock 建议：此处应补充更权威的数据来源。"),
    (("source", "publisher", "channel"), "mock 来源"),
    (("author", "editor", "owner"), "mock 作者"),
    (("url", "link", "uri", "href"), "https://example.com/evidence/mock"),
    (("tag", "category", "label", "type", "kind", "status", "stage"), "mock"),
    (("id",), "mock-id"),
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _dedupe_list(values: list[Any], fallback: str) -> list[str]:
    """去重并保持顺序，空列表时给一个兜底值。"""
    result: list[str] = []
    seen: set[str] = set()
    for item in values or []:
        text = str(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result or [fallback]


def fix_cross_field_refs(
    title: str,
    value: Any,
    context: dict[str, Any] | None = None,
) -> Any:
    """修正 JSON Schema 表达不出来的跨字段约束（引用完整性、顺序连续性）。

    这些规则只存在于 Pydantic 的 model_validator 里，通用生成器无从得知，
    因此按模型标题做定点修正。新增输出模型时若也有这类约束，需要在这里补一条。
    """
    if not isinstance(value, dict):
        return value
    ctx = context or {}
    fact_pool = [f for f in (ctx.get("_fact_ids") or []) if isinstance(f, str)]
    url_pool = [u for u in (ctx.get("_fact_urls") or []) if isinstance(u, str)]
    fallback_facts = ["F%03d" % i for i in range(1, 11)]
    fallback_urls = ["https://example.com/evidence/mock-%d" % i for i in range(1000, 1010)]

    if title == "ArticleOutline":
        for index, section in enumerate(value.get("sections") or []):
            if not isinstance(section, dict):
                continue
            section["section_id"] = "S%02d" % (index + 1)
            if "order" in section:
                section["order"] = index + 1
            # required_fact_ids 必须存在于 research_package.facts，否则业务校验会拒收
            pool = fact_pool or fallback_facts
            n = max(0, len(section.get("required_fact_ids") or []))
            if n:
                section["required_fact_ids"] = _pick_from_pool(pool, n, ensure_unique=True)
            else:
                # 默认空时也至少补一个，否则下游 section 阶段会卡在 used_fact_ids 上
                section["required_fact_ids"] = _pick_from_pool(pool, 2, ensure_unique=True)

    elif title == "ResearchPackage":
        fact_ids = _dedupe_list(
            [f.get("fact_id") for f in (value.get("facts") or []) if isinstance(f, dict)],
            "F001",
        )
        referenced = fact_ids[:2] if len(fact_ids) >= 2 else fact_ids * 2
        for bucket in ("timeline", "suggested_angles"):
            for item in value.get(bucket) or []:
                if isinstance(item, dict) and "supporting_fact_ids" in item:
                    item["supporting_fact_ids"] = list(referenced)
        for item in value.get("conflicts") or []:
            if isinstance(item, dict) and "fact_ids" in item:
                item["fact_ids"] = list(referenced)
        # 关键：source_url 必须真实可被下游 sections 引用
        for f in value.get("facts") or []:
            if isinstance(f, dict) and not f.get("source_url"):
                f["source_url"] = "https://example.com/evidence/mock-%d" % (
                    1000 + (hash(str(f.get("fact_id", ""))) % 9000)
                )

    elif title == "ArticleSection":
        # 当前章节的 outline.required_fact_ids（若已知），或者从全局池子随机挑
        target_facts: list[str] = []
        target_section_id: str | None = None
        outline_section_ids: list[str] = []
        for sid, required in (ctx.get("_outline_section_required_facts") or []):
            outline_section_ids.append(sid)
            if sid == ctx.get("section_id") and required:
                target_facts = [r for r in required if isinstance(r, str)]
            if ctx.get("section_id") and sid == ctx.get("section_id"):
                target_section_id = sid
        # section_id 必须等于 outline 里那个 section_id，否则 ReviewInput 校验会拒
        if target_section_id:
            value["section_id"] = target_section_id
        if not target_facts:
            target_facts = fact_pool or fallback_facts
        # 必须 used_fact_ids ⊆ 真实 fact_id 池，否则 citations 无法合法引用
        used = _dedupe_list(value.get("used_fact_ids"), "")
        used = [u for u in used if u in target_facts]
        if not used:
            used = target_facts[: max(1, min(2, len(target_facts)))]
        value["used_fact_ids"] = used

        # citations 一一对应，url 必须用真实来源，否则 ReviewInput 校验要求 url 一致
        citations = [c for c in (value.get("citations") or []) if isinstance(c, dict)]
        citations = citations[: len(used)]
        url_pool_eff = url_pool or fallback_urls
        # 按 fact_id 查 research_package 里真实 url；查不到再用池子里的
        fact_url_map = {}
        for fact in (ctx.get("_research_facts_raw") or []):
            if isinstance(fact, dict) and fact.get("fact_id"):
                fact_url_map[fact["fact_id"]] = fact.get("source_url")
        for index, citation in enumerate(citations):
            citation["fact_id"] = used[index]
            url = fact_url_map.get(used[index]) or url_pool_eff[index % len(url_pool_eff)]
            citation["source_url"] = url
        value["citations"] = citations

    elif title == "ArticleDraft":
        # used_fact_ids = 全部 sections 的 fact_id 合集；citations 完整覆盖
        collected = list(ctx.get("_assembled_facts") or [])
        used = _dedupe_list(collected, "F001")
        value["used_fact_ids"] = used
        url_pool_eff = url_pool or fallback_urls
        # 按 fact_id 查 research_package 里真实 url；查不到再用池子里的
        fact_url_map = {}
        for fact in (ctx.get("_research_facts_raw") or []):
            if isinstance(fact, dict) and fact.get("fact_id"):
                fact_url_map[fact["fact_id"]] = fact.get("source_url")
        # assemble 阶段的 payload 不含 research_package，需要从 sections[].citations 里抽取
        # 每个 section 的 citation 都是 Writer 阶段已经按 research_package 注入的真实 url
        for sec in (ctx.get("_sections_raw") or []):
            if not isinstance(sec, dict):
                continue
            for cit in (sec.get("citations") or []):
                if isinstance(cit, dict) and cit.get("fact_id") and cit.get("source_url"):
                    fact_url_map.setdefault(cit["fact_id"], cit["source_url"])
        citations = []
        for index, fid in enumerate(used):
            url = fact_url_map.get(fid) or url_pool_eff[index % len(url_pool_eff)]
            citations.append({"fact_id": fid, "source_url": url})
        value["citations"] = citations
        # section_ids 必须等于 outline 的全部 section_id
        outline_section_ids = ctx.get("_outline_sections") or []
        section_ids = _dedupe_list(value.get("section_ids"), "")
        if not section_ids or set(section_ids) != set(outline_section_ids):
            section_ids = outline_section_ids or ["S%02d" % (i + 1) for i in range(3)]
        value["section_ids"] = section_ids

    elif title == "ReviewReport":
        # issues 里引用的 section_id / fact_ids 必须真实存在，否则 ReviewInput 校验会拒
        # 简化策略：mock 默认产生一个空 issues + decision=approve 的"通过"报告，避免触发
        # issues 与 decision / section_decisions.issue_ids 的额外一致性约束
        value["decision"] = "approve"
        value["issues"] = []
        value["section_decisions"] = []
        # review_round 必须与调用方一致，否则业务校验拒绝
        rr = context.get("review_round")
        if rr is not None:
            value["review_round"] = rr
        return value

    # 默认：原样返回 value
    return value


def _pick_from_pool(pool: list[str], n: int, *, ensure_unique: bool = True) -> list[str]:
    """从池子里取 n 个，必要时按出现顺序循环补齐。"""
    if not pool or n <= 0:
        return []
    out: list[str] = []
    seen: set[str] = set()
    cursor = 0
    while len(out) < n and cursor < len(pool) * 4:
        item = pool[cursor % len(pool)]
        cursor += 1
        if ensure_unique and item in seen:
            continue
        seen.add(item)
        out.append(item)
    if len(out) < n:
        # 池子不够时，按顺序循环补，不要求唯一
        while len(out) < n:
            out.append(pool[len(out) % len(pool)])
    return out


class SchemaFaker:
    """按 JSON Schema 生成能通过 Pydantic 校验的假值。"""

    def __init__(self, root: dict[str, Any], context: dict[str, Any] | None = None) -> None:
        self.root = root if isinstance(root, dict) else {}
        self.rng = random.Random()
        self.context = context or {}
        self._used: dict[str, set[str]] = {}

    # ---------- 公共入口 ----------

    def generate(self, schema: Any, name: str = "", depth: int = 0) -> Any:
        if depth > MAX_DEPTH:
            return None
        if not isinstance(schema, dict):
            return None

        schema = self._resolve(schema, depth)
        if not isinstance(schema, dict):
            return None

        # default 是最安全的值，一定合法
        if "default" in schema:
            return schema["default"]
        if "const" in schema:
            return schema["const"]
        if "enum" in schema and isinstance(schema["enum"], list) and schema["enum"]:
            return self.rng.choice(schema["enum"])

        # Optional[T] / Union -> 取第一个非 null 分支
        for key in ("anyOf", "oneOf"):
            branches = schema.get(key)
            if isinstance(branches, list) and branches:
                non_null = [b for b in branches if isinstance(b, dict) and b.get("type") != "null"]
                if non_null:
                    return self.generate(non_null[0], name, depth + 1)
                return None

        # allOf 合并（简化：逐个应用，返回最后一个具体值）
        if isinstance(schema.get("allOf"), list) and schema["allOf"]:
            merged: dict[str, Any] = {}
            for sub in schema["allOf"]:
                if isinstance(sub, dict):
                    merged.update(self._resolve(sub, depth))
            merged.update({k: v for k, v in schema.items() if k != "allOf"})
            return self.generate(merged, name, depth + 1)

        node_type = schema.get("type")
        if not node_type:
            if "properties" in schema:
                node_type = "object"
            elif "items" in schema:
                node_type = "array"
            else:
                return self._string(name, schema)

        if isinstance(node_type, list):
            node_type = next((t for t in node_type if t != "null"), node_type[0])

        if node_type == "object":
            return self._object(schema, depth)
        if node_type == "array":
            return self._array(schema, name, depth)
        if node_type == "integer":
            return self._number(name, schema, is_int=True)
        if node_type == "number":
            return self._number(name, schema, is_int=False)
        if node_type == "boolean":
            return self.rng.choice([True, False])
        if node_type == "null":
            return None
        value = self._string(name, schema)
        return self._dedupe(name, value, schema)

    def _dedupe(self, name: str, value: str, schema: dict[str, Any]) -> str:
        """ID 类字段会被业务校验要求全局唯一，这里保证同名字段不产出重复值。"""
        if not name or not isinstance(value, str) or not value:
            return value
        low = name.lower()
        if "id" not in low and "pattern" not in schema:
            return value

        seen = self._used.setdefault(name, set())
        if value not in seen:
            seen.add(value)
            return value

        for _ in range(50):
            candidate = self._string(name, schema)
            if candidate and candidate not in seen:
                seen.add(candidate)
                return candidate

        # 兜底：只有无 pattern 约束时才敢追加后缀
        if "pattern" not in schema:
            suffix = 1
            while "%s-%d" % (value, suffix) in seen:
                suffix += 1
            final = "%s-%d" % (value, suffix)
            seen.add(final)
            return final
        return value

    # ---------- 各类型 ----------

    def _object(self, schema: dict[str, Any], depth: int) -> dict[str, Any]:
        props = schema.get("properties")
        if not isinstance(props, dict):
            return {}
        result: dict[str, Any] = {}
        for key, sub in props.items():
            result[key] = self.generate(sub, name=key, depth=depth + 1)
        return result

    def _array(self, schema: dict[str, Any], name: str, depth: int) -> list[Any]:
        items = schema.get("items")
        min_items = schema.get("minItems") or 0
        max_items = schema.get("maxItems")

        size = DEFAULT_ARRAY_LEN
        if isinstance(min_items, int) and min_items > size:
            size = min_items
        if isinstance(max_items, int):
            size = min(size, max_items)
        if isinstance(max_items, int) and max_items < min_items:
            size = max_items

        if items is None:
            return []
        return [self.generate(items, name=name, depth=depth + 1) for _ in range(max(size, 0))]

    def _number(self, name: str, schema: dict[str, Any], *, is_int: bool) -> Any:
        low = (name or "").lower()
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if schema.get("exclusiveMinimum") is not None:
            minimum = float(schema["exclusiveMinimum"]) + (1 if is_int else 1e-6)
        if schema.get("exclusiveMaximum") is not None:
            maximum = float(schema["exclusiveMaximum"]) - (1 if is_int else 1e-6)

        if minimum is None and maximum is None:
            if any(k in low for k in ("score", "rate", "ratio", "confidence", "weight", "percent")):
                minimum, maximum = 0.0, 1.0
            elif any(k in low for k in ("count", "total", "num", "round", "retry", "index")):
                minimum, maximum = 1, 20
            else:
                minimum, maximum = 1, 100

        lo = float(minimum) if minimum is not None else 0.0
        hi = float(maximum) if maximum is not None else lo + 100.0
        if hi < lo:
            lo, hi = hi, lo

        value = self.rng.uniform(lo, hi)
        if is_int:
            return int(round(value))
        return round(value, 4)

    def _string(self, name: str, schema: dict[str, Any]) -> str:
        fmt = schema.get("format") or ""
        low = (name or "").lower()

        if fmt == "uuid":
            return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))
        if fmt == "date-time":
            return _now_iso()
        if fmt == "date":
            return datetime.now(timezone.utc).date().isoformat()
        if fmt in ("uri", "url", "uri-reference"):
            return "https://example.com/evidence/mock-%d" % self.rng.randint(1000, 9999)
        if fmt == "email":
            return "mock@example.com"

        # pattern 是硬约束，优先级高于命名猜测与上下文
        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                generated = _generate_from_pattern(pattern, self.rng)
            except Exception:
                generated = ""
            if generated:
                return generated

        # 优先复用调用方传入的真实上下文，让输出看起来与任务相关
        for key in (low, low.replace("_id", "")):
            if key in self.context and isinstance(self.context[key], str) and self.context[key]:
                return self.context[key]

        text = "mock"
        for keywords, template in _TEXT_TEMPLATES:
            if any(k in low for k in keywords):
                text = template
                break
        else:
            text = "mock-%s" % (low or "value")

        min_len = schema.get("minLength")
        if isinstance(min_len, int) and len(text) < min_len:
            pad = "，这是一段用于补齐最小长度的占位文本。"
            while len(text) < min_len:
                text += pad
        max_len = schema.get("maxLength")
        if isinstance(max_len, int) and max_len > 0 and len(text) > max_len:
            text = text[:max_len]
        return text

    # ---------- $ref 解析 ----------

    def _resolve(self, schema: dict[str, Any], depth: int = 0) -> Any:
        ref = schema.get("$ref")
        if not isinstance(ref, str) or depth > MAX_DEPTH:
            return schema
        if not ref.startswith("#"):
            return schema
        node: Any = self.root
        for token in ref.lstrip("#").strip("/").split("/"):
            if not token:
                continue
            token = token.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            else:
                return schema
        if isinstance(node, dict):
            merged = dict(node)
            for key, value in schema.items():
                if key != "$ref":
                    merged[key] = value
            return merged
        return schema


def _extract_payload(messages: list[Any]) -> dict[str, Any]:
    """从用户消息里取出整段 payload（含 research_package / outline / sections）。"""
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            data = json.loads(content)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return {}


def extract_context(messages: list[Any]) -> dict[str, Any]:
    """从用户消息里取出真实输入（包括上游阶段产出），让假输出能引用真实 ID。"""
    payload = _extract_payload(messages)
    context: dict[str, Any] = {}
    for key in ("topic", "title", "job_id", "section_id", "angle", "review_round"):
        value = payload.get(key)
        if isinstance(value, (str, int)) and value:
            context[key] = str(value)

    # 上游 Research 阶段产出的真实 fact_id 与 url，是 Writer/Reviewer 唯一可靠的引用池
    research = payload.get("research_package")
    if isinstance(research, dict):
        facts = research.get("facts") or []
        context["_fact_ids"] = [
            f.get("fact_id") for f in facts
            if isinstance(f, dict) and isinstance(f.get("fact_id"), str)
        ]
        context["_fact_urls"] = [
            f.get("source_url") for f in facts
            if isinstance(f, dict) and f.get("source_url")
        ]
        context["_research_facts_raw"] = list(facts)

    # outline 阶段产物：每个 section 的 required_fact_ids（下游 section/assembly 阶段要用）
    outline = payload.get("outline")
    if isinstance(outline, dict):
        sections = outline.get("sections") or []
        context["_outline_sections"] = [
            s.get("section_id") for s in sections
            if isinstance(s, dict) and isinstance(s.get("section_id"), str)
        ]
        context["_outline_section_required_facts"] = [
            (s.get("section_id"), list(s.get("required_fact_ids") or []))
            for s in sections if isinstance(s, dict)
        ]

    # 已写好的章节汇总（assembly 阶段需要）
    sections_done = payload.get("sections")
    if isinstance(sections_done, list):
        context["_assembled_facts"] = []
        context["_assembled_section_ids"] = []
        context["_sections_raw"] = list(sections_done)
        for s in sections_done:
            if not isinstance(s, dict):
                continue
            sid = s.get("section_id")
            if isinstance(sid, str):
                context["_assembled_section_ids"].append(sid)
            for fid in s.get("used_fact_ids") or []:
                if isinstance(fid, str):
                    context["_assembled_facts"].append(fid)

    return context


# mock 进程内缓存：上一个阶段（同 job_id）的输出。后续阶段被拒绝时（mock 重启）会丢，
# 在本环境内足够稳定；如果未来要长期运行，再换 Redis 或 DB。
_STAGE_CACHE: dict[str, dict[str, Any]] = {}


def build_response(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    app_id = body.get("appId") or "-"
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    variables = body.get("variables") if isinstance(body.get("variables"), dict) else {}
    mode = variables.get("mode") or "-"

    output_schema = variables.get("output_schema")
    if isinstance(output_schema, str):
        try:
            output_schema = json.loads(output_schema)
        except ValueError:
            output_schema = None
    if not isinstance(output_schema, dict):
        output_schema = {"type": "object", "properties": {}}

    context = extract_context(messages)
    title = str(output_schema.get("title") or "")
    faker = SchemaFaker(output_schema, context)
    fake_value = faker.generate(output_schema, name="root")
    if fake_value is None:
        fake_value = {}
    import sys
    fake_value = fix_cross_field_refs(title, fake_value, context)
    job_id = context.get("job_id")
    if job_id and isinstance(fake_value, dict):
        _STAGE_CACHE.setdefault(job_id, {})[mode] = fake_value
        # 给后续阶段补回 outline / assembled 数据
        if mode == "outline":
            context.setdefault("_outline_sections", [
                s.get("section_id") for s in (fake_value.get("sections") or [])
                if isinstance(s, dict)
            ])
            context.setdefault("_outline_section_required_facts", [
                (s.get("section_id"), list(s.get("required_fact_ids") or []))
                for s in (fake_value.get("sections") or []) if isinstance(s, dict)
            ])
        elif mode == "section":
            used = fake_value.get("used_fact_ids") or []
            context.setdefault("_assembled_facts", list(used))
        elif mode == "assemble":
            sections_done = fake_value.get("section_ids") or []
            context.setdefault("_assembled_section_ids", list(sections_done))

    # 必须返回纯 JSON 字符串：客户端 _unwrap_json_code_fence 只接受纯 JSON 或完整代码块
    content = json.dumps(fake_value, ensure_ascii=False)

    keys = list(fake_value.keys()) if isinstance(fake_value, dict) else []
    print(
        "[mock] appId=%s mode=%s -> %d keys %s"
        % (app_id, mode, len(keys), keys[:6]),
        flush=True,
    )

    return 200, {
        "code": 200,
        "message": "success",
        "data": {
            "id": "chatcmpl-mock-%s" % uuid.uuid4().hex[:12],
            "model": "mock-fastgpt",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 128,
                "completion_tokens": len(content) // 3,
                "total_tokens": 128 + len(content) // 3,
            },
        },
    }


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MockFastGPT/1.0"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("x-request-id", "mock-%s" % uuid.uuid4().hex[:16])
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("", "/health", "/api/status"):
            self._send_json(200, {"status": "ok", "service": "mock-fastgpt"})
            return
        self._send_json(404, {"code": 404, "message": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError("body must be an object")
        except (ValueError, UnicodeDecodeError) as exc:
            print("[mock] 请求体解析失败: %s" % exc, flush=True)
            self._send_json(400, {"code": 400, "message": "invalid json body"})
            return

        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(404, {"code": 404, "message": "not found"})
            return

        status, payload = build_response(body)
        self._send_json(status, payload)

    def log_message(self, fmt: str, *args: Any) -> None:
        pass  # 静音默认访问日志


def main() -> int:
    parser = argparse.ArgumentParser(description="FastGPT 兼容 Mock 服务（仅本地验证用）")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=3000, help="监听端口，默认 3000")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print("mock fastgpt listening on http://%s:%d" % (args.host, args.port), flush=True)
    print("endpoint: POST /api/v1/chat/completions", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
