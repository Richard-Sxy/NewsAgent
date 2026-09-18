# 热点 Agent 本地 SQL 数仓模拟

本文说明如何用本地 PostgreSQL 模拟热点数仓，并把聚合指标接入现有热点
Text2SQL 取数源和热点分析流程。该演示只使用仓库中的模拟场景数据，不连接企业行为
明细库，也不修改或激活正式 Production Bundle。

## 运行方式

在 `writing-agent-service/` 目录、并处于项目配置完整的 Python 环境中运行：

```bash
# 建表并幂等写入 5 条模拟聚合数据；不调用 Agent
python -m examples.hot_news_sql_warehouse_demo --seed-only

# 没有 Text2SQL App 时，走确定性 SQL 模板、SQL 护栏和 Postgres 只读执行，
# 然后调用热点分析 App 并保存运行结果
python -m examples.hot_news_sql_warehouse_demo --template-only

# 完整模式：调用 FastGPT Text2SQL App 生成候选 SQL，再经过护栏执行
python -m examples.hot_news_sql_warehouse_demo
```

也可以在本地 Compose 服务环境中运行。数据库等依赖需已启动；若项目使用本地额外
Compose override，应在命令中一并传入该文件：

```bash
docker compose --env-file .env -f deploy/docker-compose.yml \
  run --build --rm --no-deps api \
  python -m examples.hot_news_sql_warehouse_demo --template-only
```

## 模拟表与数据边界

脚本创建 `dw.news_behavior_aggregate`，主键为
`(tenant_id, event_time, news_id, content_type)`。表中保存新闻标题、类型、频道和
窗口级指标：

| 列 | 含义 |
| --- | --- |
| `tenant_id` | 模拟租户隔离键 |
| `event_time` | 聚合窗口起点，查询按左闭右开窗口过滤 |
| `news_id`、`news_title` | 新闻 ID 与模拟标题 |
| `content_type`、`channel` | 内容类型与模拟频道 |
| `impressions`、`clicks` | 曝光量、点击量 |
| `unique_users` | 模拟独立用户估值，不是从用户明细计算的真实值 |
| `total_duration_seconds` | 总消费时长 |
| `effective_consumptions`、`interactions` | 有效阅读/播放量、互动量 |

数据来自 `examples/data/hot_news_scenario.json`，目前写入 5 条当前窗口样例。
`unique_users` 是为演示结构而合成的估值；总消费时长由样例中的单次时长与有效消费量
计算。表中没有 `user_id`、`event_id` 等原始行为明细。重复运行使用主键 UPSERT 更新这
组模拟行，不删除其他数据。脚本还会限制目标数据库只能是 localhost 或本地 Docker
Postgres 主机。

## 端到端调用链

```text
CLI
→ 读取热点场景 JSON
→ build_demo_rows / seed_demo_warehouse
→ dw.news_behavior_aggregate
→ HotNewsMetricQuery
→ Text2SqlNewsMetricSource
   ├─ 默认：Text2SqlAgentRunner → FastGPT Text2SQL App → Text2SqlPlan
   └─ --template-only：参数化 HotNewsMetricSqlTemplate
→ SqlGuard 校验单条只读 SELECT、表列白名单和租户/窗口参数
→ PostgresReadOnlySqlWarehouseClient（只读事务、statement_timeout、行数上限）
→ NewsMetricSnapshot
→ HotNewsOrchestrationService（确定性基线、热度计算与排行）
→ 热点分析 FastGPT App（结构化报告与业务校验）
→ PostgresHotNewsRunStore
→ GET /api/v1/hot-news/runs
```

历史基线与新闻内容仍由现有本地场景 Fixture 提供；关联新闻检索在此演示中返回空集合，
因此该模拟覆盖“数仓指标查询 → 热点分析 → 结果持久化”，不代表已接入企业内容库或真实
知识库检索。LLM 生成的 SQL 始终是不可信候选，只能通过 `SqlGuard` 后在只读事务中执行；
热度分数仍由确定性 Python 逻辑计算。

## 配置

| 配置 | 用途 |
| --- | --- |
| `DATABASE_URL` | 本地 PostgreSQL；演示脚本会拒绝非本地数据库主机 |
| `FASTGPT_HOT_NEWS_APP_ID` | 完整运行时调用热点分析 App；`--seed-only` 不需要 |
| `FASTGPT_TEXT2SQL_APP_ID` | 默认模式调用 SQL 生成 App；`--template-only` 不需要 |
| `FASTGPT_BASE_URL`、`FASTGPT_API_KEY` | FastGPT 地址与凭据 |
| `TEXT2SQL_MAX_ROWS` | 查询结果最大行数，默认 1000 |
| `TEXT2SQL_TIMEOUT_MS` | SQL 查询超时，默认 30000 毫秒 |

`deploy/docker-compose.yml` 通过服务公共环境配置传递
`FASTGPT_TEXT2SQL_APP_ID`。启用真实生成模式前，应在本地 Secret/环境配置中设置该 App
ID；不能把凭据写进仓库。若变量未配置，默认完整模式会在建表前 fail-closed，不会静默
退化成模板模式。

## 结果与验证

完成一次完整模板演示后，运行结果会以独立的本地模拟版本
`local-sql-text2sql-demo-v1` 写入热点运行存储，可由热点运行列表 API 查询并在运营界面
查看。重复执行具有幂等键，不会反复创建相同窗口的运行记录。

测试入口：

```bash
python -m pytest tests/test_hot_news_sql_warehouse_demo.py -q
```

测试覆盖场景数据转换、聚合表 Schema、模板路径，以及模拟 Text2SQL 候选 SQL 经共享
`SqlGuard` 校验并映射为指标快照。真实 Postgres/FastGPT 的联调需要本地 Compose 数据库和
相应 App 配置。
