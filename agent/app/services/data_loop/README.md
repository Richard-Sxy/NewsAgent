# Data Loop 运行与安全边界

Data Loop 从线上运行和人工反馈生成离线样本，回放当前与候选 Production Bundle，计算
确定性指标并输出审批材料。它可以提出候选，但不能自动批准或发布生产配置。

## 链路

```text
线上运行/显式反馈
  → FeedbackCollector（脱敏、去重、归因）
  → PostgreSQL 样本账本
  → OfflineReplayService
  → NativeProductionBundleRuntimeRegistry
  → Python Agent Runner + InferencePort
  → Pydantic/业务 Validator
  → EvaluationGate 确定性评分
  → 候选配置
  → 人工 approve
  → 人工 activate / rollback
```

回放器只解析已注册的不可变 Bundle；Prompt、模型路由、输出 Schema、Validator、指标和
热度规则都必须有明确版本。已提交的 Evaluation 先查 PostgreSQL 幂等账本，不重复调用
模型。模型输出、新闻正文和检索证据均视为不可信输入。

## 核心实现

- `automatic_feedback.py`：将完整运行结果转成可审计反馈，保留证据引用而非原始用户明细。
- `feedback_collector.py`：显式反馈收集、脱敏、租户校验和样本构建。
- `offline_replay.py`：按受控并发回放 Bundle，汇总 case 级结果。
- `evaluation_gate.py`：以确定性规则判定门禁，不让 LLM 决定自身是否通过。
- `step_handler.py`：Temporal Activity 入口与错误分类。
- `app/services/production_bundle.py`：候选、评测、审批、激活和回滚领域规则。

## 配置

- `MODEL_RUNTIME_CONFIG_PATH`：Python 模型运行时 YAML。
- `DATA_LOOP_GATEWAY_TOKEN`：管理 API 的独立鉴权令牌。
- `DATA_LOOP_REPLAY_MAX_CONCURRENCY`：离线回放最大并发。
- `DATA_LOOP_MAX_CASES_PER_COHORT`：每个样本分层的最大 case 数。
- `HOT_NEWS_RUNTIME_MANIFEST_JSON`：允许解析的不可变 Bundle 清单。

密钥只通过环境或企业密钥系统注入；日志和 Artifact 不得包含明文密钥或企业原始用户
行为明细。

## 测试

```bash
python -m pytest \
  tests/test_offline_replay.py \
  tests/test_automatic_feedback.py \
  tests/test_feedback_collector.py \
  tests/test_production_bundle.py \
  tests/test_data_loop_management_api.py -q
```

生产验收还需覆盖真实模型端点的限流、超时、幂等请求、失败恢复和 Artifact 长期审计，
以及候选审批人与提出人分离的组织流程。
