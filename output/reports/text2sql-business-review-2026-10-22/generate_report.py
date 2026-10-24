"""Render a business-readable report from preserved development replay evidence.

No model or warehouse calls. Input runs stay immutable. HTML bars repeat the
exact numerator/denominator from results.json; they are not online risk rates.
"""

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
EVAL = ROOT / "output/evaluation"
BASE = "2026-10-22-metric-contract-baseline-01"
FINAL = "2026-10-22-metric-contract-candidate-02"


def read_run(name):
    directory = EVAL / name
    summary = json.loads((directory / "results.json").read_text())
    cases = {row["id"]: row for row in map(json.loads, (directory / "cases.jsonl").read_text().splitlines())}
    spec = json.loads((directory / "run-spec.json").read_text())
    return summary, cases, spec


baseline, old_cases, old_spec = read_run(BASE)
candidate, new_cases, new_spec = read_run(FINAL)
assert old_cases.keys() == new_cases.keys()
assert old_spec["dataset"]["sha256"] == new_spec["dataset"]["sha256"]
assert candidate["completed"] == candidate["planned"] == 14
assert candidate["status"] == "passed"

labels = {
    "normal-hour": ("正常：查一个小时的新闻指标", "每条新闻的数字应与批准聚合行相同"),
    "normal-alias-order": ("正常：调整列顺序和同名别名", "表现形式变化不应导致拒绝"),
    "normal-adjacent-hour": ("正常：查相邻的另一个完整小时", "按对应小时取数，不能拿上一小时冒充"),
    "scaled-clicks": ("点击量被乘以 1000", "news-a 的 100 变成 100,000，运营看数失真"),
    "constant-clicks": ("所有新闻点击量写成固定值", "不同新闻都变成 999,999，无法据此比较表现"),
    "distinct-clicks": ("跨两小时使用 SUM(DISTINCT clicks)", "相同点击数会被去掉一次；本例也违反单桶限制"),
    "missing-group": ("聚合时漏掉新闻分组", "SQLite 中多条新闻合成一条；PG 可能直接报错"),
    "wrong-group": ("按错误维度分组", "新闻归属和数字可能错配；PG 方言需独立验证"),
    "where-filter": ("悄悄增加 clicks > 100", "原本三条新闻只剩一条，候选范围被改变"),
    "having-filter": ("增加聚合后筛选条件", "可能隐藏低点击新闻，影响后续分析范围"),
    "missing-column": ("漏掉必需指标列", "旧实现执行数仓后才失败；现在执行前拒绝"),
    "extra-column": ("添加未批准的结果列", "基础数字虽未变，但结果超出批准合同"),
    "unknown-growth": ("请求尚未接入的增长率", "旧实现成功返回基础指标，却没有交付请求的 growth"),
    "cross-hour-uv": ("把两小时 UV 相加", "同一用户可能重复计数，聚合数无法反推出去重真值"),
}


def fraction(result, key):
    value = result["metrics"][key]
    return f'{value["count"]}/{value["denominator"]}'


def chart_row(title, key, lower):
    parts = []
    for name, result, style in [("改动前", baseline, "before"), ("改动后", candidate, "after")]:
        metric = result["metrics"][key]
        percent = metric["count"] / metric["denominator"] * 100
        parts.append(f'<div class="barrow"><span>{name}</span><div class="track"><div class="fill {style}" style="width:{percent:.6f}%"></div></div><b>{fraction(result, key)} · {percent:.1f}%</b></div>')
    return f'<div class="chart"><h3>{title} <small>{"越低越好" if lower else "越高越好"}</small></h3>{"".join(parts)}</div>'


rows = []
for key, current in new_cases.items():
    previous = old_cases[key]
    name, meaning = labels[key]
    expected = "正常返回" if current["expected"] == "allow" else "拒绝"
    old_label = "返回" if previous["observed"] == "allow" else "查询后拒绝"
    new_label = "正确返回" if current["observed"] == "allow" else "查询前拒绝"
    rows.append(f'<tr><td><code>{escape(key)}</code><br>{name}</td><td>{meaning}</td><td>{expected}</td><td>{old_label}</td><td class="good">{new_label}</td></tr>')

