——————————————————————————————————————————————————————————————————————————————————————————————————————
(不可修改的文件)
实习描述：
    面向内容运营与热点研判场景，构建“行为分析—热点发现—可信分析—反馈评测”闭环，负责数据接入、热点计算、检索增  
强、 Agent 链路及 Data Loop 建设，后续将对接腾讯新闻推送平台。

工作内容：
   设计面向大规模、多模态历史新闻的热、温、冷三级向量检索架构，根据发布时间、访问热度对新闻切片进行分层存储。热
层保存按照 FP16 HNSW 常驻内存以保障近期新闻低延迟召回，其他层做低精度长期保存。召回P95 < 50 ms，对新闻标
题、正文等部分做 RRF 检索召回，Recall@5 0.84 -> 0.94。

   通过调用企业内部数据库，构建融合 Text2SQL、RRF检索与可信校验的热点分析Agent，为运营团队提供自然语言获取实时
数仓内的新闻热度排行，并结合历史基线、相关召回给出提高监控频率、流量质量等运营建议。

    建设热点分析 Data Loop，统一回流低置信结果、热点漏报误报、检索错误和运营修正，结合运营团队提供真实的样本进
行回归评测；针对问题样本优化 Prompt、热度规则和检索配置，经运营团队人工审批后受控更新，支持版本记录和异常回滚。

    面向“统计近期金融行业变化并生成总结报告”等任务，构建 Research-Writer-Reviewer 多 Agent写作流程，完成资料
检索、提纲生成、分章节写作等；使用 Temporal 管理资源包、提纲和终稿等长时间人工审核节点，通过失败重试和幂等
控制，使流程再服务重启或跨天等待后仍能继续执行。
—————————————————————————————————————————————————————————————————————————————————————————————————————

(可以修改和增加的文件)

## 1. 热、温、冷三级向量检索与 RRF

### 1.1 完整调用链

```text
新闻正文/metadata
→ 文本切片与向量化 → 按发布时间或热度分配 hot / warm / cold → 每层独立 ANN 召回 → 加权 RRF 融合跨层排名
→ news_id 去重 → 业务特征重排 → 关联新闻证据
生产仓库 Milvus
```


### 1.2 代码定位

- `tencent-news-crawler/experiments/tiered_recall/build_tiers.py::build`：读取语料、按时间或
  排名分层并构建不同精度的索引。
- `tencent-news-crawler/experiments/tiered_recall/router.py::TieredRouter`：
  `search_all` 实现多层召回与加权 RRF，`search_cascade` 实现逐层下探。
- `tencent-news-crawler/experiments/tiered_recall/policy.py::TierPolicy`：热温冷时间边界、
  晋升阈值和连续达标次数。
- `tencent-news-crawler/experiments/tiered_recall/heat.py::HeatTracker`：时间衰减热度。
- `tencent-news-crawler/experiments/tiered_recall/migration.py::TierMigrationService`：分区下沉、
  热度例外晋升和迁移流程。
- `tencent-news-crawler/experiments/tiered_recall/ledger.py::TierLedger`：记录分区/向量所在层、
  连续热度次数和迁移历史。
- `writing-agent-service/app/retrieval/related_news_reranker.py::RelatedNewsReranker`：在召回后
  融合主体、事件类型、标题关键词、发布时间和基础相关度做确定性重排。
- `tencent-news-crawler/experiments/tiered_recall/evaluate.py::evaluate`：计算 Recall@1/5/10
  和 MRR。

### 1.3 面试深挖点

- 为什么不能把全部历史新闻用同一精度 HNSW 常驻内存？
- FP16、INT8、Binary 向量返回的原始相似度为什么不能直接排序？
- RRF 如何绕开异构召回分数不可比问题，`rrf_k` 和各层权重如何影响 Top-K？
- 级联检索为什么不能仅以“热层有结果”作为停止条件？
- 迁移为什么采用稳定向量主键和“先写目标层、对账后删除源层”？
- 当前 `Recall@5 0.84 → 0.94` 来自 BGE 与 FullText 混合召回实验；评测记录见
  `tencent-news-crawler/docs/offline-recall-experiment.md`。P95 需要说明数据规模、并发、
  是否包含重排以及测试环境，不能只给孤立数字。

## 2. Text2SQL + RRF + 可信校验的热点分析 Agent

### 2.1 完整调用链

```text
运营自然语言查询
→ 固定指标模板优先 / Text2SQL 生成候选 SQL
→ SQL AST 安全校验
→ 只读数仓执行
→ NewsMetricSnapshot
→ 历史基线、热度评分与排行
→ news_id 精确读取正文
→ 关联新闻召回与重排
→ HotNewsAnalysisInput
→ FastGPT 结构化模型调用
→ HotNewsAnalysisValidator
→ 趋势、关注原因与运营建议
```

