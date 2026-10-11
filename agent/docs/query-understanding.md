# 自然问法、查询语义与时间范围

## 当前实现：自然时间 + 已配置模型 + Python 校验

新增 v4 查询理解 Prompt 与独立模型客户端，复用项目已有接口，支持省略年份、中文小时
和相对日期。原问仍经 Python 条件一致性、覆盖、权限与 SQL 护栏校验。完整调用链、
配置、启动、测试和 TODO 见 [自然时间接入说明](query-understanding-natural-time.md)。
下面保留此前日期正则修正和 v2 等历史记录；当前日期提取已统一到 `query_time.py`。

## 2026-10-10：日期紧接小时的解析修正

“查询2026年10月9日8点到9点热度最高的前5条新闻，帮我总结一下。”是合法的
单小时样本查询。此前 `_DATE` 末尾的 `日?(?!\d)` 会因为“日”后紧接“8”而回退，
只剥离“2026年10月9”，残留的“日”使本地有限词表返回 `clarify/local_syntax`。
这不是提示词注入，也不是该时段缺数据。现在使用 `(?:日|(?!\d))`，显式“日”作为
日期边界被完整消费；没有“日”时仍禁止截断连续数字。没有把“日”加入通用词表，
没有全局删除数字或放宽查询条件。

调用链：`/chat` → 聊天 API `/api/v1/conversations/{id}/messages[/stream]` →
`ConversationAgentService` / `query_hot_news` → 原问 `screen_question` → 模型 Port →
`local_query_understanding` 的 `_requirements` / `_local_text` → `QueryUnderstanding` →
`resolve_query_understanding` / 服务端 `QueryPolicy` → `expected_intent` 与规范问题 →
`SqlAssistantService.preview` / `compile_query` / SQL AST Guard → 同窗口 SQL 快照与
Temporal 热点 Workflow → PostgreSQL 只读取数与基线 → Python 热度 → 既有分析
模型与 Validator → PostgreSQL / Redis / MinIO 保存报告 → `render_tool_results` 的
`summary` → 会话保存 / SSE / 页面。模型、新闻正文和检索内容仍按原不可信输入规则
处理；本地模型 Port 仍只是确定性替身。

配置沿用 `timeline-v4`、`deploy/text2sql-scenes.timeline-v4.yml`、
`deploy/model-runtime.local.yml` 的 v2 理解场景和 `CONVERSATION_HOT_NEWS_QUERY_*`；
没有新增依赖、环境变量、迁移或生产 Prompt 变更。日期、小时、指标、5条和总结风格
全部保留；覆盖之外的10月10日上午仍停止查询。

`tests/test_query_presentation.py` 新增12项回归，包含原句、中英文日期格式、整点窗口
和非法日期、多日期、多小时、非整点、连续数字歧义、覆盖外日期。新镜像在无网络隔离
环境执行日期理解、会话语义和 SQL 编译/防护10个相关模块：`414 passed`。
本地更新仅基于原已运行 timeline 镜像加入 `query_understanding.py`、
`presentation.py` 及对应测试，再替换 API 和热点 Worker；保留原镜像回滚标签
`news-agent/writing-agent-service:timeline-before-date-fix-20261010`，数据库和其他实例保留。

实际验收通过正在运行的 `http://127.0.0.1:5175` 聊天代理发送上述完整原句，轮次与
`query_hot_news` 工具均为 `completed`，理解结果为 `ready/summary`，热度降序取5条。
工具结果与保存报告都绑定北京时间 `2026-10-09 08:00–09:00`，5条榜单、5篇分析，
答复含“热点简要总结”；SQL 使用 `python-confirmed-intent`，没有第二次模型规划。
验收会话 `792eed3b-8b07-4197-81f0-1f6525f25a22`，热点运行
`69bc9b8a-be03-429a-9a6c-2e33ab631c7d`，SQL 查询
`b0efd05c-1cdc-4f35-a890-c4b8000d380c`；报告的 SQL 查询引用与工具记录一致。
API `/ready` 的 PostgreSQL、Redis、Temporal 均为 `ok`。

TODO：企业模型的开放问法质量、真实实时水位、全天去重和跨轮补全仍待验收；本次修正
只解决明确日期紧接小时的本地解析问题，不代表企业模型质量验证。

## 2026-10-10：口语展示意图与稳定 SQL

