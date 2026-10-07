# 按长度压缩会话上下文

2026-10-06：`conversation-length-v2` 在完整请求字符数超过软阈值时压缩较早对话，
以硬预算阻止超大请求发送。上下文不再固定取最近 8 轮；低于软阈值时加载尚未压缩的
所有已完成消息，压缩后保留摘要、真实工具引用和按长度选择的近期完整轮次。
前端展示历史仍使用原有分页接口；失败轮次不参与模型上下文，原始记录始终保留。

## DeepSeek Harness 原理映射

借鉴的是压缩决策与发布规则，本项目继续使用 Python 原生链路和企业模型 Port。
DeepSeek Harness 的 `compaction-basic` 在请求压力达到阈值后选择较早区间，保护完整
工具调用/结果配对；摘要包含包装后的开销，必须比被替换内容更小才替换模型可见历史。
原事件日志继续保留。本项目将这些原则映射为完整完成轮次、字符软阈值与硬预算、
发布前收缩检查、原始 PostgreSQL 消息/工具台账和摘要水位 CAS。

Harness 的 token-meter 使用启发式计价并在条件匹配时参考供应商 usage，不等于精确
tokenizer。本项目目前明确使用完整兼容请求 body 的字符数，不把字符预算描述为模型
token 预算。Harness 的通用工具正文裁剪与真实上下文溢出后重试没有在此直接照搬；
新闻 ID、来源和权威数字仍由确定性代码处理，真实溢出恢复须先确认企业错误码契约。

## 完整调用链

```text
/chat → conversationsApi.send / sendStream
→ POST /api/v1/conversations/{id}/messages 或 messages/stream
→ 网关鉴权、tenant_id/user_id、幂等 request_id
→ ConversationAgentService.prepare_turn / run_turn
→ LengthBasedContext.load → PostgresConversationRepository.load_memory / context_page
→ PostgreSQL conversations.context_memory + conversation_turns
→ 完成轮次转换为 user / assistant / tools；以 created_at + id 游标分页，每页 100 条
→ LengthBasedContext.load / fit：按完整兼容请求 body 计量，超过软阈值后选择较早完整轮次
→ _summarize：previous_summary + 较早文本片段 + max_summary_chars
→ NativeStructuredAgentClient.input_chars：摘要每个片段也按完整请求 body 检查硬预算
→ NativeStructuredAgentClient.run_structured(mode=conversation_memory)
→ PromptRegistry / StructuredInferenceService / InferencePort
→ 严格 SummaryOutput 验证、动态摘要长度校验
→ _references：Python 从已完成工具快照提取 run / 新闻 / 窗口引用
→ 构造候选摘要 + 真实引用 + 近期完整轮次，必须严格缩小完整请求并满足硬预算
→ 候选已收缩但仍超过硬预算时，有界扩大较早区间重新摘要，不保存中间候选
→ save_memory：身份过滤、会话锁、活跃请求校验、摘要游标 CAS、模型请求 ID 检查点
→ PostgreSQL 保存摘要、压缩水位、覆盖轮次数和引用
→ 摘要 + 最近完整历史 + 当前问题 + 当前工具快照
→ input_chars 完整请求硬预算护栏 → conversation 场景规划
→ 受限工具 → checkpoint_turn → 下一次模型调用前重新计量与压缩
→ Python 渲染权威快照 → finish_turn → SSE / JSON 答复
```

核心代码是 `app/conversation/context.py` 的 `LengthBasedContext.load / fit / _summarize`。
`app/conversation/service.py` 将其接入普通消息和 SSE 共用的执行循环；每个请求使用独立
缓冲区。超过软阈值后把较早对话压到摘要中；下一次压缩会合并旧摘要，服务重启后从
数据库摘要水位继续加载。选择和保留以完整完成轮次为单位，不拆开同轮用户、答复与
工具关系。单条过长旧回答仅在摘要输入中切为有界片段，全部片段成功后才形成候选。
数据页 100 条是读取批次，不是上下文轮数上限。

`app/model_runtime/agent_client.py` 的 `NativeStructuredAgentClient.input_chars` 与
实际调用共用 `app/model_runtime/core.py` 的消息/Schema 渲染和
`app/model_runtime/http.py` 的兼容 HTTP body 构造。计量覆盖 system Prompt、用户 JSON、
输出 Schema、`response_format` 中重复的 Schema 和 `extra_body`。本地确定性替身和
测试模型也提供同一计量方法；计量本身不发起模型请求。

