"""Render existing offline measurements; never rerun models or SQL.

Uses Pillow only for chart rendering. Run with a Python containing Pillow and
pass --results, --cases and a new --output-dir. Input artifacts are read-only.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_metrics(results_path: Path, cases_path: Path) -> dict:
    summary = json.loads(results_path.read_text(encoding="utf-8"))
    cases = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if summary["execution_status"] != "completed" or not summary["source_unchanged"]:
        raise ValueError("only complete, identity-verified measurements may be rendered")
    if len(cases) != summary["expected_records"] or len(cases) != summary["completed_records"]:
        raise ValueError("case denominator mismatch")
    public = [row for row in cases if row["suite"] == "sqlshield_question_screen_only"]
    matrix = Counter()
    for row in public:
        if type(row["malicious"]) is not int or row["malicious"] not in (0, 1) or type(row["observed_blocked"]) is not bool:
            raise ValueError("unknown public label or screening decision")
        matrix[(row["malicious"], row["observed_blocked"])] += 1
    tp, fn = matrix[(1, True)], matrix[(1, False)]
    fp, tn = matrix[(0, True)], matrix[(0, False)]
    source = summary["suites"]["sqlshield_question_screen_only"]
    if any(source[key] != value for key, value in {"tp": tp, "fn": fn, "fp": fp, "tn": tn}.items()):
        raise ValueError("confusion counts differ from original results")
    if not tp + fn or not fp + tn:
        raise ValueError("both malicious and benign cohorts are required")
    suites = {}
    for name in ("assistant_sql_guard", "service_contract", "generic_sql_guard"):
        rows = [row for row in cases if row["suite"] == name]
        passed = sum(row["passed"] for row in rows)
        if len(rows) != summary["suites"][name]["count"] or passed != summary["suites"][name]["passed"]:
            raise ValueError(f"contract counts differ: {name}")
        suites[name] = {"passed": passed, "count": len(rows), "rate": passed / len(rows)}
    contracts = [row for row in cases if row["suite"] == "service_contract" and row["expected"] == "block"]
    metric_groups = {}
    for name in ("assistant_sql_guard", "generic_sql_guard"):
        rows = [row for row in cases if row["suite"] == name and row["group"] == "metric_integrity"]
        metric_groups[name] = {"passed": sum(row["passed"] for row in rows), "count": len(rows)}
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn)
    return {
        "source_run_id": summary["run_id"],
        "input_sha256": {"results": sha256(results_path), "cases": sha256(cases_path)},
        "public_screen_only": {"count": len(public), "tp": tp, "fn": fn, "fp": fp, "tn": tn,
                               "malicious_block_rate": recall, "malicious_miss_rate": fn / (tp + fn),
                               "benign_false_block_rate": fp / (fp + tn), "benign_allow_rate": tn / (fp + tn),
                               "precision_descriptive_only": precision,
                               "f1_descriptive_only": 2 * tp / (2 * tp + fp + fn)},
        "contract_suites": suites,
        "metric_integrity": metric_groups,
        "service_attack_probes": {"count": len(contracts), "blocked_before_warehouse_stub": sum(row["passed"] and row["warehouse_stub_calls_after_attack"] == 0 for row in contracts)},
        "not_run": ["real-model end-to-end attack success rate", "PostgreSQL/RLS permission enforcement", "benign query execution accuracy", "production latency/cost"],
        "scope": "Existing offline evidence visualization; no new inference, database execution or acceptance decision",
    }


def render(metrics: dict, output: Path, font_path: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont

    scale = 2
    image = Image.new("RGB", (1800 * scale, 1120 * scale), "#F3F6FA")
    draw = ImageDraw.Draw(image)
    ink, muted = "#15263F", "#607189"
    green, red, amber, track = "#187F77", "#CC5764", "#B97816", "#E8EDF3"

    def rect(box, color, radius=18):
        draw.rounded_rectangle(tuple(round(x * scale) for x in box), radius=radius * scale, fill=color)

    def text(x, y, value, size=24, fill=ink, anchor=None):
        font = ImageFont.truetype(str(font_path), size * scale)
        draw.text((round(x * scale), round(y * scale)), value, font=font, fill=fill, anchor=anchor)

    rect((0, 0, 1800, 1120), "#F3F6FA", 0)
    text(80, 52, "Text2SQL 安全评测结果", 48)
    text(82, 120, f"既有结果可视化  ·  {metrics['source_run_id']}", 23, muted)
    chips = [(80, 430, f"公开问题  {metrics['public_screen_only']['count']:,}"),
             (454, 785, f"项目探针  {sum(x['count'] for x in metrics['contract_suites'].values())}"),
             (810, 1305, "真实模型 / 数据库  未运行")]
    for left, right, label in chips:
        rect((left, 177, right, 228), "#E5ECF5", 12)
        text(left + 20, 187, label, 23)

    rect((80, 263, 866, 772), "white")
    rect((900, 263, 1720, 772), "white")
    text(113, 294, "输入筛查｜SQLShield", 31)
    text(113, 340, "分别看恶意拦截与正常误伤，不能合成总分", 21, muted)
    public = metrics["public_screen_only"]
    for y, title, n, good, bad, good_label, bad_label in [
        (410, "恶意问题", public["tp"] + public["fn"], public["tp"], public["fn"], "拦截", "漏过"),
        (575, "正常问题", public["tn"] + public["fp"], public["tn"], public["fp"], "放行", "误拦"),
    ]:
        text(115, y, f"{title}  ·  {n:,} 条", 25)
        left, right = 115, 828
        boundary = left + (right - left) * good / n
        rect((left, y + 48, right, y + 92), red, 9)
        rect((left, y + 48, boundary, y + 92), green, 9)
        text(115, y + 109, f"{good_label}  {good:,} / {n:,}  ({good/n:.2%})", 22, green)
        text(828, y + 109, f"{bad_label}  {bad:,} / {n:,}  ({bad/n:.2%})", 22, red, "ra")

    text(934, 294, "执行边界｜项目合成用例", 31)
    text(934, 340, "契约通过数（含合法正例），仅代表本轮样本", 21, muted)
    for y, label, key in [(410, "严格 SQL 护栏", "assistant_sql_guard"),
                           (522, "Service 身份 / 快照检查", "service_contract"),
                           (634, "通用 SQL 护栏", "generic_sql_guard")]:
        item = metrics["contract_suites"][key]
        color = green if item["passed"] == item["count"] else amber
        text(935, y, label, 25)
        text(1684, y, f"{item['passed']}/{item['count']}  ·  {item['rate']:.1%}", 25, color, "ra")
        rect((935, y + 48, 1684, y + 77), track, 7)
        rect((935, y + 48, 935 + 749 * item["rate"], y + 77), color, 7)

    rect((80, 804, 1720, 968), "#FFF0D7")
    text(112, 832, "优先处理：通用护栏的指标口径保护", 30, "#88530C")
    strict = metrics["metric_integrity"]["assistant_sql_guard"]
    generic = metrics["metric_integrity"]["generic_sql_guard"]
    text(112, 882, f"指标污染探针：通用阻断 {generic['passed']}/{generic['count']}；严格护栏阻断 {strict['passed']}/{strict['count']}。", 25, "#88530C")
    text(112, 923, "通用护栏允许点击量乘 1000 或改成常量；只读与租户合法不足以保证数字正确。", 23, "#88530C")
    text(82, 1000, "边界：输入漏检不等于端到端攻击成功；替身数仓零越权调用不等于真实数据库权限已验收。", 23, muted)
    text(82, 1040, "来源：results.json + cases.jsonl；图中无真实模型 ASR、正常 SQL 执行正确率或生产时延/成本。", 22, muted)
    image.save(output, "PNG", optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"))
    args = parser.parse_args()
    metrics = load_metrics(args.results, args.cases)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    output = args.output_dir / "text2sql-security-results.png"
    render(metrics, output, args.font)
    (args.output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    import PIL
    manifest = {"argv": sys.argv, "cwd": str(Path.cwd()), "renderer_sha256": sha256(Path(__file__)),
                "input_paths": {"results": str(args.results.resolve()), "cases": str(args.cases.resolve())},
                "input_sha256": metrics["input_sha256"], "Pillow": PIL.__version__, "font": str(args.font),
                "artifacts": {path.name: sha256(path) for path in args.output_dir.iterdir() if path.is_file()},
                "scope": metrics["scope"]}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(str(output.resolve()))


if __name__ == "__main__":
    main()
