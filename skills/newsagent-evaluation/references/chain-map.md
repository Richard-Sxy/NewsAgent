# NewsAgent 评测链路地图

核对时间：2026-10-16；路径相对仓库根目录。执行时重读相关代码，不沿用旧测试数字。

## 1. 采集与知识入库

**调用链**：`news/scripts/discover_and_ingest.py` → `DiscoveryIngestService.discover_and_ingest` → `NewsIngestService.ingest_url` → 抓取/缓存/标题处理/`format_article_text` → `NewsAgentKnowledgeClient.index_news_document` → `POST /api/v1/knowledge/documents`（`agent/app/api/knowledge.py`）→ 普通模式 `PostgresKnowledgeStore.upsert_documents` → `split_text`/Embedding → 文档/切片/索引台账；生命周期模式转 `KnowledgeLifecycleService` 接受 job → Worker 建投影。

**输入/输出**：URL/正文/news_id/source_revision/租户 → 规范化 `KnowledgeDocument`、切片、向量和状态。生命周期 created/updated 是接受任务，不等于 ready，须继续读取索引状态。

**评什么**：发现/抓取完整率、正文清洗/时间/来源/ID、缓存和去重、旧版本/同版本异文拒绝、部分失败、视频降级、Embedding 数量/维度、accepted→ready 时延。

**依赖/配置**：公开新闻抓取、知识 API、PG、Embedding、可选视频/ASR；`NEWSAGENT_BASE_URL`、`NEWSAGENT_TENANT_ID`、`NEWSAGENT_KNOWLEDGE_TOKEN`（仅变量名）、`MODEL_RUNTIME_CONFIG_PATH`、`KNOWLEDGE_SEARCH_CONFIG_PATH`、`KNOWLEDGE_LIFECYCLE_CONFIG_PATH`。

**现有测试**：`news/tests/test_discovery_ingest_service.py`、`test_news_ingest_service.py`、`test_newsagent_knowledge_client.py`；`agent/tests/test_native_knowledge_indexing.py`、`test_knowledge_embedding_dimensions.py`、`test_video_knowledge_ingest.py`。

**缺口**：真实抓取覆盖/正文 oracle、企业 Embedding/视频合同及规模不是替身单测结论。抓取的失败和 missing 保留分母。

## 2. 检索、索引与生命周期

**查询链**：聊天/热点关联证据/知识 search → `build_knowledge_search_index` / `build_lifecycle_runtime` → 启用生命周期时 `LifecycleKnowledgeSearch.search`（`app/knowledge/tier_lifecycle_query.py`）→ query Embedding → hot/warm/cold 并行查询及逐层 gate/refill → 最终 PG 有效投影校验 → 去重/融合 → 业务重排/证据白名单。

**更新链**：`KnowledgeLifecycleService.accept` → ledger 冻结 job → `process_job` → 切片/向量复用或 Embedding → upsert → verify → activate → 清理过期投影。目标完整验证后切换，更正/撤稿应在物理删除前禁止旧结果返回。immutable BGE snapshot 不可作为生命周期写入目标。

**四层评测**：优先复用 `agent/evaluation/news_retrieval_protocol.v1.json`，状态是草稿/待人工标签及门槛批准。

| 层 | 入口/输出 | 能证明什么 |
| --- | --- | --- |
| L0 资产/身份 | manifest、chunks、vectors、index 摘要/维度/范数/行号/隔离 | 资产和投影合同 |
| L1 编码覆盖 | `tools/audit_index_tokenization.py` → tokenizer/正文区间报告 | token/字符覆盖，非语义损失率 |
| L2 ANN | `tools/evaluate_bge_ann.py` → 对全量精确 IP 的 chunk/article agreement、延迟/区间 | 同存储向量邻域近似程度 |
| L3 语义 | `prepare_retrieval_judgments.py` → 双人盲标/仲裁/冻结 → `score_retrieval_judgments.py` | 获批标签池上的新闻相关性 |

ANN 默认用旧/新语料中的源 chunk 向量探针，不是自然用户问句；`effective_ef=max(requested_ef,pool)` 必须同时记录。单线程 warm-cache 延迟不等于 API/聊天 SLO。标注工具不编码自由问题，也不产生 gold；源 seed 不自动是正例。TopK 未判需补盲标，双人真实独立评审不能由两次助手回答冒充。