摘要字段由 `app/models/conversation.py` 声明，`app/repositories/conversation.py` 负责
隔离查询与原子保存。保存必须持有尚在 processing 的原请求和匹配的旧摘要游标；
已被恢复或被新轮次替代的请求不能覆盖摘要。原始 user、assistant 和工具结果不会因压缩
删除或改写，摘要只是可重建的上下文投影，不自动晋升到跨会话 Memory。候选必须先通过
带包装、引用和近期历史的完整请求计量，才调用 `save_memory`；摘要不收缩或仍超过硬
预算时，旧摘要和水位保持不变。不收缩的候选立即拒绝；已收缩但仍超预算的候选允许
有限重试，只扩大待压缩的较早区间，不提交中间候选。

摘要 Prompt `conversation-memory-v1` 在 `deploy/model-runtime*.yml` 的
`conversation_memory` 场景注册，使用当前会话相同的模型路由。已有 conversation Prompt
版本、摘要 Prompt 和生产 Bundle 不改写。`app/main.py` 启动时检查摘要场景，轮次 runtime_metadata 保存
压缩策略、预算、主会话/摘要 Prompt 版本与哈希、模型路由。实际推理依赖企业已有接口，
不引入外部 Agent 框架。`local_inference.py → local_conversation_summary` 是用于测试的
确定性片段压缩替身，不能作为企业语义摘要质量验收。

摘要与原消息都作为 user 消息中的数据传入，不能改变 system Prompt 或工具权限。
模型摘要中的 run_id 不会进入分析授权集合。已压缩的真实引用独立保留，按预算保留最近
快照；保留的新闻引用包括 news_id、rank、短标题和来源窗口。旧引用超预算被挤出时，可
从原始历史中取得 run_id 后重新 read_hot_news，再进行分析。权威数字仍来自 Python/SQL。

## 配置与边界

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `CONVERSATION_CONTEXT_MAX_CHARS` | 32000 | 完整兼容请求 body 的字符硬预算 |
| `CONVERSATION_CONTEXT_THRESHOLD_RATIO` | 0.8 | 超过硬预算的该比例时触发压缩，范围 (0, 1] |
| `CONVERSATION_CONTEXT_MAX_COMPACTION_ATTEMPTS` | 2 | 一个压缩决策最多生成的候选次数 |
| `CONVERSATION_CONTEXT_RECENT_CHARS` | 12000 | 压缩后近期完整消息的目标长度 |
| `CONVERSATION_CONTEXT_SUMMARY_CHARS` | 4000 | 模型摘要预算；真实引用另有同等大小预算 |
| `CONVERSATION_TURN_TIMEOUT_SECONDS` | 60 | 整轮预算，包含历史读取、所有压缩调用和工具调用 |
| `MODEL_RUNTIME_CONFIG_PATH` | 沿用部署配置 | 必须声明 conversation_memory 场景和版本 |

完整兼容 body 按 `json.dumps(ensure_ascii=False, sort_keys=True, allow_nan=False)`
计量，包含 JSON 转义及包装开销，采用保守字符预算。它不是精确 token 数，也不包含
回答 token 或 HTTP headers；部署仍须为真实模型生成预算预留空间。
recent + 2 × summary + 1000 必须小于 floor(max × threshold_ratio)，候选尝试次数须为
1–4 的整数。固定预留是配置检查，实际发送仍按完整 body 计量。

软阈值用于提前压缩，硬预算用于发送前拒绝。旧摘要及引用和待压缩文本全部进入候选
收缩比较；候选必须严格减小完整请求并在硬预算内，才保存缓存和水位。
`max_compaction_attempts` 限制候选尝试，默认最多两次；它不同于摘要片段的暂态传输
重试，后者最多重试一次。所有模型调用和历史读取仍受整轮超时约束。
压缩失败不发布中间候选，保留最近成功的摘要与原历史，不静默丢弃消息。
没有未压缩轮次时，已存摘要加固定输入处于软阈值与硬预算之间可以继续；若配置缩小后
这个不可压缩前缀超过新硬预算，则明确失败并保留旧缓存，当前不自动重建摘要。
当前问题、工具说明、本轮工具输出等不可压缩固定部分自身超过硬预算时，直接返回
`context_budget_exceeded`，不浪费摘要调用；已保存工具检查点继续保留。

旧 `CONVERSATION_HISTORY_TURNS` 已移除。原来每轮回答 2000 字截断、固定轮次切片和
超长 pop(0) 的逻辑均移除。仅压缩缓存中的标题采用短标题，原消息和原工具数据完整保留。

## 升级与测试入口

先在部署数据库执行 `alembic upgrade head`，新增迁移 `20261006_0019`；它只给
conversations 新增 JSONB context_memory 默认空对象，已有会话首次超过软阈值时创建摘要。
再发布包含上述代码和新摘要场景 YAML 的 API 镜像并重启。前端说明位于 ChatView.vue，
需按原前端发布流程更新。v2 使用原表、原迁移和原 Prompt/模型绑定，不新增数据库迁移。
已有 `conversation-length-v1` 缓存按原水位正常加载；直到下次成功压缩才写入 v2 策略版本，
不在启动或读取时强制重写缓存。本轮没有执行迁移、发布或重启；运行实例版本未核实。

