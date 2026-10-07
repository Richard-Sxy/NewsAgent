# 持续对话 Agent

这里的“持续对话”是同一会话内连续追问，刷新和 API 重启后仍能恢复历史。不是无人值守
后台循环，不授予自动发布、训练、改配置或跨会话长期记忆权限。Agent 应用层和会话存储
由本项目 Python 实现，无外部 Agent 框架服务依赖。

## 分步骤实现

1. 会话基础：PostgreSQL 保存会话、轮次、工具结果、模型请求 ID、Prompt/模型绑定和终态。
   会话按 `tenant_id + user_id` 隔离；UUID 请求幂等；同会话同时只处理一轮。
2. Agent 循环：读取有界历史 → 原生结构化模型计划 → 参数校验 → 只读工具 → 模型解释。
   第一版允许热点报告查询、报告追问、新闻知识检索和能力说明，不创建新的热点运行。
   后续已增加隔离本地管理员的 `query_hot_news`，复用现有热点SQL Workflow进行新查询与
   分析；普通READ用户仍保持第一版能力。配置和文件链路见[数仓与模型说明](conversation-warehouse-model.md)。
3. 可视化：`/chat` 页面展示会话、消息、工具调用和证据。不确定网络结果使用同一请求 ID
   重试；刷新时从服务端恢复历史。页面将输出视为文本，不渲染模型 HTML。

聊天已增加安全 SSE：过程/工具进度实时推送，答复校验并持久化后分段传输；旧 JSON 入口
保留。它不是未校验的企业模型原始 token 透传。事件、断线恢复、配置和测试见
[流式说明](conversation-streaming.md)。

默认60秒/普通只读工具10秒预算仍适用基础配置；本地查询演示将整轮预算设为120秒。
`query_hot_news` 会保存审计/启动Workflow，不套用普通工具的10秒超时或自动重试，受整轮
预算约束；取消等待不取消持久Workflow。真实模型模板未默认启用，不把规则Port当作推理质量。

## 完整调用链与文件职责

```text
Vue /chat（ChatView.vue）
  → conversationsApi /api/v1/conversations
  → 网关鉴权 + 服务端 tenant/user 绑定
  → PostgresConversationRepository.begin_turn：会话行锁、幂等、单飞、版本快照
  → ConversationAgentService.send：最近完成轮次、有界上下文、总超时
  → NativeStructuredAgentClient.run_structured(mode=conversation)
  → YAML 的 scene → Prompt 版本 + model_route → InferencePort
  → ConversationPlan（严格一步 JSON 计划）
  → ConversationTools.execute（工具白名单 + 严格参数 + tenant 绑定）
  → Python render_tool_results（从原始快照输出指标和引用）
  → finish_turn：持久化终态、工具轨迹、模型请求 ID
  → HTTP 响应 / 历史 API → Vue 消息和工具详情
```

入口在 `frontend/src/views/ChatView.vue`、`frontend/src/api/conversations.ts`、
`app/api/conversations.py`；`app/main.py` 的 lifespan 根据配置创建服务。
`app/schemas/conversation.py` 校验消息和返回结构，`app/models/conversation.py` 定义两张表，
`alembic/versions/20261004_0018_conversations.py` 只新增表，不删除旧业务数据。

`app/conversation/service.py` 管理循环、超时、重试和终态；`plan.py` 禁止额外计划字段；
`tools.py` 负责参数、证据投影和确定性显示；`local.py` 是隔离的规则模拟计划器。
`app/model_runtime/agent_client.py` 和 `local_inference.py` 接原有原生 Port，不引入框架协议。

工具下游：

| 工具 | 核心依赖与数据转换 | 输出 |
| --- | --- | --- |
| `capabilities` | 固定 Python 能力白名单 | 当前允许的只读能力 |
| `list_hot_news` | `HotNewsQueryService.list_runs`，筛选本租户已完成运行 | 最近报告 ID、窗口、篇数 |
| `read_hot_news` | `get_run_detail`，按 run/rank 投影；指标不由模型改写 | 标题、排行、指标快照、分析及限制 |
| `search_knowledge` | `EmbeddingPort.embed` → `knowledge_store.search`，绑定租户和 Embedding 版本，分层检索、去重、截断、URL 检查 | 原始证据片段及来源引用 |

热点的 Text2SQL 仍只服务原有热点 Workflow：聚合 SQL → Python 指标/热度 → 热点报告。
聊天读取这个已有报告，不新增独立 SQL 执行入口，也不绕过热点链路或人工审批。
知识库证据属于租户共享新闻库，不等于已实现企业文档逐用户 ACL；生产默认关闭聊天知识检索。

## 状态和故障边界

会话列表现提供删除确认与本页撤销；服务端按租户和用户软删除，历史保留，活跃轮次拒绝
删除。接口、调用链、迁移与验收见[会话删除说明](conversation-deletion.md)。

- 同一请求 ID、相同内容的终态重放不会再次调用模型或工具；改内容返回 409。
- 同会话未完成轮次阻止另一个新请求。页面轮询已有轮次，而不偷偷重新执行。
- 默认单轮总预算 60 秒、最多 3 次工具调用、每次工具 10 秒；可重试模型传输错误和只读
  工具暂态错误各重试一次。整个循环仍受总预算约束；结构错误、权限错误不重试。
- API 硬退出后，GET 历史或下一次消息会把超过“单轮预算 + 30 秒”的 processing 轮次
  标成 interrupted；不自动重新执行，迟到的旧进程不能覆盖终态。
