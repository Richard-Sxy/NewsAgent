# 数据分析接入运营 DataLoop

实现核对日期：2026-10-07。现有数据分析已接入配置 DataLoop 的固定 Activity，同时保留聊天 Tool 入口。运行代码已完成；开关默认关闭，本次未重启当前部署。

## 完整调用链

```text
运营 POST /api/v1/data-loop/runs（可信租户、RUN 权限）
→ DataLoopOrchestrator.start → HotNewsDataLoopWorkflow.run
→ freeze_dataset：冻结已批准案例
→ Temporal patch 分支：run_data_loop_step / analyze_dataset
→ HotNewsDataLoopStepHandler._analyze_dataset
  → EvaluationDatasetFreezer.load_manifest：租户、内容哈希、案例索引校验
  → S3DataLoopArtifactStore.find_json：优先读取首次报告回执
  → 未生成报告且已启用：DataLoopDatasetAnalysisService.analyze
    → 按 lineage.run_id 去重并校验冻结案例血缘
    → HotNewsQueryService.get_run_detail：同租户完整、已完成运行
    → 核验 run/key/Bundle/window/news/指标/热度与冻结输入一致
    → project_dataset：提取完整榜单的聚合指标和热度
    → AnalysisRunner / RemoteAnalysisRunner：固定 overview、quality，metric=ctr
    → 同一个 data_analysis.engine.analyze：整数/Decimal 计算
  → put_json_once：内容寻址报告 + receipt.json，固定首次成功提交回执的报告
  → Workflow 仅保存状态、URI/SHA256、五项整数覆盖计数
→ 原 attribute_errors
→ evaluate_candidate：核验报告与 Fresh Manifest 绑定，引用写入评测 Artifact
→ 原三层回放和确定性 Gate
→ 原 48 小时人工审批 → 配置激活 / 拒绝 / 超时拒绝
```

查询链：`GET /runs/{workflow_id}` → 租户隔离的 Workflow Snapshot → 控制台 `RunsPanel.vue` 显示状态和覆盖数。查看报告调用 `GET /runs/{workflow_id}/dataset-analysis`，要求 READ 权限；按可信 Snapshot 引用读取 S3，再核验哈希、租户、Dataset、版本、状态和摘要。客户端不能指定任意 Artifact URI。

聊天链继续通过 `ConversationTools._analyze` → `project_dataset` → 相同 Runner/engine。聊天模型可以选择批准的操作；DataLoop 的两项操作由 Python 固定，不依赖模型计划。

## 分析范围和输出

冻结 Feedback Case 缺少原始排名和阅读时长，不能补零或补排名伪装成全榜。新服务读取案例引用的完整运行快照，各运行独立统计；多个案例引用同一 run 时只分析一次，并保留 case/news/label 版本映射。

报告 `scope=referenced_ranked_runs`，单个引擎结果仍是 `ranked_news_only`。不同运行、窗口、Bundle 的业务总量与 UV 不相加。案例来源、问题类型、严重度和标签分布是筛选后的样本统计，不能推导全榜错误率、推送提升或因果关系。

报告保存固定版本、Dataset SHA256、聚合投影及其哈希、overview/quality、案例映射、不可分析理由和范围限制。新闻标题/正文、运营自由文本、模型输出、原始用户行为、执行耗时和后端请求 ID 不进入数值分析输入或稳定报告。来源不存在、快照冲突、完整榜单超限等显示固定 reason_code 和 degraded，不截断、不换用其他运行。

默认关闭时也保存 disabled 报告回执。回执首次成功提交后，开关改变不影响旧步骤重试，它继续复用已绑定报告；如需新分析，须启动新的业务幂等键运行。若仅报告对象上传成功、回执尚未提交，重试可按当前配置重新计算，此时结果和配置尚未固定。分析仅增加审批证据，未替代 Gate 阈值。数据库暂态错误按现有规则重试；分析最终失败则记录 degraded 并继续原评测，不能跳过 Gate 或人工审批。

