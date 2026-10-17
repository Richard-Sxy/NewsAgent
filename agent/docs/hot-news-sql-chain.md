# 数仓 SQL 与热点新闻全链路

本说明核对日期为 2026-10-15。当前模拟库使用 **PostgreSQL 16 的 SQL 方言**。
SQL 负责查询、过滤和候选排序；Python 负责权威指标、基线与热度；模型负责查询理解及基于证据解释。
新闻内容来自真实公开缓存，行为指标仍是模拟值。

## 先分清两条已有链路

| 链路 | 已经可以做什么 | 当前边界 |
| --- | --- | --- |
| 现有热点 Agent，`corpus-v6` | 问句 → 受限 SQL → Temporal → Python 热度 → 原文与关联证据 → 分析 → 保存 → 聊天/页面 | 120 篇真实内容，旧小时统计；不含访问 PV/访问 UV |
| 新数仓 `news_sim_v8` | 661 篇真实内容、247 万行统计；本次新增 7 类固定 SQL 查询及可复现快照 | 尚未注册进上述 Agent profile、白名单与 Workflow |

两个库、两个版本不能只靠同名字段串起来。v8 的 18 字段兼容视图仅表示形状，语义和版本仍须适配。
本次没有改变热度规则、生产 Bundle、现有 Agent 接口或数据库版本。

## SQL 是哪种语言

这里使用 `SELECT / FROM / WHERE / JOIN / GROUP BY / ORDER BY / LIMIT` 等 PostgreSQL SQL，
有界查询采用带时区的 `timestamptz`。时间范围统一用北京时间和半开区间 `[开始,结束)`，
例如 `[2026-10-14 19:00+08,2026-10-14 20:00+08)`，避免两个小时重复计入边界事件。

不要把参数占位符误认为不同的数据库语言：

| 执行方式 | 示例占位符 | 由谁处理 |
| --- | --- | --- |
| 当前 Agent 的 SQLAlchemy `text()` | `:tenant_id`、`:window_start` | SQLAlchemy 绑定，再交给 PostgreSQL 驱动 |
| 本次 CLI 的 Psycopg | `%(tenant_id)s`、`%(hour)s` | Psycopg 将 SQL 与参数分别发送给 PostgreSQL |
| 数据库 `PREPARE` | `$1`、`$2` | PostgreSQL 服务端预备语句 |
| 手工 SQL 演示 | `'2026-10-14 19:00+08'::timestamptz` | 字面值，仅用于可读的固定示例 |