本轮将普通表达和数据条件分开，保留原 SQL 权限边界。
`app/sql_assistant/presentation.py` 的 `extract_query_presentation` 仅识别固定的
“总结／概括／梳理／简要介绍”和“不要太长／不用展开”呈现短语；完整原问题先经过
`screen_question`，再提取呈现要求。`QueryUnderstanding` 与 `QueryResolution` 新增
`response_style=list|summary`，默认 `list`。总结要求不能被当作数据排除；真实日期、
范围、来源、阈值、正文过滤等条件不会因同句含总结而删除。未知本地问法、无效日期或
数量返回 `clarify`，与注入拒绝分开；未支持的数据条件仍返回 `unsupported`。

“今日的热点新闻帮我总结一下”保留 `today` 和 `summary`。没有指定小时且日期不在
coverage 内时返回 `unsupported`；日期有覆盖时返回 `clarify`，要求选择完整小时，
不会自动替换为默认样本或悄悄改为全天查询。`summary` 由 Python 根据原工具快照和
查询范围渲染，不能自行改变指标、权威热度或把样本称为实时数据。

已批准的 `expected_intent` 进入内部 `SqlAssistantService.preview` 后，再经过 Schema、
场景白名单及独立 canonical slot 复核，直接 `compile_query` → AST Guard → 保存快照，
不再调用第二个 SQL 规划模型。该预览标记 `model_provider=python-confirmed-intent`、
`model_request_id=None`；原理解模型的调用 ID 仍保留于查询轨迹。
直接 SQL 预览 API 未提供这个内部参数，仍使用既有模型规划；本地规划器也支持总结短语，
但该页面的日期必须与用户明确选择的窗口对应，不能靠删掉“今日”让查询成功。

```text
聊天 API／完整原问 → screen_question → v2 结构化模型理解
→ Python 原问、日期、数据覆盖与权限校验 → 共享查询命令
→ 固定 SQL／服务端租户绑定／AST Guard／预览快照
→ Temporal → PostgreSQL 只读候选、指标与基线 → Python 权威热度
→ 既有热点模型／Validator → 报告／PostgreSQL／Redis／MinIO
→ 工具快照的 Python summary 渲染 → 会话保存／SSE
```

沿用模型 Port、PostgreSQL、Temporal、Redis、MinIO 与现有配置，本轮没有新增环境变量、
数据库迁移或框架依赖。两套 YAML 保留 v1，并将 `query_understanding` 绑定到
`native-query-understanding-v2`／`compatible-query-understanding-v2`；其他场景版本不变。
新增测试入口为 `tests/test_query_presentation.py`、
`tests/test_sql_assistant_confirmed_intent.py`、`tests/test_query_understanding_prompt_v2.py`，
同时运行既有查询理解、会话、SQL 和热点链路回归。本轮不含环境密钥的只读源码快照，
使用已有本地测试镜像、禁用网络执行后端离线回归：`2448 passed / 5 skipped / 66 deselected`。
跳过或排除的基础设施／外部模型测试不计为通过，没有调用企业模型。

在安装开发依赖的 `agent` 环境中可复核：

```bash
python -m pytest -m 'not e2e and not integration and not live'
```

此前交付时完成源码、配置模板和文档，运行中的本地容器尚未重建；上节日期修正已在
5175／28050 的 timeline API／Worker 同步验证。运行实例不会自动加载源码修改。
生产模型配置仍需按既有
版本与人工审批流程发布；无需为本次功能增加数据库迁移。

设计映射参考三个原始来源：

