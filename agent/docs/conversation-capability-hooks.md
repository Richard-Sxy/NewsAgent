# 统一对话能力 hooks

本实现复用既有 Python Service、Repository 与 Temporal Workflow，不新增业务表或外部
Agent 框架。模型只提供建议参数；租户、用户、权限、幂等键、实体版本、指标、配置差异与
数据集来源由 Python 构造。写作、反馈、候选和评测先展示本轮确认卡。审批、记忆晋升、
Bundle 激活、回滚、CMS 发布仍在原有工作台执行。

## 完整调用链

`ChatView → conversationsApi / SSE → api/conversations.get_conversation_principal`
通过同一网关 Bearer 鉴权，分别读取 `X-Hot-News-Roles`、`X-Writing-Roles`、
`X-Memory-Roles`、`X-Data-Loop-Roles`。角色不能放进其他组的 Header。服务器绑定
tenant/user，`ConversationAgentService` 加载历史后，把获准工具清单同时传给原生模型
Port 和隔离本地规则规划器。

`ConversationPlan → HookSpec → 严格 Pydantic 参数 → 薄适配 Service → CapabilityReceipt`
是新能力的通用路径。`tools.py` 的注册表包含 name、permission、argument_schema、
execution_class、runner、receipt_builder 六项。确认动作通过既有 turn inputs 写入；
服务器生成随机 token 与固定输入 UUID，绑定原始请求、权限、来源快照、输入序号及有效期。
确认前不执行写操作；补充要求使原提案失效。重复确认使用原 UUID，不再创建第二个任务。
token 和 prepared_context 保留在审计记录里，从模型输入及压缩上下文中剥离。

每个工具完成后先 `checkpoint_turn`，再发送 `tool_finished`；确认后的执行追加新轨迹，
原始提案不被重写。断线只停止本轮等待，已落库实体和 Temporal 工作流继续存在。回执必须
满足 `receipts.py` 三态合同：落地实体有真实 ID/版本；待审批额外有 Gate；未落地实体字段
均为空。链接仅允许对应实体的 `/hot-news`、`/jobs`、`/data-loop` 精确选择器。

| 上游工具 / 权限 | 转换、核心函数与既有依赖 | 下游输出 |
| --- | --- | --- |
| list_writing_jobs / writing:read | ConversationWritingReader → JobService.list_for_operations；限量、裁掉 requirements/tenant，保留 ORM version | 多张 WritingJobCard |
| writing_job_status / writing:read | JobService.get；tenant 精确查询，映射当前人工 Gate | 原始回执 + 当前状态 |
| start_writing_intent / writing:admin | topic、200–5000 字、固定语气；ConversationWritingCommands.start → JobService.create_or_get + Outbox，提交后再 OrchestratorService.start_job | 真实 job_id、Workflow ID、版本和详情链接 |
| save_short_term_memory / memory:write | 显式用户保存意图；ConversationMemoryHooks → MemoryWriteApplicationService，user scope，源 turn.created_at 绑定幂等与 TTL | 24 小时短期记忆，不自动扩大作用域 |
| propose_long_term_memory / memory:write | 同源绑定 → propose_long_term；固定 conversation-memory-v1 策略 | 7 天候选，待人工晋升；不写生效长期记忆 |
| memory_status / memory:read | 当前 tenant/user 精确读取，计算 expiry，不修改数据库 | 仅追问时展示候选/有效状态 |
| save_feedback_case / data-loop:admin + hot-news:decide 或 admin | 当前会话已完成 read/query 的真实 run/news → prepare 冻结来源 → HotNewsDecisionService.record_decision(rejected) → 既有 AnalysisFeedbackCollector | 真实 case_id、Bundle、来源；标签仅草稿，未提交/二审 |
| propose_bundle_candidate / data-loop:admin | 已注册 model/Prompt 预设 → Python 从 active Bundle 计算完整 spec/diff → 锁定同一基准 → ProductionBundleApplicationService.propose_candidate | pending_evaluation 候选，未批准或激活 |
| start_data_loop / data-loop:admin | 用户显式候选 ID 或本会话候选回执 → DataLoopCatalog 租户查询 → 三层真实冻结集/窗口/策略快照 → DataLoopOrchestrator.start | 实际 Workflow ID，评测行生成前明确“尚未生成” |
| evaluation_status / data-loop:read | 既有 Orchestrator.get_snapshot，核对租户；快照内容哈希作为回执版本 | 阶段、门禁、等待批准状态，无审批按钮 |

