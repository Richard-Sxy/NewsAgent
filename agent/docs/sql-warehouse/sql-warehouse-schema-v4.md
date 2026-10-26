# 本地热点新闻数仓契约 v4（冻结）

标识 `news-warehouse-v4`，profile `timeline-v4`，数据版本 `news-timeline-20260910-20261009-v1`。

本版本是独立隔离数据库中的纯合成新闻和运营聚合，不能称为真实报道、企业行为或模型质量验收。
行为窗口为北京时间 2026-09-10 00:00（包含）至 2026-10-10 00:00（不包含），30天共720小时。
每租户允许120、1200或12000篇新闻；默认1200篇。两个固定隔离租户沿用v2，新闻在窗口之前发布。
默认全库维表2400行、小时指标1728000行、基线1728000行。最大规模各17280000行，按500行流式批次写入。

物理表、列顺序、约束、租户外键和索引沿用v2：dw.dim_news、dw.news_metric_hourly、dw.news_metric_baseline_hourly。
模型唯一视图dw.news_behavior_aggregate保持18列：

```
tenant_id, news_id, event_time, title, content_type, category, source, publish_time,
impressions, clicks, unique_users, total_duration_seconds, effective_consumptions, interactions,
baseline_impressions, baseline_clicks, baseline_effective_consumptions, baseline_interactions
```

计数为非负bigint，时长和基线为numeric(20,2)。CTR采用点击/曝光；未知UV和时长基线仍为null。
热点执行仅允许一个整点小时。跨日、跨周、月内首尾场景比较两个独立小时，共同新闻才可比较；
不把小时UV求和描述为去重人数，不声称中间连续走势。SQL护栏、租户绑定、只读事务和人工审批边界不变。

保留十项确定性边界情形，新增三项跨日/周/月内窗口对比；日趋势、周末变化和热点群体每日轮换。
标题带合成标记，涵盖科技、财经、体育、社会与图文/视频；新闻ID关联正文和知识上下文。
公开报道仍属于v3冻结目录，不将本版本合成标题标为公开报道。

数据身份绑定窗口、720小时、规模、场景和每张表完整行hash；行协议沿用news-scaled-fixture-v1。
启动完整核对真实行，已有数据库缺行、改值、版本不符均拒绝；不接管或清空v1/v2/v3数据库。
修改本契约必须另建版本。真实企业数据、跨小时UV、实时增量和真实历史基线仍未验收。
