# FinQA 小样本基线

用于用户下载的 **czyssrs/FinQA 英文数值推理集**。默认使用 dev 原始顺序前 20 题，
只衡量给定财报正文和表格时的计算程序生成；不是 NewsAgent 的报告写作、检索、
SQL、企业模型或生产质量验收。正式指标需要另行冻结测试样本与对照协议。

## 当前在测 Agent 的哪部分

准确名称是：**给定财报材料的数值问答与计算程序生成能力评测**。
它是数据驱动分析／写作需要的一项底层能力诊断，当前入口直接测试
DeepSeek + 评测专用 Prompt，不是已经接入 Research 或 Writer 的业务评测。

| 检查内容 | 本次如何检查 | 与项目的关系 |
| --- | --- | --- |
| 选取数字及理解数量关系 | 给定正文、表格和问题，生成运算程序 | 研究／分析阶段读取数据、辨别基期、比例及单位的基础能力 |
| 程序结构输出 | JSON、操作符、参数数目、步骤引用、EOF 校验 | 结构化工具参数输出的基础能力；不等同于实际工具调用成功 |
| 计算结果正确性 | 官方 Python 解释器执行程序，对照 qa.exe_ans | 检查程序表达的运算是否得到金标数值；不是模型口算成绩 |
| 程序等价性 | 官方符号程序比较 | 检查是否与参考程序等价，不能代替引用真实性或完整写作评测 |

当前调用链：`input_only` → 完整给定材料 → `execute` 构造 `InferenceRequest` →
`OpenAICompatibleInferenceClient.complete` → DeepSeek → `prediction_tokens` →
官方 `eval_program`／`equal_program` → 汇总报告。

这里没有调用 `ResearchAgentRunner`、`WriterAgentRunner`、`ReviewerAgentRunner`、
`StructuredAgentClient.run_structured`、`HotNewsAnalysisValidator`，也没有执行检索、
SQL 取数、热度计算、Temporal 或人工审批流程。真实写作由
`app/modules/writing/news_step_handler.py::NewsStepHandler._dispatch` 调度；热点分析由
`app/modules/hot_news/analysis/hot_news_analysis.py::HotNewsAnalysisService.analyze_with_snapshot`
构建输入并校验。FinQA 成绩不能直接写成这两条业务链路的准确率。

项目权威指标仍由 Python/SQL 计算。这里让模型生成程序，是公开基准的任务形式，
没有把生产的 CTR、UV、热度或报告数字交给 LLM 自由计算。
当前的 20 道 dev 题已用于错例分析和修改 Prompt，只能作开发诊断；最终模型质量需要
另行冻结未用于修改 Prompt 的测试样本。真实数据驱动写作还需经业务入口生成报告，
对照冻结指标核验正文数字、单位、时间窗口和来源引用，并由运营评估可用性。

## 2026-10-24：第三版提示词候选

已完成的两轮均调用真实 `deepseek-flash`，同一组 20 道 dev 题：

| 实验目录尾名 | 提示词 | 结构格式有效 | 计算结果正确 | 程序等价正确 |
| --- | --- | --- | --- | --- |
| finqa-baseline-01 | 原版 | 18/20 | 8/20 | 5/20 |
| finqa-candidate-01 | finqa-format-v2.txt | 20/20 | 7/20 | 5/20 |

结构有效不保证程序能执行：第二轮仍出现带货币符号的数值参数，以及重复百分比换算。
v3 使用 `prompts/finqa-numeric-v3.txt`，保留每步左括号约束，增加算术参数格式和
百分比／百分点规则，将两步格式示例改成普通加减。它没有加入开发题答案、修改评分器，
也不根据金标事后修正模型输出。样本的比例／百分数标注存在差异，v3 不宣称已解决全部
尺度歧义，也不规定所有题都乘或不乘 100。

新实验目录为 `output/evaluation/2026-10-24-finqa-candidate-02/`，仍使用同 20 题、
同模型参数、依赖、程序解释器及金标。准备完成后只需执行：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
./.venv/bin/python -B -m evaluation.run_finqa run \
  --run-dir /Users/shi/Project/myProject/NewsAgent/output/evaluation/2026-10-24-finqa-candidate-02
```

本轮准备校验会检查三轮 case_ids、输入、金标、评分器、源码、依赖和推理参数，
并用官方解释器验证提示词中的通用格式示例。`comparison-check.json` 是准备时快照，
实际是否已运行及分数以 `execution.json`／`results.json` 为准。运行后同时比较原基线和
v2，不能只报告格式改善或只选择最好的分数。本轮联动修改多条规则，无法单独归因到某一句。

## 首次执行

在项目 `agent/` 目录中执行。只需安装评分器的缺失依赖，不需要旧版 PyTorch：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
./.venv/bin/python -m pip install numpy tqdm sympy
./.venv/bin/python -B -m evaluation.run_finqa prepare \
  --limit 20 \
  --output-dir ../output/evaluation/2026-10-24-finqa-baseline-01
./.venv/bin/python -B -m evaluation.run_finqa run \
  --run-dir ../output/evaluation/2026-10-24-finqa-baseline-01
```