## 依赖、配置和版本

复用 PostgreSQL 的保存运行、S3/MinIO Artifact、Temporal 专用队列和现有固定分析 Worker。`app/data_loop_worker.py:create_data_loop_worker_runtime` 装配 Runner 与同租户 SourceRunReader，退出时关闭远端 Runner、模型 Port 和数据库。

| 配置 | 行为 |
|---|---|
| `DATA_LOOP_DATA_ANALYSIS_ENABLED=false` | 新增独立开关；与聊天开关无关，true 时装配分析服务 |
| `DATA_ANALYSIS_BACKEND` | 复用 process/docker/service；process 只允许 local/development/test/e2e |
| `DATA_ANALYSIS_SERVICE_URL/TOKEN` | service 模式必填；生产遵循现有 TLS 和 OS 资源限制 |
| `DATA_ANALYSIS_MAX_ROWS` | 默认 200；超限拒绝整榜，不截断 |
| `DATA_ANALYSIS_TIMEOUT_SECONDS/MAX_INPUT_BYTES/MAX_OUTPUT_BYTES/MAX_CONCURRENCY/MEMORY_MB` | 复用单次预算和并发限制 |
| `DATA_LOOP_MAX_CASES_PER_COHORT` | 默认 20，最多 100；冻结和分析使用同一上限 |
| `TEMPORAL_DATA_LOOP_TASK_QUEUE`、`ARTIFACT_*` | 继续使用现有队列、前缀、SSE/KMS 配置 |

standalone Compose 已暴露启用开关与 service 配置；分析服务部署入口仍为 `deploy/docker-compose.analysis-service.yml`，本次未启动新的生产服务。

报告逻辑键绑定 tenant、运行业务幂等键、step、Dataset ID/SHA256 和 `data-loop-dataset-analysis-v1`。S3 条件写固定首次成功提交的 receipt，竞争返回该回执的报告；读取有路径、身份、哈希检查，执行元数据不参与稳定报告哈希。

流程使用 `workflow.patched('data-loop-dataset-analysis-v1')`。旧历史保持原三步 Activity 顺序和原评测输入，新历史增加分析步骤。保持 patch，直至旧历史退出保留期；后续计算/报告语义变化须升级报告版本并审查 patch，不能原地改变冻结证据含义。[Temporal 官方版本与 patch 文档](https://docs.temporal.io/develop/python/workflows/versioning)

## 测试入口与 TODO

```bash
python -m pytest tests/test_data_loop_dataset_analysis.py \
  tests/test_data_loop_analysis_integration.py tests/test_data_loop_step_handler.py \
  tests/test_data_loop_artifact_store.py tests/test_data_loop_workflow.py \
  tests/test_data_loop_api.py tests/test_data_loop_worker.py -q
```

测试涵盖真实固定 process 计算、完整运行/重复/冲突/来源缺失、报告稳定性、回执优先、独立配置、评测引用与 Gate 保持、API 权限与跨材料拒绝、条件写竞争和篡改拒绝。前端入口为 `npm run typecheck` 与 `npm test`。

真实历史回放须显式选择本地测试 Temporal，使用随机队列，不调用企业模型或业务数据库：

```bash
RUN_TEMPORAL_ANALYSIS_REPLAY=1 TEMPORAL_ANALYSIS_TEST_ADDRESS=127.0.0.1:17233 \
  python -m pytest tests/test_data_loop_analysis_replay.py -q
```

旧版三步与新版四步历史已由当前 Workflow 的 Sandbox Replayer 验证。本轮未宣称完成企业数据/模型质量或生产全链路验收。

TODO：部署环境启用及完整 PostgreSQL/S3/Temporal/分析服务联验；48 小时 timer 的独立 time-skipping 验收；缺失历史快照的保留/归档治理；投递后聚合 Outcome、策略候选评测与团队策略注册表。当前提供分析证据，尚未自动沉淀或激活运营策略，也未接真实新闻推送。
