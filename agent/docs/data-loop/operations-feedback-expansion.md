# 运营团队反馈与数据集扩充

本变更扩充的是运营决定形成的 **Feedback Case 案例池**。新提交的采纳、驳回、延后、修正都会留存案例；案例初始状态为 `needs_label`，不自动授予“正确/错误”标签，也不发送新闻通知。正式评测样本仍须提交结构化标签、由另一位审核人批准，再冻结为不可变版本。

## 调用链与保存内容

```text
热点工作台 HotNewsDecisionForm / DataLoop DecisionsPanel
→ hotNewsApi.recordDecision / dataLoopApi.recordOperatorDecision
→ POST /api/v1/hot-news/decisions 或 /api/v1/data-loop/operator-decisions
→ 可信网关 tenant/user 与 DECIDE / FEEDBACK_WRITE 权限
→ RecordHotNewsDecisionCommand（动作、说明、可选 operator_feedback）
→ HotNewsDecisionService.record_decision
  → PostgresHotNewsRunStore.get_analysis_memory：同租户 run_id + news_id
  → 同一 PostgreSQL 事务写 hot_news_decisions
  → AnalysisFeedbackCollector.collect_from_operator_decision
    → 已保存分析输入/已校验输出 + 决定引用 + 分类/理由码
    → collect_from_analysis_memory → collect
    → 租户/血缘校验、稳定内容 SHA256、幂等冲突检查
    → PostgresAnalysisFeedbackRepository.insert_case → feedback_cases(needs_label)
→ API 返回决定 ID 与 created；运行详情/案例列表展示保存的分类
→ POST /feedback-cases/{id}/labels → SubmitAnalysisFeedbackLabelCommand
→ 独立标签二审 → 已批准标签版本
→ 手动冻结或 DataLoop 窗口挑选 → EvaluationDatasetFreezer
→ PostgreSQL 数据集索引 + S3/MinIO manifest 与 SHA256
→ 原三层回放、确定性 Gate、人工配置批准/激活或拒绝
```

两条 HTTP 入口使用同一个决定服务。租户与操作者来自可信身份，不能由 `operator_feedback` 指定。模型输出、新闻正文、运营说明都是不可信文本；它们不能授予访问、审批、发布或实际触达权限。

分类保存于 `hot_news_decisions.operator_feedback` 的可空 JSONB，并复制到案例的 `source_reference.operator_feedback`，以保留当时决定及其不可变回放血缘。它不是 `correction_payload`：更正 JSON 仍不直接成为评测标签。新闻分析快照和必要聚合指标继续沿用原合同，不增加企业原始用户行为。

## 分类与受控理由码

`operator_feedback` 可省略；提供时结构如下：

```json
{
  "schema_version": "operations-feedback-v1",
  "category": "editorial_judgment",
  "reason_codes": ["low_public_value"]
}
```

`schema_version` 默认 `operations-feedback-v1`。支持以下四类判断，动作与分类彼此独立：

| category | 用途 | 是否直接判定模型错误 |
|---|---|---|
| `analysis_error` | 事实、归因、证据或分析质量问题 | 仍须人工标签和独立二审 |
| `editorial_judgment` | 公共价值、增量、重复、表达等编辑选择 | 否，`problem_type=other` |
| `strategy_judgment` | 人群、时机、频控、渠道等策略条件 | 否，`problem_type=other` |
| `positive_example` | 运营认为有用、值得保留的正向案例 | 否，`problem_type=other`，不自动标 correct |

受控理由码：`analysis_supported`、`useful_recommendation`、`insufficient_evidence`、`fact_or_attribution_error`、`low_public_value`、`audience_mismatch`、`no_material_update`、`duplicate_event`、`frequency_budget_exhausted`、`timing_inappropriate`、`content_expired`、`copy_misleading`、`channel_or_consent_mismatch`、`risk_requires_escalation`、`other_with_explanation`。

未知分类、版本、理由码或额外字段会被 Schema 拒绝。自由说明仍在 `reason` 中填写；理由码不替代审核证据，也不声明具有真实投递结果。

## 自动 Fresh 与手动冻结