**配置/测试**：搜索/生命周期 YAML；`test_tier_lifecycle_*`、`test_lifecycle_news_content.py`、`test_bge_ann_evaluation.py`、`test_index_tokenization_audit.py`、`test_retrieval_judgments.py`、`test_retrieval_judgment_scoring.py`。真实 Milvus live 测试会写删随机租户合成单位向量，不调用 Embedding，只证明平台合同。

**缺口**：人工问题与标签校准/冻结、真实 query、完整时间/正文/池审计、获批质量门槛、规模与恢复。三层纯 RRF 单测不能替代生命周期查询。

**历史实验**：`news/intelligence/evaluation.py::evaluate_pairs` / `scripts/evaluate_event_clustering.py` 评事件对聚类，missing 必须报；不是检索 Recall。`news/experiments/local_recall_benchmark.py` 用不同 BGE-small 语料且存在未判当无关，限探索，不能并入主 BGE-large 语义验收。

## 3. Text2SQL 与权威指标

**调用链**：`/chat` 的 `query_hot_news` 或热点工作台 → `LocalConversationHotNewsQuery.execute` → 共享 `examples/local_simulation.py::execute_local_hot_news_query` → 身份/单小时/样本窗口/Bundle → `SqlAssistantService.preview` → 受限 Intent → `compile_query` / `SqlAssistantGuard.validate` → preview/claim/run_key 原子绑定 → 原热点 Workflow → `execute_for_hot_news` → 再验身份/SQL/参数/哈希 → PostgreSQL READ ONLY → `SqlAssistantHotNewsMetricSource.fetch_snapshots` → 同小时 UV/时长/基线补齐 → Python 权威排行。

**评什么**：NL→Intent（需独立标签）、编译和 AST/绑定参数白名单、冻结格式/claim/身份、数据口径/CTR/零分母/null、run一致性、幂等。模型不能传 SQL/tenant/user。小时 UV 不能跨小时相加；SQL hot_score 选候选，最终热度由 Python 算。

**配置/测试**：冻结 `agent/docs/sql-warehouse/sql-warehouse-schema-v1.md`、场景 YAML、runtime/profile/manifest、批准只读数据源；`test_sql_assistant_guard.py`、`test_sql_assistant_planner.py`、`test_sql_assistant_service.py`、`test_sql_assistant_confirmed_intent.py`、`test_hot_news_sql_binding.py`、`test_text2sql_metric_source.py`。真实本地数仓/HTTP 是 opt-in。

**缺口**：通用自然语言 SQL 规划质量 runner、真实企业数仓/权限/跨小时真实去重合同。旧独立 router 文件存在不代表应用挂载，不能绕出第二套查询业务。

## 4. 热点排序、证据与解释

**业务链**：`HotNewsMonitorWorkflow` → `HotNewsActivities.run_hot_news_window` → `HotNewsOrchestrationService.run` → 聚合/基线 → `HotNewsRanker` / `HotScoreCalculator` → enrichment（正文/关联新闻）→ `HotNewsAnalysisBatch` / `HotNewsAnalysisService.analyze_with_snapshot` → `NativeHotNewsAnalysisRunner` → Prompt/Inference → `HotNewsAnalysisValidator` → PG/Artifact/反馈 → API/SSE。

**离线入口**：`evaluation/run_hot_news_eval.py` → `load_evaluation_dataset` → `--run-model` 才构造 runner → `HotNewsEvaluationService` → stdout JSON。60例种子只是一版样本，运行时重新核对版本和数量。

**评什么**：取数水位/口径、基线/热度分量/排序、正文和证据、单次报告合同/主驱动/必要指标/必要与禁止证据/limitation、幂等/持久化。Python oracle 与报告 oracle 分开。

**范围限制**：离线输入已有指标/相关新闻，不测上游计算/检索。它每例单次调用；生产分析含最多两次反馈纠错尝试，不能混称同一通过率。CLI 不提供 `--output` 或质量阈值退出码；传输异常可整轮退出。Validator 不等于自然语言逐事实/全文数字核真。

