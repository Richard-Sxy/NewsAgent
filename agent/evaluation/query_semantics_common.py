"""Frozen, offline query-evaluation contracts shared by preparation and scoring."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
SPLITS = {"train", "development", "validation", "test"}
COHORTS = {"golden", "fresh", "high_risk"}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical_sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"),
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))


def read_jsonl(path: Path) -> list[dict]:
    result = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            if not isinstance(item, dict):
                raise ValueError("JSONL records must be objects")
            result.append(item)
    return result


def write_json(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
                                 for row in rows), encoding="utf-8")


def validate_cases(cases: list[dict]) -> None:
    from app.modules.conversation.query_understanding import QueryPolicy
    from app.sql_assistant.input_boundary import normalize_question

    if not cases:
        raise ValueError("empty case set")
    seen, groups, questions = set(), {}, {}
    for case in cases:
        if set(case) != {"id", "group_id", "split", "cohort", "question", "now", "policy", "source"}:
            raise ValueError("case keys differ from the public input contract")
        for name in ("id", "group_id"):
            if not isinstance(case[name], str) or not case[name].strip():
                raise ValueError("case/group IDs must be nonempty")
        if case["id"] in seen:
            raise ValueError("duplicate case ID")
        seen.add(case["id"])
        if case["split"] not in SPLITS or case["cohort"] not in COHORTS:
            raise ValueError("unknown split/cohort")
        if groups.setdefault(case["group_id"], case["split"]) != case["split"]:
            raise ValueError("same group crosses splits")
        question = normalize_question(case["question"])
        key = "".join(question.split()).casefold()
        if questions.setdefault(key, case["split"]) != case["split"]:
            raise ValueError("duplicate question crosses splits")
        now = datetime.fromisoformat(case["now"])
        if now.utcoffset() is None:
            raise ValueError("case clock must include timezone")
        QueryPolicy.model_validate(case["policy"])
        source = case["source"]
        if not isinstance(source, dict) or set(source) != {"kind", "reference"}:
            raise ValueError("source requires kind/reference only")
        if not all(isinstance(source[k], str) and source[k].strip() for k in source):
            raise ValueError("source kind/reference missing")
        if case["cohort"] == "fresh" and source["kind"] != "reviewed_bad_case":
            raise ValueError("Fresh requires an actual reviewed bad-case reference")


def validate_gold(cases: list[dict], gold: list[dict]) -> str:
    from app.modules.conversation.query_understanding import QueryPolicy, QueryUnderstanding
    from app.modules.conversation.structured_query_resolution import resolve_structured_query

    by_id = {row["id"]: row for row in gold}
    if len(by_id) != len(gold) or set(by_id) != {row["id"] for row in cases}:
        raise ValueError("gold IDs must match cases exactly once")
    all_approved = True
    for case in cases:
        label = by_id[case["id"]]
        if set(label) != {"id", "target", "expected_resolution_status", "main_em", "human_review"}:
            raise ValueError("label keys differ from gold contract")
        target = QueryUnderstanding.model_validate(label["target"])
        # No silently defaulted labels: every target slot must be deliberately reviewed.
        if set(label["target"]) != set(QueryUnderstanding.model_fields):
            raise ValueError("gold target requires every top-level slot")
        if target.intent is not None and set(label["target"]["intent"]) != set(type(target.intent).model_fields):
            raise ValueError("gold intent requires all slots")
        if type(label["main_em"]) is not bool or label["main_em"] != (target.status == "ready"):
            raise ValueError("all ready targets and only ready targets enter main EM")
        expected = label["expected_resolution_status"]
        if expected not in {"ready", "clarify", "unsupported"}:
            raise ValueError("invalid expected resolver decision")
        actual = resolve_structured_query(case["question"], target, QueryPolicy.model_validate(case["policy"]),
                                          datetime.fromisoformat(case["now"]))
        if actual.status != expected:
            raise ValueError("gold resolver decision contradicts current business contract")
        review = label["human_review"]
        if not isinstance(review, dict) or set(review) != {"status", "annotator", "reviewer"}:
            raise ValueError("review requires status/annotator/reviewer")
        if review["status"] not in {"draft", "approved"}:
            raise ValueError("invalid human review status")
        if review["status"] == "approved":
            if not all(isinstance(review[k], str) and review[k].strip() for k in ("annotator", "reviewer")):
                raise ValueError("approved gold requires actual review identities")
            if review["annotator"].strip() == review["reviewer"].strip():
                raise ValueError("annotator cannot self-approve")
        else:
            all_approved = False
        if case["cohort"] == "fresh" and review["status"] != "approved":
            raise ValueError("Fresh must be reviewed before freeze")
    return "approved" if all_approved else "draft"


def load_package(path: Path, *, with_gold: bool = True) -> tuple[dict, list[dict], list[dict]]:
    """Prediction never opens private labels; scoring additionally validates their hash."""
    path = Path(path)
    manifest = read_json(path / "manifest.json")
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported package version")
    for filename, key in (("inputs.jsonl", "input_sha256"), ("runtime.json", "runtime_sha256"),
                          ("schema.json", "schema_sha256")):
        if sha256_file(path / filename) != manifest[key]:
            raise ValueError("frozen input/runtime/schema hash drift")
    for relative, expected in manifest["source_files_sha256"].items():
        source = (AGENT_ROOT / relative).resolve()
        if not source.is_relative_to(AGENT_ROOT) or sha256_file(source) != expected:
            raise ValueError("frozen source hash drift")
    cases = read_jsonl(path / "inputs.jsonl")
    validate_cases(cases)
    if [row["id"] for row in cases] != manifest["case_ids"] or any(row["split"] != manifest["split"] for row in cases):
        raise ValueError("frozen case list/split drift")
    gold = []
    if with_gold:
        if sha256_file(path / "private/labels.jsonl") != manifest["gold_sha256"]:
            raise ValueError("frozen gold hash drift")
        gold = read_jsonl(path / "private/labels.jsonl")
        if validate_gold(cases, gold) != manifest["review_status"]:
            raise ValueError("gold review status drift")
    return manifest, cases, gold
