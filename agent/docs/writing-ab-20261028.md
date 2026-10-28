# 写作A/B实测与简历填写（2026-10-28）

## 当前结论

真实本地模型生成、来源/数值复算和助手初评已经完成；真实人工编辑计时尚待用户操作。
本轮是2个开发事件、2项写作任务，最终比较4篇稿，不能称独立测试或企业模型验收。
使用Hunyuan-7B-Instruct固定revision的NF4本地推理，不是Hy4或DeepSeek。

| 指标 | 基线 | 最终候选 | 证据性质 |
| --- | --- | --- | --- |
| 必需派生数值检查 | 1/6（16.7%） | 6/6（100%） | 助手确认上下文、Decimal复算；候选含确定性数值附表 |
| 必需事实覆盖 | 11/12（91.7%） | 11/12（91.7%） | 助手初评，待用户确认 |
| 有明确严重错误的稿件 | 1/2（50%） | 0/2（0%） | 助手初评，待用户确认 |
| 必需事实或数值检查未通过的稿件 | 2/2 | 1/2 | 助手初评，遗漏与明确错误分开 |
| 达到完整交付标准的稿件 | 0/2 | 0/2 | 引用与必需事实仍有缺口，不宣称全部可用 |
| 人工修订分钟/篇 | 未测 | 未测 | 当前四项真实计时均未开始；不能填0 |

明确严重错误为基线OpenAI稿把“2026年第一季度研发支出86亿美元”写为“第二季度研发支出86亿美元”，
给定S-OPENAI-GROWTH原文可核对。此处是相对于给定缓存报道的口径一致性，不独立核真报道。
两组腾讯稿均未明确给出原口径自由现金流负138亿元；候选叙事仍有[1](@ref)等无效引用。
附表正确不代表叙事、引用、全文可用性均达标。

## 原生链路与局部写作试点分别记录

1. writing-ab-hunyuan-v1：原生Research→Outline→章节→Assemble→Reviewer首轮A/B均执行，
   每组两任务均在Research失败；基线为事实包为空、候选为JSON解析失败。
   四次实际请求和失败完整保留，完整报告完成数0，不能计为无错误稿或声称RWR验收通过。
2. text-ab-v1：同模型、同公开材料和2048输出预算，四次直接Markdown写作全部完成。
   Prompt候选仍漏掉派生计算，四份原始响应与稿件保留。
3. text-snapshot-v2：保留基线原稿并核对SHA，Python仅从公开原文按三个显式模式抽数、Decimal计算，
   再生成两篇候选。模型只收到public_input与由公开来源计算的snapshot，不收到private gold。
4. text-final-v3：复用前述两基线和两候选，Python装配可复算数值附表，无新增模型调用。
   最终比较是“原材料写作”与“公开来源指标快照＋写作约束＋确定性数值附表”的局部链路改进。
   它不是Prompt单变量消融、完整原生RWR或独立测试。迭代依据开发集观察，不能冒充泛化收益。

模型身份与源文件版本在writing-ab-hunyuan-v1/model-identity.json、
各版本plan.json、响应metadata和requirements.actual.lock.txt中。六篇直接生成稿
（text-v1四篇、snapshot-v2两篇）不等于六个独立任务，最终仍为两个任务/四篇对照稿。

## 现在怎样完成真正人工计时

打开 http://127.0.0.1:8881/ 。填写真实标注名，按页面顺序处理四项任务。
点击开始后才展示稿件、原文和已确认交付检查项；读稿、核对及修改都计时。
离开或切换任务前先暂停/完成，同一编辑者不能并行运行两只计时器。
保存可刷新恢复；完成后点击“结束计时并保存终稿”。同事件先后阅读会有熟悉效应，
本轮只有一位编辑时须在结论注明，不能从两对稿件推断普遍效率收益。

原始事件在text-final-v3/manual-editing-state/，实时数据不随Git直接提交；
可通过GET /api/export或命令行只读导出一致性快照。时间来自服务端，不填估计值。
结束计时不等于自动通过质量审核。用户确认最终稿确实达统一标准后，才填写实际审核身份/时间，
再用evaluation.editing_time score计算平均、中位分钟数及配对差。当前计时仍为空。

已保存的editing-records.pending-01.json是零开始记录的历史快照，不能覆盖实时进度。
后续导出用新文件名；不会从模型生成延迟或本聊天时间推导人工耗时。

## 已交付的评分文件