- 工具或模型失败显示真实失败，不用假成功/空编造兜底。工具轨迹在轮次终态时写入；
  硬崩溃前尚未提交的轨迹可能丢失，逐工具持久检查点仍是 TODO。
- 模型回答中的阿拉伯数字和引用字段 token 会被拦截；指标、排行、引用 ID 由 Python 从
  快照显示。该防线不是通用事实验证器，也不能证明企业模型的解释质量。
- 会话历史只是本会话上下文，不自动提升到跨天 Memory。新闻、历史和工具结果以数据
  传入，不允许改变系统 Prompt、身份、工具白名单或配置。

## 配置

| 配置 | 默认值 / 作用 |
| --- | --- |
| `CONVERSATION_ENABLED` | false；关闭时 API 返回 503，本地 Compose 为 true |
| `CONVERSATION_KNOWLEDGE_ENABLED` | false；须先确认语料权限，本地演示为 true |
| `CONVERSATION_CONTEXT_MAX_CHARS` | 32000；完整兼容请求 JSON 的保守字符预算，含系统 Prompt、Schema、问题、历史、工具描述、本轮工具结果和 extra_body；不是 token 上限 |
| `CONVERSATION_CONTEXT_RECENT_CHARS` | 12000；压缩后尽量保留的最近完整对话字符数 |
| `CONVERSATION_CONTEXT_SUMMARY_CHARS` | 4000；摘要上限，已压缩工具引用另有同等大小的预算 |
| `CONVERSATION_CONTEXT_THRESHOLD_RATIO` | 0.8；在硬预算的此比例提前触发压缩 |
| `CONVERSATION_CONTEXT_MAX_COMPACTION_ATTEMPTS` | 2；一次 fit 最多生成几次候选，扩大较早完整轮次范围后重试，最多 4 次 |
| `CONVERSATION_MAX_TOOL_CALLS` | 3；可配置 0–6 |
| `CONVERSATION_TURN_TIMEOUT_SECONDS` | 60；可配置 5–120 秒 |
| `MODEL_RUNTIME_CONFIG_PATH` | 模型 YAML：`conversation` 与 `conversation_memory` 的 Prompt、版本、路由与企业接口 |

本地配置见 `deploy/model-runtime.local.yml`（规则替身）；企业模板见
`deploy/model-runtime.enterprise.example.yml`（OpenAI-compatible 推理/Embedding）。密钥沿用
企业环境变量注入，不进入消息、模型 Prompt 或前端 YAML。更改 Prompt/路由需要版本化和审批。
`frontend/dev-server/local.yml` 控制前端端口、代理和公开演示身份；修改需重启 Vite。
后端会话服务直接依赖 PostgreSQL 和模型 Port；工具依赖已有热点查询/知识库。
长度压缩调用链、迁移步骤和验收见[会话记忆说明](conversation-memory.md)。旧
`CONVERSATION_HISTORY_TURNS` 已移除，不再影响历史加载；前端历史展示分页与模型上下文独立。
整个应用启动与 `/ready` 仍沿用 Redis、Temporal 等原有依赖。

## 本地启动与演示

从仓库根目录分别在两个终端执行：

```bash
cd agent
docker compose -f deploy/docker-compose.native-e2e.yml up -d --build
curl -fsS http://127.0.0.1:28000/ready
```

```bash
cd agent/frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5174/chat`，新建对话后依次输入：

1. `你好`
2. `我关注科技`
3. `记得上一轮我说了什么吗`
4. `查看最近热点`
5. `解释第一条新闻`
6. `检索人工智能相关新闻`

先在 `/hot-news#query-tools` 完成一次热点运行，才能演示报告追问。没有知识证据时明确显示
没有结果，不伪造新闻。刷新 `/chat?conversation=...` 或重启 API 后检查历史恢复。
全部仅为本地链路验收，不是企业模型质量验收。

## 测试入口与剩余步骤

安装项目开发依赖后，在 `agent/` 执行：

```bash
python -m pytest -o addopts='' -q
CONVERSATION_E2E_BASE_URL=http://127.0.0.1:28000 \
  python -m pytest -o addopts='' tests/test_conversation_e2e.py -q
CONVERSATION_TEST_DATABASE_URL=postgresql+psycopg://newsagent_native:newsagent_native@127.0.0.1:25432/newsagent_native \
  python -m pytest -o addopts='' tests/test_conversation_postgres.py -q
```

数据库和 HTTP 验收只接受隔离本地地址；仅新增自身随机测试会话，不清空历史。
核心单测在 `test_conversation_agent.py`、`test_conversation_store.py`、
`test_conversation_security.py`；真实验收见 `test_conversation_postgres.py` 和
`test_conversation_e2e.py`。前端在 `agent/frontend/` 执行 `npm test`、`npm run lint`、
`npm run build`；`tests/conversations.test.mjs` 覆盖并发、切换、刷新和幂等重试。

后续阶段仍需用户确认与验收：

- 企业模型合同、解释质量/注入对抗评测、知识语料权限和生产 Gateway/IdP。
- 会话数据保留、加密/脱敏、精确 token 预算与企业模型摘要质量验收。
- 企业模型增量推理 Port、逐工具持久检查点和流式长稳/负载测试。
- 生产数据库事务/锁等待超时与终态保存故障演练；终态写入失败时 API 返回脱敏 503，
  但本轮尚未提交的回答/轨迹不保证恢复，不能描述为端到端 exactly-once。
- 对话内研究写作/创建热点任务：必须复用现有 Workflow 和人工审批，不能直接赋予发布权限。
- 跨会话长期记忆需明确晋升、删除和审批策略；本阶段没有开启。
