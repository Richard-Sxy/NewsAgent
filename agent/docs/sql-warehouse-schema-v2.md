# 本地热点新闻数仓契约 v2（冻结）

契约标识：`news-warehouse-v2`。本版本用于数量扩充的隔离合成演示，不代表企业真实数据、用户行为或模型质量。v1 文档、代码默认行为及已有数据库保持独立；本文件冻结后不得原地修改，合法演进需另建版本。

## 数据形状与身份

- 方言为 PostgreSQL；模型唯一白名单视图仍为 `dw.news_behavior_aggregate`。
- profile 为 `enterprise-v2`，数据版本为 `news-enterprise-scaled-v2`。
- `SQL_ASSISTANT_NEWS_PER_TENANT` 仅允许 120、1200、12000，默认每租户 1200 篇新闻。
- 固定租户为 `11111111-1111-4111-8111-111111111111` 与 `33333333-3333-4333-8333-333333333333`，两者具有相同 news_id 目录，身份由后端绑定。
- 每租户新闻 ID 为 `scale-news-000001` 起的连续六位编号，最多到 `scale-news-012000`。
- 每新闻 24 个整点小时，窗口为北京时间 `2026-10-03T00:00:00+08:00` 至 `2026-10-04T00:00:00+08:00`，左闭右开。
- 只有新闻维度、小时聚合指标和确定性参考基线。无用户、设备、IP、单次行为或企业原始正文。
- 新闻类型限定 article/video，栏目限定科技、财经、体育、社会；标题、来源和正文均明确标识合成。

| 每租户新闻 | 全库新闻 | 全库小时指标 | 全库基线 |
| --- | --- | --- | --- |
| 120 | 240 | 5760 | 5760 |
| 1200 | 2400 | 57600 | 57600 |
| 12000 | 24000 | 576000 | 576000 |

数据 profile、数量、生成器版本、全部实际行、十项固定场景及每表行数共同绑定数据 SHA-256。修改 profile 或数量必须使用新的隔离数据库，不允许更新、补齐、删除或接管旧数据。

## 物理表与约束

`dw.dim_news` 字段为 tenant_id uuid、news_id text、title text、content_type text、category text、source text、publish_time timestamptz，主键 `(tenant_id, news_id)`。

`dw.news_metric_hourly` 字段为 tenant_id uuid、news_id text、event_time timestamptz，以及以下非空聚合值：

| 字段 | 类型 | 约束 |
| --- | --- | --- |
| impressions | bigint | 非负 |
| clicks | bigint | 非负且不超过 impressions |
| unique_users | bigint | 小时内去重，非负且不超过 clicks |
| total_duration_seconds | numeric(20,2) | 非负消费总秒数 |
| effective_consumptions | bigint | 非负且不超过 clicks |
| interactions | bigint | 非负互动次数，不是去重互动人数 |

指标表主键为 `(tenant_id, news_id, event_time)`，新闻外键包含 tenant_id。小时 UV 禁止跨小时相加后称为窗口去重人数。热点完整链路继续限定一个整点小时。

`dw.news_metric_baseline_hourly` 包含相同身份三字段，以及非空非负 numeric(20,2) 的 baseline_impressions、baseline_clicks、baseline_effective_consumptions、baseline_interactions。主键及外键均包含租户、新闻、小时。缺失 UV 和时长参考保留 unknown/null，不补造。已知零基线不是缺失基线。

指标与基线表增加 `(tenant_id, event_time, news_id)` 索引，用于有界小时扫描。物理表和身份台账不向模型开放。

## 完整模型视图

```sql
CREATE VIEW dw.news_behavior_aggregate AS
SELECT m.tenant_id, m.news_id, m.event_time,
       n.title, n.content_type, n.category, n.source, n.publish_time,
       m.impressions, m.clicks, m.unique_users, m.total_duration_seconds,
       m.effective_consumptions, m.interactions,
       b.baseline_impressions, b.baseline_clicks,
       b.baseline_effective_consumptions, b.baseline_interactions
FROM dw.news_metric_hourly AS m
JOIN dw.dim_news AS n
  ON n.tenant_id = m.tenant_id AND n.news_id = m.news_id
JOIN dw.news_metric_baseline_hourly AS b
  ON b.tenant_id = m.tenant_id AND b.news_id = m.news_id AND b.event_time = m.event_time;
```

视图仍为 18 个字段，一行对应租户、新闻、小时桶。所有 JOIN 包含 tenant_id；模型不能发起 JOIN、子查询、写操作或访问其他对象。每次查询必须绑定 tenant_id、双向半开时间窗口与 row_limit。SQL 执行处于只读事务且有 statement_timeout，输出最多 1000 行；v2 展示场景每次取 Top100 候选，最终排行由 Python 热度规则重新计算。

CTR 采用 SUM(clicks)/NULLIF(SUM(impressions),0)，不能平均各新闻点击率；有效消费与互动率采用曝光作为分母。SQL 候选热度沿用透明的 0.40×CTR + 0.35×有效消费率 + 0.25×互动率，不能替代最终 Bundle 的 Python 权威热度。零曝光约定存储 CTR 为零，但趋势对比将无有效分母视为不可比较。基线为零时不生成相对增长率。

## 场景与计算范围

十项固定情况为稳定、突发增长、回落、漏斗退化、内容结构变化、小样本与零曝光、已知零基线、榜单进出、间隔恢复、大数精度。每个情况的两个窗口使用同一个点击量 Top100 查询；不同情况采用固定独立小时对。榜单进出的两个候选集保留共同新闻；趋势只比较共同 cohort，并列出新增、退出与窗口空档。

全库数量不同于分析覆盖范围。单次分析只针对已有运行的完整有界榜单快照，不把全库送入模型或 worker，也不提高原有输入、输出、行数、并发、内存或超时限制。大整数由 Python/SQL 精确计算；页面遇超出 JavaScript 安全整数时依据权威文本展示，不从损失精度的 JSON number 重新计算。

## 初始化与完整性

初始化仅在明确 `environment=e2e` 和新隔离数据库进行。一个事务持有 advisory lock，按每批不超过 500 行的维度、指标、基线顺序入库；全部行验证成功才写 `local_simulation.synthetic_dataset_contract`，避免未完成数据被声明为已完成。

流式哈希协议 `news-scaled-fixture-v1` 为每表独立 SHA-256：先写协议名与表名行；再按 tenant_id、news_id、event_time 顺序写每个规范 JSON 行和换行；最后写 `rows:<实际行数>` 与换行。JSON 使用 UTF-8、排序字段名、紧凑分隔符，日期统一 UTC、UUID 统一文本、Decimal 统一精确十进制文本。三表摘要、行数、profile、数量、生成器版本与场景目录共同形成数据 manifest 哈希。

后续初始化采用服务端游标读取全部实际行并重算，不依赖抽样、计数或缓存实际值。多余行、缺行、值漂移、profile、数量、版本、文件哈希或 manifest 不符均拒绝；不自动修复。缓存仅包含固定参数的预期 manifest，不保存全集，公开配置返回独立副本。Schema 文件由代码 SHA-256 与只读交付权限约束，所有者能重新授权文件，因此不宣称绝对不可修改。

本契约不实现企业迟到水位、跨小时去重、真实容量保证或企业权限接入；这些需要独立接口合同和生产验收。
