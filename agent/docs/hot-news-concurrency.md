# 热点新闻批量分析并发

本文说明当前源码中的批量分析执行器，并保留2026-10-05的历史演示结果。
2026-10-26仅核对源码和配置，未重新执行性能或企业模型测试。

同一热点运行中互相独立的新闻分析使用有界并发。查询、聚合、热度排序和证据准备
仍按依赖顺序执行；每篇分析沿用已有输入快照、模型 Port、校验器和校验反馈重试。

```mermaid
flowchart TD
  A[聊天 query_hot_news / 热点工作台 / Temporal Schedule] --> B[SQL或行为取数]
  B --> C[Python 指标、基线、权威排行]
  C --> D[正文与关联证据]
  D --> E[Worker共享并发限额]
  E --> F[新闻A：模型解释与校验]
  E --> G[新闻B：模型解释与校验]
  E --> H[新闻C：模型解释与校验]
  F --> I[按原榜单顺序汇总]
  G --> I
  H --> I
  I --> J[完整成功后保存运行快照]
  J --> K[热点详情 / 聊天工具结果 / SSE答复]
```

## 并发保证

- `HotNewsAnalysisBatch` 使用固定数量协程处理一批新闻，最多创建
  `min(新闻数, 并发上限)` 个任务，不为每篇无限创建协程。
- `ActiveProductionBundleHotNewsService` 在 Worker 生命周期内只创建一个分析执行器。
  不同租户、窗口和 run-scoped 服务共享其 Semaphore，避免每个运行分别占满同样的配额。
- 输入位置绑定结果槽位；先完成的新闻不会抢占榜单第一名。汇总后再沿用既有持久化链路，
  `news_id/rank/analysis_input/model_request_id` 始终属于同一篇新闻。
- 任一任务失败后停止领取新任务，取消其余协程并等待清理，再原样抛出错误。不会用
  `ExceptionGroup` 隐藏原来的 `retryable/request_id` 或破坏 Activity 的错误归因。
- 新执行器不增加重试。单篇分析的校验反馈重试、模型 HTTP 超时和 Temporal Activity
  的有界重试继续由原有层负责；一次单篇分析从初次调用到校验重试都占用一个名额。
- 并发上限设为 1 时走串行路径。空批次不调用模型。部分成功不会被保存成整轮成功。

## 完整调用链和配置

1. `/chat` 的 `ConversationAgentService → ConversationTools.execute_hot_news_query →
   LocalConversationHotNewsQuery → execute_local_hot_news_query`，或热点工作台/调度入口，
   创建原有 `HotNewsMonitorWorkflow`。
2. Workflow 调用 `HotNewsActivities.run_hot_news_window`；`create_hot_news_worker_runtime`
   将 Settings 中的并发配置注入 `ActiveProductionBundleHotNewsService`。
3. 每次运行解析不可变 Production Bundle，并创建 run-scoped
   `HotNewsOrchestrationService`；SQL 查询和指标/排行由 Python 完成，证据转换为
   `EnrichedHotNews`，经过既有身份与顺序校验。
4. `HotNewsAnalysisBatch.analyze` 并发调用 `HotNewsAnalysisService.analyze_with_snapshot`。
   后者生成独立 `HotNewsAnalysisInput` 快照，经 `NativeHotNewsAnalysisRunner →
   StructuredInferenceService → InferencePort` 调用配置选定的本地测试Port或兼容模型接口，
   再通过确定性 Validator。启用模型治理时，推理Port外层还有共享并发/RPM准入。
5. 分析结果按输入顺序组装 `AnalyzedHotNews`，性能记录写入
   `HotNewsRunResult.analysis_execution`。`PostgresHotNewsRunStore` 的已有递归序列化器
   把两者一起保存到 `analysis_runs.result_payload`，无数据库迁移。
6. `HotNewsQueryService.get_run_detail` 将执行记录投影为可选
   `HotNewsAnalysisExecutionView`；热点页面展示任务数、峰值、上限和毫秒耗时，聊天工具
   通过 `render_tool_results` 展示 Python 记录的数值。旧快照没有该字段时返回 null。

实现位于 [hot_news_analysis_batch.py](../app/modules/hot_news/analysis/hot_news_analysis_batch.py)
与 [active_bundle_hot_news.py](../app/modules/hot_news/application/active_bundle_hot_news.py)；
`app/services/` 下的同名文件是兼容导出入口。

