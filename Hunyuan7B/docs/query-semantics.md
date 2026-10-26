# 查询解析与 LoRA SFT：另一台电脑执行计划

本轮衡量用户问题到 `QueryUnderstanding` 的解析质量。最终要填的 **A = 未微调 Hunyuan 基座的槽位全匹配率，B = 同一基座加 LoRA 的槽位全匹配率**。当前 A、B 均未执行，没有可填写的成绩。这里不测热／温／冷向量检索，也不把 SQL 护栏开发测试当成模型收益。

## 1. 已经准备的内容

- `datasets/business_sql/query_semantics_v1/cases.jsonl`：24 条合成开发问题及固定时钟、查询策略。
- 同目录 `labels.jsonl`：18 条完整解析目标、5 条拒绝目标、1 条澄清目标，全部待人工审核。
- `run_query_semantics.py`：冻结、校验、打包、导出训练消息、推理、评分、对比的统一入口。
- `query_semantics_runner.py`：已有 HTTP 服务时使用；`query_semantics_local.py`：台式机直接加载本地 Transformers 权重时使用。
- `query_semantics_scoring.py`：本机离线评分，保留失败分母和各风险层结果。

这些开发样例不能直接改名成 test 用来填简历。脚本没有批准任何标签，没有开展 Hy4 标注、LoRA 训练或真实模型推理。

## 2. 先确认业务口径，再准备独立数据

先阅读并修订 24 条问题对应的 `target`。重点确认时间窗口、指标、栏目、图文／视频、条数，以及不支持条件应拒绝还是澄清。当前业务只支持**一个整点小时**，5 种指标、7 个栏目；未指定条数采用 policy 的 5 条。明确日期不能因超出数据覆盖而改成别的日期。不支持阈值、跨小时聚合、跨租户、企业原榜、发布时间查询等条件时，不能偷偷丢掉条件再执行。

人工扩充建议（数量是计划，不是已经完成的样本量）：

| 用途 | 初始目标 | 使用方式 |
| --- | ---: | --- |
| development | 现有 24 条及后续开发问题 | 检查链路、定位错误、改 Prompt |
| train | 约 500 条 | 训练 LoRA；正常与拒绝／澄清都要覆盖 |
| validation | 约 100 条 | 选择 checkpoint 和训练参数 |
| test | 约 200 条 | 模型选定后固定一次对照；建议约 150 条正常、50 条拒绝／澄清 |

先按**真实需求、语义模板及其改写、同一会话／修正链**划分 `group_id`，再分 train / validation / test。不能把“昨天8点到9点财经图文排行”改成“昨日8—9点财经文章榜单”后分到两个集合。脚本会拦同组和完全重复问句，近似改写仍需人工检查。正式 test 不用来挑 Prompt、LoRA 参数或最佳成绩；选择模型使用 validation，简历优先报告最终 test。

Golden / Fresh / High-risk 与用途 split 是两个维度：Golden 是常见稳定需求；Fresh 必须来自真实匿名坏例并保留引用；High-risk 覆盖遗漏条件、越权和不支持请求。没有真实 Fresh 就暂时留空，不把合成题称作线上回流数据。

Hy4 Preview 可以提出候选问句和 `target`，再由业务人员核对、另一位复核人确认。模型不担任自己的标准答案或最终审核人。标注和二审完成后才把 `human_review.status` 改为 `approved` 并填写真实身份；无法二审时可以探索评测，但不能伪造审核记录。标签不需要一段标准自然语言回答，只需完整 JSON 槽位与应有执行状态。

## 3. 本机冻结并打包

在仓库根目录执行；当前开发包已经准备好时可直接使用交付的 ZIP。

```bash
small_model/environments/query-eval/venv-20261025/bin/python small_model/evaluation/run_query_semantics.py prepare \
  --cases small_model/data/query_semantics_v1/cases.jsonl \
  --labels small_model/data/query_semantics_v1/labels.jsonl \
  --split development \
  --out small_model/packages/query-semantics-dev-v1

small_model/environments/query-eval/venv-20261025/bin/python small_model/evaluation/run_query_semantics.py bundle \
  --package small_model/packages/query-semantics-dev-v1 \
  --out small_model/packages/query-semantics-desktop-kit-v1.zip
```

每次修改题目、标签、Prompt 或评分源码，都新建包和实验目录，不能覆盖原实验。冻结项包括实际未提交源码内容的 SHA-256、Prompt、输出 Schema、样本、标签、固定时刻和策略。正式数据准备好后同样命令选择 `--split test`。不同模型必须使用同一个冻结包。

默认 ZIP 包含运行所需 Python 源码和公共输入，**不带标准答案、企业数据、凭证或模型权重**。标准答案留在本机的 `package/private/labels.jsonl`，评分时使用。ZIP 也不包含旧写作/FinQA 评测。

## 4. 台式机先跑未微调基座

解压 ZIP 后，在解压目录运行。沿用台式机已能加载模型的 Python 环境，先记录原环境；如需安装评测依赖，优先另建该环境的副本，避免改变已有模型环境。

