# 聊天驱动数仓查询与兼容模型分析

本文按 2026-10-26 的源码与示例配置核对，说明本地合成数仓的聊天查询链路。
配置位于仓库根目录 `deploy/`；本次仅修正文档，未调用真实模型、启动服务或重新完成业务验收。

## 当前能力与边界

自然问法和日期条件先经过结构化查询理解与 Python 校验。本地规则 Port 使用
[自然查询语义层](../app/modules/conversation/query_understanding.py) 的有限解析器；兼容模型输出使用
[结构化结果校验](../app/modules/conversation/structured_query_resolution.py)。两者均核对批准字段、
日期、整点窗口、数据覆盖与来源。当前本地和兼容示例分别绑定各自的 v5 会话与查询理解 Prompt，
`query_understanding` 是独立模型场景；历史 Prompt 版本仍保留。

聊天不再只能读取旧报告。具有 `hot-news:admin` 权限的本地演示用户可以输入查询问题，
调用 `query_hot_news`，实际执行原有热点 Text2SQL 和 Temporal Workflow，再把同一运行的
指标与分析结果送回会话模型生成答复。仅有 `hot-news:read` 权限不能启动该查询；
其他写作、评测和反馈工具按各自功能开关、权限与确认要求另行授权。
查询只读数仓，但会写查询审计和热点运行，所以它不套用普通只读工具的自动重试。
`query_hot_news` 不发布、不审批、不改规则；生产 `create_app` 不挂载本地数仓查询适配器。

默认启动仍使用规则模型 Port。兼容接口配置模板已经具备；在提供 URL、模型和密钥前，
不能声称已验证真实模型理解、推理质量或真实企业数仓。公开外部模型只能发送获得批准的
合成/非敏感数据，企业数据应只连接批准的内部地址。本轮没有调用任何真实模型端点。

## 完整文件调用链

```text
/chat / ChatView.vue
  → conversationsApi.runtime：读取后端模式/权限/默认窗口与数据覆盖，不读取密钥或服务URL
  → sendStream / conversationStream.ts：原UUID、POST SSE、UTF-8解码
  → api/conversations.py：网关tenant/user、READ入口、独立ADMIN查询权限
  → ConversationAgentService.prepare_turn / run_turn：幂等轮次与有界模型循环
  → NativeStructuredAgentClient / StructuredInferenceService：YAML Prompt + 模型路由
  → ConversationPlan：query_hot_news(question)，不接受SQL/身份/日期/场景参数
  → ConversationTools.execute_hot_news_query：显式启用、租户和ADMIN权限、严格参数
  → simulation/conversation_hot_news.py / LocalConversationHotNewsQuery
  → query_understanding场景 / QueryUnderstanding → 按provider选择Python校验器，核对日期、覆盖、来源与条件
  → canonical_question + expected_intent；未满足则说明原因，不启动SQL或Workflow
  → simulation/local_simulation.py / execute_local_hot_news_query（与热点工作台共享）
  → SqlAssistantService.preview(expected_intent=...)：Python独立解析canonical_question并逐项核对intent
  → compile_query → SQL AST/绑定参数护栏（此分支不再次调用Text2SQL规划模型）
  → SQL快照、scope哈希、HotNewsSqlBindingStore原子绑定幂等run_key
  → Temporal HotNewsMonitorWorkflow / 已有热点Activity
  → native_hot_news_sql_support：PostgreSQL READ ONLY取候选、同小时指标/基线补充
  → Python热度计算/排序 → 新闻正文/关联证据 → NativeHotNewsAnalysisRunner / InferencePort
  → HotNewsAnalysisValidator → 同一run结果保存 PostgreSQL / MinIO
  → 同一run报告、SQL query_id、查询理解/分析模型request_id返回query_hot_news
  → SQL规划标记python-confirmed-intent，query_model_request_id为null
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

优先按 [完整本地模拟](local-bge-simulation.md) 启动 BGE 演示，打开
`http://127.0.0.1:5178/chat`（API 为 `28060`；若修改 CLI 端口，以实际参数为准）：

