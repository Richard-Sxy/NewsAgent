# NewsAgent 代码精读计划（7 天）

本计划面向希望**特别熟悉**本仓库代码的人。核心方法只有一句：

> 不要按文件夹顺序读，按「一次真实运行」的顺序读；每一段代码都先读它的测试。

配套长期上下文见根目录 `PROJECT_CONTEXT.md`，分层判定见
`writing-agent-service/docs/architecture-layers.md`，按运行顺序的串讲见根目录
`CHAIN_WALKTHROUGH.txt`。

## 使用方式

- 每天约 2–4 小时，分「读文件 → 回答自测问题 → 跑验证命令 → 写当天产出」四步。
- 阅读路径默认在 `writing-agent-service/` 下。
- 所有验证命令用你本机可用的 Python 3.11 环境执行（仓库跟踪的 Linux `.venv` 在
  macOS 不可用，请自建虚拟环境并安装 `pyproject.toml` 依赖）。
- 建议单独开一个笔记文件，把每天的「数据流图 + 疑问」记下来。

## 通用读码方法

1. **测试当规格**：先看 `tests/test_*.py` 的断言，反推行为契约，再看实现。
2. **一次只追一条链**：用 `grep -rn "函数名" app` 追调用，不要并行读多个模块。
3. **分层判定**：控制流/时序/权限 → 编排；数字/规则/校验 → 确定性代码；
   开放文本理解与生成 → Agent；高风险变更与对外动作 → 人工 Gate。
4. **每读一个模块就画一条数据流**：输入对象 → 转换 → 核心类/函数 → 外部依赖 → 输出。

---

## Day 1：全局地图与运行环境

**目标**：理解三个 Loop、分层判定和幂等键，能跑起来并跑绿测试。

**阅读顺序**

1. `PROJECT_CONTEXT.md`（第 1–8、14 节必读）
2. `AGENTS.md`
3. `CHAIN_WALKTHROUGH.txt`（全篇，按运行顺序的串讲）
4. `writing-agent-service/docs/architecture-layers.md`
5. `writing-agent-service/app/main.py`（只看入口装配和路由挂载）

**必须能回答**

- 系统由哪三个 Loop 组成？各自的输入、输出、人工 Gate 是什么？
- 一次热点运行的幂等维度是哪几个字段？在哪里生成？
- 写作任务为什么「先落库、再启动 Temporal」？
- 哪些操作可以自动，哪些必须人工审批？

**验证命令**

```bash
cd writing-agent-service
python -m pytest tests/test_health.py tests/test_hot_news_demo.py -q
python examples/hot_news_demo.py
```

**当天产出**：一页纸画出三 Loop 关系图，标注每层对应目录。

---

## Day 2：写作主链路 —— 入口与编排

**目标**：从 HTTP 请求追到 Temporal Workflow 启动。

**阅读顺序**

1. `app/api/jobs.py`（`create_job` 的幂等落库与启动顺序）
2. `app/schemas/job.py`、`app/models/job.py`、`app/domain/job_scenario.py`、`app/domain/job_status.py`
3. `app/services/job.py`（`create_or_get` 的 `on_conflict_do_nothing`）
4. `app/services/orchestrator.py`（唯一与 Temporal 交互边界）
5. `app/workflows/contracts.py`（`NewsWritingInput` / `StepCommand` / `StepOutcome`）
6. `app/workflows/news_writing.py`（research→outline→sections→reviewer→publish 主干）

**必须能回答**

- 相同 `idempotency_key` 重复请求会发生什么？工作流会重复启动吗？
- Workflow 的每一步为什么用 `step_key` 而不是时间戳？
- 人工 Gate 在哪些位置？超时默认行为是什么？

**验证命令**

```bash
python -m pytest tests/test_jobs_api.py tests/test_job_service.py tests/test_orchestrator_service.py tests/test_job_state.py -q
```

**当天产出**：把 `news_writing.py` 的 `_run_pipeline` 逐段标注成流程图。

---

## Day 3：写作链路 —— Activity、Agent、Artifact、可靠性

**目标**：理解确定性编排如何调用 Agent、如何持久化不可变产物、如何恢复。

**阅读顺序**

1. `app/activities/news_steps.py`、`app/services/news_step_handler.py`
2. `app/services/agents/research.py`、`writer.py`、`reviewer.py`
3. `app/clients/fastgpt.py`（`run_structured` 的 HTTP/错误分类/JSON 解包/Schema 校验）
4. `app/services/artifact_pipeline.py`、`app/models/artifact.py`、`app/storage/s3.py`
5. `app/services/checkpoint.py`、`app/services/recovery.py`、`app/services/outbox.py`
6. `app/services/progress.py`、`app/services/event_publisher.py`、`app/api/events.py`（SSE）