**配置/测试**：模型 YAML、Prompt、Bundle、热度/基线规则、`HOT_NEWS_*` 和 Temporal队列；`test_hot_news_evaluation.py`、`test_hot_news_orchestration.py`、`test_hot_news_analysis_attempt.py`、`test_ranking.py`、`test_hot_score.py`、`test_prompt_injection_harness.py`、`test_enterprise_hot_news_adapters.py`。故障分类入口 `evaluation.run_fault_drills` 不代表真实网络演练。

## 5. 对话、SSE 与 Memory

**调用链**：`/chat` → conversations messages/stream → 网关 tenant/user → `ConversationAgentService.run_turn` → 结构化计划 → `ConversationTools` / hooks → 真实业务引用/工具 checkpoint → 数字/结构校验 → PG终态 → SSE answer_delta/done；长业务 Workflow 的受理回执不是完成结果。

**现有 runner**：`evaluation.run_hot_news_dialogue` 默认只校验；`--run` 只接受 loopback、local provider、query_enabled。冻结案例逐轮单次提交，查询保存 run 核对 SQL/参数/窗口/榜单。正确澄清/拒绝可算合同通过但不是取数成功；`expected_pending` 单列。此 runner 不覆盖 SSE/所有 hooks，不支持企业模型质量。

**Memory**：会话按长度摘要与水位→近期原文/工具引用；长期 Memory 使用 tenant/user scope、候选/晋升、冲突/有效期/版本校验。摘要引用不能扩大权限，个人记忆不能冒充共享运营策略。

**配置/测试**：`CONVERSATION_*`、各hook开关/角色与总预算、摘要 Prompt；`test_conversation_security.py`、`test_conversation_stream.py`、`test_conversation_tool_checkpoints.py`、`test_conversation_memory.py`、`test_memory_promotion.py`、`test_hot_news_dialogue_eval.py`。真实 HTTP/PG/Temporal 另列 opt-in。

**缺口**：真实模型语言/摘要语义、长上下文遗忘和权限、生产 SSE/并发/长稳、正式人工确认/审批。先读最新源码，不能复述旧上下文“仅终态存工具轨迹”。

## 6. 聊天数据分析

**调用链**：本会话合法工具来源 run → `ConversationTools._analyze` → `HotNewsQueryService.get_run_detail` → `project_dataset` → `create_analysis_runner` → process/docker/remote → 固定 worker → `engine.analyze`（Decimal）→ 同版本引擎重算验证 → `render_analysis_result` → ToolTrace/PG/SSE。

**评什么**：overview/distribution/compare/quality/baseline/trend 的可手算 oracle、完整榜单范围、加权 CTR、UV不跨新闻累加、缺失/null、同Bundle/同筛选范围/等长不重叠窗口及共同新闻、身份/输出hash/限额/超时。范围是已上榜子集，不是全数仓；trend 非连续走势/因果。

**配置/依赖**：`CONVERSATION_DATA_ANALYSIS_*`、backend/行数/字节/并发/资源、独立 HTTPS服务配置；`app/data_analysis/service.py::create_app`、`AnalysisRunner` / `RemoteAnalysisRunner`。同引擎重算验证传输/协议一致，不是独立业务 oracle；macOS process 不是已验收 Linux资源沙箱。

**测试/工具**：`test_data_analysis_engine.py`、`test_data_analysis_comparison.py`、`test_data_analysis_output_integrity.py`、`test_data_analysis_runner.py`、`test_conversation_data_analysis.py`、`test_data_analysis_remote.py`；`tools/check_analysis_data_contract.py` 默认合成，有真实Port也把 tenant_isolation 标为 not_verified，空结果不足以证明接入正确。

## 7. 研究、数据写作与审校

**调用链**：jobs/对话确认/热点转交 → `NewsWritingWorkflow` → `run_news_step` Activity → `NewsStepHandler.__call__` → `ExecutionService.prepare` → ResearchAgentRunner → 人工研究确认 → Outline/人工提纲确认 → 分章节/组装 → Reviewer/有限返工 → 人工终稿 → final_approved → 另一个显式 publish API → `CmsPublisher`。