1. 输入 `查询点击率最高的前5条视频新闻并分析原因`。
2. 查看实时工具状态；完成后展开 `query_hot_news` 的结果，核对 SQL、sql_query_id、run_id、
   understanding_model_request_id 和 analysis_model_request_ids。此链的 query_model_provider
   为 `python-confirmed-intent`，query_model_request_id 为空是预期结果。
3. 输入 `解释第一条新闻`：读取同一run，不另发新查询。
4. 刷新页面：恢复 PostgreSQL 保存的历史。

默认场景为 `news-ranking`，默认小时由所选数据 profile 与配置决定。单独 native-e2e 栈
使用 `classic-v1`，默认为北京时间 2026-10-03 00:00–01:00；上述 BGE 入口使用
`timeline-v4`，覆盖 2026-09-10 00:00 至 2026-10-10 00:00（结束不含），默认取末日
2026-10-09 00:00–01:00。明确日期和小时仍须落在 profile 覆盖内；这些均不是今日实时新闻。
本地规则 Port 支持有限自然问法／中文数量，`并分析原因` 是有限展示任务后缀，不是开放式推理；
未知条件仍拒绝，不能通过添加“分析”来忽略未知筛选条件。`查看最近热点` 专用于读取旧报告。
工具可返回最多5条展示摘要；完整run和SQL候选行数以热点报告为准。

## 真实模型配置模板

文件：[model-runtime.compatible.example.yml](../../deploy/model-runtime.compatible.example.yml) 与
[docker-compose.compatible-model.example.yml](../../deploy/docker-compose.compatible-model.example.yml)。
默认本地入口不加载该 overlay。示例目前填写了外部接口地址和 `glm-5.3-flash` 路由，
不是可直接用于企业数据的通用默认值。检查现有本地配置；仅在目标文件不存在时，才在代码编辑器中
复制前者为根目录 `deploy/model-runtime.compatible.yml`，避免覆盖已有配置。随后核对：

- `inference.url`：完整接口地址，例如企业批准的 `https://HOST/v1/chat/completions`。
  本项目不自动补 `/v1/chat/completions`；只有根URL/Base URL时应按服务契约补完整路径。
- `inference.model_routes`：将示例的 `glm-5.3-flash` 替换为已批准接口支持的模型名称，YAML锚点自动
  绑定会话压缩、会话规划、查询理解、Text2SQL、热点分析和QA场景。
- `inference.api_key_env`：示例 YAML 当前写的是 `COMPATIBLE_MODEL_API_KEY`，配套 overlay
  注入的却是 `NEWSAGENT_MODEL_API_KEY`。使用该 overlay 时，先将复制后的 YAML 改为
  `NEWSAGENT_MODEL_API_KEY`，或同步调整 overlay，确保容器注入与 YAML 引用完全一致。
  YAML 仅声明变量名，密钥由终端或密钥系统注入。
- `response_format`：默认 `none`，仅在接口确认支持时改 `json_object/json_schema`。
  响应必须有符合业务Schema的JSON content，且响应 `model` 必须与请求路由一致；模型别名
  不能在未批准时任意漂移。默认30秒单次调用预算。
- Overlay 中 manifest 的 `model_version` 当前同为 `glm-5.3-flash`，需与选定模型一致；它与
  `analysis_prompt_version=compatible-hot-news-v1` 一同形成不可变Bundle执行快照。

该文件的根目录路径不能沿用旧 `agent/deploy/` 路径的忽略结论；提交前用
`git check-ignore -- deploy/model-runtime.compatible.yml` 核对本地配置是否被忽略，勿在 YAML 写入密钥。

此模板是“真实推理 + 合成数仓 + 本地Embedding”的E2E组合，不是生产配置。只有提供
Chat Completions接口也可演示；Embedding仍为明确标识的32维规则Port。切换企业Embedding
需另外配置接口、维度、版本，并重建对应索引，不能沿用不兼容向量。
模板只包含本次查询所需场景，不宣称已把写作/Data Loop也切换为真实模型。

填好且确认允许发送测试数据后，从仓库根目录在 WSL/Linux 执行：