开发依赖齐备时，在 agent 目录执行：

```bash
python -m pytest tests/test_conversation_compaction_policy.py tests/test_model_input_budget.py \
  tests/test_conversation_memory.py tests/test_conversation_store.py \
  tests/test_conversation_agent.py tests/test_conversation_stream.py \
  tests/test_conversation_security.py tests/test_conversation_tool_checkpoints.py
python -m pytest -m 'not e2e and not live and not integration'
cd frontend
npm run typecheck
npm exec eslint src/views/ChatView.vue
```

`test_conversation_compaction_policy.py` 覆盖软阈值、候选收缩、硬预算、有限候选尝试及
失败不发布；`test_model_input_budget.py` 覆盖共享请求渲染和 system/Schema/body 开销。
`test_conversation_memory.py` 验证超过 8 轮和跨页历史、长度触发、超长单轮、摘要重启复用、
幂等重放、摘要失败、真实引用保留、伪造授权隔离、本轮工具预算与请求/水位保护。
`test_conversation_store.py` 验证迁移与 ORM；流式测试仓库也实现新的摘要和分页契约。

真实 PostgreSQL 验收须在一个新建、无持久卷的测试容器中执行，避免对任何既有数据库
操作。以下命令需本地 Docker 和已安装 pytest 的现有测试镜像；TEST_IMAGE 应替换为
实际测试镜像，不使用 .env 或企业密钥。运行目录为 agent：

```bash
docker run -d --name newsagent-memory-test --network none \
  -e POSTGRES_DB=conversation_memory_test -e POSTGRES_USER=memory_test \
  -e POSTGRES_PASSWORD=memory_test_public_fixture postgres:16-alpine
docker exec newsagent-memory-test pg_isready -U memory_test -d conversation_memory_test
docker run --rm --network container:newsagent-memory-test --entrypoint python \
  -e CONVERSATION_MEMORY_TEST_DATABASE_URL=postgresql+psycopg://memory_test:memory_test_public_fixture@127.0.0.1:5432/conversation_memory_test \
  -v "$PWD/app:/app/app:ro" -v "$PWD/tests:/app/tests:ro" \
  -v "$PWD/deploy:/app/deploy:ro" -v "$PWD/alembic:/app/alembic:ro" \
  -v "$PWD/pyproject.toml:/app/pyproject.toml:ro" TEST_IMAGE \
  -m pytest -p no:cacheprovider tests/test_conversation_memory_postgres.py
docker rm -f newsagent-memory-test
```

这个测试验证真实 0018 → 0019 无损升级、空摘要兼容、压缩水位、JSONB 保存和新仓库读取，
以及降级/再升级后原消息仍在。降级会删除摘要缓存，再升级可由原始历史重建。

v1 旧验证记录：隔离后端单元回归 1905 passed、5 skipped、59 deselected；最终会话专项
122 passed（包含后来补充的空摘要、伪造授权与重压缩失败保护）；一次性真实 PostgreSQL
验收 1 passed；前端 typecheck 与 ChatView.vue ESLint 通过。会话专项与完整回归有重叠，
不能将这些数字相加作为独立用例总数。这些数字不代表 v2 本轮验证结果；v2 的实际命令
与结果如下。

v2 本轮验证（2026-10-06）：在 `agent` 目录执行
`pytest -p no:cacheprovider -m 'not e2e and not live and not integration'`，
结果为 **1938 passed、5 skipped、59 deselected**。新增策略测试 11 项全部通过，
完整请求计量、v1 摘要兼容、固定输入超限和现有会话/工具流程均在这次回归范围内。
3 个警告来自现有 FastAPI/Starlette 与 SQLAlchemy 依赖调用。
此次未执行真实 PostgreSQL 并发/故障测试、企业模型调用或前端验证；未改动前端文件。
历史 v1 的 PostgreSQL 和前端验证记录不能作为 v2 的本轮验收结果。

## 未完成 TODO

- 企业模型语义压缩质量、偏好/纠正/待办保留和恶意内容注入评测。
- 企业模型 tokenizer/usage 计价与生成 token 预算；当前完整 body 字符计量不是 token 保证。
- 企业真实上下文溢出错误码与恢复契约；只在有明确收缩进展且未取消时进行有界模型重试。
- 生产数据保留、脱敏与加密策略；生产发布和长稳/负载验收。
- 跨会话长期记忆仍沿用独立审批链，本次未开启自动晋升或跨用户共享。
- 缩小预算后的旧摘要重建、failed/interrupted 轮次的已完成工具事实恢复及真实 PostgreSQL 并发/故障验收。
