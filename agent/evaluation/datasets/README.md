# NewsAgent 评测数据集索引

当前保留五类评测输入。目录旧、新版本不能仅凭名称判断是否重复：回归范围、数据来源和历史报告的冻结引用可能不同。

| 目录 | 保留内容 | 用途与入口 |
| --- | --- | --- |
| `sql_security/` | SQLShield、text2sql_security_v1/v2 | SQL 注入、安全边界及历史回归；`evaluation/run_text2sql_security.py` |
| `business_sql/` | metric_contract_business_v1、query_semantics_v1 | 指标口径与查询语义；`evaluation/run_metric_contract_business.py`、`evaluation/run_query_semantics.py` |
| `retrieval/` | MulRecall、related_news_review_v1 | 三层检索探索及当前人工相关性预审；`tools/test_multitier_news_recall.py`、`tools/score_retrieval_judgments.py`；[操作说明](../RETRIEVAL_RESUME.md) |
| `writing/` | FinQA、data_writing_august_2026_v1、report_pilot_v1 | 数值推理、数据写作及 Research–Writer–Reviewer 评测；`evaluation/run_finqa.py`、`evaluation/prepare_report_pilot.py`、`evaluation/run_report_pilot.py` |
| `hot_news/` | 热点种子、dialogue、operations_synthetic_v1/research_v1/evolution_v2 | 不同阶段的热点分析、对话及运营回归；对应 `evaluation/run_hot_news_*.py`、`evaluation/run_operations_*.py` |

数据存在不代表已经获得成绩；尤其 `related_news_review_v1` 仍需人工审核、实际 A/B 候选扩池和独立测试集。完整 FinQA 位于 `writing/finqa/`，其原始数据、官方评分器与许可证均保留。

## 2026-10-24 清理记录

- 删除 `business_sql/finqa/`：531 个文件，实际身份为 CSpider 通用跨库 Text2SQL 数据集，目录名误写为 FinQA。当前项目评测入口和已保存的成绩没有使用此目录。
- 删除 3 个 `.DS_Store`、14 个旧 Python 3.7 缓存文件，以及空的缓存目录。
- 共删除 548 个文件、969,968,471 字节，约 925.03 MiB；清理前保留的 171 个文件在清理后 SHA-256 完全一致。
- 未删除任何已保存的评测结果或活动索引，也未改动业务样本、标签、模型配置或生产逻辑。

逐文件大小、SHA-256、删除原因及保留核验记录见
[`output/cleanup/2026-10-24-evaluation-datasets-01/manifest.json`](../../../output/cleanup/2026-10-24-evaluation-datasets-01/manifest.json)。此处记录清理结果，不代表模型评测通过。

## 调用链与后续事项

运行链：分类目录中的原始数据／业务样本 → 对应 prepare 或 runner 的输入转换 → 业务 Port、确定性校验或人工标签评分 → `output/evaluation/` 中按运行分开的结果。FinQA 通过 `run_finqa.py` 读取 `writing/finqa/dataset/{split}.json`，使用 `code/evaluate/evaluate.py` 评分；检索通过源新闻输入、Embedding、三层索引与业务重排生成预测，再使用人工 qrels 计算 nDCG。

清理链：目录盘点 → 代码、测试和历史报告引用核对 → 删除前 SHA-256 清单 → 精确删除未接入的 CSpider 与可重建缓存 → 保留文件逐个哈希复核。清理只用 Python 标准库和文件系统，不访问模型、数仓或外部服务，没有变更评测配置。

核验入口：清理 manifest 中 `preserved_files_hash_check` 和 `deleted_paths_absent`；FinQA 保留依赖的离线测试为在 `agent/` 执行 `.venv/bin/python -B -m pytest tests/test_finqa_runner.py -q -o addopts=''`。

2026-10-24 已将旧评测 runner、预览工具与对应测试的默认数据路径同步到五类目录；数据样本与评分逻辑保持不变。历史冻结报告的原路径与哈希保留，不自动重写实验身份。后续使用历史报告时，应结合原始哈希定位迁移后的同一份输入。
