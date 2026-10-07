# 自然问法、查询语义与时间范围

聊天允许使用“看看热点新闻”“帮我看看热门新闻”等有限自然问法，也支持中文数量。
“你好，你能告诉我今日热点新闻有哪些吗？”会保留“今日”条件并核对数据覆盖，当前只有
2026-10-03固定小时样本时会明确说明无法满足请求，不返回旧榜单充当今日新闻。
页面新增“看看热点新闻”“今日热点”示例，只填入草稿，用户发送后才执行。

## 请求理解和执行边界

查询模型输出`QueryUnderstanding`，状态为`ready / clarify / unsupported`，包含批准的
候选指标／过滤／数量、日期表达、行为或发布时间口径、榜单来源及未支持条件。模型不输出
实际时间窗口、SQL或身份。`QueryPolicy`由服务器场景配置和单小时适配器生成。

Python的`resolve_query_understanding`核对原问题中可检测的日期、来源、指标、数量、
过滤和排序；条件不一致时拒绝。日期按注入时钟和`Asia/Shanghai`解析，结合整小时完整
水位与批准窗口校验。“今日”指从今日零点至最近完整数据小时，不能缩成过期样本；
“最新”指数据源可用的已批准窗口，并明确不表示当前日期的实时新闻。

无日期时使用固定样本；无数量时默认5条，无候选指标时使用场景默认值。来源未指定时
明确使用项目计算榜。SQL候选指标与最终Python权威热度排序仍是不同口径，展示候选范围，
不宣称全站TopN或保留CTR排序。企业原榜、发布时间过滤、阈值、未知栏目和不支持的
统计口径会停止执行或澄清，不能靠删词让问题执行成功。

本地`local_query_understanding`是有限规则模型替身；真实模型不经过本地有限词表，
但仍经过同一已知条件校验。门禁不能证明任意自然语言条件都被理解，未知语义需模型
主动澄清并由真实质量评测检验。企业原榜、实时数仓和全天去重聚合没有因此接入。

## 完整调用链

```text
ChatView 示例／用户问题 → 草稿 → 用户发送 → conversationsApi.sendStream
→ conversations API：网关身份、READ入口、独立ADMIN新查询授权
→ ConversationAgentService：持久轮次、历史、ConversationPlan
→ query_hot_news：原问题一致性校验，不让首个模型删掉日期／来源
→ ConversationTools：严格参数、租户与授权
→ LocalConversationHotNewsQuery._understand
→ screen_question：原始输入检查
→ NativeStructuredAgentClient / query_understanding场景 / InferencePort
→ QueryUnderstanding → resolve_query_understanding / QueryPolicy
  ├─ clarify / unsupported：QueryResolutionError → 工具检查点 → Python范围说明 → SSE答复
  └─ ready：Python canonical_question + expected_intent
     → execute_local_hot_news_query（聊天和热点页原共享命令）
     → SqlAssistantService.preview：原Text2SQL场景、结构化意图
     → 与expected_intent逐项一致 → compile_query → SQL AST／绑定参数护栏
     → 查询快照和scope → 幂等SQL绑定 → 原Temporal热点Workflow
     → PostgreSQL只读候选／同小时指标／基线 → Python排行
     → 原热点模型分析／Validator → 同run报告与Artifact
     → 带query_resolution和模型调用ID的工具结果 → 检查点
     → 会话解释 + Python权威数据／范围渲染 → 保存终态 → 页面
```

外部依赖沿用模型Port、PostgreSQL、Temporal、Redis和MinIO；本地实际推理是确定性替身。
本次不调用企业或外部真实模型。新增兼容模型场景通过MockTransport验证JSON协议与调用链，
这不等于大模型泛化质量验收。

语义拒绝不启动SQL或Workflow，不算数据服务熔断失败，也不自动重试查询命令。理解模型的
request_id保存于工具轨迹及轮次；ready只表示条件已批准，Workflow等待或失败仍不能
声称结果完成。模型企图用未按日期过滤的list/read旧报告满足日期查询时，同样被拒绝。
已有“读取热点运行 UUID”、同run追问及双窗口分析保持原语义；UUID不作为日期解析。

## 配置、测试与运行

没有新增环境变量或数据库迁移。沿用`CONVERSATION_HOT_NEWS_QUERY_ENABLED`、
`CONVERSATION_HOT_NEWS_QUERY_SCENARIO`、`CONVERSATION_HOT_NEWS_QUERY_HOUR`、
会话总预算和场景YAML模型超时。自然查询默认5条，最大值仍由批准场景限制。