```bash
python -m pip install -r requirements.txt
python -m evaluation.run_query_semantics validate --package package --public-only
```

直接本地模型调用使用 `config/base.local.example.json`，另存为 `config/base.local.json`，将模型目录、tokenizer 目录、实际 dtype/device 改成台式机真实值。基座的 `adapter_path` 和 `training_manifest_path` 均为 null。

```bash
python -m evaluation.run_query_semantics run-local \
  --package package --config config/base.local.json \
  --out runs/base-dev-01 --max-calls 24 --max-seconds 3600
```

正式 test 有多少条就设置对应的调用预算，并给基座、LoRA **完全相同**的时间预算。首次先跑开发包检查格式与性能；若显存、模型兼容或时间预算不满足，修复环境后新建实验，不拼接或挑选成功样例。一次只处理一例，失败不自动重试。

本地入口只加载已有未量化 Transformers 权重，不下载模型、不运行远程模型代码。它使用原生生成、固定非采样解码，并对 Hunyuan 关闭 thinking；输入不被静默截断。生成时间是软限制，单个生成步骤可能超过预算，不能据此声称有硬实时保证。模型加载和权重哈希耗时与逐例推理耗时分开理解，不用于填端到端服务 P95。

腾讯官方模型卡给出 Transformers 加载和 `enable_thinking=False` 的用法；其文档要求 `transformers>=4.56.0`。请以台式机已经验证的兼容版本为准，并让基座和 LoRA 使用相同版本。见 [Hunyuan-7B-Instruct 官方说明](https://huggingface.co/tencent/Hunyuan-7B-Instruct)。如果实际部署的是其他模型，应记录其真实名称，简历不能继续写 Hunyuan。

如果台式机已有兼容 OpenAI 的本地服务，可使用 HTTP 备选入口：填写 `config/base.http.example.json` 中服务名、真实模型版本、模板摘要、量化设置等；凭证仅放在指定环境变量中，然后执行 `run --endpoint config/base.http.json`。此路径不读取 `.env`，仅能核验声明身份和返回路由，不能远程证明服务端权重；本地直接加载入口会记录本地资产内容摘要。GGUF/Ollama 等非 Transformers 权重需使用兼容 HTTP 入口或另做适配，不能直接交给 `run-local`。

## 5. 构建训练数据并做 LoRA

先让 Hy4 根据开发集错误分类提出训练候选，覆盖时间相对表达、缺省日期、排序指标、栏目／类型组合、数量和拒绝／澄清。人工审核通过后，准备**仅 train** 的 cases/labels，并提供全部 development/validation/test 公共输入作为 holdout 台账。在本机仓库根目录执行（以下 paths 是你新建文件的位置）：

```bash
small_model/environments/query-eval/venv-20261025/bin/python small_model/evaluation/run_query_semantics.py export-train \
  --cases path/to/train.cases.jsonl --labels path/to/train.labels.jsonl \
  --holdout-cases path/to/all-holdouts.cases.jsonl \
  --package small_model/packages/query-semantics-dev-v1 \
  --out small_model/training/query-semantics-training-v1
```

输出 `train.messages.jsonl` 和 `training-manifest.json`。每条训练消息与评测使用同一套真实 Prompt、Schema 和输入结构，assistant 目标才是人工确认 JSON。台账记录训练组及数据摘要。把这个目录带到台式机的现有训练环境；**导出不等于已训练**。

训练执行建议：

1. 使用同一份 Hunyuan 基座，保留原 tokenizer/chat template；只对 assistant completion 计算监督损失，不把 system/user 提示当预测目标。
2. 先做单批次训练检查，确认 label mask、loss、保存和重载 adapter 正常。LoRA target modules 根据实际模型层名选择并记录，不能把别的模型层名直接套用。
3. 可以从 rank=16、alpha=32、dropout=0.05、学习率 1e-4、1 个 epoch 起试。这些只是候选起点，要按显存和 validation 调整；不得保证提升。量化训练可行性、batch size、最大长度由实际环境决定。
4. 保存 adapter、训练日志、配置、数据摘要、随机种子和 validation 结果。检查输出完整 JSON；不要用正则“补正确答案”。选择 checkpoint 后冻结，再评独立 test。

这次执行包**不提交训练任务、不包含训练框架或生产自动更新逻辑**。加载已有 LoRA 使用 PEFT 的本地 adapter API，参考 [PEFT 官方加载说明](https://huggingface.co/docs/peft/package_reference/peft_model)。数据台账只能核查输入身份，仍需训练日志证明 adapter 确实由这批数据训练。

## 6. 同条件跑 LoRA，带回本机评分

复制 `config/lora.local.example.json` 为 `config/lora.local.json`，基座、tokenizer、dtype、device、生成上限与基座实验一致；填写真实 adapter 目录和已导出的 `training-manifest.json`。

```bash
python -m evaluation.run_query_semantics run-local \
  --package package --config config/lora.local.json \
  --out runs/lora-dev-01 --max-calls 24 --max-seconds 3600
```

将两次完整 `runs/` 目录带回本机，不必搬模型权重。每次包含 `run-spec.json`、逐例 `predictions.jsonl` 和 `completion.json`，不要手改预测文件。

```bash
small_model/environments/query-eval/venv-20261025/bin/python small_model/evaluation/run_query_semantics.py score \
  --package small_model/packages/query-semantics-dev-v1 \
  --run path/to/runs/base-dev-01 --out small_model/runs/query-base-score-01

small_model/environments/query-eval/venv-20261025/bin/python small_model/evaluation/run_query_semantics.py compare \
  --package small_model/packages/query-semantics-dev-v1 \
  --baseline path/to/runs/base-dev-01 --candidate path/to/runs/lora-dev-01 \
  --out small_model/runs/query-base-lora-compare-01
```

开发包或待审核标签只产生探索结果，`resume.A_percent/B_percent` 保持 null。正式 test 包完成审核、训练/测试组隔离、两次模型身份和设置一致、逐例证据完整后，比较文件才允许输出 A、B。validation 也可输出独立验证结果，但用来调参后不能称最终独立测试成绩。

## 7. 如何理解指标并填写简历

主指标 = **正常可解析问题中，全部 13 个语义槽位正确的条数 ÷ 预注册正常问题总数**。槽位包括 status、时间表达/日期/起止小时、时间口径、榜单来源、展示方式，以及指标/排序方向/类型/栏目/数量。explanation 与拒绝理由自由文本不逐字比较。超时、非法 JSON 都保留在分母；缺记录、身份错误、数据漂移会阻断正式结论。

同时查看：逐槽位准确率、拒绝/澄清决策准确率、高风险错误放行率、Schema 合法率和 Python 执行门后的决策准确率。不能只看总 EM 或把负例仅预测 status 的得分混进主指标。模型低分被护栏拦住，不意味着模型原始语义理解正确。

当前项目真正的 Data Loop 双基线是“当前生产 Bundle＋上一实验 Bundle”，回放固定的 `HotNewsAnalysisInput`。本次“基座＋LoRA”是 SFT 对照；两者不可混称。Hy4 可以作为额外参考线，Hy4→Hunyuan 的差值不能归因为 LoRA 收益。

待真实实验完成后，可写：

> 基于 Hunyuan-7B-Instruct 开展 LoRA SFT，优化时间、指标及筛选条件解析，在 N 条独立正常查询及 M 条高风险／澄清样例上评测，**查询意图槽位全匹配率 A%→B%**，高风险错误放行率 C%→D%。

只有 B>A 才称“提升”，差值用**百分点**；同时保留样本量、失败数、三层分组和错误案例。小样本结果不能外推线上收益，当前评分器未实现置信区间。

当前简历中“统一回流查询错误”需保留为待实现：现有正式反馈模型要求 `HotNewsAnalysisInput`，已有分析校验失败、检索异常和运营修正回流，未发现查询语义错误统一进入正式账本的实现。本评测包不会补写成已上线。当前可保留“汇集分析异常、检索错误与运营修正，构建分层回归与双基线对照”；SFT 在真正完成前保留“拟开展”。

## 8. 调用链、配置、验证与 TODO

业务原链路：`ConversationTools.query_hot_news` → `_understand` → `NativeStructuredAgentClient` / query_understanding v5 → `QueryUnderstanding` → `resolve_structured_query` → `SqlAssistantService.preview(expected_intent)` → Python `compile_query` → SQL Guard → 只读数仓 → Python 热度 → 关联检索 → 分析。

本包截取解析部分：CLI `prepare` → `validate_cases/validate_gold` → 冻结 manifest/runtime/schema → 台式机 `run-local` 或 `run` → 原生 `StructuredInferenceService` 渲染 Prompt 与解析 JSON → `resolve_structured_query` → 预测记录 → 本机 `score_run/compare_runs` → results.json。没有 SQL、数仓、Embedding、向量索引、Temporal 或生产 Bundle 的副作用。

配置：数据中的 now/policy；冻结的 query_understanding Prompt；本地模型目录/dtype/device/生成上限，或 HTTP endpoint/外部凭证变量；两次相同调用及时间预算。基础依赖由 ZIP requirements 锁定；本地 GPU 还需 torch/transformers/accelerate/peft，**第一次跑基座前即准备完整的同一环境**，沿用并记录台式机兼容版本。不要在两次实验之间升级依赖。若显存只支持量化权重，使用已有兼容 HTTP 服务，或另行适配并重新冻结量化本地入口。

测试入口（仓库 `agent/` 下）：

```bash
.venv/bin/python -m pytest tests/test_query_semantics_seed.py \
  tests/test_query_semantics_preparation.py tests/test_query_semantics_runner.py \
  tests/test_query_semantics_local.py tests/test_query_semantics_scoring.py
```

这些是离线契约测试，不能写成小模型质量成绩。真实 GPU 加载/LoRA 训练、业务标签二审、独立 test、真实 Fresh 回流、完整企业数仓链路验收及生产审批均为 TODO。本包不会自动审批、上线或回滚。
