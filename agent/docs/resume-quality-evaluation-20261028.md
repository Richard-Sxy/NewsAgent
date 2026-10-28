# 简历质量指标补齐方案

日期：2026-10-28。本文是执行方案，建议样本数不是已完成数量，不预设收益方向。
年度合成容量测试独立保留，不用随机向量计算新闻相关性或模型质量。

## 当前实际状态

| 指标 | 已有材料 | 需要补什么 |
| --- | --- | --- |
| nDCG@5 | 545篇新闻、2577个真实BGE切片、30条开发查询；共同候选池454项，原300项已标注 | 新增SQLite实查0/154；30条不能称独立test |
| 严重错误稿比例 | 完整报告3组3任务，2个开发案例结构可运行，gold待审；银行验证案例缺第二来源 | 无完整报告A/B生成及人工评分 |
| 人工修订分钟/篇 | 尚无真实计时数据 | 配对初稿、统一交付标准、实际读稿/核验/编辑记录 |
| LoRA全匹配率 | v2的36条development/draft，31主任务+5模型拒绝/澄清 | 无训练集、独立test、训练adapter或配对预测；量化推理路径需对齐 |

旧写作9组27任务是9 research+9 write+9 review，不是27篇完整报告。
已有20题FinQA开发数值QA执行准确率40%→35%，程序准确率25%→25%；
该实验不是新闻稿质量，不能改成“错误稿下降”。证据在
output/evaluation/2026-10-24-finqa-{baseline,candidate}-01/results.json。

## 一、检索：补现有开发对照，再做独立测试

当前A为本地示例legacy_rrf（k60、三层等权），B为global_similarity。
同模型、查询向量、候选预算（每层20篇），只隔离融合排序；没有启用主体/事件重排。
这不能称企业旧线上基线，也不能把收益归因于所有检索组件。

先在 http://127.0.0.1:8879/ 补154项：相关2、部分相关1、不相关0。
保留原300标签，无法判断需人工处理，不能当负例。已运行服务不重复启动。
WSL根目录命令：

~~~bash
PY=var/news-events-annual-20261027/runtime/app-venv/bin/python
$PY -B agent/tools/compare_annual_retrieval.py serve --port 8879
# 全部标完并备份SQLite后，在另一个终端：
$PY -B agent/tools/compare_annual_retrieval.py score
~~~

输出在output/news-events-annual-20261027/comparison-v1/results.json：
metrics.legacy_rrf["5"].pooled_ndcg为A，metrics.global_similarity["5"].pooled_ndcg为B。
gain=2^grade−1，使用同一Top10并集的IDCG，逐查询宏平均，无正增益题另报有效分母。
缺任何标签时总体null；原单策略池0.9501287不能直接作本次A/B中的一个值。
保留逐查询/类别的改善与退化，不只报变好的题。

正式简历“独立查询”建议新增100条未用于调参的真实业务问题。
100是建设目标，不是100组合成预编码向量。按事件组隔离开发/test，固定as_of、语料、
模型和策略后生成共同池盲标；建议20%双人复核和分歧裁决，保留无相关结果题。
覆盖近期热点、历史关联、同主体不同事件、同事件进展、时间/条件约束。
源新闻找关联报道与普通问句需分任务报告。
新test按7/30/365日另建版本；当前30条冻结实验是7/90日，不能直接改配置后沿用旧标签身份。
主体/事件/时效重排另做消融；不根据test反复调参。事件组配对区间估计可后补，当前未实现。

## 二、写作错误率与编辑时间共用一套报告任务

最快试点是现有2个事件、2项完整报告任务，每方案2稿，共4稿。
先读agent/evaluation/datasets/writing/report_pilot_v1/private/gold-review.md，
核对12项必需事实、6项数值检查、严重错误定义及可交付标准。
重点核对单位、期间、百分比/百分点、舍入容差及“计划”与“既成事实”。
用户真实确认后才填写gold-review.json的approved、reviewer和带时区reviewed_at。
模型可提建议，不代替人工批准。