**必须能回答**

- Agent 输出为什么必须走结构化 Schema，而不是自由文本？
- Artifact 的不可变身份是什么（`job_id + logical_key + version`）？
- Outbox 解决什么问题？事件发布失败如何重试？
- Checkpoint/恢复如何保证重放不产生重复副作用？

**验证命令**

```bash
python -m pytest tests/test_news_step_handler.py tests/test_artifact_pipeline.py tests/test_fastgpt_client.py tests/test_checkpoint_service.py tests/test_recovery_service.py tests/test_execution_service.py tests/test_outbox.py tests/test_progress.py tests/test_event_publisher.py -q
```

**当天产出**：画一张「Agent 调用 → Artifact 落 S3 → Outbox → SSE」的时序图。

---

## Day 4：热点 —— 确定性计算与内容富化

**目标**：掌握「数字由 Python/SQL 算，模型只解释」这条铁律的落点。

**阅读顺序**

1. `app/analytics/entities.py`、`data_source.py`、`metric_source.py`
2. `app/analytics/metrics.py`（事件去重与窗口聚合）、`baseline.py`（历史基线）
3. `app/analytics/hot_score.py`（热度分量）、`ranking.py`（排行）
4. `app/analytics/news_content.py`（精确正文仓储）
5. `app/clients/knowledge_base.py`（FastGPT 关联新闻召回）
6. `app/analytics/hot_news_enrichment.py`（内容 + 关联报道富化）
7. `app/retrieval/news_features.py`、`app/retrieval/related_news_reranker.py`（确定性重排）
8. `app/analytics/text2sql_metric_source.py`、`sql_guard.py`（模板优先 + 护栏）

**必须能回答**

- 权威指标和热度分数的唯一来源是哪里？模型能改吗？
- 关联新闻重排用了哪些特征？为什么它是确定性规则而不是模型？
- 行为数据查询为什么不落原始用户明细？

**验证命令**

```bash
python -m pytest tests/test_analytics_data_source.py tests/test_news_metric_calculator.py tests/test_baseline.py tests/test_hot_score.py tests/test_ranking.py tests/test_news_content_repository.py tests/test_hot_news_enrichment.py tests/test_related_news_reranker.py tests/test_knowledge_base_client.py -q
```

**当天产出**：整理一张「指标/基线/热度/排行」公式与代码位置对照表。

---

## Day 5：热点 —— Agent 与运行编排

**目标**：吃透热点主链路契约链和幂等运行。

**阅读顺序**

1. `app/schemas/hot_news.py`（输入/输出强类型契约）
2. `app/services/hot_news_analysis.py`（InputBuilder → Runner → Validator → Service）
3. `app/services/agents/hot_news.py`（固定 App、`mode`、`output_type`）
4. `app/services/hot_news_orchestration.py`（窗口编排核心、`HotNewsRunRequest.idempotency_key`）
5. `app/workflows/hot_news.py`、`app/activities/hot_news.py`、`app/workflows/contracts.py`
6. `app/services/hot_news_run_store.py`（`analysis_runs` 幂等持久化）
7. `app/api/hot_news.py`（榜单/详情/决策/转交写作）
8. `app/services/hot_news_query.py`、`hot_news_decision.py`、`hot_news_writing_handoff.py`

**必须能回答**

- 契约链 `EnrichedHotNews → Input → Report → Validator → AgentResult` 每一步谁负责？
- 校验器必须拒绝哪些情况（news_id 不一致、未知证据、无依据数字）？
- 同一窗口重放为什么不会产生第二次运行、第二次反馈？

**验证命令**

```bash
python -m pytest tests/test_hot_news_agent_module.py tests/test_hot_news_analysis.py tests/test_hot_news_orchestration.py tests/test_hot_news_activity.py tests/test_hot_news_workflow.py tests/test_hot_news_run_identity.py tests/test_hot_news_api.py tests/test_hot_news_writing_handoff.py -q
```

**当天产出**：手写一遍 `HotNewsAnalysisValidator` 的校验规则清单。

---

## Day 6：Data Loop、Model Loop 与 Memory

**目标**：理解反馈如何变成评测数据、候选如何人工晋升、记忆如何分短期/长期。

**阅读顺序**

