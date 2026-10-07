# 会话安全 SSE 流式响应

聊天页默认使用 SSE 响应：先实时显示 Agent 处理阶段和获准工具状态，再接收已校验、已保存
答复的分段。旧 JSON 消息接口仍保留，两者执行同一个 Python Agent，不存在第二条业务链路。

这是**安全事件流与答复分段传输**，不是企业模型原始 token 的直接透传。当前原生推理 Port
返回结构化计划，完整结果必须先经过 Schema、工具权限和数字/引用约束；未校验的 JSON、
模型思考或新闻指令不会显示为最终答复。没有为了动画而添加逐字 sleep。

## 完整文件调用链

```text
ChatView.vue：原 UUID、当前会话草稿、临时阶段/工具/文本
  → conversationsApi.sendStream
  → conversationStream.ts：POST fetch、Cookie、150 秒预算、UTF-8/SSE 解码
  → /api/v1/conversations/{conversation_id}/messages/stream
  → 网关角色 + tenant/user → 严格消息 Schema
  → ConversationAgentService.prepare_turn（10 秒占用预算）
  → PostgresConversationRepository.begin_turn：身份、内容冲突、幂等、单飞
  → ConversationStreamingResponse + StreamingConversation.iter_events
  → ConversationAgentService.run_turn（复用同一 claim，不二次提交）
  → 最近历史 → NativeStructuredAgentClient → YAML Prompt/模型路由 → InferencePort
  → ConversationPlan 校验 → ConversationTools 获准查询/Embedding 检索
  → ADMIN 本地 query_hot_news：复用热点 Text2SQL/Temporal，工具结果回到会话模型
  → Python 数字/引用显示 → finish_turn 提交 PostgreSQL 终态
  → answer_delta → done → Vue 以持久结果替换临时显示
```

后端入口是 `app/api/conversations.py` 的 `stream_message`；
`app/conversation/service.py` 拆分请求占用与执行，旧 `send` 仍复用它们；
`app/conversation/streaming.py` 使用有界队列、响应生命周期内的执行任务和 JSON SSE 编码。
`app/config.py` 校验心跳和分段大小。前端入口是 `frontend/src/views/ChatView.vue`，
`frontend/src/api/conversations.ts` 提供调用入口，`frontend/src/api/conversationStream.ts`
处理增量响应；浏览器仍只带 Cookie，开发身份仍由既有 YAML 代理注入。

模型和工具外部依赖不变：本地使用隔离确定性模型 Port，企业使用经审批的
OpenAI-compatible 推理/Embedding 接口；热点报告和知识台账来自 PostgreSQL，知识检索
复用原有 Embedding/索引 Port。普通 READ 用户仍只能读取；本地显式启用且具备 ADMIN 权限
时，`query_hot_news` 可复用既有 SQL/热点 Workflow，不另建 SQL 执行器。它会写查询审计和
热点运行，不套用普通只读工具的自动重试。聊天没有发布、审批或配置修改工具。
完整取数与模型配置链见 [聊天驱动数仓查询](conversation-warehouse-model.md)。
SSE 不额外依赖 Redis，不新增数据库表；整个应用的 Redis/Temporal 就绪检查仍照旧。
取消清理使用 Web 框架已有的 AnyIO，不引入外部 Agent 框架。

## HTTP 与事件契约

请求：

```text
POST /api/v1/conversations/{UUID}/messages/stream
Content-Type: application/json
Accept: text/event-stream

{"request_id":"UUID","content":"消息原文"}
```

在返回 SSE 头之前，身份、权限、消息校验、会话归属和请求占用必须完成；这些错误仍返回
正常 JSON HTTP 401/403/404/409/422/503，不能包装为假成功的 200 流。
成功返回 `text/event-stream; charset=utf-8`、`Cache-Control: no-cache, no-store` 和
`X-Accel-Buffering: no`。网关/代理需要允许流式响应、不缓存、不缓冲。

| 事件 | 内容 | 含义 |
| --- | --- | --- |
| `accepted` | `turn_id, request_id, replayed, status` | 原请求已取得轮次；首个事件 |
| `phase` | `phase, message`，可有调用序号/重试次数 | 实时历史、模型、重试、保存进度 |
| `tool_started` | `index, name` | 正在执行获准工具 |
| `tool_finished` | `index, name, status, attempts, error_code` | 临时工具状态，不代表本轮已提交 |
| `answer_delta` | `turn_id, request_id, index, text` | 已持久化答复的片段；序号从0连续递增 |
| `done` | `turn` | PostgreSQL 返回的完整终态，作为页面最终事实源 |
| `error` | `code, message` | 流内基础设施故障；固定脱敏提示，不反射异常或密钥 |

