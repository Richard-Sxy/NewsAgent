# 实时数仓背景、现有模拟与企业接入

核对日期：2026-10-07。本文补充项目背景，不变更冻结的 v1/v2 数据合同，也不代表企业实时数仓已经接入。

2026-10-07新增[公开新闻标题演示](public-headlines-demo.md)：独立v3数据集使用1,200个近期公开原标题，两租户共2,400条新闻维度；运营指标继续模拟，并限制前端数值显示精度。下文v1/v2仍指原版本。

## 1. 这部分在 NewsAgent 中解决什么问题

新闻正文和关联召回回答“新闻讲了什么、有哪些相关报道”；行为聚合数仓回答“在什么时间窗口内，哪些新闻获得了多少曝光、点击、有效消费和互动”。热点分析需要把这两种证据关联到同一个新闻 ID，但正文相似度不能替代运营指标，LLM 也不能生成权威指标。

可以把项目背景写成下面这段：

> NewsAgent 面向腾讯新闻内部的热点分析与内容生产场景，设计上通过企业已有的行为聚合数仓取得新闻曝光、点击、去重消费人数、消费时长和互动等指标。项目负责受限查询、口径校验、确定性热度计算、证据关联与分析表达；上游埋点采集、流式聚合和数仓运维由企业数据平台承担。本地使用隔离 PostgreSQL 数仓模拟表复现聚合数据的消费链路，已验证实际 SQL 查询、约束校验、运行快照与分析输出。企业真实字段、实时水位、迟到修正、权限及服务 SDK 仍需对接验收。

这里的“实时”需要明确数据新鲜度和完整性。例如查询“10:00–11:00 的完整小时”与查询“截至 10:35 的进行中窗口”是两份不同的数据合同。当前热点模拟仅支持固定样本内的一个完整小时。

## 2. 当前确实存在什么

DDL 位于 [warehouse.py](../app/sql_assistant/warehouse.py)，由 `initialize_demo_warehouse` 在明确的 `e2e` 环境初始化；不是单独的迁移 SQL 文件。v1/v2 的三张业务表和聚合视图结构相同。

| 对象 | 粒度与用途 | 已有数据库约束 |
| --- | --- | --- |
| `dw.dim_news` | 一个租户内一篇新闻；提供标题、类型、栏目、来源和发布时间 | 主键 `(tenant_id, news_id)`；图文/视频和四个模拟栏目 CHECK |
| `dw.news_metric_hourly` | 一个租户、一篇新闻、一个小时桶；曝光、点击、UV、时长、有效消费、互动 | 复合主键；复合新闻外键；非负及计数关系 CHECK |
| `dw.news_metric_baseline_hourly` | 与指标相同的小时粒度；四项合成参考基线 | 复合主键；指向完整小时指标的复合外键；非负 CHECK |
| `dw.news_behavior_aggregate` | 内连接上述三表，提供 18 列 | 查询工具唯一批准的视图；各连接都带租户条件 |
| `dw.news_schema_contract` | Schema 版本和冻结文档 SHA-256 | 版本主键；初始化及查询链路核对合同 |

2026-10-06 对两个正在运行的本地合成 PostgreSQL 实例进行了只读查询，结果如下。数量是全库两租户之和，不是单次分析规模。

| 本地实例 | Schema | 新闻维表 | 小时指标 | 小时基线 | 聚合视图 |
| --- | --- | ---: | ---: | ---: | ---: |
| `news-agent-enterprise-analysis-demo-postgres-1` | `news-warehouse-v1` | 24 | 576 | 576 | 576 |
| `news-agent-enterprise-scale-demo-postgres-1` | `news-warehouse-v2` | 2,400 | 57,600 | 57,600 | 57,600 |

两个实例均覆盖北京时间 2026-10-03 的 24 个小时桶。v1 文档哈希为 `e4498800da447c72beb3be13ad375d9efa458605f8accf90af56b531c0aee3d0`；v2 为 `b46023255d18b583048cbe8f5c5ce2a2e4372d3d867da2a1b24b493551c18273`，与当前冻结文件一致。

