# query_hot_news 调用失败定位（2026-10-10）

本文保留 2026-10-10 的排查与评测记录，供回溯问题和生成历史报告；不是当前部署状态。
2026-10-26 仅核对源码、文档与已保存证据，没有重新发起查询或压测。当前启动入口见
[完整本地模拟](local-bge-simulation.md)，现行查询机制见
[聊天驱动数仓查询](conversation-warehouse-model.md)。

当时普通样本查询在修复前能够完成 SQL 和热点分析；失败发生在不同环节，不能都归为
“query_hot_news 无法调用”。历史排查只使用本地公开合成身份，未读取 `.env`。

| 环节 | 已确认的原因 | 处理 |
| --- | --- | --- |
| 小时条件理解 | `2026-10-09 19:00至20:00` 被当成不支持的数字条件；会话 policy 仍只批准默认 00–01 小时，未使用 30 天样本覆盖 | 增加成对小时字段，Python 核对原问题并在完整覆盖内选择单小时，实际窗口传给共享命令 |
| 热点工作流 | 旧 model-demo 的 `hot-news-724e0826366e443d9a775473387f56efa69e6c6b2674726a688bb2cd5df294a7` 已 FAILED，2026-10-09 20:13:31–20:14:12（北京时间）；最内层类别为未知证据引用，经 `AgentOutputValidationError` 拒绝 | 保留证据白名单校验；模型输出质量另行验收 |
| 错误展示 | 绑定 SQL/Workflow 后所有异常都显示 `query_in_progress`，把已经失败的运行说成仍处理中 | 502/503/504 区分工作流失败、服务不可用、等待超时；未知故障不声称仍运行，保留已绑定引用，不重新提交 |
| 当时工作区的消息入口 | `begin_turn` 已查询 `conversation_inputs`，但0021及以前的迁移未创建表；首次实际评测30个场景均在 `/messages` 返回503 | 新增0022迁移、复合归属约束和输入表；修正模型外键 `CASADE` 拼写，旧会话保留 |
| 数量展示 | 返回中缺少上榜总计数，答复显示“0篇”但实际已有5篇 | 总计数取自保存的运行，不受5条展示截断或单条追问影响 |
| 本地规划器的发布时间问法 | 对话评测发现“查询某日发布的新闻”误命中发布操作拦截，未给出查询口径说明 | 仅将“发布的/发布时间/发布日期”识别为查询语义，原问题完整交给查询校验拒绝；同一句中另有发布或删除动作仍拒绝 |

在该冻结样本中，“今日”不在覆盖内、企业原榜未接通或跨小时UV请求被拒绝，是其数据和聚合合同的
正确边界，不应删掉用户条件或换成其他日期。timeline-v4覆盖北京时间
`[2026-09-10 00:00, 2026-10-10 00:00)`；每次热点分析仍只取一个完整小时。

## 当前调用链与历史验证

`ChatView / conversationsApi` → `/api/v1/conversations/{id}/messages[/stream]` →
`PostgresConversationRepository.begin_turn` 保存轮次/检查输入请求幂等 →
`ConversationAgentService` 规划 → `ConversationTools.execute_hot_news_query` →
`LocalConversationHotNewsQuery._understand` → `QueryUnderstanding` →
本地 `resolve_query_understanding` 或兼容模型 `resolve_structured_query` 校验日期/小时/筛选和服务器 `QueryPolicy` →
`execute_local_hot_news_query` → `SqlAssistantService.preview(expected_intent=...)` 独立复核已确认意图 →
Python编译参数化SQL/SQL Guard → SQL快照绑定Temporal热点Workflow → Activity查询PostgreSQL只读聚合 → Python指标/基线/热度排序 →
模型分析及证据校验 → PostgreSQL/Artifact保存同run报告 → 工具状态和权威数字渲染 →
会话持久化 → 页面答复。外部依赖沿用模型Port、PostgreSQL、Temporal、Redis和MinIO。

`SQL_ASSISTANT_DATASET_PROFILE=timeline-v4` 与对应场景YAML决定冻结数据和完整覆盖；
`CONVERSATION_HOT_NEWS_QUERY_ENABLED`及ADMIN权限决定能否新取数，
`CONVERSATION_HOT_NEWS_QUERY_SCENARIO`决定候选指标/筛选/上限，
`CONVERSATION_HOT_NEWS_QUERY_HOUR`选择未指定日期时的默认样本小时。总轮次预算与
场景模型超时沿用现有配置。当前会话链的已确认意图分支不再次调用Text2SQL规划模型。
历史排查使用隔离演示API `28050` 和前端 `5175`；这不是现行BGE启动入口的端口。
`20261010_0022` 是当时新增的输入表迁移，当前部署应执行 `alembic upgrade head`。
当前 `ConversationAgentService` 已有轮内补充输入处理；不能把当时“仅补表”的状态当作当前能力。

测试入口：`tests/test_hot_news_query_windows.py`、`tests/test_hot_news_query_diagnostics.py`、
`tests/test_conversation_inputs_migration.py`、`tests/test_hot_news_dialogue_eval.py`及现有
查询语义/会话回归；前端 `npm test`、`npm run typecheck`。
历史实际对话由 `python -m evaluation.run_hot_news_dialogue --run` 执行；冻结集合及命令见
[对话评测说明](../evaluation/datasets/hot_news/hot_news_dialogue_v1/README.md)，实际问答和判分保存于
仓库 `output/evaluation/hot-news-dialogue-v1/`。当前保存的 `dialogue-report.json` 记录
30个场景、33轮、29个通过、1个预期待办、12次完成取数；正确澄清或拒绝也可计入合同通过，
所以29不代表29次成功取数。这是已保存的历史结果，不是本次重新执行。
首次503报告单独保留，修复后使用新会话、新请求评测，失败提交没有自动重试。

离线报告入口为 [render_hot_news_query_pdf.py](../evaluation/render_hot_news_query_pdf.py)：
`load_evidence` 读取上述 JSON、冻结评测集及 timeline manifest，校验数量和哈希，
并检查本文存在，再由 ReportLab 输出 `output/pdf/NewsAgent_热点查询修复与对话评测图解.pdf`。
它不会运行新查询或更新证据；替换评测集/报告时应按脚本契约另行核对，不能把新结果冒充旧验收。

TODO：冻结v1评测中的跨轮日期/小时补全仍标为 `expected_pending`，后续实现需重新评测并版本化更新；
企业模型的开放式语言理解和证据引用质量、真实实时水位及跨小时去重合同仍需单独验收。
本文的历史对话评测使用确定性本地Port与合成数仓，不代表企业模型质量。
