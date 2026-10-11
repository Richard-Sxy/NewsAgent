# 自然时间、已配置模型理解与 Python 查询校验

本次实现让查询理解使用项目已经配置好的模型接口。用户可以输入
“10月9号早上八点到九点热度最高的前五条新闻，帮我总结一下”，不必手写 ISO 日期。
模型只返回结构化查询意图，Python 独立核对原问中的日期、小时、指标、数量、筛选与权限。
确定性 SQL 编译与执行护栏继续生效。下面是本地演示接入，生产发布仍需既有审批流程。

## 完整调用链与代码位置

以下路径均相对于 `/Users/shi/Project/myProject/NewsAgent/agent`。

1. `frontend/src/views/ChatView.vue` 的 `/chat` →
   `app/api/conversations.py` 的 `/api/v1/conversations/{id}/messages/stream` →
   `app/conversation/service.py` 的 `ConversationAgentService` →
   权限检查后的 `query_hot_news`。工具必须保留完整用户原问，不能先删条件再理解。
2. `examples/conversation_hot_news.py` 的 `LocalConversationHotNewsQuery._understand`
   固定一次带时区的服务器时钟，再调用 `app/sql_assistant/input_boundary.py:screen_question`；
   通过后，将原问、服务端 `QueryPolicy` 与 `temporal_context` 送入独立理解客户端。
3. `examples/local_simulation.py` 在启动时调用
   `app/model_runtime/query_understanding.py:load_query_understanding_endpoint_config`，
   只提取并严格校验接口地址、凭证环境变量名称与唯一查询模型绑定；连接文件中其他
   Prompt/Embedding 不参与该客户端。重复查询绑定或未批准模型路由仍阻断，配置错误
   不回显原始值。`build_query_understanding_client` 复用已有
   `NativeStructuredAgentClient`、`StructuredInferenceService`、模型 Port 和 HTTP 客户端。
   连接地址、模型路由与环境变量名称来自已有配置；理解 Prompt 使用应用批准的 v4 绑定。
   这个客户端只开放 `query_understanding` 场景，不接管其他 Agent、Embedding 或热度计算。
4. `deploy/model-runtime.local.yml` / `model-runtime.compatible.example.yml` 注册 v4 Prompt，
   输出 `QueryUnderstanding`，包含时间表达、日期、整点小时、指标、数量与总结风格。
   Prompt 将原问视为不可信输入，要求保留全部条件，禁止生成 SQL、身份或业务答案。
5. `app/conversation/query_time.py:extract_query_time` 从原问独立提取可证明的时间要求；
   `app/conversation/query_understanding.py:resolve_query_understanding` 对照模型输出、
   白名单、数据覆盖和权限。只消费已识别时间片段，不全局删除数字或未知筛选。
   Python 可检测的已支持条件若被模型遗漏/改写，或模型输出非法 Schema、超时或调用失败，
   都停止执行；未知业务词还需要模型澄清与质量评测。不静默回退规则模型。
6. 批准的 `resolution.intent` → `LocalConversationHotNewsQuery.execute` →
   `execute_local_hot_news_query` → `SqlAssistantService.preview(expected_intent=...)` →
   `compile_query` → SQL AST Guard / 绑定参数 / 服务端租户限制 → SQL 预览快照 →
   同一窗口绑定的 Temporal 热点 Workflow。这里不再调用第二个模型生成 SQL，
   查询计划的 provider 为 `python-confirmed-intent`。
7. Worker 使用 PostgreSQL 只读候选、同小时基线与指标 → Python 权威热度与排名 →
   既有分析模型 Port / Validator → PostgreSQL、Redis、MinIO 保存报告 →
   `ConversationTools` 根据同一个工具结果渲染榜单或总结 → 会话保存、SSE 与前端。
   查询轨迹保留理解模型请求编号、Prompt 版本、配置模型路由、SQL 查询编号和运行编号。
   当前没有单独保存 HTTP 响应中供应商返回的实际模型版本字段。

外部依赖沿用既有 HTTP 模型接口、PostgreSQL、Temporal、Redis、MinIO；时间解析只用
Python 标准库。没有增加外部 Agent 框架或数据库迁移。当前其他本地演示阶段继续使用
隔离规则 Port；页面和答复明确展示“查询理解使用已配置大模型”。

## 支持的表达与仍需澄清的边界

