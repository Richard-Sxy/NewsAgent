# 四项简历质量指标：本批执行清单

本批准备于2026-10-28。当前没有新的模型质量分数、人工批准、人工计时或adapter。
[完整方案](../../agent/docs/resume-quality-evaluation-20261028.md)解释基线、分母、建议规模和边界。
plan.json将“当前盘点”“建议目标”“未知成绩”分开保存。

## 用户需要亲自做的部分

1. 8879页面新增154项检索相关性标注；原300项不用重做。
2. 审核agent/evaluation/datasets/writing/report_pilot_v1/private/gold-review.md与gold-review.json的2例。
3. 真实报告A/B运行后人工评分；修订到相同交付标准，并记录实际起止与中断。
4. 查询理解数据的语义标签审核/复核、按group隔离；不替模型或脚本填写approved。

检索启动/评分与报告运行命令见完整方案；这两套runner本次未改。
模型可辅助样本起草、证据定位、错误建议；金标/审核和真实编辑时间由人产生。

## 人工修订时间工具

从agent目录运行。仅标准库，无API、GPU或.env依赖。
本批已经按2个待审gold案例生成editing-records.template.json，四条记录均待填写；
模板不代表gold批准或报告已生成。gold批准、标准或case清单变化后应生成新模板并绑定新的gold SHA；不要将待审版本SHA当成已批身份。请复制为新的工作文件，保留原模板。

生成新模板示例（输出文件必须不存在）：

~~~bash
PY=../var/news-events-annual-20261027/runtime/app-venv/bin/python
$PY -B -m evaluation.editing_time template \
  --gold-file evaluation/datasets/writing/report_pilot_v1/private/gold-review.json \
  --experiment-id report-pilot-20261028 \
  --criterion-version editor-ready-v1 \
  --criterion-text '事实、数字、口径和来源准确，覆盖必需事实，结构和长度达标，可交付运营编辑' \
  --output ../output/resume-quality-20261028/editing-records-v2.template.json
~~~

真实编辑后在工作副本填完配对记录、初稿/终稿SHA、source_run_id、编辑者、时间和审核。
顶层data_origin填human_recorded；逐条填写blind_id和该编辑者唯一的正order_index。
criterion_version及criterion_text必须在看输出前确定；不得事后降低可交付标准。
paused_seconds是不重叠的中断总和，保留原始秒表/编辑记录；有效时长由工具计算。
必须由真实审核人确认所有最终稿达标准并记录reviewer/reviewed_at，才把顶层状态改approved。
尚未完成或未达标准不应假填完成，不完整计时不会计算总体成绩。
该工具校验元数据一致性，不验证实际编辑动作、文件内容或时钟真实性。

~~~bash
$PY -B -m evaluation.editing_time score \
  --records ../output/resume-quality-20261028/editing-records.completed.json \
  --output ../output/resume-quality-20261028/editing-time.results.json
$PY -B -m unittest tests.test_editing_time -q
~~~

同一批case的baseline/candidate各一条，按预登记ID严格匹配；不允许少一组、重复、跨事件或未审核。
汇总两方案均值/中位数分钟、配对差和相对降幅。基线为0时不输出虚假的相对百分比。
分子分母和记录SHA保留；结果是描述性人工记录统计，不是企业上线验收或显著性结论。
若只有2对，只作流程试点；单编辑者先后熟悉效应需人工写入结论限制。
本工具不生成盲化界面，不读取初稿/终稿文件来确认SHA真实性，不假称已经消除顺序偏差。

## Hunyuan当前可用的离线命令

以下可新建36题v6开发冻结包，不启动模型/训练；本次只读校验已通过，尚未执行prepare。
目标目录必须全新；历史small_model脚本和旧24题v5包不能直接冒充本轮36题v6。

~~~bash
cd /home/shi/project/NewsAgent
query_eval_python="$PWD/Hunyuan7B/environments/query-eval/venv-20261025/bin/python"
query_eval_home="$PWD/Hunyuan7B/runtime/hunyuan/python/cpython-3.11.14-linux-x86_64-gnu"
query_eval_package="$PWD/Hunyuan7B/packages/query-semantics-dev-20261028-v6"

env PYTHONHOME="$query_eval_home" "$query_eval_python" -B \
  agent/evaluation/run_query_semantics.py prepare \
  --cases Hunyuan7B/data/query_semantics_v2/cases.jsonl \
  --labels Hunyuan7B/data/query_semantics_v2/labels.jsonl \
  --split development --out "$query_eval_package"

env PYTHONHOME="$query_eval_home" "$query_eval_python" -B \
  agent/evaluation/run_query_semantics.py validate --package "$query_eval_package"
~~~

正式训练和推理前须重新验收v6上下文/token预算，并解决NF4 adapter加载与训练脚本缺口。
主指标31题语义能表达，其中10题应由Python执行门拒绝；不能缩成21题。
另5题模型拒绝/澄清单列；这些都是开发草稿，不是独立test。

## 完整调用链与剩余项

上游：现有新闻补标池、report gold案例ID和语义开发集。
转换：人工标签/真实模型输出/编辑时间分别留存；editing_time冻结case/event与交付标准并校验配对。
核心：既有calculate_comparison、score_reports、query_semantics_scoring及新增editing_time。
下游：共同池nDCG、完整报告错误比例、人工编辑分钟、同条件LoRA全匹配率。
配置：实验ID、交付标准版本、case/event清单、模型/Prompt/量化身份及独立输出路径。
依赖：计时仅标准库；写作API和本地Hunyuan训练环境另备，不在本批自动运行。
TODO：补标、gold批准、生成与评分、真实计时、独立数据集、训练adapter及量化路径、区间估计。
