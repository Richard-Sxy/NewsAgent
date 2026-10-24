# 报告生成评测：你核对答案，脚本运行和汇总

分工已经更新：**你确认业务样本、标准答案与报告的语义质量；运行、结果留存、校验、评分及对照脚本已经实现。** 不再需要你实现 Python 核心函数。

当前没有运行真实模型、没有人工批准标签、没有质量成绩。已有的自动测试只验证评测代码。今天先完成腾讯和 OpenAI 两个开发主题；银行验证案例缺第二来源，暂不进入本轮分母。

## 今天的顺序

1. 核对 `private/gold-review.json`，确认原文、必需事实、数值口径和可用标准。
2. `prepare` 冻结两个案例、标签、模型配置、Prompt 和源码身份。
3. `run` 执行真实 Research → Outline → 全部章节 → Assemble → Reviewer。
4. 核对输出并填写人工评分表；`score` 自动检查证据及计算指标。
5. 如需优化，另存候选 Prompt 配置，同样运行、人工评分，最后 `compare`。

预计人工核对和评分需要约 1–2 小时，运行及修正另计；接口或 Schema 失败可能需要新一轮实验。两主题首轮、每篇两章最多 12 次调用；当前／候选合计最多 24 次，无自动重试、修订或批准。

## 1. 你现在只需核对这个文件

材料目录：`agent/evaluation/datasets/writing/report_pilot_v1/`。

- `private/gold-review.md`：易读核对稿，事实旁附统一新闻导出中的原文片段和数值公式，建议先看此文件；也标出了待确认的小数精度口径。
- `private/gold-review-source-audit.json`：与 `var/news-storage/exports/20261023/body-candidates.jsonl` 的来源匹配证据。当前 5 篇材料均与统一导出正文、标题、URL 和发布时间一致；它是准备审计，不是模型评分。
- `inputs.jsonl`：公开新闻正文与报告任务；只有每行的 `input` 进入模型。
- `private/gold-review.json`：已经生成的两例人工确认表。原始草案仍保留。
- `private/labels.draft.jsonl`：原事实标签及证据定位，可对照使用。
- `manifest.json`：原材料包身份。新生成的人工审核文件单独冻结，不修改旧 manifest。

审核 `gold-review.json` 中两例的这些内容：

| 字段 | 你要确认什么 |
| --- | --- |
| `source_facts` | statement 是否有正文依据；value、unit、source_url、locator 是否准确 |
| `required_fact_ids` | 报告必须覆盖哪些事实；当前两例合计 12 项 |
| `numeric_checks` | 公式、前后值、单位、容差是否符合业务；当前两例合计 6 项 |
| `forbidden_claims` / `required_caveats` | 哪些推断不允许、哪些不确定性必须说明 |
| `critical_error_definition` / `usable_definition` | 严重错误和可交付报告的定义；已填建议文字，需你确认或修改 |

`locator` 是正文字符区间，往往只圈出数值，**不能代替阅读上下文**。例如区分经营利润与现金流、季度与半年、百分点与百分比；OpenAI 近似基数算得的 17.54% 与报道的 18% 不一定矛盾。

确认全部选定案例后，你填写顶层 `reviewer`、带时区的真实 `reviewed_at`，将 `status` 从 `pending_human_review` 改为 `approved`。这里表示你确认评测标签，不是生产批准。脚本不会替你填写。

不用改 `inputs_sha256` 或生成的引用 ID。需要改正文、主题、as_of 或样本分组时，另建数据版本，再生成新的 gold 表。原缓存未独立核真，且是十月抓取的内容；本轮衡量“相对于给定缓存材料的一致性”，不声称八月历史在线效果。

若以后需要另一份新表，在 `agent/` 运行（输出文件必须不存在）：

```bash
.venv/bin/python -B -m evaluation.run_report_pilot init-labels \
  --output-file evaluation/datasets/writing/report_pilot_v1/private/gold-review-v2.json
```

## 2. 冻结并运行当前方案

以下命令从 `/Users/shi/Project/myProject/NewsAgent/agent` 执行。先完成人工确认，再运行：