v2 的 `enterprise-v2` 是合成数据 profile 名称，表示扩量演示。每租户可选 120、1,200、12,000 篇；默认 1,200。它增加了批量入库、查询索引、全量行指纹和数据身份绑定，未增加实时流入能力。已有十种运营情形涵盖突增、回落、漏斗退化、结构变化、低样本、零基线、榜单进出、窗口间隔和大数精度，见 [企业情形合成数据演示](enterprise-synthetic-data.md)。

## 3. “约束 SQL”分成三层

### 数据库的结构与数值约束

现有 `dw.news_metric_hourly` 的关键 DDL 如下；完整定义以代码为准：

```sql
impressions bigint NOT NULL CHECK (impressions >= 0),
clicks bigint NOT NULL CHECK (clicks >= 0 AND clicks <= impressions),
unique_users bigint NOT NULL CHECK (unique_users >= 0 AND unique_users <= clicks),
total_duration_seconds numeric(20,2) NOT NULL CHECK (total_duration_seconds >= 0),
effective_consumptions bigint NOT NULL CHECK (
    effective_consumptions >= 0 AND effective_consumptions <= clicks
),
interactions bigint NOT NULL CHECK (interactions >= 0),
PRIMARY KEY (tenant_id, news_id, event_time),
FOREIGN KEY (tenant_id, news_id)
    REFERENCES dw.dim_news(tenant_id, news_id)
```

这些关系是当前模拟口径。企业点击 PV 若允许同一次曝光后的重复点击，`clicks <= impressions` 未必成立，必须先对齐埋点与去重定义。互动是事件次数，当前没有限制其不超过点击。

`event_time` 当前没有数据库整点 CHECK；整点与单小时要求由生成器、入口和查询护栏检查。现有视图使用 INNER JOIN，缺基线可能使指标行消失；本地外键也不保证每条指标都必有基线。生产需要明确缺基线的展示与计算策略。

### 查询范围和计算公式约束

页面/聊天中的模型只返回 `SqlAssistantIntent`，由 Python 的 `compile_query` 生成 SQL。下面是按点击量取候选新闻的编译形态，参数从可信身份和确认窗口绑定：

```sql
SELECT news_id, title, content_type, category, source,
  SUM(impressions) AS impressions,
  SUM(clicks) AS clicks,
  SUM(effective_consumptions) AS effective_consumptions,
  SUM(interactions) AS interactions,
  COALESCE(ROUND(SUM(clicks) * 1.0 / NULLIF(SUM(impressions), 0), 4), 0) AS ctr,
  COALESCE(ROUND((SUM(clicks) * 0.4 + SUM(effective_consumptions) * 0.35
    + SUM(interactions) * 0.25) / NULLIF(SUM(impressions), 0), 4), 0) AS hot_score
FROM dw.news_behavior_aggregate
WHERE tenant_id = :tenant_id
  AND event_time >= :window_start
  AND event_time < :window_end
GROUP BY news_id, title, content_type, category, source
ORDER BY clicks DESC, news_id ASC
LIMIT :row_limit
```

`SqlAssistantGuard` 用 sqlglot 检查完整 AST：单条 SELECT、单一批准视图、完整固定投影与公式、参数集合、租户和左右时间边界、排序与 LIMIT。拒绝模型查询中的 JOIN、CTE、子查询、多语句、注释、写操作、锁、星号和未知结构。视图自身的受控 JOIN 不属于模型查询权限。

CTR 使用总点击除以总曝光，不能平均各小时的 CTR。这里的 `hot_score` 是 SQL 候选演示分数；最终热度由 Python `HotNewsRanker` 按批准版本计算。小时 UV 不可相加后声称窗口去重人数；跨新闻人数也不可直接相加。

另有通用 `Text2SqlNewsMetricSource`：批准模板优先，不能覆盖时才由模型生成候选 SQL并经过通用 Guard。它与页面的意图编译链路不同；通用模型兜底的指标投影与批准统计口径绑定仍是 TODO，不能仅凭只读检查认定指标正确。

### 执行和运行身份约束

Service 在执行或复用缓存前复核 Schema、冻结意图、编译 SQL、参数、查询哈希、当前身份和运行绑定。`LocalPostgresSqlWarehouseClient` 独立再次验租户、AST 与预算，使用参数化执行、`SET TRANSACTION READ ONLY` 和 `statement_timeout`；场景默认 SQL 10 秒、排行最多 100 行，执行器上限 30 秒/1,000 行。