| 用户表达 | 解析规则 |
| --- | --- |
| `2026年10月9日8点到9点`、`2026-10-09 08:00至09:00` | 明确日期与整点小时 |
| `10月9号早上八点到九点`、`十月九日早八到九` | 使用服务器当前年份，记录补全年份说明 |
| `9号下午两点到三点` | 使用服务器当前年/月，记录补全年月说明 |
| `昨天8到9点`、`昨晚八点到九点`、`前天上午8点到9点` | 根据同一个服务器时钟与 Asia/Shanghai 时区计算 |
| `查看当前样本的热点新闻`、`最新热点新闻` | 分别使用批准默认样本小时、数据水位前的最后完整小时 |
| 只给日期、`这周`、歧义的 `8-9`、多个小时窗口 | 请求澄清，不猜测或删去窗口 |
| 非整点、跨多小时、未支持来源/阈值/过滤、覆盖之外日期 | 说明当前能力或数据范围限制，不执行查询 |
| SQL 语句、越权身份、提示词注入 | 输入边界或权限防护阻止执行 |

省略年份按服务器当前年处理，不能根据样本范围偷偷改成别的年份。服务器时钟只读取
一次，防止“昨天”在午夜前后被模型和 Python 理解为不同日期。
如果模型把“前天”写成绝对日期，Python 只在该日期与原问相对日期按同一时钟计算出的
日期完全一致时归一回原时间口径；小时、筛选与覆盖仍逐项核验。错误日期、样本替换或
另一种相对日期不会因这条等价规则获准。

当前 timeline-v4 样本覆盖北京时间 **2026-09-10 00:00 至 2026-10-10 00:00，结束不含**。
每次仍限一个完整小时。2026-10-10 上午不在样本内；接入模型不会让不存在的数据可查。
未知口语不会被一律称为注入：需要澄清、能力不支持与安全拒绝分别返回。

## 配置与启动

新增环境变量 `CONVERSATION_QUERY_UNDERSTANDING_CONFIG_PATH` 仅控制理解客户端。
继续使用 `MODEL_RUNTIME_CONFIG_PATH` 中批准的 Prompt 绑定，避免直接继承连接文件中的
旧 Prompt。接口配置内的 `api_key_env` 指向环境变量 `NEWSAGENT_MODEL_API_KEY`，
密钥只经原有环境注入，不写入 Prompt、代码、文档、镜像或日志。

本地第三个 Compose overlay 为 `deploy/docker-compose.timeline-query-model.yml`，
只给 API 挂载已有连接配置与注入密钥，Worker 保留本地分析配置。使用已经更新的
`news-agent/writing-agent-service:timeline-demo` 镜像，在原来的环境注入终端执行：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent
docker compose --env-file /dev/null \
  -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.timeline-demo.yml \
  -f deploy/docker-compose.timeline-query-model.yml \
  up -d --no-deps --no-build api hot-news-worker
```

命令要求该终端已经注入 `NEWSAGENT_MODEL_API_KEY`。`--env-file /dev/null` 防止自动读取
`.env`；不要输出展开后的 Compose 配置。已有连接文件
`deploy/model-runtime.compatible.yml` 挂载为只读，不需要改写其中的密钥或旧场景版本。
API 仍为 `http://127.0.0.1:28050`，当前前端为 `http://127.0.0.1:5175/chat`。
若前端尚未运行，在另一个终端执行（沿用本地网关 YAML 配置，不改动5174实例）：

```bash
cd /Users/shi/Project/myProject/NewsAgent/agent/frontend
VITE_DEV_PROXY_TARGET=http://127.0.0.1:28050 VITE_DEV_PORT=5175 \
  npm run dev -- --host 127.0.0.1 --port 5175 --strictPort
```

启动后通过 `/ready` 检查服务，通过 `/api/v1/conversations/runtime` 的
`query_scope.understanding_model` 确认理解 provider、model_route 和 prompt_version。

## 测试入口与未完成事项

新增 `tests/test_query_time.py` 与 `tests/test_query_understanding_runtime.py`；扩展
`tests/test_query_understanding_integration.py`、`tests/test_query_understanding_prompt_v2.py`、
`tests/test_hot_news_query_windows.py`。
覆盖口语日期/中文小时/相对时间、单时钟、模型日期/数量/SQL 篡改、环境凭证缺失、
模型故障零执行、原问筛选保留与多窗口不可丢弃。继续运行现有 SQL 编译/Guard、
会话和热点查询测试。

