# NewsAgent 具体新闻案例数仓契约 v6

本版本仅用于隔离本地演示，profile 为 `corpus-v6`。新闻 ID、标题、正文、来源链接和
发布时间来自已提供的腾讯新闻原文缓存；曝光、点击、UV、消费时长、互动和历史基线均为
Python 确定性模拟。该榜单不能代表真实用户行为、新闻热度或企业模型质量。

## 内容来源与身份

- 冻结目录：`data/corpus-news-v6/catalog.json`，精选 120 篇与现有 BGE 向量快照相交的
  原文。标题、正文和 URL 使用原缓存值；逐源文件 SHA-256 与目录摘要保留以供追溯。
- 发布时间只解析原缓存 `publish_time`；无时区字面量沿用现有缓存适配器的
  `Asia/Shanghai` 约定，保留原字段。不得从新闻 ID 或片段内容猜测时间。
- 缓存未提供权威栏目。`category` 是本版本明确标注的、按标题关键词确定的演示分类，
  不是企业原始栏目；这些缓存是文本新闻，`content_type` 为 `article`。
- `source` 表示“腾讯新闻公开页面缓存”，不声称是原发布者。`url` 必须与原 `news_id`
  精确匹配。正文和检索片段均是不可信证据，不能改变系统行为或权限。
- BGE/HNSW 快照只读，检索使用其配套 1024 维 query embedder，不能把检索分数当热度。

## 时间窗口与隔离

模拟指标覆盖北京时间 `2026-09-10T00:00:00+08:00` 至
`2026-10-10T00:00:00+08:00`，结束不含，共 720 个小时。历史新闻发布日期和模拟指标
日期独立。热点每次只查询一个整点小时，不跨小时相加 UV。

每租户 120 篇新闻、86,400 行小时指标和 86,400 行基线。仅两个隔离模拟租户：
`11111111-1111-4111-8111-111111111111` 与
`33333333-3333-4333-8333-333333333333`。新版本使用独立 Compose 项目与数据卷，
不得改写 v1–v5 数仓、历史运行、会话、反馈或既有生产包。

## 模型唯一允许读取的视图

唯一视图为 `dw.news_behavior_aggregate`，由以下三张表连接：
`dw.dim_news`、`dw.news_metric_hourly`、`dw.news_metric_baseline_hourly`。
模型仅生成受限意图，SQL 由 Python 编译并绑定身份、窗口与参数。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| tenant_id | uuid | 可信网关绑定的租户 |
| news_id | text | 原腾讯新闻 ID，贯穿指标、正文、知识和报告 |
| event_time | timestamptz | 模拟指标的小时桶起点 |
| title | text | 原缓存新闻标题 |
| content_type | text | 本版仅 article，不能伪造视频 |
| category | text | 科技、财经、体育、社会中的演示分类 |
| source | text | 腾讯新闻公开页面缓存 |
| publish_time | timestamptz | 原缓存发布时间按已声明时区解析 |
| impressions | bigint | 模拟曝光次数 |
| clicks | bigint | 模拟点击次数 |
| unique_users | bigint | 模拟单小时去重用户数，禁止跨桶相加 |
| total_duration_seconds | numeric | 模拟累计消费时长 |
| effective_consumptions | bigint | 模拟有效消费次数 |
| interactions | bigint | 模拟互动次数 |
| ctr | numeric | Python/SQL 按 clicks / impressions 计算，零分母为 NULL |
| baseline_impressions | numeric | 模拟曝光基线 |
| baseline_clicks | numeric | 模拟点击基线 |
| baseline_effective_consumptions | numeric | 模拟有效消费基线 |
| baseline_interactions | numeric | 模拟互动基线 |
| baseline_ctr | numeric | 模拟基线点击率，零分母为 NULL |
| hot_score | numeric | SQL 候选筛选分数，最终权威热度仍由 Python 四分量规则计算 |

## 查询与版本门禁

只允许单条只读 SELECT、白名单视图/列/聚合函数和绑定参数；不允许用户 SQL、身份参数、
DDL、DML、跨租户读取或访问系统表。API 与热点 Worker 使用相同 profile、目录、场景
YAML、SQL 契约和 production bundle。查询绑定的完整 dataset identity 包含新版本与
内容摘要，提交与实际执行时均核对；冻结文件或数据库内容变化后拒绝查询，不自动修补。

目录、小时指标、基线和场景共同形成版本化 SHA-256 身份。修改本 Markdown 必须新增
契约版本，不能绕过摘要校验；所有旧版冻结文件保持原样。新知识文档的 text_source 为
`real-news-cache-v6`，不得标为合成正文。企业数据与真实推送/发布仍需独立合同和审批。