```bash
.venv/bin/python -B -m evaluation.run_report_pilot prepare \
  --gold-file evaluation/datasets/writing/report_pilot_v1/private/gold-review.json \
  --output-dir ../output/evaluation/2026-10-24-report-baseline-01

.venv/bin/python -B -m evaluation.run_report_pilot run \
  --run-dir ../output/evaluation/2026-10-24-report-baseline-01
```

`run` 是唯一访问网络的子命令。它使用已经设置的 `DEEPSEEK_API_KEY`，或在交互终端隐藏输入密钥；不会读取 `.env`，不会把密钥写进结果。默认配置为 `evaluation/config/report-pilot.deepseek.yml`，使用 DeepSeek Flash，单次超时 120 秒、全局 1800 秒、并发 1、最大输出 8192 token/次。调用次数和输出上限受控，**没有人民币费用上限或价格估计**。

`prepare` 记录输入、标签、配置、Prompt、源码、依赖和固定分母。修改任何冻结输入或相关执行代码后，必须新建运行目录。已开始的 run 禁止重复执行；中断也不会偷偷重试。`completed` 只表示技术阶段运行完成，不等于报告正确或审批通过。

尚未确认标签时可以用 `prepare --exploratory` 调试链路，但这轮不能事后改标签变成正式质量成绩。确认标签后另建运行并重新执行。

## 3. 填写运行后人工评分表

运行完成后，在 run 目录生成 `human-review.template.json`。复制为新文件 `human-review.json`，逐例查看 `cases/<case_id>/` 的结构化输出。也可通过以下命令生成新表：

```bash
.venv/bin/python -B -m evaluation.run_report_pilot review-template \
  --run-dir ../output/evaluation/2026-10-24-report-baseline-01 \
  --output-file ../output/evaluation/2026-10-24-report-baseline-01/human-review.json
```

人工字段这样填：

| 字段 | 填写要求 |
| --- | --- |
| `fact_checks[].correct` | true/false；正确时粘贴 Research 的 claim/evidence 原句到 output_quote；错误或遗漏填 evidence_note |
| `numeric_checks[].correct_context` | 数字的主体、期间、比较方式是否正确；true 时填写 observed_value、observed_unit 和 draft.content 原句；false 时填原因 |
| `critical_errors` | 无严重错误填 `[]`；有则逐项填 `{output_quote, evidence_note}`，引用稿件原句 |
| `usable` | 是否符合冻结的可用标准；false 时填 usable_note；true 要求全部必需事实／数字通过、无严重错误、技术链完成 |
| `reviewer_assessment` | 人工判断 draft_has_major_error、detected_major_error、false_alarm，填写证据说明；检出或误报为 true 时引用 Reviewer 的 issue_ids 和 reason 原句到 output_quote |

例：稿件写“提升18个百分点”，上下文正确时记录 `observed_value: "18"`、`observed_unit: "个百分点"`，并粘贴原句。脚本用 Decimal 检查容差和单位，但“这里是否在说同一个指标”由你判断。当前观察值检查支持阿拉伯数字；不能把未支持的数字写法强行判断为模型错误。

某阶段实际失败且没有有效输出时，不需要编造该阶段人工判断；脚本保留失败分母，对应必需项计未通过。已成功完成的上游仍要评分。来源不足、模型身份错误、接口依赖问题、预算耗尽或不完整中断记录标为 `blocked`；未运行/待判断与真实回答错误分开。

表填完后填写顶层审核人、时间及 `status: "approved"`，不要改 run_id 或各项 hash。审批字段不代表事实已被机器核真，只记录你的实际审核。

## 4. 自动计算分数

```bash
.venv/bin/python -B -m evaluation.run_report_pilot score \
  --run-dir ../output/evaluation/2026-10-24-report-baseline-01 \
  --human-review ../output/evaluation/2026-10-24-report-baseline-01/human-review.json
```

结果位于该 run 的 `score-01/`：`results.json`、`report.md`、评分身份和已确认人工表的副本。重复评分需用 `--output-dir` 指定新目录，避免覆盖。

| 指标 | 分母 |
| --- | --- |
| Research 必需事实覆盖率 | 所有计划案例的必需事实数，执行失败不删除 |
| Writer 必需数值通过率 | 所有计划案例的数值检查项，遗漏和执行失败不删除 |
| 严重错误稿件比例 | 实际有效生成的稿件数，同时报告未生成数 |
| 完整报告可用率 | 全部计划报告数 |
| Reviewer 错误检出率 | 人工确认存在严重错误的生成稿数；该稿审校失败计未检出 |
| Reviewer 误报率 | 有实际审校输出且人工认定无严重错误的稿件数；未执行审校不当作无误报 |