**数据转换/输出**：事实/证据研究包→大纲→章节/稿件→审校意见，结构和引用校验后 `ArtifactPipeline.persist_success` → PG checkpoint + S3。Research 当前≥3可追溯facts、≥2唯一URL、引用覆盖100%，时间线/角度有 fact_id 支撑；ID/URL有效不等于事实核真/独立信源。

**评什么**：事实与来源、数值/单位/同比环比、引用可追溯、研究/写作/审校各角色、返工/审批/恢复、人工修改成本/采用价值。Reviewer只作内部返工控制，不能自己给自己最终gold。

**已有/缺口**：`evaluation/data_writing_dataset.py --check` 仅检查素材、Decimal数值标签与哈希；27例包未接通Research/Writer/Reviewer批量推理和全文事实数字语义评分。只向模型提供 input，不能发送 seed/labels/答案预览。cache_only 不证明原始发表时刻正文，hotness_verified=false 不证明当时热点。

**配置/测试**：writing场景YAML、原人工Gate/Temporal队列、CMS默认关闭；`test_native_writing_chain.py`、`test_writing_schema.py`、`test_artifact_pipeline.py`、`test_data_writing_august_sample.py`、`test_data_writing_dataset.py`。真实CMS合同/发布幂等、全文事实/数字和企业模型质量未由local测试证明。

## 8. Data Loop 与运营建议演化

**正式链**：DataLoop API/对话确认 → `DataLoopOrchestrator.start` → `HotNewsDataLoopWorkflow` → 冻结本次fresh_bad_case（Golden/High-risk由请求绑定既有冻结ID）→可选数据分析/错误归因→`HotNewsDataLoopStepHandler._evaluate_candidate`加载校验三层 → 当前active/候选/双基线policy下服务端合格上一实验 → `HotNewsOfflineReplayService.evaluate` → Registry支持性检查 → 两版或三版分析输入回放 → `record_evaluation` → `EvaluationGate` → PG+不可变Artifact → failed 或 waiting_approval → 人工决定后按既有Workflow继续。

**实际门禁**：`app/services/data_loop/step_handler.py` 注册版本化policy；当前golden/fresh/high_risk最低通过率分别0.90/0.80/1.00，evidence_precision均1.00，指标覆盖0.90/0.80/1.00，critical=0；pass/coverage相对允许退化0.02，evidence不退化。min_case_count=1仅工程底线。`hot-news-gate-v1`无上一实验，`hot-news-gate-double-baseline-v1`要求服务端上一实验。执行时核实当前注册值与批准用途，不用模板默认值替代。

`NativeProductionBundleRuntimeRegistry.ensure_transition_supported` 拒绝本地实现字段变化；当前支持注册的模型/Prompt回放，不支持只改热度/重排/Schema/Validator/Memory版本号证明其效果。审批等待当前48小时超时拒绝，不能照搬TAOTIAN的20小时。人工approve后同一Workflow会激活，评测Skill不得自发approve。

**独立运营离线链**：`evaluation.run_operations_evolution` → catalog/同主线split/截至as_of事实与个人scope → blind_input → NativeEvolutionReviewRunner → predictions → 固定分母/板块/人员/阶段/主线评分 → current/previous对照 → offline_recommendation、production_activation_authorized=false。合成主线评分不等于真实推送效果；compare退出0也可能gate失败。

**配置/测试**：`MODEL_RUNTIME_CONFIG_PATH`、`HOT_NEWS_RUNTIME_MANIFEST_JSON`、`TEMPORAL_DATA_LOOP_TASK_QUEUE`、`DATA_LOOP_REPLAY_MAX_CONCURRENCY`、`DATA_LOOP_MAX_CASES_PER_COHORT`、`DATA_LOOP_DATA_ANALYSIS_ENABLED`、PG/S3；`test_evaluation_gate.py`、`test_offline_replay.py`、`test_data_loop_workflow.py`、`test_operations_evolution.py`、`tests/e2e/test_data_loop_full_chain.py`。E2E会模拟批准/激活/回滚，只能用专用隔离栈，不能算正式人工审核。

**缺口**：正式运营人工校准、真实匿名聚合/非模板主线、标签二审/冻结回流、个人→共享策略审核撤销、完整检索资产回放、企业合同/运营收益和生产长稳。
