"""Single-page SQL and Agent call-chain diagram, bound to live v8 examples."""

from __future__ import annotations

import argparse
import ast
from decimal import Decimal
import json
from pathlib import Path
from render_real_news_warehouse_pdf import (
    Diagram, W, H, INK, MUTED, GREEN, BLUE, AMBER,
    PALE_GREEN, PALE_BLUE, PALE_AMBER, GRAY, white, HexColor,
)


def default_weights() -> list[Decimal]:
    # Inspect literal defaults without importing the full Agent dependency tree.
    source = Path(__file__).resolve().parents[1] / "app/analytics/hot_score.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    fields = ("click_weight", "effective_consumption_weight", "interaction_weight", "growth_weight")
    configuration = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "HotScoreConfig")
    values = {}
    for node in configuration.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in fields:
            value = node.value
            if (not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name)
                    or value.func.id != "Decimal" or len(value.args) != 1
                    or not isinstance(value.args[0], ast.Constant) or not isinstance(value.args[0].value, str)):
                raise ValueError("hot score defaults require explicit Decimal literals")
            values[node.target.id] = Decimal(value.args[0].value)
    weights = [values[name] for name in fields]
    if sum(weights) != 1 or any(not 0 <= value <= 1 for value in weights):
        raise ValueError("invalid source hot score weights")
    return weights


