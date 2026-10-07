# 聊天驱动数仓查询与兼容模型分析

## 当前能力与边界

自然问法和日期条件先经过结构化查询理解与Python覆盖校验，完整契约、调用链和测试见
[自然查询语义层](query-understanding.md)。当前会话绑定v5 Prompt，并使用独立的
`query_understanding`模型场景；历史Prompt版本保留。

聊天不再只能读取旧报告。具有 `hot-news:admin` 权限的本地演示用户可以输入查询问题，
调用 `query_hot_news`，实际执行原有热点 Text2SQL 和 Temporal Workflow，再把同一运行的
指标与分析结果送回会话模型生成答复。`hot-news:read` 用户仍只能读报告/批准的新闻知识。
查询只读数仓，但会写查询审计和热点运行，所以它不套用普通只读工具的自动重试。
不发布、不审批、不改规则；生产 `create_app` 不挂载本地数仓查询适配器。

默认启动仍使用规则模型 Port。兼容接口配置模板已经具备；在提供 URL、模型和密钥前，
不能声称已验证真实模型理解、推理质量或真实企业数仓。公开外部模型只能发送获得批准的
合成/非敏感数据，企业数据应只连接批准的内部地址。本轮没有调用任何真实模型端点。

## 完整文件调用链

```text
/chat / ChatView.vue
  → conversationsApi.runtime：读取后端模式/权限/固定窗口，不读取密钥或服务URL
  → sendStream / conversationStream.ts：原UUID、POST SSE、UTF-8解码
  → api/conversations.py：网关tenant/user、READ入口、独立ADMIN查询权限
  → ConversationAgentService.prepare_turn / run_turn：幂等轮次与有界模型循环
  → NativeStructuredAgentClient / StructuredInferenceService：YAML Prompt + 模型路由
  → ConversationPlan：query_hot_news(question)，不接受SQL/身份/日期/场景参数
  → ConversationTools.execute_hot_news_query：显式启用、租户和ADMIN权限、严格参数
  → examples/conversation_hot_news.py / LocalConversationHotNewsQuery
  → query_understanding场景 / QueryUnderstanding → Python日期、覆盖、来源与条件校验
  → canonical_question + expected_intent；未满足则说明原因，不启动SQL或Workflow
  → examples/local_simulation.py / execute_local_hot_news_query（与热点工作台共享）
  → SqlAssistantService.preview：模型生成受限intent → expected_intent逐项核对 → compile_query → SQL AST/绑定参数护栏
  → SQL快照、scope哈希、HotNewsSqlBindingStore原子绑定幂等run_key
  → Temporal HotNewsMonitorWorkflow / 已有热点Activity
  → native_hot_news_sql_support：PostgreSQL READ ONLY取候选、同小时指标/基线补充
  → Python热度计算/排序 → 新闻正文/关联证据 → NativeHotNewsAnalysisRunner / InferencePort
  → HotNewsAnalysisValidator → 同一run结果保存 PostgreSQL / MinIO
  → 同一run报告、SQL query_id、规划/分析模型request_id返回query_hot_news
  → 会话模型再次读取工具结果，生成解释（不生成权威数字/ID）
  → Python显示权威快照 → 会话终态保存 → answer_delta/done → 页面
```

热点查询入口没有第二套 SQL 执行器，没有内部 HTTP 自调用，也没有 FastGPT。外部依赖为
既有 PostgreSQL、Temporal、Redis、MinIO；真正推理使用既有 `OpenAICompatibleInferenceClient`
直接 POST 兼容模型接口。MockTransport 合同测试验证的是 HTTP 协议/模型循环，不是大模型质量。

`query_hot_news` 先保存稳定 `workflow_id/sql_query_id` 到当前内存轨迹，再等待 Workflow。
取消聊天只停止等待，不会取消持久 Workflow；尽可能在 interrupted 轮次保存这些引用。
响应丢失用原 UUID 恢复，不重新执行已完成轮次。新请求同查询作用域复用原有绑定/运行。
失败不回退旧报告。工具完成后提交同轮次检查点；硬退出前尚未提交的绑定引用仍可能丢失。

## 现在如何演示

按 README 启动本地 Compose 与前端，打开 `http://127.0.0.1:5174/chat`：

1. 输入 `查询点击率最高的前5条视频新闻并分析原因`。
2. 查看实时工具状态；完成后展开 `query_hot_news` 的结果，核对 SQL、query_id、run_id、
   query_model_request_id 和 analysis_model_request_ids。
3. 输入 `解释第一条新闻`：读取同一run，不另发新查询。
4. 刷新页面：恢复 PostgreSQL 保存的历史。

默认场景 `news-ranking`，固定北京时间2026-10-03 00:00–01:00合成样本，并非今日实时新闻。
本地规则 Port 支持有限自然问法／中文数量，`并分析原因` 是有限展示任务后缀，不是开放式推理；
未知条件仍拒绝，不能通过添加“分析”来忽略未知筛选条件。`查看最近热点` 专用于读取旧报告。
工具可返回最多5条展示摘要；完整run和SQL候选行数以热点报告为准。

