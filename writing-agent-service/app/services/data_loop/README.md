# 热点分析 Data Loop 预设目录

> 状态：**PRESET ONLY**。本目录当前没有可执行 Python 模块，不可导入，也不能作为
> Data Loop 已完成的证据。

本目录只作为 Data Loop 业务服务的规划入口。项目继续沿用现有横向分层，不在这里
混放 Pydantic Schema、ORM、API 或 Temporal Workflow。

## 目标调用链

```text
Online Insight Loop
-> 校验失败/低置信/运营纠正/检索错误/发布后效果
-> FeedbackCaseBuilder
-> FeedbackCurator
-> EvaluationDatasetFreezer
-> ErrorAttributionAgent
-> OfflineReplay + EvaluationGate
-> 人工审批
-> 新 ProductionBundle
```

## 本目录未来服务

- `feedback_collector.py`：汇聚业务校验、运营决策、检索质量和发布后聚合效果。
- `feedback_curator.py`：关联可信快照，执行脱敏、幂等去重、时序校验和分层采样。
- `dataset_freezer.py`：生成版本、来源血缘、内容哈希及不可变 Dataset Manifest。
- `offline_replay.py`：按历史快照重放热点计算、检索、Prompt 和输出校验。
- `evaluation_gate.py`：比较线上、上一实验和候选 Bundle，执行确定性退化门禁。

## 跨分层目标文件

- `app/schemas/analysis_feedback.py`：Feedback Case、Outcome、Attribution 契约。
- `app/schemas/evaluation.py`：Dataset、Evaluation Run、候选和审批契约。
- `app/models/analysis_feedback.py`：反馈、人工标签和发布后效果 ORM。
- `app/models/evaluation.py`：数据集、评测、候选和 Production Bundle ORM。
- `app/repositories/analysis_feedback.py`：反馈与标签持久化。
- `app/repositories/evaluation.py`：Dataset、评测和候选持久化。
- `app/services/agents/error_attribution.py`：严格结构化的错误归因 Agent Runner。
- `app/services/configuration_candidate.py`：候选 Diff 和人工晋升领域服务。
- `app/api/analysis_feedback.py`：运营反馈与人工修正 API。
- `app/activities/data_loop.py`：Data Loop Activities。
- `app/workflows/data_loop.py`：有边界的 Data Loop Workflow。
- `app/data_loop_worker.py`：Worker 装配和注册。
- `alembic/versions/0010_*.py` 及后续迁移：实际编号必须在实现时根据迁移头确认。
- `tests/test_analysis_feedback_*.py`、`test_dataset_freezer.py`、
  `test_error_attribution_agent.py`、`test_offline_replay.py`、
  `test_data_loop_workflow.py`：单元、契约及编排测试。

## 职责与安全边界

- Python/SQL 负责数据关联、脱敏、去重、采样、版本冻结和评测指标。
- Agent 负责 Bad Case 聚类、错误归因、回归用例和候选 Diff，不生成权威指标。
- 基础设施故障与模型质量问题必须分开分类。
- 不保存企业原始用户行为明细，只保存必要聚合快照、匿名错误特征和证据引用。
- Prompt、热度规则、重排参数、Schema、模型和生产发布不得自动晋升。
- 数据同步与数据质量失败属于硬阻塞；归因或 Memory 更新失败可以降级。
- 有副作用的外部提交最多自动重试一次，审批超时默认不晋升。

## 最小完成条件

1. 运营决策、校验失败和至少一种检索错误能够生成幂等 Feedback Case。
2. Case 能反查分析输入、指标、证据、模型输出、人工修正和版本。
3. 审核后的 Case 能冻结成带哈希与来源的 Evaluation Dataset。
4. 归因 Agent 输出严格 Schema，并通过证据和问题类型白名单校验。
5. 至少跑通一次历史快照 Replay、双基准评测和人工批准/拒绝流程。
6. 真实 PostgreSQL 迁移、Temporal Workflow 和端到端测试通过。
