# query_hot_news 调用失败定位（2026-10-10）

普通样本查询在修复前能够完成 SQL 和热点分析；失败发生在不同环节，不能都归为
“query_hot_news 无法调用”。本次排查没有读取 `.env`，只使用本地公开合成身份。

| 环节 | 已确认的原因 | 处理 |
| --- | --- | --- |
| 小时条件理解 | `2026-10-09 19:00至20:00` 被当成不支持的数字条件；会话 policy 仍只批准默认 00–01 小时，未使用 30 天样本覆盖 | 增加成对小时字段，Python 核对原问题并在完整覆盖内选择单小时，实际窗口传给共享命令 |
| 热点工作流 | 旧 model-demo 的 `hot-news-724e0826366e443d9a775473387f56efa69e6c6b2674726a688bb2cd5df294a7` 已 FAILED，2026-10-09 20:13:31–20:14:12（北京时间）；最内层类别为未知证据引用，经 `AgentOutputValidationError` 拒绝 | 保留证据白名单校验；模型输出质量另行验收 |
| 错误展示 | 绑定 SQL/Workflow 后所有异常都显示 `query_in_progress`，把已经失败的运行说成仍处理中 | 502/503/504 区分工作流失败、服务不可用、等待超时；未知故障不声称仍运行，保留已绑定引用，不重新提交 |
| 最新工作区的消息入口 | `begin_turn` 已查询 `conversation_inputs`，但0021及以前的迁移未创建表；首次实际评测30个场景均在 `/messages` 返回503 | 新增0022迁移、复合归属约束和输入表；修正模型外键 `CASADE` 拼写，旧会话保留 |
| 数量展示 | 返回中缺少上榜总计数，答复显示“0篇”但实际已有5篇 | 总计数取自保存的运行，不受5条展示截断或单条追问影响 |
| 本地规划器的发布时间问法 | 对话评测发现“查询某日发布的新闻”误命中发布操作拦截，未给出查询口径说明 | 仅将“发布的/发布时间/发布日期”识别为查询语义，原问题完整交给查询校验拒绝；同一句中另有发布或删除动作仍拒绝 |

“今日”不在样本覆盖内、企业原榜未接通或跨小时UV请求被拒绝，是当前数据和聚合合同的
正确边界，不应删掉用户条件或换成其他日期。timeline-v4覆盖北京时间
`[2026-09-10 00:00, 2026-10-10 00:00)`；每次热点分析仍只取一个完整小时。

## 调用链、配置与验证

`ChatView / conversationsApi` → `/api/v1/conversations/{id}/messages[/stream]` →
`PostgresConversationRepository.begin_turn` 保存轮次/检查输入请求幂等 →
`ConversationAgentService` 规划 → `ConversationTools.query_hot_news` →
`LocalConversationHotNewsQuery._understand` → `QueryUnderstanding` →
`resolve_query_understanding` 校验原日期/小时/筛选和服务器 `QueryPolicy` →
`execute_local_hot_news_query` → Text2SQL受限意图/参数化SQL/SQL Guard →
PostgreSQL只读聚合 → Temporal热点Workflow → Python指标/基线/热度排序 →
模型分析及证据校验 → PostgreSQL/Artifact保存同run报告 → 工具状态和权威数字渲染 →
会话持久化 → 页面答复。外部依赖沿用模型Port、PostgreSQL、Temporal、Redis和MinIO。

`SQL_ASSISTANT_DATASET_PROFILE=timeline-v4` 与对应场景YAML决定冻结数据和完整覆盖；
`CONVERSATION_HOT_NEWS_QUERY_ENABLED`及ADMIN权限决定能否新取数，
`CONVERSATION_HOT_NEWS_QUERY_SCENARIO`决定候选指标/筛选/上限，
`CONVERSATION_HOT_NEWS_QUERY_HOUR`选择未指定日期时的默认样本小时。总轮次预算与
场景模型超时沿用现有配置。本次更新隔离演示API28050和热点Worker，前端5175代理该API。
0022只补存储契约，不自动完成补充输入的执行循环。

测试入口：`tests/test_hot_news_query_windows.py`、`tests/test_hot_news_query_diagnostics.py`、
`tests/test_conversation_inputs_migration.py`、`tests/test_hot_news_dialogue_eval.py`及现有
查询语义/会话回归；前端 `npm test`、`npm run typecheck`。
实际对话由 `python -m evaluation.run_hot_news_dialogue --run` 执行；冻结集合及命令见
[对话评测说明](../evaluation/datasets/hot_news_dialogue_v1/README.md)，实际问答和判分保存于
仓库 `output/evaluation/hot-news-dialogue-v1/`。首次503报告单独保留，修复后使用新会话、
新请求进行评测，失败提交没有自动重试。

TODO：跨轮日期/小时/筛选补全，企业模型的开放式语言理解和证据引用质量验收，真实实时
水位及跨小时去重合同。当前对话评测使用确定性本地Port与合成数仓，不代表企业模型质量。
