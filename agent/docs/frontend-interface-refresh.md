# 前端简化与业务链路核查（2026-10-14）

## 交付范围与结论

NewsAgent 已有真实的本地核心业务链：对话入口、受限热点取数、确定性排行、结构化分析、
研究写作、反馈与配置评测均连接 API、服务、持久化和相应 Worker。前端不是静态演示。
企业模型、数仓、身份网关与 CMS 的真实合同和生产容量仍待验收，不能据此宣称整个
长期业务目标或企业生产链已经完成。本次先对照当前代码核查，再简化页面信息层级；
未沿用历史完成度或测试数量，未修改长期项目上下文。

对话作为常用入口；热点、写作、评测与反馈工作台承接结果查看和人工操作。默认展示
问题、结果、状态与下一步，把 SQL、原始 JSON、版本和调试信息放入可展开区域。

## 界面源码与交互

| 源码（相对 `agent/frontend/`） | 本次界面职责 |
| --- | --- |
| `src/layouts/AppShell.vue` | 统一侧边导航、当前模块和环境提示；主要入口为对话、热点运营、写作任务、评测与反馈，安全演练仍保留独立入口。 |
| `src/styles/base.css` | 统一颜色、字号、间距、卡片、按钮、表单和展开区；提供窄屏布局与可见键盘焦点。 |
| `src/router/index.ts` | 首页进入 `/chat`；保留业务路由、Data Loop 子路由和旧 SQL 页面重定向；真实实体链接继续定位详情。 |
| `src/views/ChatView.vue` | 对话和输入框作为主区域，保留历史、进度、确认及回执卡片；完整答复、处理详情、模型调用编号和更多示例按需展开。 |
| `src/views/JobsView.vue` | 优先展示任务列表、筛选和人工待办；新建表单及列表设置折叠；`job_id` 链接独立于列表分页定位任务。 |
| `src/views/HotNewsView.vue` | 优先展示当前窗口和榜单；查询入口、历史运行、取数证据、决策历史按职责展开；保留模拟入口与决策操作。 |
| `src/views/DataLoopView.vue` | 用六个清晰标签组织反馈、运行、决策、数据集、生产包和候选；描述随当前面板切换，保留原子页面及接口。 |
| `src/components/hotnews/HotNewsRankTable.vue` | 优先展示标题、排行和分析；指标与热度构成可展开，沿用后端权威数值及选择事件。 |
| `src/components/jobs/JobInspector.vue` | 补齐状态组件导入，恢复加载、错误、空状态与真实任务详情，人工 Gate 仍经原有服务。 |
| `src/views/data-loop/FeedbackCasesPanel.vue` | 案例与二审表格在独立区域横向滚动，避免编号逐字挤成竖排；深链接和标签版本校验保持原样。 |
| `src/views/data-loop/BundlesPanel.vue` | 从真实响应的 `bundle_version/status` 提取摘要，缺失显示未知；完整 JSON、初始化与回滚表单默认折叠。 |
| `tests/format.test.mjs` | 调整旧表格标签位置断言，继续验证 CTR、热度、置信度的展示精度和输入快照不被修改。 |

页面仍经 `src/api/` 和 Pinia store 访问 Python API。视觉简化不增加假状态、模型调用、
自动批准或自动发布能力，也不改业务 Schema、数值算法和人工 Gate。

## 完整调用链

### 对话与 M1–M4 能力

`/chat` → `ChatView` → `conversationsApi.sendStream` → UTF-8/SSE 解码 →
`POST /api/v1/conversations/{id}/messages/stream` → 网关身份与独立权限校验 →
`ConversationAgentService.prepare_turn/run_turn` → 历史与上下文压缩、结构化计划 →
`ConversationTools` → 既有业务服务 → `PostgresConversationRepository` 保存轮次、
工具检查点、引用和终态 → `StreamingConversation` 发送进度及已校验、已保存的答复 →
页面回执卡片与历史恢复。

Python 绑定回执中的真实实体 ID、版本、Gate 与白名单链接；模型不能自行声称落库。
写作卡片按既有 3 秒间隔、评测卡片按 15 秒间隔轮询当前状态，原始落地快照可追溯。

