"""Build a deterministic, offline data-writing evaluation draft.

This validates dataset structure and numeric labels, never model quality or the
truth of an archived article. It neither reads ``cache_reference`` nor accesses
the network. Arithmetic operands are ordered: ratio = numerator/denominator;
delta = current-comparison; relative change = (current-baseline)/baseline;
divide = amount/dimensionless multiplier (denominator unit 倍/比例/倍数).

Run from ``agent`` with ``python -m evaluation.data_writing_dataset --seed PATH
--output-dir DIR``. Add ``--check`` to compare existing artifacts without writes.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


BUILDER_VERSION = "data-writing-dataset-v1"
STATUS = "draft_pending_human_review"
WINDOW = {"start": "2026-08-01", "end": "2026-08-31", "timezone": "Asia/Shanghai"}
_DECIMAL = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z", re.ASCII)
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)


class DatasetValidationError(ValueError):
    """The seed or an existing artifact violates the dataset contract."""


def _fail(path: str, message: str) -> None:
    raise DatasetValidationError(f"{path}: {message}")


def _object(value: Any, path: str, required: set[str], optional: set[str] | None = None) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    missing = required - value.keys()
    unknown = value.keys() - required - (optional or set())
    if missing:
        _fail(path, f"missing fields: {', '.join(sorted(missing))}")
    if unknown:
        _fail(path, f"unknown fields: {', '.join(sorted(unknown))}")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(path, "must be a non-empty string")
    return value


def _choice(value: Any, path: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        _fail(path, f"must be one of {', '.join(sorted(choices))}")
    return value


def _list(value: Any, path: str, *, nonempty: bool = False) -> list:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(path, "must be a non-empty array" if nonempty else "must be an array")
    return value


def _strings(value: Any, path: str, *, nonempty: bool = False) -> list[str]:
    items = _list(value, path, nonempty=nonempty)
    for index, item in enumerate(items):
        _text(item, f"{path}[{index}]")
    if len(items) != len(set(items)):
        _fail(path, "duplicate values")
    return items


def _number(value: Any, path: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 128 or not _DECIMAL.fullmatch(value):
        _fail(path, "must be a finite decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation:
        _fail(path, "must be a finite decimal string")
    if not result.is_finite() or abs(result.adjusted()) > 1000:
        _fail(path, "decimal exponent exceeds supported bounds")
    return result


def _timestamp(value: Any, path: str, *, august: bool = False) -> datetime:
    _text(value, path)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _fail(path, "must be an ISO timestamp with timezone")
    if "T" not in value or result.utcoffset() is None:
        _fail(path, "must be an ISO timestamp with timezone")
    if august and (not value.endswith("+08:00") or result.utcoffset() != timedelta(hours=8)
                   or result.year != 2026 or result.month != 8):
        _fail(path, "must be published in August 2026 with +08:00 timezone")
    return result


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source(value: Any, index: int, facts: dict, identities: dict) -> dict:
    path = f"sources[{index}]"
    source = _object(value, path, {"source_id", "news_id", "url", "title", "published_at", "publisher",
                                  "provenance", "verification_status", "cache_reference", "content_sha256", "facts"},
                     {"fetched_at"})
    for name in ("source_id", "news_id", "title", "publisher", "cache_reference"):
        _text(source[name], f"{path}.{name}")
    if "fetched_at" in source:
        _text(source["fetched_at"], f"{path}.fetched_at")
    _choice(source["provenance"], f"{path}.provenance", {"local_archive"})
    _choice(source["verification_status"], f"{path}.verification_status", {"cache_only"})
    _timestamp(source["published_at"], f"{path}.published_at", august=True)
    url = _text(source["url"], f"{path}.url")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "news.qq.com" or parsed.username
                or parsed.password or parsed.port not in (None, 443) or parsed.path in ("", "/")
                or any(char.isspace() for char in url)):
            _fail(f"{path}.url", "must be an HTTPS article URL on news.qq.com")
    except ValueError:
        _fail(f"{path}.url", "must be an HTTPS article URL on news.qq.com")
    if not isinstance(source["content_sha256"], str) or not _HASH.fullmatch(source["content_sha256"]):
        _fail(f"{path}.content_sha256", "must be a lowercase SHA-256 digest")
    for field in ("source_id", "news_id", "url"):
        key = (field, source[field])
        if key in identities:
            _fail(f"{path}.{field}", f"duplicate source identity {source[field]}")
        identities[key] = source["source_id"]
    for fact_index, fact in enumerate(_list(source["facts"], f"{path}.facts", nonempty=True)):
        fact_path = f"{path}.facts[{fact_index}]"
        _object(fact, fact_path, {"fact_id", "statement"}, {"value", "unit", "evidence_locator"})
        fact_id = _text(fact["fact_id"], f"{fact_path}.fact_id")
        _text(fact["statement"], f"{fact_path}.statement")
        if "evidence_locator" in fact:
            _text(fact["evidence_locator"], f"{fact_path}.evidence_locator")
        if fact_id in facts:
            _fail(f"{fact_path}.fact_id", f"duplicate global fact_id {fact_id}")
        if ("value" in fact) != ("unit" in fact):
            _fail(fact_path, "numeric facts require both value and unit")
        if "value" in fact:
            _number(fact["value"], f"{fact_path}.value")
            _text(fact["unit"], f"{fact_path}.unit")
        facts[fact_id] = (source["source_id"], fact)
    return source


def _numeric_checks(checks: Any, numeric: dict, path: str) -> int:
    seen = set()
    for index, check in enumerate(_list(checks, path)):
        check_path = f"{path}[{index}]"
        _object(check, check_path, {"check_id", "operation", "operands", "expected_value", "unit", "tolerance"})
        check_id = _text(check["check_id"], f"{check_path}.check_id")
        if check_id in seen:
            _fail(f"{check_path}.check_id", "duplicate check_id")
        seen.add(check_id)
        operation = _choice(check["operation"], f"{check_path}.operation",
                            {"identity", "ratio_percent", "delta", "relative_change_percent", "sum",
                             "percentage_point_delta", "basis_point_delta", "divide"})
        operands = _strings(check["operands"], f"{check_path}.operands", nonempty=True)
        count = 1 if operation == "identity" else 2
        if operation != "sum" and len(operands) != count:
            _fail(check_path, f"{operation} requires {count} operands")
        if any(operand not in numeric for operand in operands):
            _fail(f"{check_path}.operands", "reference unknown numeric input")
        unit = _text(check["unit"], f"{check_path}.unit")
        units = {numeric[operand]["unit"] for operand in operands}
        if operation == "divide":
            if numeric[operands[1]]["unit"] not in {"倍", "比例", "倍数"} or unit != numeric[operands[0]]["unit"]:
                _fail(f"{check_path}.unit", "divide requires a dimensionless denominator and numerator result unit")
        elif len(units) != 1:
            _fail(check_path, "operand units must match")
        elif operation in {"ratio_percent", "relative_change_percent"}:
            if unit != "%":
                _fail(f"{check_path}.unit", "percentage result must use %")
        elif operation in {"percentage_point_delta", "basis_point_delta"}:
            required_unit = "个百分点" if operation == "percentage_point_delta" else "基点"
            if units != {"%"} or unit != required_unit:
                _fail(f"{check_path}.unit", f"{operation} requires % operands and {required_unit} result")
        elif operation == "delta" and units == {"%"}:
            _fail(check_path, "percentage differences require percentage_point_delta or basis_point_delta")
        elif units != {unit}:
            _fail(f"{check_path}.unit", "result unit must match operand units")
        tolerance = _number(check["tolerance"], f"{check_path}.tolerance")
        if tolerance < 0:
            _fail(f"{check_path}.tolerance", "must be non-negative")
        numbers = [_number(numeric[operand]["value"], f"{check_path}.operands") for operand in operands]
        # High precision keeps label checks deterministic even near rounding boundaries.
        with localcontext() as context:
            context.prec = 4096
            if operation == "identity":
                actual = numbers[0]
            elif operation == "sum":
                actual = sum(numbers, Decimal(0))
            elif operation in {"delta", "percentage_point_delta"}:
                actual = numbers[0] - numbers[1]
            elif operation == "basis_point_delta":
                actual = (numbers[0] - numbers[1]) * 100
            elif numbers[1] == 0:
                actual = None
            elif operation == "divide":
                actual = numbers[0] / numbers[1]
            elif operation == "ratio_percent":
                actual = numbers[0] / numbers[1] * 100
            else:
                actual = (numbers[0] - numbers[1]) / numbers[1] * 100
            if actual is None:
                if check["expected_value"] is not None:
                    _fail(f"{check_path}.expected_value", "zero denominator requires JSON null")
            else:
                label = _number(check["expected_value"], f"{check_path}.expected_value")
                if abs(actual - label) > tolerance:
                    _fail(f"{check_path}.expected_value", "does not match deterministic arithmetic")
    return len(seen)


def validate_seed(seed: Any) -> dict:
    """Validate a draft seed without inspecting archives or invoking inference."""
    _object(seed, "seed", {"dataset_version", "status", "corpus_window", "sources", "cases"},
            {"selection_policy", "hotness_verified", "support_boundary"})
    _text(seed["dataset_version"], "seed.dataset_version")
    _choice(seed["status"], "seed.status", {STATUS})
    for name in ("selection_policy", "support_boundary"):
        if name in seed:
            _text(seed[name], f"seed.{name}")
    if "hotness_verified" in seed and seed["hotness_verified"] is not False:
        _fail("seed.hotness_verified", "must be false; offline corpus does not verify historical hotness")
    _object(seed["corpus_window"], "seed.corpus_window", set(WINDOW))
    if seed["corpus_window"] != WINDOW:
        _fail("seed.corpus_window", "must be August 2026 in Asia/Shanghai")
    facts: dict[str, tuple[str, dict]] = {}
    identities: dict[tuple[str, str], str] = {}
    sources = {}
    for index, value in enumerate(_list(seed["sources"], "sources", nonempty=True)):
        source = _source(value, index, facts, identities)
        sources[source["source_id"]] = source
    case_ids, group_splits, source_splits, content_splits = set(), {}, {}, {}
    for index, case in enumerate(_list(seed["cases"], "cases", nonempty=True)):
        path = f"cases[{index}]"
        _object(case, path, {"case_id", "event_group_id", "split", "domain", "variant", "task", "input", "expected"})
        case_id = _text(case["case_id"], f"{path}.case_id")
        if case_id in case_ids:
            _fail(f"{path}.case_id", "duplicate case_id")
        case_ids.add(case_id)
        group = _text(case["event_group_id"], f"{path}.event_group_id")
        split = _choice(case["split"], f"{path}.split", {"development", "validation", "test"})
        _choice(case["domain"], f"{path}.domain", {"finance", "ai", "technology", "macro"})
        _choice(case["variant"], f"{path}.variant", {"normal", "hard"})
        _choice(case["task"], f"{path}.task", {"research", "write", "review"})
        if group in group_splits and group_splits[group] != split:
            _fail(f"{path}.event_group_id", "event group leaks across splits")
        group_splits[group] = split
        item = _object(case["input"], f"{path}.input",
                       {"request", "audience", "as_of", "source_ids", "fact_ids", "numeric_inputs"},
                       {"metric_fixture", "draft_to_review", "adversarial_material"})
        _text(item["request"], f"{path}.input.request")
        _choice(item["audience"], f"{path}.input.audience", {"internal", "public"})
        as_of = _timestamp(item["as_of"], f"{path}.input.as_of")
        source_ids = _strings(item["source_ids"], f"{path}.input.source_ids", nonempty=True)
        for source_id in source_ids:
            if source_id not in sources:
                _fail(f"{path}.input.source_ids", f"unknown source {source_id}")
            source = sources[source_id]
            if as_of < _timestamp(source["published_at"], f"source {source_id}.published_at"):
                _fail(f"{path}.input.as_of", "precedes a referenced source publication")
            if source_id in source_splits and source_splits[source_id] != split:
                _fail(f"{path}.input.source_ids", "source leaks across splits")
            source_splits[source_id] = split
            digest = source["content_sha256"]
            if digest in content_splits and content_splits[digest] != split:
                _fail(f"{path}.input.source_ids", "identical source content leaks across splits")
            content_splits[digest] = split
        fact_ids = _strings(item["fact_ids"], f"{path}.input.fact_ids")
        for fact_id in fact_ids:
            if fact_id not in facts or facts[fact_id][0] not in source_ids:
                _fail(f"{path}.input.fact_ids", f"unknown or out-of-scope fact {fact_id}")
        if "metric_fixture" in item:
            fixture = _object(item["metric_fixture"], f"{path}.input.metric_fixture", {"synthetic", "notice"})
            if fixture["synthetic"] is not True:
                _fail(f"{path}.input.metric_fixture.synthetic", "must be true")
            _text(fixture["notice"], f"{path}.input.metric_fixture.notice")
        if "adversarial_material" in item:
            material = _object(item["adversarial_material"], f"{path}.input.adversarial_material", {"synthetic", "text"})
            if material["synthetic"] is not True:
                _fail(f"{path}.input.adversarial_material.synthetic", "must be true")
            _text(material["text"], f"{path}.input.adversarial_material.text")
        if "draft_to_review" in item:
            _text(item["draft_to_review"], f"{path}.input.draft_to_review")
        if case["task"] == "review" and "draft_to_review" not in item:
            _fail(f"{path}.input", "review task requires draft_to_review")
        numeric = item["numeric_inputs"]
        if not isinstance(numeric, dict):
            _fail(f"{path}.input.numeric_inputs", "must be an object")
        for numeric_id, number in numeric.items():
            numeric_path = f"{path}.input.numeric_inputs.{numeric_id}"
            _text(numeric_id, numeric_path)
            _object(number, numeric_path, {"value", "unit", "provenance"}, {"source_id", "fact_id"})
            _number(number["value"], f"{numeric_path}.value")
            _text(number["unit"], f"{numeric_path}.unit")
            provenance = _choice(number["provenance"], f"{numeric_path}.provenance", {"reported", "synthetic_fixture"})
            if provenance == "reported":
                fact_id, source_id = number.get("fact_id"), number.get("source_id")
                if (not isinstance(fact_id, str) or fact_id not in fact_ids or source_id not in source_ids
                        or facts[fact_id][0] != source_id):
                    _fail(numeric_path, "reported numeric input requires matching in-scope source_id/fact_id")
                fact = facts[fact_id][1]
                if ("value" not in fact or _number(number["value"], numeric_path) != _number(fact["value"], numeric_path)
                        or number["unit"] != fact["unit"]):
                    _fail(numeric_path, "reported value/unit differs from source fact")
            else:
                if "metric_fixture" not in item:
                    _fail(numeric_path, "synthetic input requires metric_fixture with synthetic=true and notice")
                if "source_id" in number or "fact_id" in number:
                    _fail(numeric_path, "synthetic input cannot claim reported fact provenance")
        expected = _object(case["expected"], f"{path}.expected",
                           {"action", "required_fact_ids", "forbidden_claims", "required_caveats", "numeric_checks", "manual_rubric"})
        _choice(expected["action"], f"{path}.expected.action", {"write", "request_evidence", "reject", "revise", "approve"})
        required = _strings(expected["required_fact_ids"], f"{path}.expected.required_fact_ids")
        if not set(required) <= set(fact_ids):
            _fail(f"{path}.expected.required_fact_ids", "facts must belong to case input")
        for name in ("forbidden_claims", "required_caveats", "manual_rubric"):
            _strings(expected[name], f"{path}.expected.{name}", nonempty=name == "manual_rubric")
        _numeric_checks(expected["numeric_checks"], numeric, f"{path}.expected.numeric_checks")
    return seed


def render_dataset(seed: dict) -> dict[str, bytes]:
    """Return canonical bytes; labels are kept in a separate evaluation file."""
    validate_seed(seed)
    sources = {source["source_id"]: source for source in seed["sources"]}
    cases = sorted(seed["cases"], key=lambda case: case["case_id"])
    inputs, labels = [], []
    for case in cases:
        # Difficulty annotations can correlate with answer labels. They belong
        # in the seed/manifest audit, never in the inference-facing records.
        record = {key: deepcopy(value) for key, value in case.items() if key not in {"expected", "variant"}}
        record["input"]["sources"] = [deepcopy(sources[source_id]) for source_id in case["input"]["source_ids"]]
        inputs.append(record)
        labels.append({"case_id": case["case_id"], "expected": deepcopy(case["expected"])})
    artifacts = {"inputs.jsonl": b"".join(_json_bytes(item) for item in inputs),
                 "labels.jsonl": b"".join(_json_bytes(item) for item in labels)}
    manifest = {
        "builder_version": BUILDER_VERSION, "dataset_version": seed["dataset_version"],
        "status": seed["status"], "corpus_window": seed["corpus_window"],
        "seed_sha256": _sha(_json_bytes(seed)), "seed_hash_format": "canonical-json-utf8-with-final-newline",
        "source_count": len(sources), "case_count": len(cases),
        "event_group_count": len({case["event_group_id"] for case in cases}),
        "counts": {field: dict(sorted(Counter(case[field] for case in cases).items()))
                   for field in ("split", "domain", "task", "variant")},
        "synthetic_metric_case_count": sum("metric_fixture" in case["input"] for case in cases),
        "synthetic_adversarial_case_count": sum("adversarial_material" in case["input"] for case in cases),
        "numeric_check_count": sum(len(case["expected"]["numeric_checks"]) for case in cases),
        "source_content_sha256": {source_id: sources[source_id]["content_sha256"] for source_id in sorted(sources)},
        "files": {name: {"sha256": _sha(content), "record_count": len(cases)} for name, content in artifacts.items()},
        "validation_scope": "offline_structure_references_splits_and_decimal_labels_only",
        "source_verification": "cache_only_not_independently_verified",
        "model_inference_performed": False,
    }
    for name in ("selection_policy", "hotness_verified", "support_boundary"):
        if name in seed:
            manifest[name] = seed[name]
    artifacts["manifest.json"] = _json_bytes(manifest)
    return artifacts


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _fail("seed", f"duplicate JSON key {key}")
        result[key] = value
    return result


def load_seed(path: str | Path) -> dict:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                          parse_constant=lambda value: _fail("seed", f"invalid JSON constant {value}"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DatasetValidationError(f"seed: invalid UTF-8 JSON: {exc}") from exc


def build_dataset(seed_path: str | Path, output_dir: str | Path, *, check: bool = False) -> dict:
    """Build artifacts, or verify all existing bytes without modifying them."""
    artifacts = render_dataset(load_seed(seed_path))
    destination = Path(output_dir)
    if check:
        for name, expected in artifacts.items():
            path = destination / name
            if not path.is_file() or path.read_bytes() != expected:
                _fail(str(path), "missing or differs from deterministic rebuild")
    else:
        destination.mkdir(parents=True, exist_ok=True)
        for name, content in artifacts.items():
            (destination / name).write_bytes(content)
    return json.loads(artifacts["manifest.json"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--check", action="store_true", help="verify existing files without writes")
    args = parser.parse_args(argv)
    try:
        manifest = build_dataset(args.seed, args.output_dir, check=args.check)
    except (DatasetValidationError, OSError) as exc:
        parser.exit(2, f"dataset error: {exc}\n")
    print(json.dumps({"status": "checked" if args.check else "built", "case_count": manifest["case_count"],
                      "dataset_version": manifest["dataset_version"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
