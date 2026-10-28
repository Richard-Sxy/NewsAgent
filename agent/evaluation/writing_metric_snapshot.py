"""Three narrow public-source metric rules for the local writing development pilot.

Pure standard-library transformation: no files, private gold, model, network or
general warehouse extraction. A matched calculation is relative to the supplied
source text, not independent verification that the source's claim is true.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, localcontext
import hashlib
import json
import re

RULES_VERSION = "public-source-three-patterns-20261028-v1"
SCOPE = "narrow_regex_pilot"
NUMBER = r"[+-]?\d{1,18}(?:\.\d{1,8})?"
OLD = r"(?P<old_approx>约)?\s*(?P<old>" + NUMBER + r")"
NEW = r"(?P<new_approx>约)?\s*(?P<new>" + NUMBER + r")"
RULES = (
    {
        "rule_id": "percentage_transition",
        "pattern": re.compile(r"由\s*" + OLD + r"\s*[%％]\s*提升至\s*" + NEW + r"\s*[%％]"),
        "metric_label": "来源中明确陈述的百分比变化",
        "unit": "%", "absolute_unit": "个百分点", "from_period": None, "to_period": None,
        "semantic_note": "绝对变化是百分点，相对变化是相对基数的百分比；二者不可混用。主体和期间由所引原文确定。",
    },
    {
        "rule_id": "q1_q2_revenue",
        "pattern": re.compile(r"营收\s*从\s*Q1\s*的\s*" + OLD
                              + r"\s*亿美元\s*涨到\s*Q2\s*的\s*" + NEW + r"\s*亿美元", re.IGNORECASE),
        "metric_label": "营收", "unit": "亿美元", "absolute_unit": "亿美元",
        "from_period": "Q1", "to_period": "Q2",
        "semantic_note": "这是所给来源营收数值的差额和相对变化，不是年化营收或利润；年份不由本规则推断。",
    },
    {
        "rule_id": "operating_loss_magnitude",
        "pattern": re.compile(r"营业亏损(?:规模)?(?:更是)?\s*从\s*第一季度的\s*" + OLD
                              + r"\s*亿美元\s*扩大至\s*" + NEW + r"\s*亿美元"),
        "metric_label": "营业亏损规模", "unit": "亿美元", "absolute_unit": "亿美元",
        "from_period": "第一季度", "to_period": None,
        "semantic_note": "正数表示亏损规模；扩大表示亏损增加，不是利润增长，也不等同净利润或净亏损。扩大后期间须结合原文，规则不自行推断。",
    },
)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plain(value):
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _evidence(source, content, match):
    start, end = match.span()
    quote_start, quote_end = max(0, start - 80), min(len(content), end + 80)
    quote = content[quote_start:quote_end]
    return {
        "source_id": source["source_id"], "source_url": source["url"],
        "source_content_sha256": _sha(content),
        "quote": quote, "quote_start": quote_start, "quote_end": quote_end,
        "quote_sha256": _sha(quote),
        "matched_text": content[start:end], "match_start": start, "match_end": end,
        "offset_unit": "unicode_codepoints",
    }


def build_metric_snapshot(public_input: dict) -> dict:
    """Calculate source-local deltas using the three explicit patterns above.

    gaps describe rule coverage or unsafe arithmetic; they are not task failure
    labels. All model-facing derived values retain source/offset identities.
    """
    try:
        identity = hashlib.sha256(json.dumps(
            public_input, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    except (ValueError, TypeError) as exc:
        raise ValueError("public_input must be JSON-compatible") from exc
    result = {
        "schema_version": 1, "scope": SCOPE, "rules_version": RULES_VERSION,
        "public_input_sha256": identity, "metrics": [], "gaps": [],
        "limitations": [
            "只实现三个显式文字模式，不是通用数仓或新闻指标抽取器。",
            "缺口只说明规则未匹配或计算条件不足，不据此判定任务缺少必需事实。",
            "计算相对于给定来源，不独立核真来源；近似或取整输入的派生结果仍为近似值。",
            "重复来源或多个匹配分别保留证据，不将匹配条数当成独立事件数量。",
        ],
    }
    req = public_input.get("requirements") if isinstance(public_input, dict) else None
    sources = req.get("source_documents") if isinstance(req, dict) else None
    if not isinstance(sources, list) or not sources:
        result["gaps"].append({"code": "source_documents_missing", "rule_id": None})
        return result
    counts = {rule["rule_id"]: 0 for rule in RULES}
    for index, source in enumerate(sources):
        if (not isinstance(source, dict) or not isinstance(source.get("content"), str)
                or not source["content"] or not isinstance(source.get("source_id"), str)
                or not source["source_id"].strip() or not isinstance(source.get("url"), str)
                or not source["url"].strip()):
            result["gaps"].append({"code": "invalid_source_identity_or_content", "source_index": index})
            continue
        content = source["content"]
        for rule in RULES:
            for match in rule["pattern"].finditer(content):
                counts[rule["rule_id"]] += 1
                old, new = Decimal(match["old"]), Decimal(match["new"])
                approximate = bool(match["old_approx"] or match["new_approx"])
                with localcontext() as context:
                    context.prec = 50
                    absolute = new - old
                    relative = ((new - old) / old * Decimal(100)).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP) if old > 0 else None
                evidence = _evidence(source, content, match)
                identifier = "metric-" + _sha("|".join([
                    rule["rule_id"], source["source_id"],
                    evidence["source_content_sha256"], str(match.start())]))[:20]
                semantic_note = rule["semantic_note"]
                if approximate:
                    semantic_note += " 基数含近似或取整限定；不要用派生值与报道取整差异证明来源错误。"
                metric = {
                    "metric_id": identifier, "rule_id": rule["rule_id"],
                    "metric_label": rule["metric_label"],
                    "base_value": _plain(old), "current_value": _plain(new), "unit": rule["unit"],
                    "from_period": rule["from_period"], "to_period": rule["to_period"],
                    "absolute_change": {"value": _plain(absolute), "unit": rule["absolute_unit"],
                                        "display": _plain(absolute) + rule["absolute_unit"],
                                        "formula": "current_value - base_value"},
                    "relative_change": None if relative is None else {
                        "value": format(relative, ".2f"), "unit": "%",
                        "display": format(relative, ".2f") + "%",
                        "formula": "(current_value - base_value) / base_value * 100"},
                    "approximate": approximate,
                    "operand_approximate": {"base": bool(match["old_approx"]), "current": bool(match["new_approx"])},
                    "semantic_note": semantic_note, "evidence": evidence,
                }
                result["metrics"].append(metric)
                if relative is None:
                    result["gaps"].append({"code": "nonpositive_comparison_base", "rule_id": rule["rule_id"],
                                           "metric_id": identifier, "source_id": source["source_id"],
                                           "note": "仍保留绝对差，不推断相对变化。"})
    for rule_id, count in counts.items():
        if not count:
            result["gaps"].append({"code": "rule_not_matched", "rule_id": rule_id,
                                   "note": "所给来源未匹配该窄规则；不等于该任务缺少必需事实。"})
    return result