1. `app/services/data_loop/README.md`（先读它）
2. `app/workflows/data_loop.py`、`app/workflows/data_loop_contracts.py`、`app/activities/data_loop.py`
3. `app/services/data_loop/feedback_collector.py`、`automatic_feedback.py`、`dataset_freezer.py`
4. `app/services/data_loop/offline_replay.py`、`evaluation_gate.py`、`publication_outcome.py`
5. `app/services/production_bundle.py`、`production_bundle_runtime.py`、`active_bundle_hot_news.py`
6. `app/services/memory_context.py`、`memory_context_application.py`、`memory_prompt.py`、`memory_promotion.py`、`memory_promotion_application.py`、`memory_write_application.py`
7. `app/api/data_loop.py`、`app/api/memory.py`、`app/api/dependencies.py`（RBAC 与可信网关）

**必须能回答**

- Feedback Case 的触发来源有哪些？标签为什么要二审（四眼分离）？
- 三层评测集（黄金集/当日 bad case/高风险回归）缺一不可的原因？
- 候选晋升的审批账本与激活账本为什么分离？48 小时超时如何处理？
- 短期/长期记忆的写入、作用域、冲突解析与晋升条件分别是什么？

**验证命令**

```bash
python -m pytest tests/test_feedback_collector.py tests/test_automatic_feedback.py tests/test_dataset_freezer.py tests/test_offline_replay.py tests/test_evaluation_gate.py tests/test_production_bundle.py tests/test_production_bundle_application.py tests/test_memory_context.py tests/test_memory_context_application.py tests/test_memory_promotion.py tests/test_memory_promotion_application.py tests/test_memory_write_application.py tests/test_memory_api.py -q
RUN_TEMPORAL_TIME_SKIPPING=1 python -m pytest tests/test_data_loop_time_skipping.py -q
```

**当天产出**：画「失败/低置信 → Feedback → 标签 → Dataset → 回放 → Gate → 审批 → 激活/回滚」闭环图。

---

## Day 7：数据模型、迁移与横切关注点（复盘）

**目标**：从表结构和迁移理解系统骨架，并做一次完整复盘。

**阅读顺序**

1. `app/models/*.py` 对照 `alembic/versions/*.py`（重点：幂等键、租户复合外键、唯一约束、状态 CAS、不可变触发器）
2. `app/domain/errors.py`、`app/domain/execution.py`、`app/domain/transition.py`
3. `app/db/session.py`、`app/db/base.py`（命名约定与事务边界）
4. `app/storage/s3.py`、`app/observability/*`
5. `app/clients/enterprise/*`（企业 Adapter 契约，部署侧实现）
6. `app/hot_news_dependencies.py`、`app/hot_news_bootstrap.py`、`app/worker.py`、`app/data_loop_worker.py`（装配与 fail-closed）

**必须能回答**

- 为什么热点运行/事件/反馈/评测各自独立聚合根，而不是挂在 `WritingJob` 上？
- 哪些表用复合外键保证租户隔离？哪些用不可变触发器？
- 依赖工厂为什么未配置就 fail-closed，而不提供默认假数据？

**验证命令**

```bash
python -m pytest tests/test_models.py tests/test_data_loop_integrity_migration.py -q
python -m alembic heads
python -m alembic upgrade head --sql > /tmp/migration.sql
python -m pytest -o addopts="" -q          # 全量回归
```

**当天产出**：一页「全系统数据流 + 表关系」总结，并列出仍属企业侧/部署侧的 TODO。

---

## 精读进度清单

- [ ] Day 1 全局地图与运行环境
- [ ] Day 2 写作入口与编排
- [ ] Day 3 Activity / Agent / Artifact / 可靠性
- [ ] Day 4 热点确定性计算与富化
- [ ] Day 5 热点 Agent 与运行编排
- [ ] Day 6 Data Loop / Model Loop / Memory
- [ ] Day 7 数据模型 / 迁移 / 复盘

## 已知坑（读码时不要被误导）

- `app/agents/hot_news_analysis.py` 是遗留/在改文件，主链路走
  `app/services/hot_news_analysis.py`。
- `app/analytics.py` / `entities.py` 附近有疑似遗留目录，不被热点主链路引用。
- 仓库错误跟踪了 `.venv`、`__pycache__`、egg-info 和 `.env`；跟踪的 Linux `.venv`
  在 macOS 不可用，请自建环境。
- `FastGPT/` 是上游完整工程，是知识库/模型应用基础设施，不是本项目的自研业务能力。
- 企业行为数据、内容库、网关/IdP 属部署侧 Adapter；仓库里是 Port + 本地 Adapter，
  不能据此判断为「功能缺失」。

## 读完之后的进阶验证

1. 不看代码，口头讲清一次热点运行和一次 Data Loop 的完整调用链。
2. 给任意一个改动（如新增一种热度分量），说出需要改哪些文件、哪些测试。
3. 对照 `PROJECT_CONTEXT.md` 第 3 节，独立复核各模块完成度是否与代码一致。
