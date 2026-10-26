# 真实新闻模拟数仓 Agent 视图契约 v7（冻结）

契约标识：`news-warehouse-v7`。本文件定义 `real-news-v8` 数据包向热点 Agent 暴露的只读
视图，不是企业真实数仓合同，也不把模拟行为指标描述成企业测量结果。

## 数据范围

- 内容：8,120 篇真实公开新闻（661 篇冻结目录 + 7,459 篇 v2 公开增量），保留 `news_id`、
  标题、URL、发布时间和正文哈希；正文来自公开页面缓存。
- 指标：仅 661 篇具备既有 v8 聚合指标的新闻进入 `dw.news_behavior_aggregate`；这些指标
  仍由确定性模拟器生成，标记 `metric_data_synthetic=true`。
- 7,459 篇新增真实文章没有企业行为数据，不生成曝光、点击、UV、时长、基线或榜单行；
  它们只存在于 `dw.dim_news` 和 `dw.news_content_feature`，不得被表述为热点榜候选。
- 时间窗口：北京时间 `2026-09-30 00:00` 至 `2026-10-14 20:00`（结束不含）。
- 栏目：`科技`、`财经`、`体育`、`社会`、`汽车`、`教育`、`娱乐`。栏目是公开发现入口或
  标题规则映射的演示标签，不是原始编辑栏目。

## 只读视图 `dw.news_behavior_aggregate`

一行对应租户、新闻和小时间桶。字段顺序固定：

```text
tenant_id, news_id, event_time,
title, content_type, category, source, publish_time,
impressions, clicks, unique_users,
total_duration_seconds, effective_consumptions, interactions,
baseline_impressions, baseline_clicks,
baseline_effective_consumptions, baseline_interactions
```

- `tenant_id` 必须来自服务端身份上下文，模型不能生成。
- `unique_users` 是单新闻小时点击 UV，不等于详情访问 UV；渠道、小时和不同新闻的 UV
  不能相加。
- `baseline_*` 只来自真实存在的历史同小时样本；`sample_count=0` 时基线为空，不补 0。
- 视图只包含具备指标的文章；没有真实指标的新闻不得以 0 值或模拟值出现在榜单中。

## 查询边界

- 只允许单条只读 `SELECT`，只能读取上述视图，必须带租户、`window_start`、`window_end`
  和 `row_limit` 绑定参数。
- 单次窗口必须为正且不超过 7 天；热点榜要求恰好 1 小时。
- 允许指标：`impressions`、`clicks`、`effective_consumptions`、`interactions`；
  派生指标：`ctr`、`hot_score`；热度公式由 Python 固化。
- 不允许 JOIN、子查询、集合运算、窗口函数、写操作或注释。
- 排序必须使用批准指标并以 `news_id` 升序作为确定性兜底。

## 版本与校验

- 数据包 `manifest.json` 绑定目录、扩展清单、生成器、DDL、SQL 和每表 SHA-256。
- Agent 初始化时校验视图列顺序、表行数和摘要；不一致即停止，不回退旧数据。
- 新增真实指标必须来自企业批准的数据源并新建版本；不得用模拟值填补内容-only 文章。