| 能力 | 转换、核心服务与下游输出 |
| --- | --- |
| M1 写作只读 | `list_writing_jobs/writing_job_status` → `ConversationWritingReader` → `JobService` 的租户查询 → 真实任务卡及 `/jobs?job_id=…`。 |
| M2 发起写作 | `start_writing_intent` → 本轮确认快照 → `ConversationWritingCommands.start` 把建议转为 `CreateWritingJobRequest` → 幂等任务落库 → `OrchestratorService` 或受控 Admission 队列启动原写作 Workflow → 启动回执；研究、提纲和终稿仍在工作台人工处理。 |
| M3 记忆与反馈 | 显式记忆请求 → `ConversationMemoryHooks` → 既有 Memory 写入应用服务 → 短期记录或待人工晋升的长期候选；读取状态不等于批准。`save_feedback_case` → 本轮确认明确披露的运营决策副作用 → `ConversationFeedbackHooks` → `HotNewsDecisionService`/Feedback Collector → 真实案例及详情链接；标签二审继续独立执行。 |
| M4 候选与评测 | `propose_bundle_candidate/start_data_loop` → 已注册模型或 Prompt 预设、真实差异与数据集确认 → `ConversationDataLoopHooks` → `ProductionBundleApplicationService`/`DataLoopOrchestrator` → 候选或原生评测 Workflow → 真实回执、轮询及评审台链接；对话不批准、激活或回滚。 |

外部依赖为 PostgreSQL、原生 `InferencePort/EmbeddingPort`、Temporal、Redis 和业务
Artifact 的 S3/MinIO。长任务只等待启动受理；对话断线不取消已启动的业务 Workflow。

### 热点取数、分析与运营决定

本地工作台 `/hot-news` → `hotNewsApi.runLocalSimulation` →
`POST /api/v1/local-simulation/hot-news/run`；对话新查询则由
`ConversationTools.execute_hot_news_query` → `LocalConversationHotNewsQuery` 进入同一个
`execute_local_hot_news_query`，没有第二套 SQL 执行器或内部 HTTP 自调用。

问题 → 输入边界与受限查询理解 → 场景/冻结数仓契约 → SQL 编译与护栏 → PostgreSQL
只读预览 → 查询原子绑定热点 `run_key` → `HotNewsMonitorWorkflow` → 热点 Worker 的
`HotNewsActivities.run_hot_news_window` → `ActiveProductionBundleHotNewsService` →
run-scoped 指标源 → `HotNewsOrchestrationService.run` → Python/SQL 指标、基线和热度
排行 → `HotNewsEnrichmentService` 按 `news_id` 取正文与关联新闻 → EvidencePacket →
原生热点 Runner、模型 Port 与 Validator → `PostgresHotNewsRunStore.save_completed`。

下游 `/api/v1/hot-news/runs` 与 `/runs/{id}` 由 `HotNewsQueryService` 返回同一运行的
榜单、分析及 SQL 证据，页面不拼接其它查询。`/decisions` →
`HotNewsDecisionService.record_decision` → 决策和反馈在同一数据库事务保存。当前新建的
采纳、延后、驳回、修正均会回流；旧采纳/延后事件重放不静默补填案例。

本地新查询只由 `examples.local_simulation.create_local_app` 在隔离 E2E 环境、显式
开关和获准租户/角色下装配。生产 `app.main.create_app` 不挂本地模拟路由。
SQL 指标排序用于挑选候选，最终榜单使用 Python 权威热度；本地小时 UV 不跨小时相加。

### 研究写作与发布

`/jobs` → `JobCreateCard/useJobsStore/jobsApi` → `POST /api/v1/jobs` →
`JobService.create_or_get` 保存幂等任务 → `OrchestratorService.start_job` →
`NewsWritingWorkflow` → 写作 Worker 的 `NewsStepActivities` → `NewsStepHandler` →
原生 Research/Writer/Reviewer Runner → Pydantic Schema/业务校验 →
`ArtifactPipeline/CheckpointService` → PostgreSQL 状态与 Checkpoint、S3/MinIO Artifact。

研究 → 人工确认研究包 → 提纲 → 人工确认提纲 → 章节与组装 → Reviewer → 有限返工
→ 人工确认终稿。API、Redis 进度事件和详情查询供列表、Inspector 与回执卡显示。
恢复入口复用 `RecoveryService` 和既有 Workflow；显式补研究恢复使用独立 checkpoint
namespace。导出直接读取校验过的 Artifact；发布由独立 `/publish` 在
`final_approved` 状态下调用 `CmsPublisher.publish`，带业务幂等键并保存外部发布 ID。