admin 仅隐含同一组 read，不扩大其他组权限。反馈保存同时产生分析驳回决策，因此确认卡
披露该副作用并要求热点 decide 权限，不能冒用 low-confidence 来源。原有数据库没有
conversation feedback source 枚举，保留既有 operator_decision 来源契约。

## 回执界面与只读刷新

`CapabilityReceiptsPanel → CapabilityReceiptCard` 校验实体合同后分别渲染写作、记忆、
反馈、评测卡。确认卡只提交固定 UUID/token 控制消息，不信任模型宣称“已获批准”。
`CapabilityConfirmationCard` 共用通用确认；写作保留专用参数展示。文本按转义内容渲染。

写作按 3 秒、评测按用户确认的 15 秒轮询。`createEntityPoller` 合并同实体读取、页面隐藏
暂停、终态停止、卸载释放监听；连续三次失败降级并允许手动刷新。原始快照和最新状态
分开，迟到响应不能覆盖更新的实体状态。读取入口为
`GET /api/v1/conversations/receipts/writing-jobs/{job_id}` 和
`GET /api/v1/conversations/receipts/evaluation-runs/{workflow_id}`，复用同一 HookSpec 权限、
tenant 约束与业务读取，不执行写操作。

详情跳转：`/jobs?job_id` 独立于列表筛选分页定位同一任务；`/data-loop?case_id`、
`candidate_id`、`workflow_id` 进入对应面板并精确 GET。记忆尚无管理页面，因此 deep_link
为空。评测 Workflow ID 与 evaluation_run_id 分开，不能把前者伪造成评测数据库记录。

## 对话异步任务预算

可选 `AdmissionService` 放在新对话写作/评测适配器与原有 Orchestrator 之间。页面和其他 API
保持既有启动端口；该预算覆盖对话发起的任务，并非全系统所有异步任务的统一限额。

`AdmissionRequest → AdmissionRequestWorkflow → TenantAdmissionWorkflow FIFO →
AdmissionActivities.dispatch → 原有 writing / data-loop Workflow`。每租户默认 2 个活跃
业务工作流，人工 Gate 也占名额。队列与槽位由 Temporal 历史持久化，不使用 Redis TTL
租约。独立请求 Workflow 的固定 ID、memo tenant 和请求指纹防止重放/串租户。协调器先
只读 bootstrap 既有 conversation-writing 任务；未知状态或超过有界读取量时停止放行。
只在实际 business Workflow 的 Describe 返回终态后释放槽位；不因断线、观测失败或租约
到期放行。Worker 重启不丢队列。长历史由保留完整状态的 continue-as-new 控制。

`admission_accepted=true` 表示进入持久队列；`workflow_accepted=true` 才表示业务已启动。
写作排队时数据库 job 已存在，因此回执已落库；评测排队时业务 Workflow 尚未创建，回执
保持未落地并另展示真实 admission 状态。评测行仍为空。API 不等待排队完成或业务完成。
`worker.py` 仅在开关开启时注册 admission 工作流与 Activities，旧部署默认路径保持一致。

