# 本地热点新闻数仓契约 v1（冻结）

契约标识：`news-warehouse-v1`。本文件是 NewsAgent Text2SQL 展示环境的固定格式，不是企业真实数仓的反向推测或生产数据合同。所有示例新闻与指标均为本项目生成的合成数据，不包含用户级行为、企业内容或身份信息。

本版本冻结后不得原地修改。格式需要演进时，新建 `sql-warehouse-schema-v2.md` 和对应版本化代码/数据迁移，并保留 v1。文件交付为只读权限；Python 代码绑定本文件的 SHA-256，启动、场景配置读取、SQL 预览和执行时校验，内容不一致即停止。只读权限不能阻止操作系统文件所有者重新授权，哈希校验用于发现改动而非宣称绝对不可修改。

## 1. 数据粒度与范围

- 数据库方言：PostgreSQL。
- 业务隔离键：`tenant_id`（UUID），由后端身份上下文绑定，模型和前端问题无权指定。
- 新闻关联键：`news_id`（字符串），与租户共同唯一。
- 指标粒度：每租户、每新闻、每自然小时一行。`event_time` 为带时区小时桶起点。
- 本地展示窗口：`2026-10-03T00:00:00+08:00 <= event_time < 2026-10-04T00:00:00+08:00`。
- 主展示租户：`11111111-1111-4111-8111-111111111111`；隔离测试租户：`33333333-3333-4333-8333-333333333333`。
- 每租户 12 篇合成新闻，每篇 24 个小时桶。两个租户共 24 个新闻维度记录、576 个指标记录、576 个基线记录。
- 所有内容类别使用 `article` / `video`；本地栏目使用 `科技`、`财经`、`体育`、`社会`。企业接入时需按真实字典显式映射，不自动猜测。

## 2. 物理表

物理表只供本地初始化与运维核对。Text2SQL 只允许查询第 3 节的聚合视图，不向模型开放物理表或应用运行表。

### `dw.dim_news`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| tenant_id | uuid | 主键组成部分，租户 |
| news_id | text | 主键组成部分，新闻标识 |
| title | text | 合成新闻标题，不含正文 |
| content_type | text | 只能为 article / video |
| category | text | 四个固定中文栏目之一 |
| source | text | 合成来源名称 |
| publish_time | timestamptz | 新闻发布时间 |

主键：`(tenant_id, news_id)`。

### `dw.news_metric_hourly`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| tenant_id | uuid | 主键组成部分 |
| news_id | text | 主键组成部分，关联新闻维度 |
| event_time | timestamptz | 主键组成部分，小时桶起点 |
| impressions | bigint | 展示次数，非负 |
| clicks | bigint | 点击次数，非负，不大于 impressions |
| unique_users | bigint | 小时内去重访问人数，非负，不大于 clicks |
| total_duration_seconds | numeric(20,2) | 消费时长总秒数，非负 |
| effective_consumptions | bigint | 有效消费次数，非负，不大于 clicks |
| interactions | bigint | 点赞、评论、分享等互动次数合计，非负 |

主键：`(tenant_id, news_id, event_time)`；外键：`(tenant_id, news_id) -> dw.dim_news`。

`unique_users` 只在单个小时桶内去重。跨小时相加会重复计数，不能把 SUM(unique_users) 描述为窗口去重人数。v1 查询助手不提供窗口去重人数排名。行为记录只有聚合值，没有 user_id、设备、IP 或单次行为。

### `dw.news_metric_baseline_hourly`

| 字段 | 类型 | 约束与含义 |
| --- | --- | --- |
| tenant_id | uuid | 主键组成部分 |
| news_id | text | 主键组成部分 |
| event_time | timestamptz | 主键组成部分，与指标桶对齐 |
| baseline_impressions | numeric(20,2) | 对照历史同小时展示次数均值，非负 |
| baseline_clicks | numeric(20,2) | 对照历史同小时点击次数均值，非负 |
| baseline_effective_consumptions | numeric(20,2) | 历史同小时有效消费次数均值，非负 |
| baseline_interactions | numeric(20,2) | 历史同小时互动次数均值，非负 |

本地基线是确定性生成的参考值，用于演示增长计算，不是统计估计或企业真实历史数据。真实接入需另行确认基线口径、样本和迟到数据处理。