本地查询事务只读不等于生产账号已经配置最小权限。当前模拟 DDL 没有创建生产专用角色、GRANT 或 RLS 策略。PostgreSQL 的只读事务限制数据库写操作，RLS 还需要单独配置并验证角色绕过边界，见 [事务设置](https://www.postgresql.org/docs/current/sql-set-transaction.html) 和 [行级安全](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)。

## 4. 企业实时数仓需要提供的上游能力

下面是接入时需要确认的逻辑链路，不表示腾讯新闻已经使用所列开源产品：

```mermaid
flowchart LR
    A[客户端与服务端埋点] --> B[企业事件接入与消息流]
    B --> C[去重与事件时间窗口聚合]
    C --> D[聚合数仓与指标服务]
    E[新闻内容与维度服务] --> D
    D --> F[NewsAgent 受限查询]
    F --> G[Python 指标校验与热度计算]
    G --> H[正文与关联召回]
    H --> I[模型分析与运行证据]
```

消息流可参考 Kafka 的发布订阅、保留与重放；窗口聚合可参考 Flink 的事件时间、水位与迟到处理。这些只是工程选项。水位用于决定事件时间窗口何时触发，窗口完整性还取决于迟到预算、源端覆盖和修正策略，不能把“到了整点”视为数据完整。参考 [Kafka 官方简介](https://kafka.apache.org/intro/) 与 [Flink 流式分析](https://nightlies.apache.org/flink/flink-docs-stable/docs/learn-flink/streaming_analytics/)。

你主要需要对接企业内部团队和接口合同，而非自行采购硬件或确定厂商：

| 对接对象 | 要取得并确认的内容 |
| --- | --- |
| 新闻内容/内容资产团队 | 正文及维度接口、统一新闻 ID、撤稿和删除状态、类型与栏目字典 |
| 埋点/数据平台团队 | 曝光、点击、有效阅读/播放、互动、时长的定义；去重键和过滤规则；聚合视图或指标接口 |
| 实时数仓/指标服务团队 | 时间粒度与时区、事件时间/处理时间、可用水位、迟到与修正、快照版本、历史基线和查询 SLA |
| 身份/安全/平台团队 | 业务域到 tenant 的映射、服务身份、只读权限、审计、配额和秘密注入方式 |
| 运维/基础设施团队 | 计算与存储配额、CPU/内存、磁盘 I/O、网络、监控告警和容量压测 |
| 模型平台团队 | 企业推理与 Embedding Port、版本、超时、并发与配额；这是数仓查询下游的独立依赖 |

数仓查询和流式聚合通常关注 CPU、内存、状态存储、磁盘与网络。当前本地模拟不需要专门 GPU；推理/Embedding 的硬件由所接模型服务决定。容量申请应提供峰值事件速率、新闻数、窗口粒度、状态保留、查询并发和新鲜度要求，不能用当前 57,600 行样本直接推算生产机器规格。

生产聚合数据合同至少需要：`tenant_id/news_id` 的可信映射、窗口起止、粒度/时区、指标口径版本、数据版本、快照时间、窗口完整性及水位语义、缺失/零值定义、基线来源与版本、修正策略和证据引用。它们是拟补充的合同信息，不是当前 18 列视图中已存在的字段。

代码中已经有 `BehaviorWarehouseRpc` / `RpcBehaviorDataSource` 的水位及分页版本校验，但这属于行为事件 Port，尚不能证明当前聚合 SQL 链路已具备水位。接入本项目时优先取得必要的聚合结果；不在本地持久保存企业原始用户行为，明细接口如需使用必须遵守企业边界并另行验收。

## 5. 本地如何演示，以及怎样更接近实时

现有演示入口见 [企业情形合成数据演示](enterprise-synthetic-data.md)：v2 API 为 `http://127.0.0.1:28030`，前端配置目标为该地址的 `5177/chat`。选择运营情形后，实际执行两个固定单小时窗口的 SQL、热点运行和分析；Top100 是候选上限，输出以实际保存的榜单为准。这可以展示 SQL、榜单变化、指标变化和分析可追溯性。

可使用 [只读数仓核对 SQL](sql/news-warehouse-readonly-checks.sql) 检查当前表规模、约束、时间对齐和缺基线情况。该文件是本地运维核对脚本，包含多条受信任 SELECT，不作为模型工具输入：

```bash
# 从项目根目录执行；账号/库名是公开的本地合成演示配置。
docker exec -i news-agent-enterprise-scale-demo-postgres-1 \
  psql -X -U newsagent_native -d newsagent_native -v ON_ERROR_STOP=1 \
  < agent/docs/sql/news-warehouse-readonly-checks.sql
```

若要继续模拟真正的时间推进，下一步应新建独立的版本与数据库：生成合成事件 → 去重 → 按事件时间窗口聚合 → 发布带版本和完整性标志的快照 → Agent 查询。实验加入重复、乱序、迟到和补数，测量事件到可查询结果的 P50/P95/P99 延迟、聚合结果与全量重算的一致率、重复去除准确性、迟到修正收敛和 SQL 延迟。明确区分进行中与完成窗口，修正后生成新的快照版本。

这条增量管道目前未实现；不得往冻结 v1/v2 样本持续追加或修改数值来冒充实时。当前数据规模与约束验收也不能替代企业采集、吞吐量或模型质量验收。

## 6. 完整调用链、配置、验证与 TODO

上游页面/聊天/演示 CLI → 可信身份和确认窗口 → `SqlAssistantService.preview` → 模型 Port 返回 `SqlAssistantIntent` → `validate_intent` / `compile_query` → `SqlAssistantGuard` → 查询快照及 `HotNewsSqlBindingStore` 原子绑定 → Temporal Workflow/Activity → `execute_for_hot_news` 复核 → `LocalPostgresSqlWarehouseClient` 查询聚合视图 → `native_hot_news_sql_support` 补齐并交叉核对同小时 UV、时长和基线 → `NewsMetricSnapshot` → Python `HotNewsRanker` → 正文与关联召回 → 模型分析及确定性校验 → 保存同 run 的指标、SQL 轨迹、榜单和报告 → 前端/会话有界数据分析输出。

模拟数据准备链路为：`Settings` → `synthetic_profiles` / `scaled_profiles.iter_scaled_rows` → `initialize_demo_warehouse` → 分批写入 → Schema/实际行指纹验证。生产真实聚合 Adapter 尚未装配。

外部依赖：PostgreSQL、SQLAlchemy、sqlglot、Pydantic、PyYAML；工作流使用 Temporal，运行栈还依赖 Redis、MinIO。模型和 Embedding 通过 Port 调用，SQL 规划不需要 Embedding；关联召回需要它。会话数据分析按配置使用独立分析服务。

关键配置：`ENVIRONMENT=e2e`、本地模拟开关、`SQL_ASSISTANT_DATASET_PROFILE`、`SQL_ASSISTANT_NEWS_PER_TENANT`、`SQL_ASSISTANT_SCENARIOS_PATH`；API 和热点 worker 的 profile/规模/场景必须一致。v2 场景为 `deploy/text2sql-scenes.enterprise-v2.yml`，预览 TTL 900 秒、模型最多 2 次且每次 15 秒、SQL 默认 10 秒。规模变化必须使用新的隔离数据库。

本次验证：只读查询两个运行数据库的版本、实际四对象行数、两租户覆盖、小时范围和指标表约束；新核对脚本也在两个实例只读执行。仅补充文档与核对脚本，未重启服务、变更数据或修改冻结合同；未重跑完整后端套件。

现有测试入口（进入 `agent/`，需要项目 dev 依赖）：

```bash
python -m pytest tests/test_sql_assistant_planner.py \
  tests/test_sql_assistant_guard.py tests/test_sql_assistant_service.py \
  tests/test_sql_assistant_warehouse.py tests/test_scaled_profiles.py \
  tests/test_hot_news_sql_binding.py tests/test_native_hot_news_sql_support.py -q
```

真实 PostgreSQL 集成测试需要显式提供独立测试库地址，见对应测试及演示文档。不要把会初始化/演练漂移的集成测试指向当前业务演示库。

剩余 TODO：企业真实字段与 SDK/方言 Adapter；聚合 SQL 的完整性、水位与修正合同；真实历史基线与跨窗口/跨新闻 UV；生产只读账号、必要 RLS、配额/成本与审计；通用模型 SQL 的口径绑定；独立增量模拟及企业容量/模型质量验收。