当前runner仅做Research→Outline→章节→Assemble→Reviewer首轮，不自动返工。
本轮先比较同链路、同模型下的预登记Prompt版本：
候选明确事实来源、主体/期间/单位、允许结论和未知时的保留表述。
private/gold不能进入模型输入。不能把该对照叫“单轮Writer vs完整RWR返工”，
Reviewer发现错误不代表成稿已修复；架构消融需要独立执行分支和最终稿评分。

从agent目录、gold真实批准后：

~~~bash
PY=../var/news-events-annual-20261027/runtime/app-venv/bin/python
$PY -B -m evaluation.run_report_pilot prepare \
  --gold-file evaluation/datasets/writing/report_pilot_v1/private/gold-review.json \
  --output-dir ../output/resume-quality-20261028/report-baseline-v1
$PY -B -m evaluation.run_report_pilot run \
  --run-dir ../output/resume-quality-20261028/report-baseline-v1
~~~

现成runner只支持受控环境DEEPSEEK_API_KEY及DeepSeek配置，不读取.env。
两案例每方案首轮最多12次调用，两方案最多24次；本次未调用。
Hy4/GLM需先适配同Port、核对模型返回身份，再登记新实验，不能仅替换简历名称。
候选另存配置并更新Prompt版本，模型、解码预算、输入和gold保持相同。
review-template、score、compare命令详见[现有报告评测说明](../evaluation/REPORT_PILOT.md)。

严重错误稿比例=有至少一项实质性事实/数字/口径错误的稿件数÷实际有效生成稿件数。
风格/错字单列；同时报计划、实际生成、失败/未生成和完整可用率。
两方案有效分母不一致时现成compare会阻断差值，先诊断，不手选有利交集。
遗漏必需事实也需单独计入覆盖/可用率，不能用“没写所以没错”证明质量好。

下一批建议10个独立事件组×2个完整任务=20任务，每方案20稿，共40稿。
这是试点评估规模建议，尚未建设；按事件组安排开发/测试，保留来源和gold。
20任务不等于20独立事件，A/B共40稿也不等于40任务。
每稿可约500–800字；缺第二来源时补真实资料，不复制同一来源凑数。
每方案20稿中1篇即5个百分点；只有2稿时是50个百分点，小样本不报虚假的精度/泛化。

### 人工修订耗时

计时从开始读初稿到修成同一标准的可交付稿，包含读稿、核对给定证据及编辑，
排除模型等待、离开任务和其他工作中断。保留起止、暂停记录、初稿/终稿SHA和实际审核。
同一批预登记任务成对记录；若只做10对，必须看输出前选定，不能事后挑提升最大稿件。
隐藏方案名称并平衡顺序；优先两位编辑交叉分配，避免同一编辑看同事件两稿。
只有一人时记录顺序/熟悉效应并标为探索性，不宣称消除了偏差。

新增editing_time只生成待填模板并统计，不自动计时、批准或填记录。
active_seconds=(finished_at-started_at)-paused_seconds；暂停应是不重叠中断的总和，建议附原始秒表记录。
全套预登记配对完整、人工确认同一交付标准后计算各方案mean/median分钟及配对差。
缺记录、未完成、未审核不当0分钟；元数据校验不能证明计时真实性。
生成/评分准确命令见[本批执行清单](../../output/resume-quality-20261028/README.md)。

## 三、LoRA：数据和运行路径先齐，再测收益

保留36条为开发集。31个main_em主任务包括10个语义可表达、但Python执行门拒绝的请求；
不能把分母缩成21个能执行请求。另5个模型拒绝/澄清（3 unsupported+2 clarify）单列。
这批共30个group，Golden18/High-risk18/Fresh0，不能直接同时训练并报告test收益。