`prepare` 不联网。它冻结原始数据哈希、样本 ID、输入、标准答案、Prompt、官方评分器、
实际 Python/依赖版本和相关项目源码哈希；输出目录必须是新的。先安装依赖再 prepare。

`run` 会发出真实、计费的模型请求，然后自动评分。凭证默认从进程环境变量
`DEEPSEEK_API_KEY` 获取；若终端未配置，会隐藏输入提示。也可用
`--api-key-env NEWSAGENT_MODEL_API_KEY` 选择已有进程变量。不会读取 `.env`，不保存 Key。
非交互执行必须提前在进程环境注入凭证，不能将 Key 作为命令参数。

默认实际请求：固定官方 `https://api.deepseek.com/v1/chat/completions`，
`deepseek-flash`、`temperature=0`、`thinking.type=disabled`、JSON object 输出，
每题最多 2048 输出 token、60 秒、串行、无自动重试；整批最多 1200 秒。
调用数和输出 token 有上限，输入长度有上限，但不承诺某个金额；实际 usage 随响应保存。
不能把“20 次调用”当作固定费用。模型返回的版本须满足现有 Port 的绑定检查。

## 文件与输入隔离

- `inputs.json`：每例的 ID 和仅含 pre_text/table/post_text/question 的模型输入。
- `gold.json`：供官方评分使用的 ID、table、qa.program、qa.exe_ans。不会发送给模型。
- `prompt.txt`、`official_evaluate.py`、`run-spec.json`：本次冻结的执行依据。
- `responses/0000.json` 等：成功收到的 HTTP 200 原始响应体，不包含请求头或 Key。
- `cases.jsonl`：逐例状态、请求 ID、版本、usage、耗时；每例追加后落盘。
- `predictions.json`：每个冻结 ID 都有一条预测；程序格式失败以 `["EOF"]` 占位。
- `execution.json`：固定样本总数、执行状态、停止原因和预测/案例文件哈希。
- `scorer.log`、`results.json`：官方评分日志及汇总指标。

整个原始样本不能发给模型：原集还有 answer、explanation、steps、model_input、
gold_inds、program_re 和预计算检索字段，可能泄漏答案。程序只使用明确白名单。

`status=completed` 表示本批评分完成，不是达到业务验收门槛。
空回答和格式错误保留在分母中；网络、模型绑定、认证失败或服务端生成中断停止后续请求，剩余题记为 `not_run`，
结果状态为 `blocked` 且不输出最终准确率。官方评分器异常或超时同样记为 blocked。
系统会预检评分依赖，避免发完请求才发现缺包。

同一实验一旦开始便拒绝再次发请求，不自动恢复或重试。崩溃后保留逐例证据，
不要把不完整预测当成完整样本。确认原因后用新的输出目录重新创建独立实验。
若推理完整结束、尚未执行评分，可以离线执行：

```bash
./.venv/bin/python -B -m evaluation.run_finqa score \
  --run-dir ../output/evaluation/2026-10-24-finqa-baseline-01
```

## 后续对照

保持原始数据文件、split、limit、模型和依赖不变。复制并编辑一份新的 Prompt 文件，
用 `prepare --prompt-file /absolute/path/candidate-prompt.txt --output-dir <新目录>`
创建候选实验。核对两份 run-spec 的 case_ids 完全相同再比较，不能直接修改已冻结文件。
可选 `--split test`、`--model`、`--finqa`；本小样本入口每批上限 200 题。
开发题用于迭代；不要用 test 挑选 Prompt。API 别名不能单独保证供应商模型永远不变，
需要保留响应中的 model/request_id 并尽量在相近时间执行配对实验。

## 调用链、验证及限制

CLI `prepare` → `input_only` 白名单转换 → 冻结输入/标签/源码/依赖 →
CLI `run` → `verify` → `execute` → `InferenceRequest` →
项目 `OpenAICompatibleInferenceClient` → DeepSeek HTTP →
原始响应 + `prediction_tokens` → 逐例日志与预测 →
`score` 校验 ID 全覆盖与哈希 → 子进程执行冻结的官方 evaluate.py → results.json。

复用项目模型 Port、EndpointConfig、模型版本检查和安全遥测，不修改生产模型配置。
官方 scorer 依赖 numpy/tqdm/sympy；HTTP 依赖项目现有 httpx、Pydantic、OpenTelemetry。
脚本没有引入独立 Agent 框架，不连接企业数仓、Embedding、Temporal 或生产数据。

离线测试入口（真实 HTTP 用 MockTransport 替换，不能算模型质量）：

```bash
./.venv/bin/python -B -m pytest tests/test_finqa_runner.py -q
```

TODO：v3 真实模型对照由用户执行；最终测试集及配对统计、独立业务写作审核、供应商固定快照
模型版本与正式上线质量验收均不在本次实现范围。官方 scorer 的逐例结果文件本身未启用，
目前可对照 responses、gold 和日志人工定位错误，不将程序准确率等同于引用真实性。