配置：`HOT_NEWS_ANALYSIS_MAX_CONCURRENCY`，范围 1–16，Settings 默认 1；仓库根目录
`deploy/docker-compose.native-e2e.yml` 显式设为 3。改变配置需要重启热点 Worker。
这是运行资源配置，调整它不修改 Prompt、模型、业务 Schema、热度规则或 Production Bundle。
既有已完成运行仍幂等复用，不会因为改变并发上限而自动重新分析或补写历史性能记录。

外部依赖保持为已有 PostgreSQL、Redis、Temporal、模型 HTTP Port 和知识检索 Port；
没有新增 Agent 框架、数据库表或自动发布权限。不同新闻的 SQL 取数没有在此修改为并发。

`analysis_execution` 包含 `executor_version=bounded-analysis-v1`、`max_concurrency`、
`observed_concurrency`、`task_count`、`elapsed_ms`。耗时覆盖完整分析批次，包括等待其它
运行释放名额，但不含前面的 SQL、基线和证据检索。峰值是本批次实际同时分析的任务数。
批次终态日志仅记录状态、任务数、上限、峰值和耗时，不记录正文、原始行为或模型输出。

## 可复现演示与验证

在 `agent/` 目录使用已安装dev依赖的 Python 3.11+ 环境执行：

```sh
python -m examples.hot_news_concurrency_demo --concurrency 3 --delay-ms 200
python -m pytest tests/test_hot_news_analysis_batch.py tests/test_hot_news_orchestration.py tests/test_active_bundle_hot_news.py tests/test_native_hot_news_runtime.py tests/test_hot_news_api.py
```

演示经过本地域服务编排和校验，用同 Port 的确定性本地模型，并人为加入等待。
它不是API/Temporal端到端验收。在未设置 `MODEL_GOVERNANCE_CONFIG_PATH` 的普通本地测试环境中，
演示无需数据库、网络、密钥或真实模型；启用该配置后，模型工厂会额外连接Redis准入服务。
示例仍经 `agent/deploy` 兼容链接读取配置，建议在WSL/Linux运行并确认该链接可用。
2026-10-05 的一次历史实测：五篇新闻，串行分析
1043 ms，三路并发 420 ms，分析阶段约 2.48 倍；榜单、权威指标和分析报告均一致。
这不是企业模型质量、真实端点吞吐或端到端响应时间承诺。

并发单测通过 Event 屏障检查实际重叠、上限、乱序完成后的顺序、跨运行共享名额、错误
类型保留、等待取消清理、失败后名额回收和串行兼容；不依赖易抖动的毫秒阈值。
原生链路测试分别使用 1 和 3 的配置验证新闻/报告绑定。API 和持久化测试覆盖性能记录投影。

2026-10-05历史记录为：相关后端回归124项、修复镜像组合回归93项、前端155项通过；
当时前端类型检查、构建、相关ESLint与 `git diff --check` 通过。上述数量不代表当前测试状态。

本文与示例程序也是 [并发PDF生成器](../evaluation/render_concurrency_pdf.py) 的存在性依赖。
该生成器另要求已删除的 `conversation-concurrency.md`，目前会在输入检查处失败；
保留本文不会自动修复该报告入口。生成器使用历史JSON而非重新压测，本次未修改或运行它。

## 当前边界与 TODO

- 本文的Semaphore额度只在每个热点Worker进程内共享；多副本和聊天调用不共享这个名额。
  项目另有可选 [模型治理](../app/model_runtime/governance.py)：设置
  `MODEL_GOVERNANCE_CONFIG_PATH` 后，同一policy/pool与Redis的参与进程共享模型并发/RPM，
  并预留交互容量。它不提供租户公平、TPM或Embedding配额，也不代替本文的批量顺序保证。
- 取消协程会终止本地等待和请求；模型供应商已经接受的推理是否停止，取决于接口合同。
- 并发任务仍属于一个有界 Temporal Activity，未拆成逐篇持久化 Activity/Checkpoint。
  若整轮失败，Activity 重试可能重新分析此前已成功的新闻；HTTP幂等Header的供应商语义
  仍需合同验收。整轮失败的成功子结果没有当成成功报告持久化。
- 企业模型的限流合同、并发质量和长稳仍须单独验收；历史证据ID不合法的模型输出仍会被
  拦截。并发优化不会修复“没有匹配新闻”或“报告未完成”的业务问题。
- `hot-news-concurrency-20261005` 镜像及原容器状态只是当时部署记录，不是当前启动配置。
  当前构建应以所选Compose与源码为准；本次未检查或改变Docker运行状态。
- 并发PDF生成器需移除或替换失效的历史文档依赖后再离线验证，本轮仅修正文档。