```bash
# 两套示例栈使用相同回环端口，先停旧栈；不删除数据卷。
docker compose --env-file /dev/null -p news-agent-native-e2e -f deploy/docker-compose.native-e2e.yml stop

# 新项目名使用单独数据卷/Bundle，旧历史仍留在原栈。
# 不要运行 docker compose config：它可能打印注入的密钥。
docker compose --env-file /dev/null -p news-agent-model-demo \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.compatible-model.example.yml up -d --build
```

使用本节完整兼容模型 overlay 时，API 和热点 Worker 必须一起切换，以保持会话/查询理解
与热点分析的配置及 Bundle 一致。`deploy_bge_demo.py --model compatible` 则使用另一份
`docker-compose.timeline-query-model.yml`，只替换查询理解模型；不要混淆这两个入口的覆盖范围。
如果复用已有数据库，活动Bundle与新manifest不符会被拒绝；应走既有人工审批/激活流程，
不可直接覆盖旧Bundle或清库。示例新项目名避免修改旧运行的模型身份，但不会删除旧历史。
密钥未注入时Compose/模型工厂失败关闭，不回退本地规则伪装成功。

这两份 Compose 不包含 `web` 服务；其 API 均使用回环端口 `28000`。需要另外启动前端开发服务器，
在 `agent/frontend/dev-server/local.yml` 中将代理目标设为相应 API；页面端口以该文件为准。
这与 BGE 入口自带的 `5178` 前端、`28060` API 是不同部署组合。
停止模型演示栈后可重新 `up -d` 原栈恢复旧数据。前端刷新后通过
`/api/v1/conversations/runtime` 显示实际 provider，不根据前端 dev 标志猜测是否用了真实模型。
“兼容接口模式”仅说明配置，不代表该模型的分析质量已经验收。

## 配置与测试入口

- `CONVERSATION_HOT_NEWS_QUERY_ENABLED` 默认false，仅本地Compose显式true。
- `CONVERSATION_HOT_NEWS_QUERY_SCENARIO` 默认`news-ranking`，必须是已批准ranking场景。
- `CONVERSATION_HOT_NEWS_QUERY_HOUR` 默认0，范围0..23，选择样本末日的默认整点小时。
  `SQL_ASSISTANT_DATASET_PROFILE` 提供完整样本覆盖；明确日期和小时可在覆盖内选取，
  由Python核对模型未更改条件。每次仍是单小时，跨小时UV不可相加。
- `CONVERSATION_TURN_TIMEOUT_SECONDS` 程序默认60秒，native-e2e/兼容 overlay 设置为120秒；
  默认工具预算3。服务端 SSE 最多等待轮次预算加10秒，前端请求默认150秒。
  本地 SQL 场景为15秒模型预算、10秒只读SQL上限；聊天链的已确认 intent 分支不再次模型规划。
  聊天等待受总预算限制，Workflow可能继续运行。
- 本地会话Prompt绑定`native-conversation-v5`，真实模板为`compatible-conversation-v5`；
  每轮保存路由、provider、Prompt版本和哈希。不是修改既有生产Prompt。

在 `agent/` 执行：

```bash
python -m pytest -o addopts='' tests/test_conversation_hot_news_query.py tests/test_sql_assistant_confirmed_intent.py -q
# 仅对已启动的隔离 native-e2e API；当前测试仅接受 28000 或容器内 http://api:8000。
CONVERSATION_E2E_BASE_URL=http://127.0.0.1:28000 \
  python -m pytest -o addopts='' tests/test_conversation_hot_news_e2e.py -q
```

以上需要已安装 dev 依赖的 Python 3.11+ 环境；E2E 会创建会话与热点运行，需要完整本地中间件。
当前 E2E 地址白名单不接受 BGE 端口 `28060`，不能仅改环境变量就套用该测试。
前端在 `agent/frontend/` 运行 `npm test`、`npm run lint`、`npm run build`。测试覆盖兼容HTTP规划/结果复入、鉴权、
身份/SQL注入拒绝、幂等、查询不自动重试、取消后保留任务引用、模式展示，以及真实本地
PostgreSQL/Temporal的同run查询/分析绑定。这里列的是测试入口和覆盖目标，不是本次运行结果。

TODO：真实服务合同与泛化质量验收；企业只读数仓Adapter、完整水位和权限；
待完成Workflow恢复工具；真实增量token Port；Gateway/长稳/并发压测。
