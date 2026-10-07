# 本地热点新闻数仓契约 v3（冻结）

契约标识：`news-warehouse-v3`。profile 为 `public-headlines-v3`，数据版本为 `qq-public-headlines-2026-10-07-v1`。本版本将近期腾讯新闻公开网页的原标题、来源、真实发布时间和原文链接，配合确定性生成的运营聚合指标使用。公开标题不代表企业真实正文、行为、点击量、热度或模型质量；v1/v2 文件、数据和已有快照保持各自身份。

## 新闻与数据身份

- 公开标题目录固定于 `data/public-headlines/2026-10-07/catalog.json`，由代码固定 SHA-256 校验；运行时不联网、不自动换标题或回填缺失记录。
- 目录保存 catalog_version、collected_at 和 articles；每条 article 含 news_id、title、source、url、published_at、category、content_type。ID、标题和URL唯一；news_id 使用公开腾讯新闻文章ID，url为其固定HTTPS原文地址。
- 目录包含1,200个不同的公开标题；两个隔离租户复用同一新闻目录。因此默认新闻维表有2,400行，但公开新闻原文数量是1,200，不能描述为2,400篇不同原文。
- 标题和来源保持原文，不加合成编号或改写标题。新闻ID属于关联标识，不作为标题展示。
- 栏目是将公开页面分类映射到科技、财经、体育、社会的本地演示分类；不能当作企业栏目字典。类型根据原文文章/视频标识映射为article/video。
- `publish_time`保留公开发布时间，原文链接保存在目录及标题来源元数据中，不增加模型视图列。
- 公开标题资料日期为2026-10-06。模拟行为窗口仍固定为北京时间2026-10-03全天，即2026-10-03T00:00:00+08:00至2026-10-04T00:00:00+08:00，左闭右开。两个时间轴独立；模拟数据不证明新闻发布前发生真实消费行为。
- 固定租户为11111111-1111-4111-8111-111111111111及33333333-3333-4333-8333-333333333333，身份由后端绑定；不保存真实用户、设备、IP或单次行为。
- 每租户只允许120或1,200篇，默认1,200；数量不得超过已验证目录。每篇24个整点小时。

| 每租户新闻 | 全库新闻维表 | 全库小时指标 | 全库基线 |
| --- | --- | --- | --- |
| 120 | 240 | 5,760 | 5,760 |
| 1,200 | 2,400 | 57,600 | 57,600 |

## 物理表、唯一视图与指标口径

物理结构继承v2的三张业务表；新增版本不复用旧数据库，既有数据不更新、不补齐、不接管。

- `dw.dim_news`：tenant_id uuid、news_id text、title text、content_type text、category text、source text、publish_time timestamptz。主键(tenant_id,news_id)，类型与四个栏目CHECK。
- `dw.news_metric_hourly`：tenant_id、news_id、event_time以及impressions、clicks、unique_users、total_duration_seconds、effective_consumptions、interactions。主键(tenant_id,news_id,event_time)，新闻外键包含租户。计数非负，clicks≤impressions，UV和有效消费≤clicks，时长非负；时长numeric(20,2)，其余bigint。计数关系仅为演示口径。
- `dw.news_metric_baseline_hourly`：相同身份字段以及非空非负numeric(20,2)的baseline_impressions、baseline_clicks、baseline_effective_consumptions、baseline_interactions。主键和完整小时指标外键都包含租户、新闻及时间。未知UV/时长参考为null，不补造；已知零基线不是缺失值。
- 指标及基线有(tenant_id,event_time,news_id)索引。
- `dw.news_schema_contract`记录本冻结版本与文档hash；`local_simulation.synthetic_dataset_contract`绑定profile、版本及全量数据hash；两者不对模型开放。

模型唯一批准视图仍为`dw.news_behavior_aggregate`，18列顺序固定为：

```text
tenant_id, news_id, event_time, title, content_type, category, source, publish_time,
impressions, clicks, unique_users, total_duration_seconds, effective_consumptions, interactions,
baseline_impressions, baseline_clicks, baseline_effective_consumptions, baseline_interactions
```

视图按tenant_id/news_id连接新闻和指标，再按tenant_id/news_id/event_time内连接基线；缺基线的指标不会出现在视图，必须由数据核对暴露。

每次查询只允许直接读取唯一批准视图，必须绑定可信租户、半开时间窗口与row_limit；模型查询不允许JOIN、CTE、子查询、写操作、锁或其他表。参数绑定、完整AST与固定公式验证、只读事务及statement_timeout由各层独立执行。执行器最多1,000行和30秒；场景默认Top100、10秒。热点完整链路只接受一个完整小时；UV不能跨小时或跨新闻相加后称为去重人数。

CTR为SUM(clicks)/NULLIF(SUM(impressions),0)，不能平均分项CTR。SQL候选分数为0.40×CTR＋0.35×有效消费率＋0.25×互动率；最终热度仍由批准Bundle中的Python规则计算。基线为零时相对增长率未知；未知指标和已知零值分别保留。

## 生成、来源与不可变性

十项运营情形继承既有稳定、突增、回落、漏斗变化、内容类型、小样本、零基线、榜单进出、间隔及大数精度。公开标题不是采集行为的证据，全部运营数值仍由确定性公式产生。

正文Port仅提供“公开标题引用＋来源链接＋模拟数据说明”的上下文，不把合成段落称为原文，也不生成标题之外的新闻事实。知识入库可索引该明确标注的标题上下文；需要新闻事实证据时必须另行取得并核验原文。

初始化仅environment=e2e、PostgreSQL和新的隔离数据库；同事务advisory lock，每批最多500行，顺序写维度、指标、基线，并在登记数据身份前验证全部实际行。后续启动用服务端游标完整重算各表hash与行数；缺行、多余行、值变化、标题/来源变化、规模或目录hash变化都拒绝，不静默修复。

行哈希沿用news-scaled-fixture-v1：协议名和表名、按tenant/news/hour排序的规范JSON行、最后rows计数。日期统一UTC，Decimal保留精确十进制字符串，UUID转文本。manifest绑定profile、数据版本、目录字节hash、数量、24小时、两租户、场景、各表完整hash与行数；发布另一标题目录必须另建版本。

前端只对显示值舍入：参考/均值最多2位小数、百分比2位、热度分最多4位。数据库、Python/SQL计算、审计及原始报告保持完整精度；超出JavaScript安全整数的值使用权威字符串，不能转为不精确Number后重算。公开标题日期与模拟行为日期在页面分别披露。

本版本不实现企业实时流入、水位、迟到修正、跨窗口UV、真实历史基线、生产权限或企业模型质量验收。