regression = {
    "recorded_at": datetime.now(timezone.utc).isoformat(),
    "note": "Post-run evidence index, not a preregistered run specification. Core output was observed in the tool transcript; no raw core log was captured.",
    "core_observed": {"passed": 215, "seconds": 212.84, "exit_code": 0, "pytest_args": ["tests/test_text2sql_metric_source.py", "tests/test_metric_sql_contract.py", "tests/test_sql_assistant_planner.py", "tests/test_sql_assistant_confirmed_intent.py", "tests/test_sql_assistant_service.py", "-o", "addopts=", "-p", "no:cacheprovider", "-q"], "metric_cases_at_this_stage": 46},
    "related": {"passed": 226, "skipped": 1, "evidence": "2026-10-22-metric-contract-regression-01", "skip_reason": "SQL_WAREHOUSE_INTEGRATION_URL was not set; isolated PostgreSQL integration was not run"},
    "final_metric": {"passed": 47, "evidence": "2026-10-22-metric-contract-regression-02", "note": "46 previous cases plus one DST regression; replaces the 46 earlier metric results"},
    "distinct_passed": 215 - 46 + 226 + 47,
    "test_environment": "Offline audit fence; Settings and VideoIngestSettings dotenv loading disabled before pytest.main; no enterprise model or database",
}
(HERE / "test-evidence.json").write_text(json.dumps(regression, ensure_ascii=False, indent=2) + "\n")