表名、字段名不能当普通值绑定。排序字段只从批准列表中选择；新闻 ID、租户、时间和数量均绑定。
本次 CLI 不接受 `--sql`，也不允许用户通过自然语言传入任意 SQL。
参见 [PostgreSQL 查询语法](https://www.postgresql.org/docs/16/queries.html) 与
[Psycopg 参数绑定](https://www.psycopg.org/psycopg3/docs/basic/params.html)。

## 一次查询究竟经过什么

```text
query_real_news_warehouse.py 参数
→ QueryRequest 校验租户、真实 news_id、整点、窗口覆盖、指标及行数
→ compile_query 选择固定 PostgreSQL 模板 + 绑定参数
→ 本地 PostgreSQL 连接
→ REPEATABLE READ READ ONLY + 超时
→ information_schema 列契约与数据窗口核验
→ 最新 snapshot_revision（先选最新，再检查质量）
→ complete/corrected + passed + watermark >= window_end
→ 同租户、同新闻、同窗口、同修订查询，过滤 eligible_news
→ 查询快照：数据版本、摘要、SQL、参数、门禁、模拟来源与结果
→ JSON / 本次 PDF 证据；后续再作为 Agent Adapter 的输入
```

`REPEATABLE READ` 让同一事务的水位检查与查询结果使用一致数据库快照，避免前一步检查 rev1、后一步
却读到 rev2。事务只读，且设置语句和锁等待超时。参见
[PostgreSQL 事务隔离](https://www.postgresql.org/docs/16/transaction-iso.html)。

缺失或不完整窗口必须明确报错，不能解释为“没有热点”。v8 默认：

- 19:00 最新 rev2 为 corrected / passed，可查。
- 18:00 的 rev1 完整，但最新 rev2 不完整，业务查询拒绝；不能回退 rev1。
- 20:00 是开放小时，业务查询拒绝；`windows` 诊断可查看。
- 相同 ID 在另一租户不等于同一数据；每个 JOIN 都包括 `tenant_id`。
- 当前状态受限或有 tombstone 的新闻不进入业务候选；模拟生命周期只在隔离租户演练。

## 可运行的 7 类 SQL

连接为本地示例库 `127.0.0.1:28438`；凭据在 CLI 中固定为本地演示值，不读取 `.env`。
默认整点从冻结 manifest 的闭合结束时间减一小时取得，并非今天或 `NOW()`。

```bash
# 点击候选前20篇；只输出候选，不冒充最终热度榜
python3.11 agent/tools/query_real_news_warehouse.py --query ranking --sort clicks --limit 20

# 点击率候选，曝光至少1000次，避免1次点击/1次曝光占据榜首
python3.11 agent/tools/query_real_news_warehouse.py --query ranking --sort ctr --min-impressions 1000

# 访问PV或访问UV候选
python3.11 agent/tools/query_real_news_warehouse.py --query ranking --sort detail_page_views
python3.11 agent/tools/query_real_news_warehouse.py --query ranking --sort detail_visitors

# 某篇真实新闻同小时详情、五分钟轨迹、入口构成和基线
python3.11 agent/tools/query_real_news_warehouse.py --query hourly
python3.11 agent/tools/query_real_news_warehouse.py --query five_minute
python3.11 agent/tools/query_real_news_warehouse.py --query channel
python3.11 agent/tools/query_real_news_warehouse.py --query baseline

# 每日存储的去重结果，日期含首尾；最大31天，末日partial如实展示
python3.11 agent/tools/query_real_news_warehouse.py --query daily --start-date 2026-09-30 --end-date 2026-10-14 --limit 31

# 查看18:00旧完整/新不完整两次修订，诊断允许pending
python3.11 agent/tools/query_real_news_warehouse.py --query windows --hour 2026-10-14T18:00:00+08:00

# 只看SQL及绑定参数，不连接数据库；可将实际查询保存到全新目录
python3.11 agent/tools/query_real_news_warehouse.py --query ranking --show-sql
python3.11 agent/tools/query_real_news_warehouse.py --query hourly --output output/warehouse/my-first-query
```

`ranking` 支持 clicks、ctr、detail_page_views、detail_visitors、interactions 五个排序字段。
CTR 门槛只适用于 CTR 排序；同分按 news_id 稳定排序，NULL 排最后。单小时不把 UV 再 SUM 一次。
`hourly / five_minute / channel / baseline / daily` 默认新闻为
[WorkBuddy开放平台上线，AI办公迎来硬件入口之争](https://news.qq.com/rain/a/20260903A05YQE00)，可用 `--news-id` 换成目录内其他真实 ID。

| 查询 | 从哪里取数 | 结果和注意点 |
| --- | --- | --- |
| ranking | 小时指标 + 最新窗口 + eligible_news | 受控排序、候选数量上限、CTR 样本阈值 |
| hourly | 相同三表 | 点击 UV 与访问 UV 分开，带水位、修订与质量 |
| five_minute | 五分钟 + 最新窗口 + eligible_news | 同一个修订最多12桶，首次发布桶显示部分覆盖 |
| channel | 渠道小时 + 入口维度 | 可加量与总小时对账，入口 UV 不相加 |
| daily | 已去重的新闻每日表 | 逐日 UV、partial、observed/excluded_hours；不累加成跨日 UV |
| baseline | 小时 + 可空基线 | 样本不足/零基线分开，点击增长分母不成立时为 NULL |
| windows | 原始窗口修订 | 最新标记、历史水位与 pending；仅诊断，不产生热点榜 |

下面是点击候选查询的可读形式。业务 CLI 还会先做版本、水位检查，然后以参数绑定方式执行：

```sql
SELECT m.news_id, n.title, m.clicks,
       m.unique_users AS click_uv,
       m.detail_page_views AS visit_pv,
       m.detail_visitors AS visit_uv,
       m.snapshot_revision
FROM news_sim_v8.news_metric_hourly m
JOIN news_sim_v8.latest_window_snapshot w
  ON w.tenant_id=m.tenant_id AND w.event_time=m.event_time
 AND w.snapshot_revision=m.snapshot_revision
JOIN news_sim_v8.eligible_news n
  ON n.tenant_id=m.tenant_id AND n.news_id=m.news_id
WHERE m.tenant_id='11111111-1111-4111-8111-111111111111'
  AND m.event_time='2026-10-14 19:00:00+08'::timestamptz
  AND w.completeness IN ('complete','corrected')
  AND w.quality_status='passed' AND w.watermark_at>=w.window_end
ORDER BY m.clicks DESC, n.news_id ASC
LIMIT 20;
```

这里可加的是曝光、点击、PV、时长与互动等计数。UV 则是人数：渠道、五分钟、小时、日期和不同新闻的
UV 都不能直接相加。当前 daily 是单新闻日 UV，不是全站 DAU。

## 点击最高，为什么不一定是最终热点

当前 Agent 的 SQL 里也有名为 `hot_score` 的候选分：

```text
候选SQL分 = (0.40×点击 + 0.35×有效消费 + 0.25×互动) / 曝光
```

它不是最终热度。现有 `HotScoreCalculator` 默认最终计算为：

```text
最终热度 = 0.30×clamp(点击/曝光)
         + 0.35×clamp(有效消费/点击)
         + 0.20×clamp(互动/有效消费)
         + 0.15×增长分

活动量 = 点击 + 有效消费 + 互动
g = (当前活动量 - 基线活动量) / 基线活动量
增长分 = g/(1+g)，仅 g>0 时计入
```

`clamp` 把比例限制在 `[0,1]`；分母不为正时取0。无基线、基线活动量不为正或下降时，增长分为0。
本次查询工具没有重新实现、修改或激活这套规则。查询里展示的点击增长率保留 NULL，是说明性指标；
权威热度中没有可用基线时增长分为0，两者口径不同。

现有 Agent 先取候选再由 `HotNewsRanker` 重排，得到的是**候选池内的权威排序**。若只取点击前20篇，
不能声称它们就是全仓热度最高的20篇。要取得全仓热点，需要批准的候选召回策略、候选预算和召回验收。

## 现有 Agent 的实际端到端链路

1. **入口与身份**：`app/api/conversations.py` → `ConversationAgentService` → `execute_hot_news_query()`。
   后端注入租户、用户、权限，问句不能更改身份。新查询需批准的 `hot-news:admin` 权限。
2. **理解问句**：`LocalConversationHotNewsQuery._understand()` → `QueryUnderstanding` → Python 核对日期、
   整点单小时、指标与来源 → `execute_local_hot_news_query()`，聊天与工作台共用业务入口。
3. **编译 SQL**：`SqlAssistantService.preview()` → `SqlAssistantIntent` → `planner.compile_query()` →
   `SqlAssistantGuard` 的 PostgreSQL AST 校验。模型输出受限意图，SQL由Python生成。
   聊天已确认意图时用 `expected_intent` 分支独立复核后直接编译，不再次调用 Text2SQL 模型；
   工作台无已确认意图时才调用规划模型。
4. **保存并绑定**：`sql_assistant_query_audit` 保存意图、SQL、参数、版本及指纹；
   `HotNewsSqlBindingStore.get_or_claim()` 将查询绑定到同一租户/窗口/Bundle 的幂等运行。
5. **工作流和真实执行**：`HotNewsMonitorWorkflow` → `run_hot_news_window` Activity →
   `SqlAssistantService.execute_for_hot_news()` → `LocalPostgresSqlWarehouseClient.execute()`。
   执行器再次检查 AST，在 READ ONLY 事务中读批准的 `dw.news_behavior_aggregate`。
6. **指标和排行**：`SqlAssistantHotNewsMetricSource.fetch_snapshots()` 用固定 `SUPPLEMENTAL_SQL`
   补齐同小时人数、时长和基线，交叉核对候选计数 → `NewsMetricSnapshot` → `HotNewsRanker.rank()`
   → `HotScoreCalculator.calculate()`，保存四个可解释分量。
7. **正文和证据**：`SqlHotNewsContentRepository.batch_get_by_news_ids()` 按同一真实 ID 查询冻结正文，
   核对标题、来源、栏目、类型和发布时间 → Python 知识台账 / BGE 关联召回 → `RelatedNewsReranker`。
   原文与检索文本都是不可信数据，不能借其指令扩大工具权限。
8. **模型分析与校验**：`HotNewsAnalysisInputBuilder` 将当前指标、四分量、原文片段和最多5份关联证据
   交给 `NativeHotNewsAnalysisRunner` / `StructuredInferenceService` → `HotNewsAnalysisReport`
   → `HotNewsAnalysisValidator`。实际载体名为 `HotNewsAnalysisInput.related_news`，没有叫 EvidencePacket 的类。
9. **保存同一快照**：`ActiveProductionBundleHotNewsService.run()` 追加 SQL trace →
   `PostgresHotNewsRunStore.save_completed()` → `public.analysis_runs.result_payload`。
   这条热点成功链目前没有每次报告双写 MinIO/S3 的实现；Redis 用于进度与缓存。
10. **返回用户**：`HotNewsQueryService.get_run_detail()` → 查询 API；聊天 `read_hot_news` 投影同一保存结果
    → 模型解释 + Python 权威数字展示 → 会话终态保存 → SSE 分段输出。不是原始模型 token 直接透传。

当前模型输入只含六项指标（曝光、点击、CTR、unique_users、有效消费、互动）、四个热度分量、
标题/摘要/正文片段及关联证据；没有 v8 的访问 PV、访问 UV、原始基线表或完整行为日志。
Validator 核对身份、指标 key 和证据引用等规则，不是完整事实查证，也不能证明任意正文数字正确。

## 要把 v8 接入 Agent，还需补什么

| 接口 | 必须完成的适配 |
| --- | --- |
| profile / schema / dataset | 新版本注册、661数量、七栏目、内容目录摘要、场景和绑定白名单 |
| MetricSource | 最新窗口质量门禁；缺基线省略 baseline key；保留真实 sample_count，不能写死1 |
| 指标语义 | v8.unique_users=点击UV，旧行为计算器覆盖全部行为用户；访问UV保持独立字段 |
| 时长与有效消费 | v8时长包含直接访问，旧READ/PLAY不同；有效消费为点击归因，不偷换分母 |
| 统计旁车 | PV/访问UV、版本、水位、修订、样本数存入同一 sql_tool_trace，页面/聊天读同一快照 |
| 正文与知识 | v8真实news_id/内容版本映射、内容资格、正文摘要校验、知识和BGE检索边界 |
| 生命周期 | 真实CMS通知、检索denylist、版本化物理清理、重试和删除复核；当前任务仍pending |

不能只把 `news_sim_v8` 重命名为 `dw` 后接入。当前旧 MetricSource 不接受 NULL 基线、写死 sample_count=1，
旧静态 `data_watermark` 只表示覆盖终点，尚不会消费 v8.window_snapshot。配置需要新版本和语义测试。
数据访问的 RLS/最小只读角色、全站跨新闻 UV、企业统计和模型验收、真实连续流处理仍为 TODO。

## 本次新增文件的完整调用链、配置和测试

上游为已经冻结的真实新闻 v8 数仓与 manifest；`queries.py` 的 `QueryRequest` 校验参数，
`compile_query()` 生成固定 SQL，`run_query()` 连接只读 PostgreSQL、核验契约/水位并输出有界聚合快照。
入口为 `query_real_news_warehouse.py`，下游为新目录的 `query.sql / gate.sql / parameters.json / snapshot.json`。
外部依赖仅 Psycopg 与 PostgreSQL；不调用模型、Embedding、Temporal 或企业服务。
配置为 query、tenant、hour、news_id、sort、limit、min_impressions、start_date、end_date、dataset、output。
`--show-sql` 不连接数据库。已有输出目录拒绝覆盖。

测试入口：`agent/tests/test_real_news_warehouse_queries.py`；真实库示例与拒绝结果保存在
`output/warehouse/real-news-query-20261015/`。PDF由单独渲染脚本读取这些结果与源代码中现有热度规则，
使用 ReportLab、中文字体和 Poppler；生成后检查一页、字体与排版。

本次验收：**37项测试通过（包含本地PostgreSQL集成）**；保存11组实际查询，覆盖全部7类模板、
点击/CTR/访问PV排序、NULL缺历史和日历史截断，并保存18:00、20:00两组明确质量拒绝。
WorkBuddy同一19:00修订的12个五分钟桶、3个入口，其曝光、点击、PV、时长、有效消费、互动均与
小时快照一致；访问UV没有相加。点击候选Top20的完整匹配数为661，只显示20条，明确omitted641。
完整日历史显示15天，`--limit 1` 明确省略14天。所有例子绑定同一查询目录摘要
`4f4ca30585af6c1f8de828b7ead3eda24dc8e61de12d68192ecb63eb4aa58271`。
核验摘要保存在上述目录的 `verification.json`。

```bash
# 仅对本地已启动v8库启用只读集成测试
NEWSAGENT_RUN_V8_QUERY_INTEGRATION=1 PYTHONPATH=agent python3.11 -m pytest \
  agent/tests/test_real_news_warehouse_queries.py -p no:cacheprovider -o addopts=''

# 单页图由真实查询证据和源码中默认热度权重生成，不导入完整Agent运行时
python3 agent/evaluation/render_hot_news_sql_chain_pdf.py \
  --evidence output/warehouse/real-news-query-20261015 \
  --output output/pdf/NewsAgent_SQL查询与热点全链路_2026-10-15.pdf
```

本次完善的是固定只读查询、实际链路说明和演示证据；未来 Agent Adapter 尚未装配。
