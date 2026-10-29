"""Export a fixed writing run for human review and pre-registered editing.

No model calls, semantic scores, approvals, invented labels or timing events.
A failed or unknown arm remains in the planned denominator.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from evaluation.run_writing_batch import ARMS
from evaluation.verify_writing_run_portable import verify_portable as verify, status_portable as status

CRITERION = (
    "依据所给缓存材料，核对主体、时间、数字单位、来源归属、计划与已发生的区别；"
    "不补无依据的事实或因果，完整回应短稿任务，修订到可交付标准。"
    "阅读来源、核对和修改均计时，中断暂停；计时结束不等于质量审核通过。"
)


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path, obj):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def evidence_for(row):
    req = row["input"]["requirements"]
    result = [{"title": "本项任务与统一完成标准",
               "content": str(req["task_description"]) + "\n" + CRITERION, "url": ""}]
    result.extend({"title": doc["title"], "content": doc["content"], "url": doc["url"]}
                  for doc in req["source_documents"])
    return result


def export_review(run_dir, dataset, out):
    require(not out.exists(), "output exists; use a new review export")
    plan, rows = verify(run_dir, dataset)
    execution = status(run_dir, dataset)
    inputs = [json.loads(line) for line in (dataset / "inputs.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    source_rows = {r["case_id"]: r for r in inputs}
    require(all(source_rows.get(row["case_id"]) == row for row in rows),
            "dataset no longer matches frozen run")
    gold_file = dataset / "private" / "gold-review.draft.json"
    gold = read(gold_file)
    require(gold.get("status") == "draft", "this exporter requires the original draft gold")
    gold_sha = digest(gold_file)
    require(plan.get("gold_reference", {}).get("sha256") == gold_sha,
            "draft gold differs from pre-generation reference")
    subset_file = dataset / "editing-subset.json"
    subset = read(subset_file)
    require(plan.get("editing_subset_sha256") == digest(subset_file),
            "editing subset differs from pre-generation selection")
    ids = subset.get("case_ids")
    require(isinstance(ids, list) and len(ids) == 10 and len(set(ids)) == 10,
            "exactly ten pre-registered unique editing pairs required")
    by_id = {row["case_id"]: row for row in rows}
    require(set(ids) <= by_id.keys(), "editing subset not included in frozen run")
    require(len({by_id[case]["event_group_id"] for case in ids}) == 10,
            "editing subset requires ten distinct candidate groups")
    arms_by_case = {}
    records = []
    tasks = {}
    for row in rows:
        case = row["case_id"]
        arms_by_case[case] = {}
        for arm in ARMS:
            directory = run_dir / "cases" / case / arm
            meta_file = directory / "metadata.json"
            meta = read(meta_file) if meta_file.exists() else None
            state = (meta or {}).get("status")
            if not state:
                state = "unknown" if (directory / "start.json").exists() else "not_run"
            draft_file = directory / "draft.md"
            draft_sha = None
            if state == "completed":
                require(draft_file.is_file() and digest(draft_file) == meta.get("draft_sha256"),
                        "completed draft hash mismatch")
                draft_sha = digest(draft_file)
            record = {
                "case_id": case, "event_group_id": row["event_group_id"], "split": row["split"],
                "arm": arm, "generation_status": state,
                "draft_path": str(draft_file.relative_to(run_dir)) if draft_sha else None,
                "draft_sha256": draft_sha, "status": "pending_human_review",
                "critical_error": None, "required_fact_coverage": None,
                "numeric_applicability": None, "numeric_error": None,
                "source_attribution_error": None, "chronology_or_planned_status_error": None,
                "unsupported_conclusion": None, "meets_delivery_criterion": None,
                "error_evidence": [], "reviewer": None, "reviewed_at": None, "notes": "",
            }
            records.append(record)
            arms_by_case[case][arm] = state
            if case in ids and state == "completed":
                blind = "W" + hashlib.sha256(
                    (digest(run_dir / "plan.json") + "|" + case + "|" + arm).encode()).hexdigest()[:12]
                tasks[(case, arm)] = {
                    "blind_id": blind, "case_id": case, "event_group_id": row["event_group_id"],
                    "arm": arm, "source_run_id": run_dir.name + "/" + arm,
                    "title": row["input"]["topic"],
                    "draft_content": draft_file.read_text(encoding="utf-8"),
                    "evidence": evidence_for(row),
                }
    ready = all(arms_by_case[case].get(arm) == "completed" for case in ids for arm in ARMS)
    out.mkdir(parents=True, exist_ok=False)
    write_new(out / "review-template.json", {
        "schema_version": 1, "status": "pending_human_review",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "run_dir": str(run_dir.resolve()), "plan_sha256": digest(run_dir / "plan.json"),
        "dataset_path": str(dataset.resolve()), "gold_status": "draft", "gold_sha256": gold_sha,
        "planned_tasks": len(rows),
        "planned_event_groups": len({r["event_group_id"] for r in rows}),
        "planned_requests": len(records), "quality_scoring": "not_performed",
        "reviewer": None, "reviewed_at": None, "criterion_text": CRITERION,
        "instructions": [
            "First review event boundaries, relevance and required facts in private draft gold.",
            "Do not mark unknown or unreviewed samples as correct; preserve null.",
            "For an error cite the exact draft quote and relevant source_id/quote/Unicode offsets.",
            "Number applicability is covered/not_applicable/unsupported/ambiguous, not an empty-list pass.",
            "Report generation failures separately and keep every planned arm in the task accounting.",
            "Do not infer editing minutes from model generation latency.",
            "This pending template cannot support a resume claim of a measured human quality gain.",
        ], "records": records,
    })
    if ready:
        # Counterbalance the first arm within ten pairs and separate counterparts.
        order = [(case, ARMS[index % 2]) for index, case in enumerate(ids)]
        order += [(case, ARMS[1 - index % 2]) for index, case in enumerate(ids)]
        write_new(out / "editing-tasks.json", {
            "schema_version": 1, "experiment_id": run_dir.name + "-editing10",
            "gold_sha256": gold_sha, "gold_status": "draft",
            "criterion_version": "source-bound-short-writing-v1",
            "criterion_text": CRITERION, "data_origin": "human_recorded",
            "tasks": [tasks[key] for key in order],
        })
    receipt = {
        "schema_version": 1, "status": "ready" if ready else "blocked_missing_selected_drafts",
        "pre_registered_case_ids": ids, "subset_sha256": digest(subset_file),
        "planned_pairs": 10, "planned_drafts": 20,
        "completed_selected_drafts": len(tasks), "replacement_of_failed_pairs": False,
        "human_editing_time": None, "gold_status": "draft",
        "instructions": "No timers have started. A human must read, edit, finish and review.",
    }
    write_new(out / "editing-readiness.json", receipt)
    write_new(out / "generation-summary.json", {
        key: value for key, value in execution.items() if key != "requests"
    })
    text = (
        "# 100 项写作评测审核入口\n\n"
        "review-template.json 保留全部计划 A/B 稿，所有质量字段仍待人工审核。\n"
        "先在数据集 human-review.md 中核对事件边界与金标，再逐稿填写引用证据。\n"
        "editing-readiness.json 使用生成前登记的 10 对；失败样本不自动替换。\n\n"
        "若 editing-tasks.json 已就绪，在 agent 目录运行：\n\n"
        "    python -m evaluation.manual_editing_server --package <本目录>/editing-tasks.json "
        "--state-dir <本目录>/manual-editing-state --port 18882\n\n"
        "真人点击开始后才计时；阅读、核查、修改均计时，中断暂停。"
        "gold_status=draft 不会自动变成已审核，旧两例的批准不覆盖这些材料。\n"
        "未知质量不记零错；完成生成不等于完成语义评分或生产验收。\n"
    )
    with (out / "README.md").open("x", encoding="utf-8") as stream:
        stream.write(text)
    return {"status": "exported", "planned_tasks": len(rows), "planned_requests": len(records),
            "editing_pairs_ready": ready, "out": str(out.resolve())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = export_review(args.run_dir.resolve(), args.dataset.resolve(), args.out.resolve())
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(2, "Review export blocked: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
