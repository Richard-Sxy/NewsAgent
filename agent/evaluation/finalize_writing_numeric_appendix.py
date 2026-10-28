"""Deterministic numeric appendix from public-source snapshots; no gold/model call."""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
from datetime import datetime, timezone

def read(p):
    return json.loads(p.read_text(encoding="utf-8"))

def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()

def save(p, value):
    with p.open("x",encoding="utf-8") as f:
        json.dump(value,f,ensure_ascii=False,indent=2)
        f.write("\n")

def cell(value):
    return str(value).replace("|","\\|").replace("\n"," ")

def render(snapshot):
    lines=["", "", "## 可复算数值附表", "",
        "以下数值由Python根据给定来源原文计算；近似输入的派生结果仍为近似值，未作独立来源核真。", "",
        "| 指标与期间 | 前值 | 后值 | 绝对变化 | 相对变化 | 来源 |",
        "| --- | --- | --- | --- | --- | --- |"]
    for metric in snapshot["metrics"]:
        relative=metric.get("relative_change")
        period=" → ".join(str(x) for x in (metric.get("from_period"),metric.get("to_period")) if x)
        label=metric["metric_label"] + ("（"+period+"）" if period else "")
        if metric["approximate"]:
            label += "（近似）"
        url=metric["evidence"]["source_url"]
        source_id=metric["evidence"]["source_id"]
        values=[label,metric["base_value"]+metric["unit"],metric["current_value"]+metric["unit"],
                metric["absolute_change"]["display"],relative["display"] if relative else "不可计算",
                "["+source_id+"]("+url+")"]
        lines.append("| "+" | ".join(cell(v) for v in values)+" |")
    lines.extend(["", "亏损规模扩大不等于利润增长；百分点差与相对增幅分别列示。", ""])
    return "\n".join(lines)

def finalize(run_dir, out):
    if out.exists():
        raise ValueError("new output directory required")
    execution=read(run_dir/"execution.json")
    if execution.get("completed_candidate_drafts")!=2:
        raise ValueError("two completed candidate drafts required")
    plan=read(run_dir/"plan.json")
    snapshots=read(run_dir/"metric-snapshots.json")
    if sha(run_dir/"metric-snapshots.json")!=plan["snapshots_sha256"]:
        raise ValueError("snapshot drift")
    inputs=read(run_dir/"inputs.json")
    expected={r["case_id"] for r in inputs}
    if set(snapshots)!=expected or len(expected)!=2:
        raise ValueError("paired task set drift")
    files=[]
    for case in expected:
        for arm in ("baseline","candidate"):
            p=run_dir/"cases"/case/arm/"draft.md"
            meta=read(p.parent/"metadata.json")
            if sha(p)!=meta["draft_sha256"]:
                raise ValueError("source draft changed")
            files.append(p)
    out.mkdir(parents=True)
    scope="development raw-material writing versus public-source Python metrics + writing + deterministic numeric appendix; not native RWR"
    frozen={"status":"preregistered_before_assembly","prepared_at":datetime.now(timezone.utc).isoformat(),
        "scope":scope,"source_run":str(run_dir.resolve()),"source_plan_sha256":sha(run_dir/"plan.json"),
        "gold_reference":plan["gold_reference"],"planned_tasks":2,"new_model_calls":0,
        "transformation":"Append every extracted public-source metric as a correctly unit-labelled table, preserving the model narrative and all original drafts.",
        "source_drafts_sha256":{str(p):sha(p) for p in files},"snapshots_sha256":sha(run_dir/"metric-snapshots.json"),
        "source_script_sha256":sha(Path(__file__).resolve()),"quality":"pending independent review","human_editing_minutes":None}
    save(out/"plan.json",frozen)
    shutil.copy2(run_dir/"inputs.json",out/"inputs.json")
    shutil.copy2(run_dir/"metric-snapshots.json",out/"metric-snapshots.json")
    for row in inputs:
        case=row["case_id"]
        for arm in ("baseline","candidate"):
            src=run_dir/"cases"/case/arm
            dst=out/"cases"/case/arm
            dst.mkdir(parents=True)
            content=(src/"draft.md").read_text(encoding="utf-8")
            if arm=="candidate":
                content += render(snapshots[case])
            with (dst/"draft.md").open("x",encoding="utf-8") as f:
                f.write(content)
            save(dst/"metadata.json",{"case_id":case,"event_group_id":row["event_group_id"],"arm":arm,
                "status":"completed","draft_sha256":sha(dst/"draft.md"),"scope":scope,
                "new_model_calls":0,"source_draft":str((src/"draft.md").resolve()),
                "source_draft_sha256":sha(src/"draft.md"),"has_deterministic_numeric_appendix":arm=="candidate"})
    result={"status":"assembled","planned_tasks":2,"paired_drafts":4,"new_model_calls":0,
        "scope":scope,"quality":"pending independent review","human_editing_minutes":None}
    save(out/"execution.json",result)
    return result

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-dir",type=Path,required=True)
    p.add_argument("--out",type=Path,required=True)
    a=p.parse_args()
    print(json.dumps(finalize(a.run_dir.resolve(),a.out.resolve()),ensure_ascii=False))