- text-final-v3/assistant-review.proposal.json：助手逐事实/数字/错误判断，仍pending_human_review。
- text-final-v3/verify_provisional_metrics.py：校验原句、稿件SHA、gold身份、来源定位、公式与分母。
- text-final-v3/provisional-metrics.json：暂定A/B、6个派生数值、3项来源度量的验证记录。
- text-final-v3/editing-tasks.json：四篇真实配对稿和材料，网页隐藏方案名。
- text-final-v3/resume-ready.md：可用表述和待复核表述。
- 所有v1/v2/v3稿件、原始响应、版本身份与失败均保留，未挑掉失败或改写模型稿。

verify_provisional_metrics.py的输出文件采用x模式，默认不覆盖已有结果；
新一轮复核应另选输出版本或先在临时副本运行。它不将助手判断伪装为人工批准，
也不证明稿件已修好。人工编辑发生在独立状态目录，不改冻结初稿。

## 完整调用链、配置、依赖和测试

上游：已人工批准的两例gold、公开news inputs。
原生尝试：run_report_pilot prepare/run→受限本地HTTP→原生Runner/Schema/业务校验→失败产物。
直接写作：run_writing_text_ab prepare/run→冻结公开input→Hunyuan真实HTTP→Markdown与响应身份。
数据改进：writing_metric_snapshot.build_metric_snapshot→公开文本规则/定位/SHA→Decimal指标→
run_writing_snapshot_ab→同条件两次候选调用→finalize_writing_numeric_appendix→确定性附表。
评分：助手提议→verify_provisional_metrics→逐项证据/数学核验→暂定指标，等待人工语义确认。
人工计时：manual_editing_server.EditingStore→开始/暂停/保存/结束事件→待审核导出→
人工确认终稿→editing_time.score_records→真实C/D分钟；没有数据则保持未知。

本地服务固定127.0.0.1:8018、模型local-hunyuan-7b-6fd6ecb-nf4-writing，
16384上下文、NF4双重量化/BF16计算；直接写作2048输出、300秒、零自动重试。
HTTP保留JSON/Schema校验能力的真实边界，未声称受约束JSON解码。
人工页固定本机8881，同源校验、文本渲染和revision防覆盖；不执行新闻内容中的指令。
应用依赖锁在deploy/requirements.writing-eval.lock.txt；GPU模型环境独立。
无企业接口/原始行为/生产发布/训练调用，不读取.env，不更改旧模型部署。

本次相关离线合同统一回归180项全部通过；这些是工程测试，不是模型正确率。
测试从agent目录运行本批11个test文件，包含local_profile、原report_pilot四组、
text_ab、snapshot_ab、模型接口、手动计时、editing_time和metric_snapshot。
真实模型生成和独立数学校验的证据另见各运行目录。
TODO：用户完成四稿真实编辑与质量确认、最终C/D、独立事件组扩展、生产模型/完整RWR质量、
通用抽取规则与LoRA对照；本轮没有训练adapter。

## 复现入口（WSL）

应用环境位于Hunyuan7B/runtime/writing-eval/app-venv，模型环境位于Hunyuan7B/environments/hunyuan。
旧模型venv链接/旧small_model脚本仍有路径问题，本轮使用明确的解释器与PYTHONHOME/PYTHONPATH：

~~~bash
env PYTHONHOME=/home/shi/project/NewsAgent/Hunyuan7B/runtime/hunyuan/python/cpython-3.11.14-linux-x86_64-gnu \
 PYTHONPATH=/home/shi/project/NewsAgent/Hunyuan7B/environments/hunyuan/lib/python3.11/site-packages \
 /home/shi/project/NewsAgent/Hunyuan7B/runtime/hunyuan/python/cpython-3.11.14-linux-x86_64-gnu/bin/python3.11 \
 -u Hunyuan7B/scripts/serve_writing_eval.py
~~~

所有新模型实验使用新输出目录，不重复run已开始的任务。
本轮模型调用已完成，可停止该隔离GPU服务释放显存；编辑页不依赖GPU。

从agent目录恢复现有人工编辑页（仅未运行时，复用原任务包/状态目录）：

~~~bash
../Hunyuan7B/runtime/writing-eval/app-venv/bin/python -B -m evaluation.manual_editing_server \
 --package ../output/resume-quality-20261028/text-final-v3/editing-tasks.json \
 --state-dir ../output/resume-quality-20261028/text-final-v3/manual-editing-state --port 8881
~~~

原状态不在Git时，在新电脑会新建独立计时，不会自动带来旧电脑的编辑时长。
切电脑前先暂停，由运行中的服务导出并备份状态，不在两台机器同时写同一实验。
