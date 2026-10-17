# 本地热点新闻数仓契约 v5（冻结）

标识 `news-warehouse-v5`，profile `timeline-v5`，数据版本 `news-timeline-20260910-20261009-v2`。

本版本是独立隔离数据库中的纯合成新闻和运营聚合，不能称为真实报道、企业行为或模型质量验收。
行为窗口与 v4 相同：北京时间 2026-09-10 00:00（包含）至 2026-10-10 00:00（不包含），30天共720小时。
每租户允许120、1200或12000篇新闻；默认1200篇。两个固定隔离租户沿用v2，新闻在窗口之前发布。

物理表、列顺序、约束、租户外键和索引沿用v2：dw.dim_news、dw.news_metric_hourly、dw.news_metric_baseline_hourly。
模型唯一视图dw.news_behavior_aggregate保持18列：

```
tenant_id, news_id, event_time, title, content_type, category, source, publish_time,
impressions, clicks, unique_users, total_duration_seconds, effective_consumptions, interactions,
baseline_impressions, baseline_clicks, baseline_effective_consumptions, baseline_interactions
```

计数为非负bigint，时长和基线为numeric(20,2)。CTR采用点击/曝光；未知UV和时长基线仍为null。
热点执行仅允许一个整点小时。对比场景比较两个独立小时，共同新闻才可比较；
不把小时UV求和描述为去重人数，不声称中间连续走势。SQL护栏、租户绑定、只读事务和人工审批边界不变。

相对v4的扩充：每栏目主题从12个增至24个（共96个），标题带栏目、主题、阶段（启动/进展/观察/复盘）
与合成标记，来源扩展为8个合成观察团队；小时指标在日趋势与周末效应之上叠加主题级每日轮换波动，
突发小时改为18、20、21、23点；场景在v4的13个之上新增工作日/周末、同日早晚、视频早高峰跨周、
午间小样本与零基线四项，共17个。

数据身份绑定窗口、720小时、规模、场景和每张表完整行hash；行协议沿用news-scaled-fixture-v1。
启动完整核对真实行，已有数据库缺行、改值、版本不符均拒绝；不接管或清空v1–v4数据库。
修改本契约必须另建版本。真实企业数据、跨小时UV、实时增量和真实历史基线仍未验收。