```bash
python -m pytest -q -m 'not e2e and not integration and not live'
cd frontend
npm run build
npm exec eslint -- src/api/conversations.ts src/views/ChatView.vue
```

最终离线全量回归：**2638 passed / 5 skipped / 66 deselected**，3 条既有依赖弃用警告。
基础设施、真实接口与端到端标记的排除项不计为通过。待部署镜像内独立执行新增/扩展的
6 个理解与窗口模块 **237 passed**；前端类型检查、构建和
两个修改文件的 ESLint 通过。

2026-10-11 已更新 timeline API 与热点 Worker；两者 16 个指定源码/测试文件 SHA-256
均与已测试快照一致，镜像为
`sha256:e58c617b456682603be66ea1f9073c112cdc5482277e4830379d9de74c6e84ac`。
更新基于原运行镜像，仅叠加本次查询理解相关文件。旧镜像保留为
`news-agent/writing-agent-service:timeline-before-natural-time-20261010`；数据库和其余服务沿用。
凭证仅在部署进程内存中从同项目已有模型实例的指定环境变量复用到 API，未保存或输出。
首次启动因共享连接文件存在无关的重复 Prompt 登记而被完整配置验证阻断，已通过上述
专用加载器隔离：只校验该客户端需要的连接和查询绑定，私有文件没有被修改；新增20项
文件投影测试覆盖此问题、非法路由、重复查询绑定和安全错误信息。
实际模型曾正确把“前天”转换为绝对日期，但仅比较枚举标签的旧校验误拒绝了该请求；
新增17项回归证明同钟、同日的转换可归一，错误日期、小时/数量漂移和覆盖外日期仍阻断。

## 真实接口聊天验收（2026-10-11）

通过5175的聊天 SSE 代理，使用已配置 `glm-5.3-flash` 与
`native-query-understanding-v4`；每轮有新的模型请求编号，其他本地演示阶段仍为规则 Port。

| 原问 | 实际结果 |
| --- | --- |
| `10月9号早上八点到九点热度最高的前五条新闻，帮我总结一下。` | ready，2026-10-09 08:00–09:00，5条及总结 |
| `前天晚上八点到九点热度最高的前五条新闻，帮我总结一下。` | ready，2026-10-09 20:00–21:00，5条及总结 |
| `查询10月10号早上八点到九点的热点新闻` | invalid_proposal，模型输出结构校验失败，SQL/Workflow零执行 |
| `查询昨天早上八点到九点点击量超过100的新闻` | unsupported_condition，SQL/Workflow零执行 |
| `查询昨天8点到9点和9点到10点的热点新闻` | clarify / ambiguous_requirements，SQL/Workflow零执行 |

两条成功查询均核对了工具与保存报告的相同时区窗口、SQL查询引用、5条榜单和5份分析，
确认SQL provider为 `python-confirmed-intent`。首条请求复用同窗口、同意图的已有合法
幂等报告；理解模型仍实际调用，不是拿其他日期的旧榜单代替请求。

首条会话 `16f36433-65a3-433a-a130-c4ab9ec89377`，模型请求
`2026101113242310ba6bcc03cf4e3b`，运行 `69bc9b8a-be03-429a-9a6c-2e33ab631c7d`；
相对日期会话 `da0ff79a-d411-42e7-9551-b2533feb734e`，模型请求
`20261011132428c0bea0bda2124223`，运行 `ff8f6c5a-4bfc-4378-933f-1ffb8254dbff`，
SQL查询 `3a0ae1dc-6b53-4557-a497-9440d262e0cd`。
完整公开断言证据在
[`output/query-understanding-natural-time-verification-2026-10-11.json`](../../output/query-understanding-natural-time-verification-2026-10-11.json)。

这5条真实请求只证明本次接入与已覆盖样例，不代表广泛模型质量验收。模型仍可能输出
非法结构；上述覆盖外样例实际被Schema校验先挡住，因此不把它记成时间覆盖门禁的
真实模型通过案例。时间覆盖门禁本身由离线测试验证；实时产品还需改进模型失败的
澄清体验和结构化输出稳定性。API `/ready` 的 PostgreSQL、Redis、Temporal 均为ok。

TODO：生产企业模型的大规模开放问法质量评测、真实实时水位、跨小时去重、全天查询、
对未知业务词和复杂筛选的语义层扩展、跨轮补全与生产发布审批。当前实现减少格式负担，
不能承诺消除任意模型幻觉；本地规则回归也不能代替企业模型质量验收。