## 真实模型配置模板

文件：`deploy/model-runtime.compatible.example.yml` 与
`deploy/docker-compose.compatible-model.example.yml`。暂不启用，默认栈不会连接模板地址。
在代码编辑器中复制前者为 `deploy/model-runtime.compatible.yml`（已加入gitignore），填写：

- `inference.url`：完整接口地址，例如企业批准的 `https://HOST/v1/chat/completions`。
  本项目不自动补 `/v1/chat/completions`；只有根URL/Base URL时应按服务契约补完整路径。
- `inference.model_routes`：替换 `CHANGE_ME_MODEL` 为接口支持的模型名称，YAML锚点自动
  绑定会话、Text2SQL、热点分析和QA场景。
- `inference.api_key_env`：默认 `NEWSAGENT_MODEL_API_KEY`，只声明变量名。密钥由安全终端
  或密钥系统注入该变量；修改变量名时也同步修改Compose环境注入。
- `response_format`：默认 `none`，仅在接口确认支持时改 `json_object/json_schema`。
  响应必须有符合业务Schema的JSON content，且响应 `model` 必须与请求路由一致；模型别名
  不能在未批准时任意漂移。默认30秒单次调用预算。
- Overlay中的 `model_version` 也替换为同一模型名称；它与
  `analysis_prompt_version=compatible-hot-news-v1` 一同形成不可变Bundle执行快照。

此模板是“真实推理 + 合成数仓 + 本地Embedding”的E2E组合，不是生产配置。只有提供
Chat Completions接口也可演示；Embedding仍为明确标识的32维规则Port。切换企业Embedding
需另外配置接口、维度、版本，并重建对应索引，不能沿用不兼容向量。
模板只包含本次查询所需场景，不宣称已把写作/Data Loop也切换为真实模型。

填好且确认允许发送测试数据后，在 `agent/` 执行：

```bash
# 两套示例栈使用相同回环端口，先停旧栈；不删除数据卷。
docker compose -f deploy/docker-compose.native-e2e.yml stop

# 新项目名使用单独数据卷/Bundle，旧历史仍留在原栈。
# 不要运行 docker compose config：它可能打印注入的密钥。
docker compose -p news-agent-model-demo \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.compatible-model.example.yml up -d --build
```

API和热点Worker必须一起切换，不能只改API，否则数仓规划与热点分析会用不同模型。
如果复用已有数据库，活动Bundle与新manifest不符会被拒绝；应走既有人工审批/激活流程，
不可直接覆盖旧Bundle或清库。示例新项目名避免修改旧运行的模型身份，但不会删除旧历史。
密钥未注入时Compose/模型工厂失败关闭，不回退本地规则伪装成功。

停止模型演示栈后可重新 `up -d` 原栈恢复旧数据。前端仍连接相同回环API端口，刷新后通过
`/conversations/runtime` 显示实际provider，不再根据前端dev标志猜测是否用了真实模型。
“兼容接口模式”仅说明配置，不代表该模型的分析质量已经验收。

## 配置与测试入口

- `CONVERSATION_HOT_NEWS_QUERY_ENABLED` 默认false，仅本地Compose显式true。
- `CONVERSATION_HOT_NEWS_QUERY_SCENARIO` 默认`news-ranking`，必须是已批准ranking场景。
- `CONVERSATION_HOT_NEWS_QUERY_HOUR` 默认0，范围0..23，固定一天内单整小时；服务端选取，
  模型和浏览器不得改变。跨小时UV不可相加。
- `CONVERSATION_TURN_TIMEOUT_SECONDS` 本地120秒；默认工具预算3，流预算150秒；
  SQL场景本身有15秒模型、10秒只读SQL上限。聊天等待受总预算限制，Workflow可能继续运行。
- 本地会话Prompt绑定`native-conversation-v5`，真实模板为`compatible-conversation-v5`；
  每轮保存路由、provider、Prompt版本和哈希。不是修改既有生产Prompt。

在 `agent/` 执行：

```bash
python -m pytest -o addopts='' tests/test_conversation_hot_news_query.py -q
CONVERSATION_E2E_BASE_URL=http://127.0.0.1:28000 \
  python -m pytest -o addopts='' tests/test_conversation_hot_news_e2e.py -q
python -m pytest -o addopts='' -q
```

前端 `npm test`、`npm run lint`、`npm run build`。测试包括兼容HTTP规划/结果复入、鉴权、
身份/SQL注入拒绝、幂等、查询不自动重试、取消后保留任务引用、模式展示，以及真实本地
PostgreSQL/Temporal的同run查询/分析绑定。

TODO：真实服务合同与泛化质量验收；企业只读数仓Adapter、完整水位和权限；
待完成Workflow恢复工具；真实增量token Port；Gateway/长稳/并发压测。