四种动作分别产生 `operator_accepted`、`operator_rejected`、`operator_deferred`、`operator_corrected` 来源。新 accepted/deferred 未提供结构化分类时统一使用 `other/low`；无分类的 rejected/corrected 保留历史默认行为。显式非 `analysis_error` 分类无论提交什么错误类型，都归为 `other`。

自动 Fresh bad case 只选择截止时点已批准的最新有效标签，且 verdict 为 `incorrect` 或 `partially_correct`。显式 `editorial_judgment`、`strategy_judgment`、`positive_example` 被排除，即使误填错误标签也不能污染自动坏例集。未分类运营案例仅保留旧 rejected/corrected 的错误来源语义，accepted/deferred 没有分类时不会进入 Fresh；原自动校验/检索等非运营来源仍须满足已批准错误标签。`correct`、`not_evaluable`、未二审案例不会进入自动 Fresh。

手动冻结允许审核人明确选择案例与层，但新建 Fresh 版本也必须通过上述坏例资格检查，不能用手选绕过分类。已复核正向案例可显式选择 Golden；动作是“采纳”不会自动归 Golden，动作是“驳回”也不会自动归 High-risk。人工要审核完整输入、主驱动、必需/禁止证据、必需指标、局限条件及严重度，再决定样本用途。已冻结旧版本的读取与回放不会被新过滤规则改写。

## 幂等与旧记录兼容

- 新决定和 Case 在同一事务提交；案例写入失败会回滚决定，避免新记录只存动作却没有回流。
- Collector 的目标不存在、幂等冲突、持久化失败在决定服务统一转换，两条 API 分别返回 404、409、503，并保持事务回滚。
- 同幂等键内容相同返回原决定；改变分类或理由码也属于内容冲突，不能静默覆盖。
- 历史 accepted/deferred 的原请求重放不会暗中补建 Case。本变更不会自动批量迁移这些历史运营判断。
- 旧 rejected/corrected 没有 `operator_feedback` 时，案例引用定向省略这个新字段；其他原有 nullable 字段保留原序列化，历史内容哈希不被改写。
- 冻结 Dataset 不修改；后续标签和分类更正应保留血缘并创建新版本。

## 四动作合成示例

下面只演示请求结构，所有 `synthetic-` 新闻、理由与 UUID 都是文档合成数据。没有写入真实数据库；实际调用必须引用当前租户已经完成的运行与新闻，服务会拒绝不存在的示例身份。共同字段：

```json
{
  "run_id": "00000000-0000-4000-8000-000000000101",
  "news_id": "synthetic-operations-001",
  "decision_type": "accepted",
  "reason": "合成示例：分析有证据支持，可保留作正向案例",
  "operator_feedback": {
    "category": "positive_example",
    "reason_codes": ["analysis_supported", "useful_recommendation"]
  },
  "idempotency_key": "synthetic-operations-accepted-v1"
}
```

另外三条使用不同 `news_id` 与业务幂等键：

| 动作 | news_id / idempotency_key | category / reason_codes | 合成说明与补充字段 |
|---|---|---|---|
| 驳回 `rejected` | `synthetic-operations-002` / `synthetic-operations-rejected-v1` | `analysis_error` / `["insufficient_evidence"]` | `reason`：合成示例：解释引用证据不足，进入待标注；不自动标 incorrect |
| 延后 `deferred` | `synthetic-operations-003` / `synthetic-operations-deferred-v1` | `strategy_judgment` / `["timing_inappropriate", "frequency_budget_exhausted"]` | `reason`：合成示例：当前时机不合适；不把频控限制当模型事实错误，不创建投递 Timer |
| 修正 `corrected` | `synthetic-operations-004` / `synthetic-operations-corrected-v1` | `editorial_judgment` / `["copy_misleading"]` | `reason`：合成示例：表达需收敛；`correction_payload={"trend_assessment":"仅描述当前窗口观察，避免扩大推断"}`；仍须另行标签二审 |

## 依赖、配置与验证

