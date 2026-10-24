"""Offline contracts for draft query seeds, never a model-quality evaluation."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from app.modules.conversation.query_understanding import QueryPolicy, QueryUnderstanding
from app.modules.conversation.structured_query_resolution import resolve_structured_query
from evaluation.query_semantics_common import validate_cases, validate_gold


DATA = Path(__file__).resolve().parents[1] / "evaluation/datasets/business_sql/query_semantics_v1"
CASES = [json.loads(line) for line in (DATA / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
LABELS = [json.loads(line) for line in (DATA / "labels.jsonl").read_text(encoding="utf-8").splitlines()]
BY_ID = {row["id"]: row for row in LABELS}


def test_seed_is_draft_development_only_and_has_no_invented_fresh():
    validate_cases(CASES)
    assert validate_gold(CASES, LABELS) == "draft"
    assert len(CASES) == 24 and len(BY_ID) == 24
    assert {row["split"] for row in CASES} == {"development"}
    assert {row["source"]["kind"] for row in CASES} == {"synthetic_development"}
    assert {row["cohort"] for row in CASES} == {"golden", "high_risk"}
    assert sum(row["main_em"] for row in LABELS) == 18
    assert all(row["human_review"] == {"status": "draft", "annotator": None, "reviewer": None}
               for row in LABELS)


@pytest.mark.parametrize("case", CASES, ids=[row["id"] for row in CASES])
def test_manual_target_matches_public_contract_and_expected_execution_decision(case):
    label = BY_ID[case["id"]]
    policy = QueryPolicy.model_validate(case["policy"])
    target = QueryUnderstanding.model_validate(label["target"])
    assert case["now"] == "2026-10-24T12:30:00+08:00"
    assert policy.default_limit == 5 and policy.max_limit == 100
    assert policy.scenario.default_limit == 10
    assert policy.data_kind == "synthetic"
    assert set(label["target"]) == set(QueryUnderstanding.model_fields)
    if target.intent is not None:
        assert set(label["target"]["intent"]) == set(type(target.intent).model_fields)
    result = resolve_structured_query(case["question"], target, policy, datetime.fromisoformat(case["now"]))
    assert result.status == label["expected_resolution_status"]
    assert label["main_em"] == (target.status == "ready")
    if not label["main_em"]:
        assert result.intent is None and not result.canonical_question


@pytest.mark.parametrize("case_id,start,end", [
    ("QS001", "2026-10-23T08:00:00+08:00", "2026-10-23T09:00:00+08:00"),
    ("QS004", "2026-10-22T06:00:00+08:00", "2026-10-22T07:00:00+08:00"),
    ("QS006", "2026-10-20T10:00:00+08:00", "2026-10-20T11:00:00+08:00"),
    ("QS008", "2026-10-24T11:00:00+08:00", "2026-10-24T12:00:00+08:00"),
    ("QS011", "2026-10-23T23:00:00+08:00", "2026-10-24T00:00:00+08:00"),
    ("QS015", "2026-10-23T17:00:00+08:00", "2026-10-23T18:00:00+08:00"),
])
def test_fixed_clock_and_window_examples(case_id, start, end):
    case = next(row for row in CASES if row["id"] == case_id)
    result = resolve_structured_query(
        case["question"], BY_ID[case_id]["target"], QueryPolicy.model_validate(case["policy"]),
        datetime.fromisoformat(case["now"]),
    ).require_ready()
    assert result.requested_window_start.isoformat() == start
    assert result.requested_window_end.isoformat() == end


def test_normal_seed_covers_supported_slots_and_negative_conditions_stay_out_of_main_em():
    ready = [row["target"] for row in LABELS if row["main_em"]]
    assert {row["intent"]["sort_by"] for row in ready} == {
        "clicks", "ctr", "interactions", "impressions", "hot_score"
    }
    assert {row["intent"]["category"] for row in ready} == {
        None, "科技", "财经", "体育", "社会", "汽车", "教育", "娱乐"
    }
    assert {row["intent"]["content_type"] for row in ready} == {None, "article", "video"}
    assert {row["time_expression"] for row in ready} == {
        "sample", "today", "yesterday", "day_before_yesterday", "date", "latest"
    }
    assert {row["intent"]["sort_direction"] for row in ready} == {"asc", "desc"}
    assert {row["response_style"] for row in ready} == {"list", "summary"}
    assert sum(row["target"]["status"] == "unsupported" for row in LABELS) == 5
    assert sum(row["target"]["status"] == "clarify" for row in LABELS) == 1
    assert all(not row["main_em"] for row in LABELS if row["target"]["unsupported_conditions"])


def test_public_cases_do_not_include_gold_or_review_fields():
    for case in CASES:
        assert set(case) == {"id", "group_id", "split", "cohort", "question", "now", "policy", "source"}
        assert not {"target", "expected_resolution_status", "main_em", "human_review"}.intersection(case)
    assert BY_ID["QS005"]["target"]["explicit_date"] == "2026-10-19"
    assert BY_ID["QS020"]["target"]["hour_end"] == 10
    assert BY_ID["QS022"]["target"]["ranking_source"] == "enterprise_board"
    assert BY_ID["QS023"]["target"]["time_basis"] == "publication"