def render(evidence: Path, output: Path) -> None:
    snapshots = {name: json.loads((evidence / name / "snapshot.json").read_text())
                 for name in ("hourly", "ranking-clicks", "five-minutes", "channels", "daily", "baseline", "window-18")}
    hourly = snapshots["hourly"]
    identities = {row["identity"]["dataset_sha256"] for row in snapshots.values()}
    if len(identities) != 1 or any(row["status"] != "passed" for row in snapshots.values()):
        raise ValueError("SQL diagram requires successful examples from one frozen dataset")
    sample = hourly["rows"][0]
    if (sample["news_id"] != "20260903A05YQE00" or sample["snapshot_revision"] != 2
            or hourly["gate"]["quality_status"] != "passed"):
        raise ValueError("real news example must use the verified corrected snapshot")
    diagnostic = snapshots["window-18"]
    if diagnostic["rows"][0]["quality_status"] != "pending" or not diagnostic["diagnostic_only"]:
        raise ValueError("latest incomplete diagnostic example is required")
    weights = [f"{value*100:.0f}%" for value in default_weights()]
    d = Diagram(output)
    d.c.setTitle("NewsAgent - PostgreSQL SQL 与热点新闻全链路")
    d.c.setSubject("v8固定查询已验证；现有corpus-v6 Agent链路与v8适配边界")
    d.c.setFillColor(HexColor("#FAFCFD"))
    d.c.rect(0, 0, W, H, fill=1, stroke=0)
    d.text("NEWSAGENT / SQL & HOT NEWS PIPELINE / 2026-10-15", 32, 25, 10, GREEN, True)
    d.text("用 PostgreSQL SQL 查候选，再用 Python 算热点", 32, 49, 27, INK, True)
    d.text("SQL 取数与过滤  →  Python 确定性排名  →  模型依据正文与关联证据解释", 32, 96, 14, MUTED)
    d.box(32, 124, 1125, 30, PALE_AMBER)
    d.text("真实新闻 · 模拟行为指标   |   v8查询可运行；现有Agent使用corpus-v6，v8适配仍待接入。", 45, 132, 11.5, AMBER)

    d.text("A  v8 现在可运行：固定参数化只读查询", 32, 175, 17, GREEN, True)
    top = (
        (32, "1 输入查询参数", "query=ranking / sort=clicks\nhour=2026-10-14 19:00+08\n租户 + Top20；详情传news_id", "QueryRequest → compile_query"),
        (322, "2 先检查最新窗口", "最新 snapshot_revision\ncomplete/corrected + passed\nwatermark ≥ window_end", "18:00最新pending，明确阻断"),
        (612, "3 PostgreSQL 执行", "固定 SQL + 分离绑定参数\nJOIN 同租户 / 同小时 / 同修订\n过滤 eligible_news", "REPEATABLE READ · READ ONLY"),
        (902, "4 输出事实快照", "候选新闻 + 指标 + 修订\n保存 SQL / 参数 / 版本摘要\n点击UV、访问UV各自保留", "候选排序不等于最终热度"),
    )
    for x, title, body, note in top:
        d.box(x, 208, 255, 155, PALE_GREEN, GREEN)
        d.text(title, x+15, 222, 16, GREEN, True)
        d.para(body, x+15, 252, 227, 11.5, 56)
        d.para(note, x+15, 328, 227, 10, 29, MUTED)
        if x < 902:
            d.arrow(x+255, 283, x+282, 283, GREEN)
    d.text("7类模板：排行 · 小时详情 · 五分钟 · 渠道 · 每日 · 基线 · 窗口诊断", 32, 382, 11.5, MUTED)

    d.text("B  现有 Agent 的完整链路：corpus-v6 已有实现", 32, 420, 17, BLUE, True)
    steps = (
        (32, "1 理解问句", "身份后端注入\nQueryUnderstanding\n确认时间/指标/范围"),
        (225, "2 编译和护栏", "compile_query\nPostgreSQL AST\n批准视图 + 参数绑定"),
        (418, "3 绑定与取数", "幂等 run_key\nTemporal Workflow\n候选 + 同小时指标"),
        (611, "4 计算与证据", "HotNewsRanker\n四分量权威热度\n真实正文 + BGE召回"),
        (804, "5 模型和校验", "HotNewsAnalysisInput\n模型生成解释\n身份/指标/证据校验"),
        (997, "6 同运行保存", "analysis_runs\n报告 + 输入 + SQLtrace\nAPI / 聊天 / SSE"),
    )
    for x, title, body in steps:
        d.box(x, 457, 160, 119, PALE_BLUE, BLUE)
        d.text(title, x+12, 470, 13.5, BLUE, True)
        d.para(body, x+12, 501, 137, 10.8, 55)
        if x < 997:
            d.arrow(x+160, 516, x+184, 516, BLUE)
    d.text("聊天已确认意图时，Python独立复核并直接编译；无需再次调用Text2SQL模型。", 32, 591, 10.5, MUTED)

    d.box(32, 624, 383, 142, white, GREEN)
    d.text("一条真实新闻的查询结果", 48, 638, 16, GREEN, True)
    d.para(sample["title"], 48, 668, 352, 11.5, 35)
    d.text(f"模拟点击 {sample['clicks']} · 访问PV {sample['detail_page_views']} · 访问UV {sample['detail_visitors']}", 48, 714, 12, INK, True)
    ratio = Decimal(sample["clicks"]) / Decimal(sample["impressions"]) * 100
    d.text(f"19:00 rev2 · CTR {ratio:.2f}% · 源码/SQL/参数可追溯", 48, 742, 10.2, MUTED)

    d.box(435, 624, 349, 142, PALE_BLUE, BLUE)
    d.text("最终热度的默认四分量", 451, 638, 16, BLUE, True)
    d.para(f"{weights[0]} 点击/曝光 + {weights[1]} 有效消费/点击\n{weights[2]} 互动/有效消费 + {weights[3]} 历史增长分\n比例限制[0,1]；正增长压缩为g/(1+g)", 451, 673, 316, 11.3, 54)
    d.para("SQL候选分与最终分不同；先截TopN再重排，\n只能得到候选池内排名。", 451, 732, 316, 9.8, 30, MUTED)

    d.box(804, 624, 353, 142, PALE_AMBER, AMBER)
    d.text("v8 → Agent 还需补的接口", 820, 638, 16, AMBER, True)
    d.para("注册profile / schema / 场景 / dataset摘要\n水位门禁 + NULL基线 + 人数/时长口径\nPV/访问UV旁车写入同一次SQLtrace", 820, 672, 320, 11.2, 56)
    d.text("真实撤稿与向量清理执行器仍待实现。", 820, 742, 10.1, AMBER)

    d.text("UV不跨渠道/小时/日期/新闻相加；CTR零分母为NULL；日历史partial与截断均如实显示。", 32, 786, 10, MUTED)
    d.text("参数语法：SQLAlchemy :name / Psycopg %(name)s  |  PostgreSQL 16 SQL  |  Dataset " + next(iter(identities))[:16] + "…", 32, 808, 9.6, MUTED)
    d.c.linkURL("https://www.postgresql.org/docs/16/queries.html", (32, 12, 470, 32), relative=0)
    d.c.showPage()
    d.c.save()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    render(args.evidence, args.output)
    print(args.output.absolute())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