本次增加 `alembic/versions/20261010_0021_operations_feedback.py` 数据库迁移和 nullable JSONB，扩展反馈来源的数据库约束；部署前执行已有 Alembic 升级流程。核心依赖是 FastAPI/Pydantic、SQLAlchemy/PostgreSQL；数据集下游继续使用 Temporal、S3/MinIO 和 Python 模型 Port，不新增外部 Agent/知识库框架。`app/schemas/operations_feedback.py:OperationsFeedback` 定义分类与理由码，`is_fresh_bad_case_eligible` 供新 Fresh 冻结检查使用；`PostgresEvaluationDatasetRepository.list_approved_case_ids_for_window` 使用等价 SQL 条件约束自动选择。

无需新开关。继续使用 `DATA_LOOP_GATEWAY_TOKEN`、可信租户/用户角色头与已有数据库设置；`MODEL_RUNTIME_CONFIG_PATH`、`HOT_NEWS_RUNTIME_MANIFEST_JSON`、`DATA_LOOP_MAX_CASES_PER_COHORT`、`DATA_LOOP_REPLAY_MAX_CONCURRENCY` 继续约束下游运行。密钥通过原渠道注入，本说明和测试不读取 `.env`。

从 `agent/` 的已有依赖环境执行：

```bash
python -m pytest tests/test_operations_feedback.py \
  tests/test_operations_feedback_migration.py \
  tests/test_hot_news_decision_service.py tests/test_feedback_collector.py \
  tests/test_analysis_feedback_schema.py tests/test_analysis_feedback_repository.py \
  tests/test_hot_news_api.py tests/test_data_loop_management_api.py \
  tests/test_dataset_freezer.py -o addopts= -q
```

`test_operations_feedback.py` 使用合成快照和独立替身，验证四动作回流、分类边界、身份透传、事务失败与 HTTP 错误映射、幂等内容冲突、JSON 往返、历史哈希、自动 Fresh 过滤与手动冻结区别。`test_operations_feedback_migration.py` 使用 Alembic/PostgreSQL 方言离线编译检查升级/降级 SQL、约束名称与回退保护；它不能替代真实数据库迁移、部署后浏览器或完整 PostgreSQL/S3/Temporal 验收。前端继续运行 `npm test`、`npm run typecheck`、`npm run build`。

2026-10-10 在当前工作区的 Python 3.11 依赖环境运行上述九个模块，结果 **132 passed**。出现一条已有的 Starlette/httpx TestClient 弃用提示；未使用真实业务数据库、企业接口或投递服务。该结果只证明这些契约和隔离测试，不声明部署后完整链路已验收。

## 剩余 TODO

最终工作区验证：反馈、Schema、决定服务、两套 API、冻结、回放、Gate、Workflow、数据分析与迁移编译共15个模块 **216 passed**；前端覆盖231项测试，类型检查、改动文件ESLint与生产构建通过。前端已有端口测试在沙箱中因EPERM失败，获准重跑后通过。较宽检查另有两项既有会话模型失败：模型表清单未包含 `conversation_inputs`，以及其外键 `ondelete="CASADE"` 不能编译；本次未改这些会话文件。

部署仍未执行：先在目标环境按既有配置升级到 `20261010_0021`，再更新API、热点Worker与DataLoop Worker，验证四类决定、标签二审、冻结和回放。迁移测试覆盖PostgreSQL离线DDL编译和审计保护，不代表真实数据库执行或部署验收。真实团队反馈来源尚待提供；上面的示例与测试快照均为合成数据。

1. 历史 accepted/deferred 的显式审核与回补治理；当前不自动迁移，不能伪造当时分类。
2. 标签次日才批准的迟到消费、水位和回补；仍受现有发生窗与 cutoff 约束。
3. 事件/转载去重、来源与时间 holdout、分层覆盖量及新生产 Gate；扩充案例数不等于泛化验收。
4. 完整 PushSuggestion 审批版本、延后 Timer、团队策略注册表、支持案例与反例、策略专属评测。
5. 模拟/真实投递与聚合 Outcome；采纳不证明发送、点击提升或因果效果。
6. 真实业务数据覆盖、数据库迁移实跑、原生 DataLoop E2E driver 和部署后的前端到 Worker 联验、企业模型合同与质量验收。