## 3. 唯一 SQL 白名单视图

```sql
CREATE VIEW dw.news_behavior_aggregate AS
SELECT
    m.tenant_id, m.news_id, m.event_time,
    n.title, n.content_type, n.category, n.source, n.publish_time,
    m.impressions, m.clicks, m.unique_users,
    m.total_duration_seconds, m.effective_consumptions, m.interactions,
    b.baseline_impressions, b.baseline_clicks,
    b.baseline_effective_consumptions, b.baseline_interactions
FROM dw.news_metric_hourly AS m
JOIN dw.dim_news AS n
  ON n.tenant_id = m.tenant_id AND n.news_id = m.news_id
JOIN dw.news_metric_baseline_hourly AS b
  ON b.tenant_id = m.tenant_id
 AND b.news_id = m.news_id
 AND b.event_time = m.event_time;
```

该视图是模型看到的完整 Schema：18 个字段，粒度仍为每租户、每新闻、每小时。JOIN 同时绑定 tenant_id，禁止只按 news_id 关联。Text2SQL 不开放 JOIN、子查询、写操作、SQL 注释或视图外表；参数必须绑定。

## 4. 确定性指标口径

| 指标 | 窗口内计算 |
| --- | --- |
| 展示次数 impressions | SUM(impressions) |
| 点击次数 clicks | SUM(clicks) |
| 互动次数 interactions | SUM(interactions) |
| 点击率 ctr | SUM(clicks) / NULLIF(SUM(impressions), 0) |
| 有效消费率 | SUM(effective_consumptions) / NULLIF(SUM(impressions), 0) |
| 互动率 | SUM(interactions) / NULLIF(SUM(impressions), 0) |
| 点击增长率 | SUM(clicks) / NULLIF(SUM(baseline_clicks), 0) - 1 |
| 本地热度 hot_score | 0.40 × ctr + 0.35 × 有效消费率 + 0.25 × 互动率 |

热度规则版本：`sql-demo-hot-score-v1`。该公式是透明、可复现的展示规则，并非项目其他热点运行 Bundle 的替代。展示查询对 ctr 和 hot_score 四舍五入到小数点后 4 位；SUM(impressions) 为零时通过 COALESCE 返回 0，表达没有有效曝光产生的率和分数。点击增长率若未来开放且基线为零，则返回 NULL（暂无可比数据）。模型无权补造数字。CTR 与互动率必须先对次数求和再相除，不能平均小时率。有效消费以展示次数为分母，避免与点击转化率混用。

## 5. Text2SQL 输入和执行合同

查询采用“自然语言 -> 结构化意图 -> Python 参数化 SQL 编译 -> SQL Guard -> PostgreSQL 只读执行”的混合方式。模型理解查询种类、排行指标、内容类型和栏目；SQL、租户、时间窗口和限制由 Python 确定性生成并检查。本地模型 Port 是可复现模拟，不代表真实企业模型的自然语言泛化质量。

每次 SQL 都必须包含：

```sql
WHERE tenant_id = :tenant_id
  AND event_time >= :window_start
  AND event_time < :window_end
LIMIT :row_limit
```

允许附加内容类型和栏目参数。排序指标白名单：clicks、ctr、interactions、hot_score、impressions；趋势按小时分组。查询必须有界，只读事务，执行超时最多 30 秒，返回最多 1000 行。前端执行已保存、校验过的查询快照，不提交可任意修改的 SQL。

## 6. 初始化与冻结策略

本地初始化只允许 `environment=e2e`，在 PostgreSQL 内使用事务和 advisory lock。表、视图和合成样本只按契约创建；已存在样本使用 `ON CONFLICT DO NOTHING`，不会覆盖用户记录。存在同名无契约对象、版本不符或文件哈希不符时拒绝接管；不得自动 DROP、清空或 ALTER。

`dw.news_schema_contract` 保存契约版本和文件 SHA-256。它不属于 Text2SQL 白名单。v1 代码和文件哈希绑定，后续演进必须创建新版本而非改变 v1。企业实际数仓格式尚待真实接口联调；本地合成结构仅用于完整演示和安全契约测试。