模型YAML保留旧Prompt版本，新增`query_understanding`场景，绑定
`native-query-understanding-v1 / compatible-query-understanding-v1`；会话新增并绑定
`native-conversation-v5 / compatible-conversation-v5`。现有热点分析Prompt和冻结数仓格式
不改变。部署新API前需同时包含这些场景；不自动改变生产Bundle或外部模型连接。

后端测试入口：`tests/test_query_understanding.py`、
`tests/test_query_understanding_integration.py`、`tests/test_conversation_query_resolution.py`，
以及原会话、SQL、分析服务和热点链路回归。前端入口为`frontend/tests/conversations.test.mjs`，
运行`npm test`、`npm run lint`、`npm run typecheck`、`npm run build`。

本地更新先构建缓存镜像，再只重建API；已有数据库继续使用0020，不清库或改样本。
命令见[本地项目运行](local-project-simulation.md)。当前入口为
[会话页面](http://127.0.0.1:5177/chat)，API为`http://127.0.0.1:28030`；
已部署`native-conversation-v5`，聊天默认批准窗口为
`2026-10-03T00:00:00+08:00`至`2026-10-03T01:00:00+08:00`。
日期未指定时使用这一小时，不能将它解释为今日数据。

新建会话按以下顺序演示；示例按钮只填草稿，每次都需要用户发送：

1. 发送“你好，你能告诉我今日热点新闻有哪些吗？”，查看当天不在数据覆盖内的说明。
2. 发送“你好，请帮我看看热门新闻”，核对上述固定样本窗口及返回的5条新闻。
3. 发送“解释第一条新闻”，确认读取上一步同一`run_id`，不重新取数。
4. 分别发送“官方热榜前五条”和“查询点击量超过1000的前五条新闻”，查看来源和条件限制。
5. 刷新同一会话，确认答复、查询决议、模型ID与工具轨迹仍可读取。

## 本地实际验收（2026-10-07）

真实HTTP结果保存在
[/private/tmp/newsagent-query-understanding-http-proof.json](/private/tmp/newsagent-query-understanding-http-proof.json)。
该文件是本次机器上的临时证据，不能作为另一环境已通过验收的依据。
可打开[已保存的验收会话](http://127.0.0.1:5177/chat?conversation=dcbf170e-603d-4d88-8d4b-70228e3e5751)
查看持久记录。HTTP证据采集时保存5轮，随后增加浏览器验收，目前共6轮。

| 原问题 | 查询决议 | 实际结果 |
| --- | --- | --- |
| 你好，你能告诉我今日热点新闻有哪些吗？ | `time_coverage_unavailable` | 明确缺少当天覆盖；未启动Workflow，SQL审计条数不变 |
| 你好，请帮我看看热门新闻 | `ready` | 返回5条；使用固定样本的同run SQL、排行和分析 |
| 解释第一条新闻 | 同run追问 | 沿用上一轮运行，不新建SQL或Workflow |
| 官方热榜前五条 | `ranking_source_unavailable` | 明确企业原榜未接入；未启动Workflow，SQL审计条数不变 |
| 查询点击量超过1000的前五条新闻 | `unsupported_condition` | 不支持阈值筛选；未启动Workflow，SQL审计条数不变 |

成功运行的`run_id`为`d1d52138-f314-4089-b5a4-1b020ee780a5`，
`query_id`为`0f38cd0b-0c64-474b-81b0-c406fb6b529f`。本次没有调用真实模型；
兼容模型YAML已绑定上述场景，MockTransport验证理解与SQL两阶段协议及条件漂移阻断，
仍须连接企业模型检验实际语言质量。

浏览器在同一独立验收会话中展开“其他示例问题”，点击“今日热点”仅填入草稿，
未自动发送；用户点击发送后，SSE处理完成，答复明确显示固定样本窗口、没有当天覆盖，
本轮未执行SQL或启动热点Workflow。验收会话保留供演示，用户原有会话未被修改。
页面证据见
[浏览器验收截图](/Users/shi/.codex/visualizations/2026/10/04/01a106d4-db70-77e0-9fca-ae5d3c55682c/newsagent-query-understanding.jpg)。

后端回归为`2097 passed / 5 skipped / 62 deselected`；最后一轮范围提示渲染调整后，
上述3个新增测试模块再次通过`118 passed`。前端全量`220 passed`，
`npm run lint`、`npm run typecheck`和`npm run build`通过。专项可在`agent`目录执行：

```bash
python -m pytest tests/test_query_understanding.py \
  tests/test_query_understanding_integration.py \
  tests/test_conversation_query_resolution.py
```

TODO：真实模型同义问法与漏条件评测；企业原榜／实时数据Adapter与完整水位；全天UV和
基线聚合合同；将相同语义契约推广到正式统一查询入口及生产权限联调。当前仍是固定样本
单小时本地演示，增加自然问法不代表新增实时新闻或任意SQL查询能力。