心跳使用 `: keep-alive` SSE 注释。模型的原始计划、未校验参数/结果和堆栈不进入进度事件。
答复中的换行由 JSON 编码，不能伪造新的 SSE event/data 行。
正常终态的所有 `answer_delta.text` 拼接应与 `done.turn.assistant_content` 完全相等。
前端拒绝错请求、错轮次、重复/乱序片段、坏 UTF-8、过大缓冲、未知事件和缺少 done 的结束。
前端临时内容不写浏览器持久存储，不使用模型 HTML。

## 断线、重放与持久化

- **连接断开不等于请求没有执行。** 同一 UUID 只能恢复原轮次，不自动生成新请求。
- 响应生成器拥有执行任务。离开页面/断线会取消执行并等待有界清理，尽可能保存
  `failed/interrupted`；未发布内容、未修改规则。普通会话切换不会把迟到流写入别的会话。
- 覆盖 ASGI 发送失败和旧版 disconnect 通知两种路径：响应结束时显式关闭生成器，清理
  被 await，不留下无人管理的后台任务。
- 断线恰好落在数据库提交后，真实仓储的状态 CAS 不允许 interrupted 覆盖已完成轮次；
  刷新历史能恢复完整答案，即使没收到最后一个 delta/done。
- 数据库不可用/硬退出/占用后尚未开始响应体时，现有“轮次预算 + 30 秒”过期恢复仍生效；
  GET 历史或下一条明确请求触发回收，不自动重新运行旧轮次。
- 已完成/失败轮次用相同 UUID 和相同内容重放，会直接发送原终态的片段和 done，不调用
  模型或工具；processing 返回409；相同 UUID 改内容返回409。
- `phase/tool_*` 是临时进度，不是逐工具持久检查点。流内存丢失不损坏 PostgreSQL 已提交
  的历史，但未提交的轨迹仍可能丢失。
- 查询绑定后会发送 `hot_news_workflow` 阶段，并尽可能保存任务引用。断线只取消聊天等待，
  不取消已启动的持久热点 Workflow；未确认完成不会显示为成功，也不回退旧报告。

## 配置和预算

| 配置 | 默认值 | 作用 |
| --- | --- | --- |
| `CONVERSATION_STREAM_HEARTBEAT_SECONDS` | 10 | 空闲心跳，范围1–30秒 |
| `CONVERSATION_STREAM_CHUNK_CHARS` | 128 | 已保存答复每段字符数，范围16–1024 |
| `CONVERSATION_TURN_TIMEOUT_SECONDS` | 60 | 复用原模型/工具总预算，最大120秒 |

占用请求另有10秒预算；流执行有“轮次预算 + 10秒”总界限，取消落库最多5秒；前端流请求
预算150秒，旧 JSON 请求保持120秒。工具/模型重试上限、上下文和 Prompt 版本绑定不变。
`MODEL_RUNTIME_CONFIG_PATH` 决定模型 YAML；查询集成后本地会话绑定为
`native-conversation-v2`，兼容接口模板为 `compatible-conversation-v2`。查询演示总预算
显式设为120秒；原只读工具仍使用有限重试和短超时，查询等待由整轮预算限制。

## 演示与测试

按 [持续对话启动说明](conversation-agent.md) 启动，打开 `/chat`，输入 `查看最近热点`，
查看处理/工具状态及答复；继续输入 `解释第一条新闻`，再刷新页面检查同会话结果。
本地模型很快，浏览器可能一次渲染多个片段；不要把规则替身演示说成企业模型 token 质量验收。

后端在 `agent/` 执行：

```bash
python -m pytest -o addopts='' tests/test_conversation_stream.py -q
CONVERSATION_E2E_BASE_URL=http://127.0.0.1:28000 \
  python -m pytest -o addopts='' tests/test_conversation_stream_e2e.py -q
python -m pytest -o addopts='' -q
```

前端在 `agent/frontend/` 执行 `npm test`、`npm run lint`、`npm run build`；
SSE 解析测试和会话测试覆盖跨块编码、早于 done 的临时显示、断线与迟到响应。
以上流式 HTTP 测试只接受隔离本地地址，只创建自身随机会话，不删除数据、不触发热点
Workflow。新增 `test_conversation_hot_news_e2e.py` 则明确触发本地热点查询，验收入口和
权限边界见 [聊天驱动数仓查询](conversation-warehouse-model.md)。

未完成 TODO：企业模型真正的增量推理 Port（仍需安全校验门禁）、企业 Gateway SSE/闲置
超时合同、流式长稳与并发压测、逐工具持久检查点、会话数据保留治理。当前不是从中间
token/cursor 恢复；通过原请求 UUID 和已保存的整轮结果恢复。
