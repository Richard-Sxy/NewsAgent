# NewsAgent 完整业务链代码核查（2026-10-12）

本次按 `DemoMD/PROJECT_CONTEXT.md`、`DemoMD/BUSINESS_SCENARIO.md` 与
`DemoMD/TAOTIAN.md` 核查当前工作区。保留已有部署、前端、查询理解及其它未提交修改；
本文是检查报告，不修改长期项目背景，也不把历史测试数字当作本次结果。

## 结论与范围

本地热点对话主链具备实际运行能力：聊天问题进入受限查询理解、只读 SQL 和既有
Temporal 热点 Workflow，Python 计算权威指标与排行，证据增强后进行结构化分析，
最后持久化并经 SSE 展示。研究写作和分析配置 Data Loop 已有应用、持久化、
Worker 与人工门禁实现，但尚不能把全部长期业务目标称为完整交付或企业生产验收。

| 需求 | 当前证据 | 实际边界 |
| --- | --- | --- |
| 多轮聊天、历史恢复、安全 SSE | API、服务、PostgreSQL、HTTP/并发测试 | 答复保存后分段；不是企业模型原始 token 流 |
| 问题→取数→热点分析→同 run 追问 | 现有隔离 API + PostgreSQL + Temporal HTTP 验收 | 合成数仓与本地 Port；企业数仓/模型契约待联调 |
| 确定性指标、热度和证据校验 | Python/SQL、Schema/Validator 及回归 | 不允许模型生成权威数字或扩大权限 |
| 相关新闻检索 | 本地知识链与 BGE 部署预检、单元回归 | BGE 演示为静态快照，新入库内容不会自动进入该快照 |
| 研究→人工确认→写作→审稿→终稿 | 原生 Runner、Workflow、Checkpoint 与门禁测试 | 本次修复研究恢复门禁和 checkpoint 碰撞；CMS 真契约待验收 |
| 反馈→标签二审→冻结→评测→批准→激活/回滚 | Data Loop Worker、领域/应用/Workflow 测试 | 黑盒入口引用的 `examples.data_loop_e2e` 缺失；未称真实全链 E2E 通过 |
| 短期/长期 Memory | 写入、候选审批、解析、上下文 API | MemoryAware 热点服务尚未装入生产热点调用；不是完整自动跨天学习 |
| 个人策略积累、审核后推广共享 | 合成运营数据、离线审核/分组门禁 | 正式策略注册表、共享审批/撤销、线上人员隔离及下一次建议复用未贯通 |
| 企业原榜与项目计算榜双来源 | 接入草案与已有项目榜实现 | 企业原榜 Adapter 和统一生产入口待真实表/接口合同 |

## 本次修改的完整调用链

### 查询理解输入边界

- **上游入口**：`/chat` → `ConversationAgentService` → `query_hot_news`。
- **转换与核心**：`LocalConversationHotNewsQuery._understand` 在模型前执行
  `screen_question`，拒绝明确 SQL、身份越界和提示注入；通过后才生成受限理解结果，
  继续确定性窗口解析、SQL 编译与执行护栏。
- **外部依赖/输出**：原生 `InferencePort`、PostgreSQL 和 Temporal；输出绑定同一
  查询/运行的结构化报告与受控失败原因。
- **配置**：现有模型 YAML、查询场景与明确开启的本地查询权限；未扩大生产权限。
- **测试入口**：`test_query_understanding_integration.py`、
  `test_sql_assistant_guard.py` 等输入边界测试；热点 HTTP 测试同时检查理解调用审计。
- **TODO**：真实企业查询理解质量、范围及身份合同；同期查询理解修改需以最终工作区测试为准。

### 活跃轮次补充输入

- **上游入口**：`POST /api/v1/conversations/{conversation_id}/turns/{turn_id}/inputs`
  → 已有鉴权/Schema → `ConversationAgentService.submit_input`。
- **转换与核心**：`enqueue_input` 用同会话行锁检查所有权、请求 UUID、冻结限额与期限，
  保存 `pending`；`claim_inputs` 变为 `applied`；`TurnInputSnapshot` 保留原问题，追加
  已领取要求。模型前后检查补充，丢弃过时的未执行计划，保留完成工具与检查点。
  `finish_or_claim` 在同一锁内决定领取迟到输入继续规划或提交终态。
- **外部依赖/输出**：PostgreSQL、原生模型 Port、既有只读工具；输出幂等受理记录、
  完整历史与同一 SSE 答复。失败/中断将补充标为失败，终态不接收新的补充。
- **配置**：服务构造参数 `max_inputs`、`max_inputs_chars`、`max_planning_steps`、
  既有整轮超时；创建轮次时写入限额快照。使用已有 `20261010_0022` 表契约。
- **测试入口**：`test_conversation_inputs.py`、会话/流式/上下文回归及显式本地
  PostgreSQL 补充并发验收。
- **TODO**：前端专用追加输入控件；跨输入自动合成 SQL 语义未授权，要求下一轮完整提交；
  企业模型语义质量和生产并发预算待验收。

### 研究恢复与人工审批