html = f'''<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>NewsAgent · SQL 业务问题与改进验证</title>
<style>
:root{{color-scheme:light;--ink:#153045;--muted:#536775;--line:#d9e4e8;--teal:#007e78;--orange:#c25a25}}
*{{box-sizing:border-box}} body{{margin:0;background:#f3f6f7;color:var(--ink);font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}
main{{max-width:1120px;margin:auto;padding:42px 30px 72px}} header{{border-top:6px solid var(--teal);padding:28px 0 20px}} h1{{font-size:34px;line-height:1.35;margin:10px 0}} h2{{font-size:24px;margin:0 0 16px}} h3{{font-size:18px;margin:0 0 12px}} p{{margin:12px 0}} small,.muted{{color:var(--muted)}} small{{font-weight:400;font-size:13px}} .eyebrow{{letter-spacing:1px;font-size:13px;color:var(--teal);font-weight:700}}
section{{background:white;border:1px solid var(--line);border-radius:12px;padding:28px;margin-top:22px}} .callout{{background:#e8f4f1;border-left:4px solid var(--teal);padding:14px 20px}} .note{{background:#fff5e9;border-left:4px solid var(--orange);padding:14px 20px}} a{{color:#006b88;text-decoration:underline;text-underline-offset:3px}} code,pre{{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}} code{{overflow-wrap:anywhere}} pre{{background:#eff4f6;padding:18px;white-space:pre-wrap;word-break:break-word;border-radius:6px;line-height:1.7}} table{{border-collapse:collapse;width:100%;font-size:14px}} th,td{{padding:12px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}} th{{background:#edf4f5}} .scroll{{overflow:auto}} .good{{color:#007469;font-weight:600}} .chart{{margin:24px 0}} .barrow{{display:grid;grid-template-columns:60px 1fr 136px;gap:12px;align-items:center;margin:10px 0;font-size:14px}} .track{{height:24px;background:#edf1f3;border-radius:4px;overflow:hidden}} .fill{{height:100%}} .before{{background:var(--orange)}} .after{{background:var(--teal)}} .barrow b{{font-variant-numeric:tabular-nums}} ul,ol{{padding-left:23px}} li{{margin:8px 0}} footer{{margin-top:28px;color:var(--muted);font-size:13px}} @media(max-width:640px){{main{{padding:20px 12px}}section{{padding:20px 15px}}h1{{font-size:27px}}.barrow{{grid-template-columns:48px 1fr 110px;gap:7px;font-size:12px}}}}
</style>
<main>
<header><div class="eyebrow">NEWSAGENT / 2026-10-22 / 本地代码复核</div><h1>SQL 查询能执行，<br>数字也必须符合业务口径</h1><p>按“企业新闻运营看数”的场景，复核前面的讨论、修正代码，并保留改动前后可复现的证据。</p><div class="callout"><b>结论：</b>已补通用指标查询的口径约束，并修复页面查询的栏目问题。14 个定向业务用例全部满足预期；核心与相邻回归累计覆盖 442 个通过用例，另 1 项 PostgreSQL 集成测试未运行。</div></header>

<section><h2>1. 哪些问题已确认，如何改进</h2><div class="scroll"><table><thead><tr><th>业务场景</th><th>原来的问题与影响</th><th>已完成的优化</th></tr></thead><tbody>
<tr><td>运营问“这个小时各新闻有多少点击”</td><td>通用 SQL 护栏允许合法的只读运算，可能把 100 算成 100,000，或者悄悄过滤部分新闻。</td><td>绑定服务端批准的完整查询：指标公式、过滤、分组、排序和上限都要一致。</td></tr>
<tr><td>问增长率，或统计多个小时的去重用户</td><td>请求的 growth 可能被静默丢弃；小时 UV 相加无法去重。跨时区夏令时还可能误算实际时长。</td><td>未支持指标提前拒绝；按 UTC 检查完整单桶；CTR 由 Python 计算；重复新闻行拒绝。</td></tr>
<tr><td>数仓已经给出“每新闻、每小时一行”</td><td>原模板读取裸指标列却附加 GROUP BY，普通 PostgreSQL 视图可能报错。</td><td>直接读已聚合的唯一行，不进行不必要分组；明细聚合需另外批准表达式。</td></tr>
<tr><td>运营查看汽车、教育或娱乐栏目</td><td>批准场景已有这些栏目，但本地第二次解析的词表只有旧栏目，正常请求被拒绝。</td><td>解析词表统一来自场景 allowed_categories；补合法与未批准栏目回归。</td></tr>
<tr><td>七栏目数仓，仅授权其中四栏目</td><td>旧代码按“数量等于四”判断全量，省略栏目时可能扩大到整张批准视图。</td><td>按数仓版本比较完整栏目集合；预览、执行和复用缓存前都检查范围。</td></tr>
</tbody></table></div><p class="note"><b>影响范围：</b>公式缺口位于显式装配的通用指标源，目前不是默认 Worker 或当前页面的取数实现。页面专用链已有固定公式和同小时权威数据交叉核对。本次没有据此声称线上榜单曾出错。</p></section>

<section><h2>2. 改动前后：同一批业务开发用例</h2><p>分母固定：<b>3 个正常查询 + 11 个应拒绝的异常请求</b>。执行真实应用函数和内存 SQLite 查询，生成器仅返回预设 SQL，真实模型调用为 0。</p>
{chart_row("异常请求仍被放行的比例", "bad_query_allowed", True)}
{chart_row("正常查询正确完成的比例", "normal_success", False)}
<div class="scroll"><table><thead><tr><th>指标与公式</th><th>改动前</th><th>改动后</th></tr></thead><tbody>
<tr><td>异常放行率＝异常且返回结果 / 应拒绝异常数</td><td>{fraction(baseline,'bad_query_allowed')}（90.9%）</td><td>{fraction(candidate,'bad_query_allowed')}（0%）</td></tr>
<tr><td>查询前拦截率＝未访问数仓即拒绝 / 应拒绝异常数</td><td>{fraction(baseline,'bad_query_blocked_before_warehouse')}</td><td>{fraction(candidate,'bad_query_blocked_before_warehouse')}</td></tr>
<tr><td>含公式偏差用例的放行数 / 该组用例数</td><td>{fraction(baseline,'bad_formula_allowed')}</td><td>{fraction(candidate,'bad_formula_allowed')}</td></tr>
<tr><td>正常成功率＝返回且基础数字完全正确 / 正常用例数</td><td>{fraction(baseline,'normal_success')}</td><td>{fraction(candidate,'normal_success')}</td></tr>
<tr><td>数仓实际调用次数（均为内存 SQLite）</td><td>{baseline['warehouse_calls']}</td><td>{candidate['warehouse_calls']}</td></tr>
</tbody></table></div><p class="note"><b>不能解释为“线上风险从 90.9% 降到 0%”。</b>这 11 个例子是围绕已知问题刻意构造的开发用例，不是生产请求随机抽样。线上发生率、真实模型理解错误率、真实业务损失均未测量。3/3 正常通过也不能证明所有正常表达都能处理。</p><p>“公式偏差”组中有一个 SUM(DISTINCT) 跨小时例子，改动后先因时间桶限制拒绝；并非所有例子都由同一层护栏拦截。单小时公式变更另由单元回归覆盖。</p></section>

<section><h2>3. 每个例子到底发生了什么</h2><div class="scroll"><table><thead><tr><th>场景 / 样本 ID</th><th>业务含义</th><th>应当</th><th>改动前</th><th>改动后</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p>点击量全部乘以相同倍数，不会改变单纯按点击量的先后顺序；可能改变数字解释和组合热度，但本轮没有测量最终热度榜变化。额外列、未知 growth 即使基础数字正确，也属于输出或请求合同失败，不能混称为“算错数字”。</p><p>SQLite 容许部分裸列聚合写法，PostgreSQL 可能直接报错；表内漏分组/错分组结果只说明本次模拟执行。跨小时 UV 没有用户明细真值，只验证是否按批准规则拒绝，未编造去重准确率。</p></section>

<section><h2>4. 优化后的代码链路与阅读位置</h2>
<p><b>通用指标查询：</b></p><pre>可信调用方 HotNewsMetricQuery
→ create_text2sql_metric_source 注入服务端 metric_contract
→ fetch_batch：指标支持范围 / UTC 完整时间桶
→ 批准模板生成 SQL；模板优先，模型候选必须有批准合同
→ SqlGuard：表列、租户、时间、参数、只读结构
→ validate_metric_sql_contract：完整查询与批准查询一致
→ SqlWarehouseClient.execute
→ 截断、重复行、内容类型和数值校验
→ SqlMetricBatch：口径版本/哈希 + NewsMetricSnapshot
→ Python 计算 CTR、热度和排行 → 下游分析</pre>
<p><b>页面栏目查询：</b>原始问题 → 结构化理解 → 受控规范问句与 Intent → preview 独立复核 → 场景栏目范围检查 → Python 编译 / Guard → 计划绑定 → execute 再查范围（含缓存）→ 只读数仓 → 同小时指标交叉核对 → 热度分析。</p>
<ul><li><a href="../../../agent/app/modules/hot_news/scoring/text2sql_metric_source.py">text2sql_metric_source.py</a>：契约、时间桶、取数入口和结果转换。</li><li><a href="../../../agent/app/modules/hot_news/scoring/metric_sql_contract.py">metric_sql_contract.py</a>：完整 SQL 比对；只容许格式、列顺序、同名冗余别名的差异。</li><li><a href="../../../agent/app/sql_assistant/planner.py">planner.py</a> 与 <a href="../../../agent/app/sql_assistant/service.py">service.py</a>：栏目解析与预览/执行范围复核。</li><li><a href="../../../agent/app/bootstrap/hot_news.py">bootstrap/hot_news.py</a> 和 <a href="../../../agent/app/schemas/text2sql.py">schemas/text2sql.py</a>：工厂注入、模型输入的批准 SQL/版本字段。</li></ul>
<p><b>配置：</b>默认契约版本 hot-news-metric-template-v2，bucket_seconds=3600；工厂沿用 text2sql_max_rows、text2sql_timeout_ms；栏目沿用场景 YAML 的 allowed_categories 与冻结数仓版本。仅有 generator 的旧调用方现在必须提供 metric_contract。已批准模板优先执行；未登记新指标时明确失败。</p><p><b>依赖与输出：</b>运行时代码继续使用 sqlglot、Pydantic、既有模型/数仓 Port，没有新增框架或依赖。表达式映射被复制冻结；SqlMetricBatch 增加口径版本和 SHA-256，便于追溯。本次修改保留在本地工作区，没有发布模型、Prompt 或生产配置。</p></section>

<section><h2>5. 如何复现，如何核验证据</h2><p>在仓库的 agent 目录使用已有虚拟环境，输出目录须是一个新目录：</p><pre>PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m evaluation.run_metric_contract_business \\
  --output-dir ../output/evaluation/metric-contract-new-run \\
  --require-mode candidate</pre><p>测试集位置：<a href="../../../agent/evaluation/datasets/metric_contract_business_v1.json">metric_contract_business_v1.json</a>。运行入口：<a href="../../../agent/evaluation/run_metric_contract_business.py">run_metric_contract_business.py</a>。合成数据按 tenant_id / news_id / content_type / 小时组织，只有 7 行聚合数据，不含企业用户行为明细。</p><p>Runner 先冻结样本、源码、依赖和预期，再调用应用真实链路；结果写入 run-spec.json、cases.jsonl、results.json、commands.json。离线 audit hook 禁止读取 .env 和连接外网。退出码 0 仅表示回放完成，应继续检查 results.status、逐例结果与分母。</p>
<ul><li><a href="../../evaluation/{BASE}/results.json">旧版汇总</a> · <a href="../../evaluation/{BASE}/cases.jsonl">旧版逐例</a> · <a href="../../evaluation/{BASE}/run-spec.json">旧版规格</a></li><li><a href="../../evaluation/{FINAL}/results.json">最终版汇总</a> · <a href="../../evaluation/{FINAL}/cases.jsonl">最终版逐例</a> · <a href="../../evaluation/{FINAL}/run-spec.json">最终版规格</a></li><li><a href="test-evidence.json">回归证据索引</a> · <a href="../../evaluation/2026-10-22-metric-contract-regression-01/pytest.log">相邻链路日志：226 passed，1 skipped</a> · <a href="../../evaluation/2026-10-22-metric-contract-regression-02/pytest.log">最终指标日志：47 passed</a></li></ul>
<p>最先核心回归为 215 passed，其中 46 项是指标测试；UTC 修正后指标测试为 47 passed，替换之前 46 项，再加相邻链路 226 项，独立通过用例数为 <b>215 − 46 + 47 + 226 = 442</b>。核心结果来自工具执行输出，未捕获原始日志，证据索引如实注明为运行后记录。1 项 PostgreSQL 测试因未配置 SQL_WAREHOUSE_INTEGRATION_URL 跳过，不能视为数据库验收通过。</p><p>旧版完整指标源保存在旧 run 的 evidence 目录；其他依赖按该次运行的哈希记录。最终版源码身份以最终 run-spec 为准。中间 candidate-01 保留，不覆盖旧报告和实验。样本 split 为 development，尚无独立业务标签审核；本轮不包含真实模型、企业数据库、Temporal 或生产流量。</p></section>

<section><h2>6. 前面讨论中，需要纠正的判断</h2><ol><li>通用 Guard 的弱点不能直接写成“当前页面已经可以篡改指标”。两个入口的护栏、装配关系不同。</li><li>两条刻意篡改用例中的 2/2 放行，只能说明该批用例未拦住；不能写成企业业务风险率 100%。</li><li>历史 SQLShield 的 455/964 正常问题误拒，衡量的是当时筛查函数。当前真实模型对话入口先做基础规范化，不能沿用为当前页面误拒率。</li><li>字段取值合法、SQL 安全、模型理解正确是三个不同问题。例如用户问曝光量，模型提案却选点击量，后续 SQL 可以完全合法但答非所问。</li><li>旧代码草案路径和“尚未实现”的状态已更正；通用公式契约现在已有代码和验证入口。</li></ol></section>

<section><h2>7. 下一轮应测什么</h2><div class="scroll"><table><thead><tr><th>优先顺序</th><th>业务问题</th><th>建议指标和分母</th><th>本轮状态</th></tr></thead><tbody>
<tr><td>1</td><td>用户问曝光，模型是否误选点击；日期、栏目、数量是否遗漏</td><td>意图完整匹配率＝全部必需条件正确 / 有人工标签的原始问题；另报各字段准确率、澄清率和错误放行率</td><td>待独立数据集与真实模型评测</td></tr>
<tr><td>2</td><td>企业实际字段、时长小数、数据水位和只读权限是否符合合同</td><td>指标正确任务数 / 全部计划任务；误拒率＝被拒正常请求 / 正常请求；失败保留在分母</td><td>真实 PostgreSQL/企业接口未测</td></tr>
<tr><td>3</td><td>Top-K 是否完整，候选上限是否隐藏漏数</td><td>最终 Python 热度榜 Top-K 集合与顺序一致率；候选完整性；达到 LIMIT 时不能仅凭 truncated=false 判完整</td><td>本轮只检查基础指标及点击量次序</td></tr>
<tr><td>4</td><td>线上的实际出错率、慢查询与成本</td><td>按栏目/问题类型分层抽样；报样本量、置信区间、P50/P95、超时率及每次调用成本</td><td>线上风险率未知</td></tr>
</tbody></table></div><p>跨小时 UV 需要企业提供真正的区间去重结果或批准的去重数据结构；不能通过相加小时计数或单纯放大 bucket_seconds 实现。通用源目前严格支持批准桶，这项限制是有意收紧。</p><p>另一个后续方向是 Memory 来源追踪：模型改写的记忆内容不应自动等同于用户原话；来源标记应跨检索与摘要保留。本轮未修改 Memory 策略，也未声称这类风险已被实际利用。</p></section>
<footer>完整工程说明：<a href="../../../agent/docs/text2sql-security.md">text2sql-security.md</a>。本报告由同目录 generate_report.py 读取保留证据生成；图中比例只描述该开发集。契约回归 passed；模型质量 not_run；企业合同 not_run；生产验收 not_approved。</footer>
</main></html>'''
(HERE / "report.html").write_text(html, encoding="utf-8")
print(HERE / "report.html")