### Data Loop 评测与人工晋升

`/data-loop` 六个子面板 → `dataLoopApi` → `/api/v1/data-loop` 的独立角色校验 →
反馈收集、标签提交/二审、`EvaluationDatasetFreezer` 冻结与版本哈希、生产包及候选应用
服务。`POST /runs` → `DataLoopOrchestrator.start` → `HotNewsDataLoopWorkflow` →
独立 Data Loop Worker/`DataLoopActivities` → `HotNewsDataLoopStepHandler`。

冻结 Fresh bad case → 可选数据集分析/错误归因 →
`HotNewsOfflineReplayService` 对 Golden/Fresh/High-risk 执行当前、候选和上一实验回放 →
确定性 Evaluation Gate → PostgreSQL 评测账本与 S3/MinIO 哈希产物 → 人工批准或拒绝
→ 原有生产包应用服务激活。超时不批准；显式回滚和激活恢复继续绑定原审批与版本。
辅助分析降级不放宽评测 Gate。页面通过 Workflow 快照和产物 API 展示真实运行。

## 配置与权限来源

- 浏览器运行配置：`agent/frontend/public/config.json` →
  `loadRuntimeConfig()` → app store/HTTP Client。`apiBaseUrl` 决定 API 目标，`appTitle`、
  `environment` 和 `enableHotNews` 控制呈现；生产可由 ConfigMap 覆盖，不放企业密钥。
- 本地开发配置：`agent/frontend/dev-server/local.yml` →
  `vite.config.ts/loadLocalDevSettings/parseLocalDevYaml` → Node 侧 `/api` 代理。
  配置含监听地址/端口、代理目标、模拟开关及本地身份/角色。身份只在代理层注入；
  最终监听和目标必须为环回地址，配置目录不能由开发服务器下载。生产构建和 preview
  不加载该 YAML，不携带本地身份。
- Python 运行时：`agent/app/config.py`、部署配置与版本化模型 YAML。主要项为
  `MODEL_RUNTIME_CONFIG_PATH`、`HOT_NEWS_RUNTIME_MANIFEST_JSON`、知识配置、
  `CONVERSATION_ENABLED`、热点查询/知识/Admission 开关、上下文与超时预算、三个
  Temporal task queue、Data Loop 并发/案例限额以及 CMS 目标/超时。
- 生产身份来自可信网关；Writing、Hot News、Memory、Data Loop 使用独立角色组。
  浏览器和模型输出不能自行扩大租户、用户或角色权限。SSE 保留取消、幂等恢复和安全
  答复提交语义；外部新闻、检索内容和模型输出继续作为不可信输入。

## 仍未完成的范围

- 企业模型/Embedding、行为/内容/基线/关联新闻和真实数仓合同；Gateway/IdP、生产
  容量、故障恢复、索引规模与真实 CMS 发布语义验收。
- 个人运营策略到团队共享的版本注册、审核、撤销、人员隔离及下次建议实际复用。
- `MemoryAwareHotNewsAnalysisService` 尚未装入当前热点 Worker 的分析调用链；
  会话压缩、长期候选与跨天自动策略学习不能视为同一能力。
- BGE 演示是静态快照；新入库内容的增量重建、删除和更正尚需贯通与验收。
- 旧独立全链测试 `agent/tests/e2e/test_data_loop_full_chain.py` 仍引用缺失模块
  `examples.data_loop_e2e`，需补原生 driver/runbook，覆盖标签二审到批准、激活、下一
  次热点运行及回滚。最新对话 M4 到 `waiting_approval` 的真实接线不消除该缺口。
- 三位正式人工合同评审及现有候选的独立人工决定仍保留；本地规则 Port、合成演员和
  Agent 技术审查不能替代人工审核或企业模型质量验收。
- 重排调参候选需先版本化真实重排资产、完整检索证据回放与 Worker 注册；当前只支持
  已注册模型/Prompt 预设，不能把其它资产改动当成重排效果。