| 配置 | 默认 / 约束 |
| --- | --- |
| CONVERSATION_ASYNC_ADMISSION_ENABLED | false；API 与 writing-worker 须一致开启 |
| CONVERSATION_ASYNC_ADMISSION_BUDGET | 2；1–8，现有租户协调器策略不可隐式改写 |
| CONVERSATION_ASYNC_ADMISSION_QUEUE_LIMIT | 100；1–1000，队满明确拒绝 |
| CONVERSATION_TURN_TIMEOUT_SECONDS | 60；确认寿命受剩余轮次预算限制 |
| HOT_NEWS_RUNTIME_MANIFEST_JSON | 可信完整 Bundle 白名单，模型无权构造；单一规格时隐藏候选提交能力 |
| MODEL_RUNTIME_CONFIG_PATH | 已注册 Prompt、推理路由；企业密钥只由环境注入 |
| X-Writing / Memory / Data-Loop-Roles | 网关断言的独立权限组，不从消息或工具参数获取 |

外部运行依赖沿用 PostgreSQL、Temporal、现有模型/Embedding Port；评测 artifacts 沿用 S3，
本地演示 BGE 保持原快照索引。没有新增 Python/npm 依赖或数据库迁移。

## 本地演示和验证入口

M1/M2 在 5174 + native-e2e 验证；M4 使用含 data-loop-worker 的 bge-demo 栈。仅新增 overlay，
不改 native-e2e compose。`conversation-hooks.model-runtime.local.yml` 是隔离规则 Port 的
版本化候选接线夹具，不代表 Prompt/企业模型质量。该文件不改当前 active Bundle。
`docker-compose.conversation-hooks-bge-demo.yml` 开启本地预算并挂载此夹具。

源码镜像离线构建可复用已核对哈希的本地依赖：

```sh
docker build --pull=false --build-arg E2E_DEPENDENCY_IMAGE=news-agent/conversation-hooks-bge-dependencies:20261013 -f agent/Dockerfile.e2e.cached -t news-agent/conversation-hooks-bge-demo:local agent
docker compose --env-file /dev/null -p news-agent-bge-demo -f agent/deploy/docker-compose.native-e2e.yml -f agent/deploy/docker-compose.bge-snapshot.yml -f agent/deploy/docker-compose.bge-demo.yml -f agent/deploy/docker-compose.conversation-hooks-bge-demo.yml up -d --no-deps api writing-worker hot-news-worker data-loop-worker
```

演示步骤：写一篇 AI 芯片周报 → 确认 → 写作卡片 → 同一任务资料人工 Gate；记住我关注科技 →
短期记忆卡；长期记住我的偏好 → 候选卡 → 追问候选状态；读取真实热点运行 → 保存反馈案例
→ 确认明确披露的驳回动作 → 同一 case 详情；提交已注册 Prompt 候选 → 确认实际差异 →
发起该候选评测 → 确认真实数据集与策略 → 评测卡 → 评审台人工 Gate。助手不点击批准。

后端全量入口：`python -m pytest -o addopts='' -q`。专项入口包括
test_conversation_receipts、tools_writing、writing_commands、writing_confirm、writing_stream、
memory_tools、feedback_hook、dataloop_hook、capability_integration、admission、
admission_temporal。HTTP/SSE 写作验收通过 `CONVERSATION_E2E_BASE_URL` 选择隔离本地栈；
真实队列/重启/Replayer 验收通过 `NEWS_AGENT_ADMISSION_TEMPORAL_TEST=1` 开启。
前端：`npm test`、`npm run lint`、`npm run build`。

## 未完成范围

重排调参已由用户明确另列 TODO：需版本化实际重排资产、完整检索证据重放与 Worker 运行
注册，再提供该类候选，当前必须明确拒绝。三人正式人工合同评审仍待回填。企业模型效果、
Gateway/IdP、多租户生产容量及运维故障演练不由本地规则 Port 验收替代。

三层评测使用真实冻结集；现有 Fresh Workflow 会新冻结同一受控窗口，确认卡明确展示其
来源和新冻结语义，不能声称直接复用 Fresh ID。没有冻结集时返回未落地说明，需要先准备
数据。不得为了演示自动标注/二审真实业务反馈、自动批准或激活配置。