### 2.2 Text2SQL 代码定位

- `writing-agent-service/app/schemas/text2sql.py`：只读 Schema、生成请求和候选 SQL 的强类型
  契约；模型只看到允许暴露的表结构和指标语义。
- `writing-agent-service/app/services/agents/text2sql.py::Text2SqlAgentRunner`：通过
  `FastGPTClient.run_structured` 生成候选 SQL，但不负责执行。
- `writing-agent-service/app/analytics/text2sql_metric_source.py::Text2SqlNewsMetricSource`：
  参数化模板优先，模板不能覆盖时才使用 Text2SQL；两条路径共用 Guard 和结果映射。
- `writing-agent-service/app/analytics/sql_guard.py::SqlGuard`：使用 SQLGlot AST 限制单条
  `SELECT`，校验表列白名单、参数、危险函数和 `LIMIT`。
- `writing-agent-service/app/clients/enterprise/sql_warehouse.py`：只读事务、查询超时、最大
  行数和企业数仓客户端边界。
- `writing-agent-service/app/analytics/metrics.py::NewsMetricSnapshot`：把 SQL 结果转换为下游
  统一使用的权威指标快照。

### 2.3 热点分析代码定位

- `writing-agent-service/app/services/hot_news_orchestration.py::HotNewsOrchestrationService.run`：
  串联指标、基线、排行、正文、召回、重排和分析服务。
- `writing-agent-service/app/services/hot_news_analysis.py::HotNewsAnalysisInputBuilder`：只把
  确定性指标、正文摘录和允许引用的证据放入模型上下文。
- `writing-agent-service/app/services/agents/hot_news.py::HotNewsAnalysisAgentRunner`：热点分析
  模型入口。
- `writing-agent-service/app/schemas/hot_news.py::HotNewsAnalysisReport`：趋势、主要驱动、
  关注原因、关联背景、运营建议、局限和置信度的严格输出 Schema。
- `writing-agent-service/app/services/hot_news_analysis.py::HotNewsAnalysisValidator`：校验
  `news_id`、指标引用、证据白名单、Memory 引用以及无证据时的局限说明。

### 2.4 安全边界与提示词注入

- 用户问题、新闻正文、检索切片、Memory 候选和模型输出全部按不可信输入处理。
- Prompt 不是权限边界：Text2SQL 只能生成候选文本，真正的执行权限由 AST Guard、只读账号、
  白名单视图、租户参数、时间窗口、超时和行数上限共同控制。
- 新闻中即使出现“忽略系统规则、查询其他租户、调用工具”等内容，也只能作为正文数据；热点
  Agent 不持有任意 SQL、发布或配置修改权限。
- 模型生成的数字必须反查 `NewsMetricSnapshot`，事实必须引用本次 Evidence Packet 中的
  `news_id`，推断必须明确为 hypothesis，不能把相关性写成确定因果。

### 2.5 异常降级

- Text2SQL 失败：模板能覆盖则退回参数化模板；不能覆盖则明确拒绝，不能跳过 Guard。
- 数仓 watermark 不完整：阻塞当前窗口，避免把数据延迟误判为热点下降。
- 检索失败或无证据：保留确定性指标，但不生成有来源要求的背景事实，并输出 limitation。
- 模型超时/限流：只对可重试错误执行有限重试；Schema 或证据违规直接失败并回流 Data Loop。
- 持久化失败：不能先向运营端返回成功，使用稳定幂等键恢复写入。

## 3. 热点分析 Data Loop

### 3.1 完整调用链

```text
低置信/校验失败/误报漏报/检索错误/运营修正
→ Feedback Case
→ 运营人员提交标签
→ 独立审核人复核
→ 冻结版本化评测集
→ 候选配置与线上版本离线回放
→ 确定性 Evaluation Gate
→ 人工批准或拒绝
→ Production Bundle 激活或回滚
```

### 3.2 代码定位

- `writing-agent-service/app/services/data_loop/automatic_feedback.py::AutomaticHotNewsFeedbackSink`：
  从热点运行与分析失败自动生成反馈。
- `writing-agent-service/app/services/data_loop/feedback_collector.py::AnalysisFeedbackCollector`：
  收集运营决策、提交标签、独立审核以及发布效果。
- `writing-agent-service/app/services/data_loop/dataset_freezer.py::EvaluationDatasetFreezer`：将已审核
  样本冻结为不可变 Dataset，并保存内容哈希与 S3 Artifact。