- [dbt MetricFlow 语义查询与 SQL 编译](https://docs.getdbt.com/docs/build/about-metricflow)：
  借鉴批准指标／维度到 SQL 的编译分层，本项目仍使用自有 Python 实现，不引入 dbt。
- [OWASP SQL 防注入指南](https://cheatsheetseries.owasp.org/cheatsheets/SQL_Injection_Prevention_Cheat_Sheet.html)：
  保留值参数绑定、列和排序枚举、只读最小权限；扩展口语不会扩大 SQL 白名单。
- [AmbiSQL 原始论文 v2](https://arxiv.org/abs/2508.15276v2)：
  借鉴歧义检测与用户澄清，不能为执行成功而删掉未理解的要求。

TODO：企业模型真实口语质量、实时数据水位、全天去重与跨轮条件补全仍未验收。
本轮不保证消除任意幻觉，未发布生产；本地规则与协议测试不等于企业模型质量验收。

> 以下保留此前实现和验收的历史记录，包括旧双模型规划链及 v1 配置；
> 当前口语展示与 SQL 规划行为以上节为准，历史测试结果不代表本轮已通过。

## 2026-10-10：30 天样本的小时查询与失败诊断

`timeline-v4` 覆盖北京时间 `2026-09-10 00:00` 至 `2026-10-10 00:00`
（结束时间不包含）。会话适配器的 `QueryPolicy.coverage_window_start/end` 来自
`dataset_window(SQL_ASSISTANT_DATASET_PROFILE)`；`supported_window_start/end` 仍表示
默认样本小时。`CONVERSATION_HOT_NEWS_QUERY_HOUR` 只选择这个默认小时。
`query_scope` 向页面展示完整覆盖和默认样本，避免把一个小时误认为全部数据。

`QueryUnderstanding.hour_start/hour_end` 保存用户明确给出的整点小时。Python 先移除
日期再识别小时，核对模型未遗漏或改变日期、小时、指标与筛选；批准后将
`requested_window_start/end` 传入共享 `execute_local_hot_news_query`，沿用 SQL Guard、
Temporal 和同小时基线计算。每次仍只查询一个完整小时，不累加跨小时 UV。
例如“查询2026-10-09 19:00至20:00点击量前5条新闻”执行该小时；“最新热点”选择
数据水位前的最后完整小时。仅提供日期会要求补充小时；超出覆盖的今日请求会拒绝。
旧单窗口策略未提供 coverage 字段时继续受原批准窗口约束。

`ConversationAgentService._execute_query` 对 HTTP 502、503、504 分别保存
`query_workflow_failed`、`query_backend_unavailable`、`query_wait_timeout`，未知绑定后
故障保存 `query_result_unavailable`，不再统称 `query_in_progress`。已绑定 SQL/Workflow
引用保留在检查点，无自动重新提交。`ConversationTools` 用安全中文说明渲染状态，
不展示异常 detail/headers。运行总计数来自保存的报告，不受页面只展示 5 条影响。

上游 `/chat` → `conversationsApi` → `/conversations/{id}/messages[/stream]` →
`ConversationAgentService` → `query_hot_news` → 查询理解/校验 → 共享命令 →
SQL/Temporal/Python 排行与热点模型 → PostgreSQL 保存报告 → 工具投影/状态渲染 →
保存会话/页面。外部依赖和模型 Port 沿用下文；没有新增环境变量或 SQL 执行权限。
隔离演示 API 与 Worker 必须一起更新，数据库迁移使用当前工作区的既有 Alembic 链。

当前补充输入仓库在 `begin_turn` 检查 `conversation_inputs.request_id`，而之前迁移链
缺少该表，会使 `/messages` 在工具执行前返回 503。`20261010_0022` 先补齐轮次的
租户/用户/会话/轮次复合唯一约束，再创建补充输入表、复合外键、索引和状态约束；
保留所有旧轮次和内容，模型外键的 `CASADE` 拼写修正为 `CASCADE`。降级遇到已保存
补充输入时拒绝丢失历史。迁移测试入口为 `tests/test_conversation_inputs_migration.py`；
这只修复存储契约，补充输入的执行循环仍须完成，不能声称跨轮条件补全已支持。

专项测试入口为 `tests/test_hot_news_query_windows.py`、
`tests/test_hot_news_query_diagnostics.py` 及下文三个语义回归模块；前端执行
`npm run typecheck`。实际 HTTP 对话集合、运行命令、报告格式和判分规则见
[热点 Agent 对话评测 v1](../evaluation/datasets/hot_news_dialogue_v1/README.md)。
集合只评测隔离的本地确定性 Port。TODO：跨轮补全日期/小时和筛选，企业模型开放问法
与证据引用质量验收，真实实时水位及跨小时去重聚合；未连接的数据不替换成旧样本。

以下保留旧单窗口演示及其 2026-10-07 验收记录；当前 timeline 实例以本节为准。

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
水位与批准窗口校验。旧单窗口策略中的“今日”指从今日零点至最近完整数据小时；
30 天小时策略需补充具体小时，两者都不能缩成过期样本；
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
基线聚合合同；将相同语义契约推广到正式统一查询入口及生产权限联调。当前为合成样本
单小时查询，增加自然问法不代表新增实时新闻或任意SQL查询能力。