## 验收夹具、负载报告与 Prompt 草稿

`tools/seed_conversation_evaluation_demo.py` 默认 `--check`；`--apply` 仅在固定隔离DB/MinIO、
ENVIRONMENT=e2e、LOCAL_SIMULATION_ENABLED=1、provider=local 下添加自己的namespace合成
Case/Label，再经现有收集、标签、冻结服务保存三层内容寻址artifact。两个测试演员的标签
审核明确 `human_review=false`、`enterprise_quality_validated=false`，不修改业务标签或
active Bundle。复跑核对完整内容与指纹，不清库。相应入口为 test_conversation_evaluation_seed。

`tools/conversation_hooks_load.py` → 本地URL/规则模型/runtime准入开启预检 → 两个随机租户、
六个独立会话SSE确认 → AdmissionService → 只读回执观察 → 聚合JSON报告。接受
ENVIRONMENT=e2e、--base-url、--observe-seconds、--resource-report、--output；不保存消息或
确认token，不自动审批或取消Gate。CPU/内存来自Docker聚合stats；15秒有限观察不代表
长稳容量。测试入口 test_conversation_hooks_load，真实执行报告位于 output/conversation-hooks。

远端registry不可用时，已验证的 `frontend/dist` 可用 Dockerfile.conversation-hooks.cached
与核对哈希的本地Nginx镜像离线打包；构建上下文只传dist。不会将本地身份YAML或.env带入
镜像。默认WEB_RUNTIME_IMAGE为 conversation-hooks-web-base:20261013；先 npm run build。

`deploy/conversation-capability-prompt.example.yml` 提供版本化提示草稿，仅含Prompt片段，
没有 scene binding，不自动启用企业模型；经人工评审后合并到正式版本化模型配置。既有
企业Prompt、规则与生产路由未被该草稿更改。

最终本地源码回归为后端3208通过、116跳过，前端322通过；lint、类型检查、构建通过。
M4真实HTTP/SSE主链路4项通过，既有数据集接口跨租户拒绝沿用409（仅错误详情），
修正断言后以现有请求只读复验1项通过，不再次创建占用Gate的任务。
多租户实际HTTP报告：两个随机租户各3个确认写作请求，每租户峰值2；15秒观察全部检查
通过，轮次最大0.544秒（自动测试立即确认，不能与人工等待的浏览器耗时直接比较）。
前后聚合资源水位及 m5-load-report.json 均在 output/conversation-hooks，未保存消息/令牌。

实际写作 Worker 再次重启后，原六个实体只读复验保持每租户2运行、1排队，
m5-worker-restart-check.json 为独立证据。业务Gate未获助手批准，队列未被强行排空。

跨窗口与同租户READ用户的下游读合并：receipt GET → ReceiptReadCoordinator.read →
每caller重新HookSpec权限/启用/参数校验 → tenant/hook/entity键 → singleflight或短缓存 →
ConversationTools.execute → 原有SQL/Temporal → 独立deepcopy响应。前端同页合并请求；
跨窗口HTTP独立鉴权，后端每API进程合并下游读取，API副本之间不做全局缓存。
用户Memory不参与共享。固定TTL写作3秒/评测15秒，256项LRU、128个在途、10秒读取界限；
失败及带错误的unknown不缓存，取消waiter不取消共享任务，lifespan先关闭读任务再关闭DB。
两个回执读取端点均返回 `Cache-Control: private, no-store`，浏览器和HTTP代理不复用身份切换前的响应。
入口test_conversation_receipt_reads包含并发、租户、403、失败、取消、TTL、容量、生命周期。

最终部署只读复验 `/ready` 三项均ok、API/web镜像与最终tag一致且healthy；现有评测回执
GET=200并返回private,no-store，真实评测记录completed、Workflow仍waiting_approval。
证据保存于 `output/conversation-hooks/final-deployment-check.json`，未新增任务或提交审批。