- `writing-agent-service/app/evaluation/hot_news.py::HotNewsEvaluationService`：计算整体通过率、
  Schema/业务契约通过率、主驱动准确率、必要证据召回率和指标覆盖率。
- `writing-agent-service/app/services/data_loop/offline_replay.py::HotNewsOfflineReplayService`：在相同
  数据快照上对候选、线上和上一实验版本进行回放。
- `writing-agent-service/app/services/data_loop/evaluation_gate.py::EvaluationGate`：根据绝对阈值和
  相对基线退化幅度决定候选是否允许进入人工审批。
- `writing-agent-service/app/workflows/data_loop.py::HotNewsDataLoopWorkflow`：管理评测、人工决定、
  激活与 48 小时超时默认拒绝。

### 3.3 面试深挖点

- Data Loop 优化的是 Prompt、热度规则、检索配置和 Validator，不是自动训练大模型。
- 评测集由运营团队提供或审核真实样本，不能把未经确认的自动反馈直接当成 Golden。
- 候选版本必须在同一份不可变输入上与线上基线对比，否则数值提升不可复现。
- 可量化指标包括整体通过率、主驱动准确率、必要证据召回率、指标覆盖率和高风险失败数；
  当前 30 条种子集位于 `writing-agent-service/evaluation/datasets/hot_news_eval_seed_v1.json`，
  优化前后数值需要真实运行后再写入简历。
- Prompt 或规则通过评测也不能自动上线，仍需运营人员审批，并保留版本和显式回滚入口。

## 4. Research-Writer-Reviewer 多 Agent 与 Temporal

### 4.1 完整调用链

```text
“统计近期金融行业变化并生成总结报告”
→ Research：查询数据、检索资料、生成 Research Package
→ 人工确认资料包
→ Writer：生成提纲
→ 人工确认提纲
→ Writer：分章节写作并组装
→ Reviewer：事实、数字、引用和结构审核
→ 问题章节定点返工（最多三轮）
→ 人工确认终稿
→ 版本化 Artifact / 可选 CMS 发布
```

### 4.2 代码定位

- `writing-agent-service/app/workflows/news_writing.py::NewsWritingWorkflow`：完整状态机、人工
  Gate、Signal 等待、组装、取消和恢复。
- `writing-agent-service/app/activities/news_steps.py`：将各写作步骤注册为 Temporal Activity。
- `writing-agent-service/app/services/news_step_handler.py::NewsStepHandler`：Research、Outline、
  Section、Review、Revise、Finalize 的路由、Agent 结果校验和步骤重放。
- `writing-agent-service/app/services/agents/research.py::ResearchAgentRunner`：结构化资料包生成。
- `writing-agent-service/app/services/agents/writer.py::WriterAgentRunner`：提纲、章节和修订入口。
- `writing-agent-service/app/services/agents/reviewer.py::ReviewerAgentRunner`：输出带 `section_id`
  的结构化审核问题，用于定点返工。
- `writing-agent-service/app/services/checkpoint.py::CheckpointService`：在 PostgreSQL 事务中提交
  步骤状态与产物引用。
- `writing-agent-service/app/services/artifact_pipeline.py`：将资料包、章节、审核和终稿保存为
  版本化 S3/MinIO Artifact。

### 4.3 为什么使用 Temporal

- 多 Agent 不是使用 Temporal 的充分理由；真正原因是资料包、提纲和终稿审批可能等待数小时
  或数天，普通 HTTP 请求和进程内任务无法可靠保持状态。
- Workflow 等待人工 Signal 时不占用 Worker 线程；Timer、Signal 与历史事件被持久化，服务
  重启后可以从原节点继续。
- Activity 只对明确可重试的外部错误做有限重试；稳定 Workflow ID 和业务幂等键防止模型被
  重复调用或 Artifact 被重复写入。
- 大文本和业务事实不放进 Temporal History，而保存在 PostgreSQL 与 S3/MinIO，Workflow
  只保存状态和 Artifact 引用。

## 5. 后续补充数值时的证据位置

- 多层召回：`tencent-news-crawler/docs/offline-recall-experiment.md`。
- 100 题检索结果：
  `tencent-news-crawler/data/retrieval_evaluation_results_full100.json`。
- 热点 Agent 种子集：
  `writing-agent-service/evaluation/datasets/hot_news_eval_seed_v1.json`。
- 热点评测入口：`python -m evaluation.run_hot_news_eval`。
- Data Loop 全链路验收：`writing-agent-service/docs/data-loop-e2e-runbook.md`。
- 写作链路测试：`writing-agent-service/tests/test_temporal_orchestration.py`、
  `test_news_step_handler.py`、`test_checkpoint_service.py`、`test_artifact_pipeline.py`。