- 显式补研究后再次普通续跑的恢复上下文、历史 Workflow replay/Worker 发布，及
  Data Loop 迟到标签回补、逐案例检查点和跨 Worker 配额仍需后续验证与完善。

## 验证入口与浏览器验收

前端在 `agent/frontend/` 执行：

```sh
npm test
npm run lint
npm run build
```

`build` 已包含 `vue-tsc` 类型检查；单独检查使用 `npm run typecheck`。
后端在 `agent/` 使用既有 Python 环境执行 `python -m pytest -o addopts='' -q`。
专项入口包括 `test_conversation_hot_news_e2e.py`、`test_conversation_capability_e2e.py`、
`test_conversation_dataloop_e2e.py`、`test_native_writing_chain.py`、
`test_temporal_orchestration.py`、`test_data_loop_worker.py` 与
`test_data_loop_workflow.py`。真实 HTTP 测试用 `CONVERSATION_E2E_BASE_URL` 选择已部署的
隔离栈；测试开关、租户和受控夹具以各测试文件及
`agent/docs/conversation-capability-hooks.md` 为准，不默认连接企业服务。

浏览器验收应使用当前 YAML 的前端监听地址和实际后端栈：

1. 打开 `/`，确认进入对话；检查四个主导航、环境提示、键盘焦点和窄屏布局。
2. 对话发送获准示例，检查处理中、最终答复、展开详情、历史刷新恢复；保留同一
   `conversation_id` 与真实 run/job 引用，不以文本声称成功代替回执。
3. 通过已有回执打开 `/jobs?job_id=…`；检查分页/筛选不阻断实体定位，新建区域可展开，
   人工 Gate、产物导出与进度保持可用。正式审批和发布须由获授权的人执行。
4. 打开 `/hot-news` 读取已有运行；检查榜单、指标展开、同 run SQL 证据、历史切换及
   决策入口。需要取数验收时只在隔离栈触发获准的一小时模拟窗口。
5. 打开 `/data-loop` 六个标签及真实 workflow/candidate 链接；检查数据、运行状态和
   待审批信息可见，评测卡按 15 秒更新，简化页面不跳过二审/审批/回滚确认。
6. 在桌面和窄屏检查表格横向滚动、折叠区域、输入框、确认卡、错误/空状态及长内容。

## 本次验证结果

- 当前前端完整 `npm test`：332 项通过，0 失败；`npm run lint` 无错误与警告；
  `npm run build` 的 Vue/TypeScript 检查与 Vite 生产构建通过。
- 浏览器检查桌面 1360px、手机 390px 和实际应用窗口 319px；检查过的页面没有整页
  横向溢出，宽表格保留在独立区域滚动。Data Loop 六个子页面均能正常导航。
- 本地发送“查看最近热点”，收到真实工具快照答复；刷新后恢复同一会话与消息。
  快捷问题只填入草稿，处理时输入锁定和完成后恢复输入均正常。
- 热点榜单分析可展开；“决策”打开并定位原表单，正确预填 `news_id`；
  `/hot-news#query-tools` 锚点唯一且查询区自动展开。
- 写作列表选择真实任务后显示详情与原有人工 Gate，空选择恢复明确空状态；修复了
  `JobInspector` 遗漏 `NaStateBlock` 导入导致状态区无法正常渲染的问题。
- 生产包摘要显示后端返回的真实版本与 active 状态；完整配置及高级操作默认折叠。

浏览器使用 `http://127.0.0.1:5174` 的 Vite 开发服务和既有 `28000` 本地 API。
生产静态资源生成在 `agent/frontend/dist`，容器前端更新沿用原有构建/部署入口。
本轮后端结论来自源码接线核查，企业合同、模型质量及完整生产验收仍是上文待办。

验收截图由浏览器直接采集到 `output/frontend-refresh-2026-10-14/`：
[对话页](../../output/frontend-refresh-2026-10-14/chat-desktop.jpg)、
[热点工作台](../../output/frontend-refresh-2026-10-14/hot-news-desktop.jpg)、
[手机反馈页](../../output/frontend-refresh-2026-10-14/feedback-mobile.jpg)、
[生产包页面](../../output/frontend-refresh-2026-10-14/bundles-desktop.jpg)。
这些图片是前端 → 当前本地 API → 页面投影的验收输出，不作为企业模型质量证据。