- **上游入口**：写作恢复 API → `RecoveryService.prepare_resume` 持久化恢复 Workflow ID
  → `OrchestratorService.start_job` → `NewsWritingInput`。
- **转换与核心**：显式 `research` 恢复从持久化 ID 派生内部 checkpoint namespace；
  `NewsWritingWorkflow._checkpoint_key` 为本次研究及所有下游步骤隔离幂等键。
  研究决策必须为 `approve` 才进入提纲或完成研究包；其它决策转回人工处理。
- **外部依赖/输出**：Temporal → `run_news_step` → 原生 Agent/Validator →
  PostgreSQL Checkpoint、S3/MinIO Artifact → 研究/提纲/终稿人工节点。
- **配置**：不新增环境变量或数据库迁移；旧 Workflow payload 的 namespace 默认为空，
  普通续跑保持原键。现有模型/Prompt 与有限审稿轮次继续生效。
- **测试入口**：`test_temporal_orchestration.py`、`test_orchestrator_service.py`、
  `test_native_writing_chain.py`、恢复/执行/Jobs API 回归。
- **TODO**：显式补研究后再次普通续跑的恢复上下文尚未持久化；历史 Workflow replay 与
  Worker 版本部署仍需验收。本次不自动重启运行中的写作 Worker。

### 知识身份与 Embedding 维度

- **上游入口**：爬虫 `NewsAgentKnowledgeClient` 或内容文本化
  `ContentIngestService` → 文档 API/Writer。
- **转换与核心**：短文档 ID 保持原值，超长 ID 使用完整源 ID 的哈希避免截断碰撞；
  `NativeKnowledgeIndexingService` / `PostgresKnowledgeStore` 在修改索引和台账前
  校验所有 Embedding 批次维度，以及同租户、同 Embedding 版本的已有维度。
- **外部依赖/输出**：`EmbeddingPort`、PostgreSQL 或测试向量 Port；输出单调版本文档、
  切片/向量、入库状态；维度漂移拒绝写入，保留旧内容和旧索引。PostgreSQL 用事务锁
  协调同 Embedding 版本的并发写入。
- **配置**：现有 Embedding 版本、批大小、切片长度/重叠；不改变模型路由或冻结数仓格式。
- **测试入口**：原生索引测试、PostgreSQL 存储回归、`news/tests/test_news_ingest_service.py`
  及爬虫客户端回归。
- **TODO**：企业 opaque 内容版本到正整数 revision 的可信映射未提供；
  `NewsContent` 与文本化 Builder 目前仍缺少该版本传递合同；BGE 增量重建、删除、
  更正与规模验收待完成。

### Data Loop 模型调用租户

- **上游入口**：Data Loop API → Workflow → `HotNewsDataLoopStepHandler._evaluate_candidate`。
- **转换与核心**：`HotNewsOfflineReplayService.evaluate` 在创建模型 Runner 前验证三层
  Manifest 属于同一租户，显式将该租户传给 Bundle Runner Factory；
  `NativeProductionBundleRuntimeRegistry.create` → 原生热点 Runner → `InferencePort`。
- **外部依赖/输出**：企业或本地模型 Port、S3/MinIO 和 PostgreSQL；评测产物携带实际租户，
  不再把企业案例统一记录成 `offline-evaluation`。混租户输入在任何推理前失败。
- **配置**：默认评测器版本提升为 `hot-news-offline-replay-v2`；既有并发/案例限额、
  模型 YAML 和运行清单继续生效，不激活或修改任何生产 Bundle。
- **测试入口**：`test_offline_replay.py`（双租户与混租户拒绝）、原生运行时、Data Loop
  StepHandler/Worker 回归。
- **TODO**：独立原生 E2E driver、迟到审批回补、逐案例检查点及跨 Worker 配额。

### 爬虫回归与热点审计测试

- `news/tests/test_getPCList.py` 的真实五页请求从模块导入期移到显式 `integration`
  测试，设置 HTTP 超时并检查状态与列表合同；默认 `pytest` 不联网，直接运行脚本仍可探测。
  上游为测试 CLI，下游为接口检查/当天标题输出；依赖 `httpx`，配置为 pytest 标记。
- `test_conversation_hot_news_e2e.py` 对本地三次模型调用（两次规划、一次查询理解）校验，
  并要求理解请求 ID 出现在轮次审计；分析调用仍由同一热点 run 保存。
- 真实腾讯新闻与企业模型接口测试未在本次执行。

## 验证记录

最终回归结果在全部修复完成后记录于此。已完成的初始证据：前端 246 项测试、lint、
类型检查及生产构建通过；爬虫离线 170 项通过、2 项真实网络测试排除；现有本地聊天/
SSE/SQL/Temporal HTTP 16 项通过；真实 PostgreSQL 会话/摘要 9 项通过、1 项跳过；
一键部署 58 项测试和真实只读 `--check` 通过。

以上 HTTP 验收针对现有运行镜像；新增补充输入、知识与写作修复另以当前工作区回归验证，
不能把旧镜像的健康状态当作新代码已部署。未清空数据库、未发布 CMS、未发送新闻、
未审批或激活生产配置、未读取 `.env`，未重新引入历史 Agent/知识框架。