每项都有分子、分母和比例；分母为 0 时比例为 null。结果状态 `scored` 表示已按协议汇总，**不等于通过生产验收**。Reviewer 自评分和 Research 的结构引用覆盖率都不被当成事实正确率。

这里的 Reviewer 指标来自自然生成稿，不能冒充固定注错集检出能力。原 `reviewer-probes.draft.jsonl` 尚未接入运行；专门的正确稿/注错稿对照留作后续扩充。

## 5. 跑候选方案并比较

把当前公开配置复制为 `evaluation/config/report-pilot.deepseek.candidate.yml`，仅修改你希望比较的场景 Prompt，同时更新该场景的 Prompt version 和 agent_scenes.prompt_version。其他字段保持一致，保留当前配置。

```bash
.venv/bin/python -B -m evaluation.run_report_pilot prepare \
  --gold-file evaluation/datasets/writing/report_pilot_v1/private/gold-review.json \
  --config evaluation/config/report-pilot.deepseek.candidate.yml \
  --label candidate \
  --output-dir ../output/evaluation/2026-10-24-report-candidate-01

.venv/bin/python -B -m evaluation.run_report_pilot run \
  --run-dir ../output/evaluation/2026-10-24-report-candidate-01
```

候选方案同样需独立填人工评分表并执行 `score`，然后：

```bash
.venv/bin/python -B -m evaluation.run_report_pilot compare \
  --baseline ../output/evaluation/2026-10-24-report-baseline-01/score-01 \
  --candidate ../output/evaluation/2026-10-24-report-candidate-01/score-01 \
  --output-dir ../output/evaluation/2026-10-24-report-comparison-01
```

同输入、gold、模型、预算和评分器才能比较。输出比例差用**百分点**表示；条件指标的适用案例或分母改变时不计算差值。自然稿内容变化会影响 Reviewer 难度，因此不能单独归因 Reviewer 能力提升。两个开发事件只支持试跑与排错；之后需要补充独立验证材料。

若后续优化为“正文数值 → Python 指标快照 → Writer”，必须明确作为链路改动另立版本。当前运行器只接受材料型输入，没有自动加入指标计算器，也不把隐藏 gold 当模型上下文。

## 调用链、配置、测试与剩余工作

数据准备：原 seed → `prepare_report_pilot.read_source_document` 缓存身份／哈希校验 → `prepare` 分离 inputs/private → 材料包 manifest。仅标准库和本地缓存，`--output-dir` 控制新输出位置。

正式入口：`run_report_pilot init-labels/prepare` → `report_pilot_scoring.make_gold_template/validate_gold` → 冻结输入和版本；`run` → `RunBudget` → `run_report_case` → 原生三 Runner → `NewsStepHandler._validate_agent_result` 与来源检查 → `NativeStructuredAgentClient` → `StructuredInferenceService` → `OpenAICompatibleInferenceClient` → DeepSeek HTTP → Pydantic 对象与逐阶段产物 → `make_human_review` → 人工判断 → `score_reports` 的 Decimal 校验与汇总 → JSON/Markdown → `compare`。

外部依赖：DeepSeek 接口和已有 Python 环境。无 Embedding、数仓、搜索、Temporal、PG、S3 或发布调用。配置内 embedding 仅为公共配置结构占位，不初始化。原始响应仅记录哈希／长度；必要业务输出按结构化对象保存，不记录密钥、Headers 或异常原文。

离线测试入口（从 agent/）：

```bash
.venv/bin/python -B -m pytest \
  tests/test_report_pilot_preparation.py tests/test_report_pilot_core.py \
  tests/test_report_pilot_scoring.py tests/test_report_pilot_cli.py
```

TODO：你确认 gold，运行真实当前方案，核对输出并评分；再按结果决定候选优化。银行第二来源、独立验证集、固定注错探针、确定性指标输入、修订后评测、Temporal 审批恢复和企业模型验收尚未完成。没有这些证据时，不在简历中声称对应收益。