建议建设300 train/60 validation/100主任务test，另留拒绝/澄清风险集。
这些是目标，不是已有数量；资源不足可做小试点并报告实际分母。
按原始语义需求、近似改写、事件/会话修正链整体分组后划分；仅换参数也要检查模板泄漏。
覆盖绝对/相对时间、跨日、指标别名、组合筛选、排序TopN和拒绝/澄清。
相对时间固定as_of/时区；模型可辅助提样本，由真实人工标注和不同复核人确认。
Fresh须有真实已审核来源，不把合成改写包装为线上反馈。

冻结后先跑基座；train训练、validation选checkpoint，最后固定test比较adapter。
同一test、基座revision、tokenizer、量化、Prompt、Schema、时钟及解码预算，仅adapter变化。
错误JSON、超时、缺预测都按协议保留分母，同时报告逐槽位、模型拒绝、错误放行和执行门结果。

当前NF4 HTTP服务不加载adapter；支持PEFT的local评测按BF16/quantization=none运行。
必须补同NF4 adapter加载，或确认显存可行后两组都走同一BF16路径；不能跨路径拼成绩。
正式训练脚本、可训练模块、长度/显存验收、checkpoint及adapter清单仍待建设。
已有qlora-backward冒烟optimizer_steps=0、adapter_saved=False，不是完成SFT。
旧README/run-base-dev.sh仍有small_model路径，不能直接执行；优先Hunyuan7B真实路径和
agent/evaluation/run_query_semantics.py，本批README给出已核对的离线命令。

最终主指标=全部适用槽位同时正确的题数÷全部计划主任务题数。
独立test、真实adapter、同条件逐题预测和可复算成绩齐全后才填A%→B%。
[NF4与LoRA训练是不同步骤，参考PEFT官方说明](https://huggingface.co/docs/peft/developer_guides/quantization)。

## 今天顺序与数字映射

1. 完成154项检索补标，先得到现有30条开发查询真实A/B。
2. 审核2个报告gold，跑A/B各2稿并人工评分/实际计时，仅作试点。
3. 扩充独立检索test和报告事件组，按事件组隔离开发/测试。
4. 并行审核语义样本、补训练及量化评测断点；LoRA不使用预期收益占位。

| 简历空格 | 合格证据 |
| --- | --- |
| 检索N/A/B | 新独立test数量/同共同池nDCG@5；旧30条只能称开发对照 |
| 写作G/N | 实际审核评测的事件组/完整报告任务数，不是阶段数或A/B稿件总数 |
| 错误稿A/B | 同标准、同任务的真实人工严重错误稿比例及分子分母 |
| 修订C/D | 完整配对真实有效人工编辑分钟均值，并附中位数与配对数 |
| LoRA A/B | 同一独立主任务test上的基座/adapter全匹配率，风险集单列 |

未知保持null；先跑结果再决定简历措辞，持平/下降照实保留。
没有结果前可以写已做的数据/评测工程，不能写已验证质量提升。

## 调用链、配置、依赖、测试和TODO

上游是冻结新闻/标签、待审gold和Hunyuan开发题；本文不改原标签/生产设置。
检索：compare_annual_retrieval→ComparisonReviewService/SQLite→calculate_comparison→results/report。
写作：run_report_pilot prepare→run_report_case/原生模型Port→人工逐稿检查→score_reports→compare。
计时：人工原始记录→editing_time template/score→起止减暂停→配对均值/中位数/差值。
LoRA：审核并分组切分→冻结包→真实训练adapter→同条件推理→query_semantics_scoring→compare_runs。
写作依赖真实模型API；LoRA依赖GPU和模型环境；检索补标/评分和计时统计不需要模型。

本次新增协议、模板和计时统计属于工程准备，不是质量成绩；不读取.env，不启动模型/训练。
参数/产物见本批README；输出用新文件，不覆盖历史实验或标签。
计时测试从agent运行：python -B -m unittest tests.test_editing_time -q。
既有检索/报告/语义代码未改，不为文档重复重跑整仓。
TODO：154项补标、gold审批、写作A/B与实际人工计时、独立test、训练adapter、同量化路径、区间估计。

[nDCG方法参考](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.ndcg_score.html)。
本项目使用指数gain，不直接换成库默认线性增益而改变历史口径。
