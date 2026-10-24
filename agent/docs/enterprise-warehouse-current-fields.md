# 当前本地结构字典，企业映射参考

核对日期：2026-10-23。范围：`news_sim_v8` 的 **13 张物理表、183 个物理字段、4 个只读视图**。同名字段在不同表分别计数，JSON 内部元素不算物理列。

用户于 2026-10-23 已确认企业数仓使用 **TDSQL-A、PostgreSQL 兼容**；本文依据当前仓库的 PostgreSQL 风格 DDL 与生成器整理，**不宣称这些是腾讯内部真实库名、表名、字段、物理类型或已经部署到 TDSQL-A 的结构**。企业接入沿 PostgreSQL 方言映射，具体内核版本、分布键和读接口仍需确认。它是便于逐字段确认的参考，不是建库脚本。

先对齐 Agent 输入时，请从 [企业 TDSQL 接入合同](enterprise-tdsql-warehouse-contract.md) 的 18 列核心字段表开始；其中也包含建议新增的快照元数据、指标公式和评测准备步骤。

数据来源与边界：

- 8,120 篇正文来自真实公开新闻缓存；其中 661 篇有明确标记的模拟行为聚合，7,459 篇只有内容及特征。这里的数字是当前本地包规模。
- 没有指标行表示“没有可用观测”，不是 0 次曝光、0 次点击；禁止为了让文章上榜补 0 或模拟指标。
- 只保留聚合量、来源证据和版本；本文不设计落地企业用户原始行为明细或用户身份列表。
- 独立 `news_sim_v8.*` 与 Agent 的 `dw.*` 是两套结构。列名相似不代表互通、都已导入或具备相同约束。

字段类型与空值说明：以下保留源 DDL 的 `uuid`、`text`、`timestamptz`、`jsonb`、`numeric(20,2)` 等类型名称。是否可空按物理表 DDL 判断；视图经过 LEFT JOIN 或除零保护后还可能出现 NULL。没有列被默认填 0。

## 统一口径

1. **时间**：事件桶为左闭右开区间 `[event_time, window_end)`；时间戳带时区，当前模拟配置按 Asia/Shanghai；日表用北京时间自然日。统计桶起点、新闻发布时间、数据水位、快照时间、事件生效时间各有不同含义。
2. **可加指标**：曝光、点击、详情 PV、直接 PV、累计时长、有效消费、各类互动都是事件计数或总量；只有统计口径一致、时间及渠道无重复、修订版本一致时才可相加。
3. **UV 不可直接相加**：`unique_users` 是点击 UV，`detail_visitors` 是详情访问 UV。跨小时、渠道、新闻必须依赖上游给出的目标粒度去重结果或批准的并集计算；仅有这些整数不能还原并集。当前模拟器采用临时数学区间并集，未保存真实用户 ID。
4. **版本**：同一小时的多个 `snapshot_revision` 是替代关系。先选择最大修订，再判断水位、完整性和质量。不能同时累加新旧快照，也不能在最新修订不完整时自行回退旧快照。
5. **派生指标**：CTR = clicks / impressions；点击归因有效率 = effective_consumptions / clicks；每详情 PV 平均时长 = total_duration_seconds / detail_page_views。分母为 0 时视图返回 NULL；不能用 UV 替代 PV 而仍沿用原指标名。跨组比例须重新用分子分母计算，不能简单平均各组比例。
6. **合成标记**：`*_synthetic` 是来源属性，不是“该行存在指标”的判断。真实正文可同时带 `metric_data_synthetic=true` 和 `metric_available=false`，表示此包的行为体系为合成、但该新闻没有行为观测。
7. **业务约束边界**：`clicks ≤ impressions` 等是当前本地合同假设，不能直接认定企业所有数据均遵守；真实企业事件归因、延迟补数及过滤方式需逐项确认。

## 物理表总览

| 表名（均位于 news_sim_v8） | 字段数 | 作用 |
| --- | ---: | --- |
| `dim_behavior_policy` | 7 | 行为口径版本表 |
| `dim_channel` | 5 | 访问来源渠道维表 |
| `dim_news` | 28 | 新闻内容与生命周期维表 |
| `news_content_feature` | 10 | 内容特征表 |
| `window_snapshot` | 10 | 小时窗口快照表 |
| `news_metric_5min` | 18 | 5 分钟行为聚合表 |
| `news_metric_channel_hourly` | 19 | 渠道小时行为聚合表 |
| `news_metric_hourly` | 20 | 新闻小时行为聚合表 |
| `news_metric_daily` | 25 | 新闻自然日行为聚合表 |
| `news_metric_baseline_hourly` | 9 | 历史同小时基线表 |
| `content_change_event` | 12 | 内容变更事件表 |
| `content_tombstone` | 8 | 内容不可见标记表 |
| `index_cleanup_task` | 12 | 索引清理计划表 |

## 1. `dim_behavior_policy`：行为口径版本表

粒度：每租户每口径版本一行。主键：`tenant_id` + `policy_version`。

口径说明是文本；没有单独的阈值、去重键或归因时窗字段。`metric_data_synthetic=true` 是本地来源限制。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 5 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `policy_version` | `text` | 否 | 指标定义版本；关联同 tenant_id 的 dim_behavior_policy。当前生成值为 sim-click-attributed-reading-v8.1。 |
| `click_definition` | `text` | 否 | 点击计数口径的文字说明；当前：去重点击事件，主动重复点击仍计数。事件去重键与去重时窗未由此字段结构化定义。 |
| `uv_definition` | `text` | 否 | UV 口径说明；当前明确 unique_users 为点击 UV，detail_visitors 为独立详情访问 UV；用户标识与跨端合并规则待企业确认。 |
| `pv_definition` | `text` | 否 | PV 口径说明；当前为成功详情打开次数，包含直接访问，可超过点击次数。 |
| `effective_definition` | `text` | 否 | 有效消费口径说明；当前只规定点击归因，不含所有直接访问阅读；秒数、阅读比例等有效阈值待企业确认。 |
| `metric_data_synthetic` | `boolean` | 否 | 行为数据的合成属性；当前 CHECK 固定 true，仅表示数据来源属性，不等于本文章存在指标行。 |

## 2. `dim_channel`：访问来源渠道维表

粒度：每租户每渠道一行。主键：`tenant_id` + `channel_id`。

当前生成六个渠道：recommend、channel、search、hot_board、push、web；`platform` 只区分 app/web。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 12 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `channel_id` | `text` | 否 | 访问来源渠道编码，关联同 tenant_id 的 dim_channel；必须明确归因规则，不能与栏目 category 混用。 |
| `channel_name` | `text` | 否 | 渠道展示名称；本地分别为推荐信息流、栏目页、搜索结果、热榜入口、消息入口、网页入口。 |
| `platform` | `text` | 否 | 访问终端类别；CHECK 枚举 app / web。本地字段不区分操作系统或 App 版本。 |
| `dimension_data_synthetic` | `boolean` | 否 | 渠道维度为本地演示定义；当前 CHECK 固定 true，不代表腾讯内部官方渠道字典。 |

## 3. `dim_news`：新闻内容与生命周期维表

粒度：每租户每新闻一行，保存当前公开正文及本地场景状态。主键：`tenant_id` + `news_id`。

`source_content_version = content_version ≤ fixture_content_version`。主展示租户 11111111-1111-4111-8111-111111111111 必须 published / none，且 fixture_content_version = content_version。两个来源哈希可空，其余字段非空。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 18 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `title` | `text` | 否 | 真实公开新闻标题；非空文本，长度 > 0。 |
| `content` | `text` | 否 | 真实公开新闻正文；非空文本，长度 > 0。仅公开缓存内容，不含企业原始用户行为。 |
| `source_url` | `text` | 否 | 原文来源 URL；CHECK 要求 https:// 前缀。 |
| `content_type` | `text` | 否 | 内容形态；独立 v8 CHECK 固定 article。不能据此宣称已覆盖真实视频行为。 |
| `category` | `text` | 否 | 本地演示栏目；来自公开发现入口或标题规则。当前常见七类为科技、财经、体育、社会、汽车、教育、娱乐；本表 DDL 不限制枚举。 |
| `category_data_synthetic` | `boolean` | 否 | 栏目映射为演示属性；当前 CHECK 固定 true，不是内部编辑栏目认证。 |
| `source` | `text` | 否 | 来源展示标签，默认腾讯新闻公开页面缓存；不是抓取机器 ID。 |
| `publish_time` | `timestamptz` | 否 | 标准化的新闻发布时间，带时区；指标不得早于包含该发布时间的首个允许时间桶。 |
| `publish_time_raw` | `text` | 否 | 来源记录中的原始发布时间文本；用于核对解析与时区，不作为排序或聚合时间直接计算。 |
| `source_content_version` | `integer` | 否 | 公开内容来源的版本号，整数 > 0；由来源台账提供。 |
| `content_version` | `integer` | 否 | 当前真实正文版本；CHECK 必须等于 source_content_version。 |
| `fixture_content_version` | `integer` | 否 | 生命周期演示场景中的版本，≥ content_version；模拟 correct 可递增此值，但不会伪造修改后的真实正文。 |
| `content_sha256` | `text` | 否 | 正文 UTF-8 内容的 SHA-256，64 位小写十六进制；用于内容一致性与版本证据。 |
| `source_file` | `text` | 否 | 原始公开缓存／目录条目的来源路径或标识；可追溯资料，不是企业机器上的固定路径契约。 |
| `source_sha256` | `text` | 否 | 来源文件或来源目录记录 SHA-256，64 位小写十六进制；不是正文哈希的别名。 |
| `source_package` | `text` | 否 | 来源数据包标识；区分冻结目录、公开增量及测试注入来源。 |
| `source_manifest_sha256` | `text` | 是 | 来源清单 SHA-256；可空，非空时须为 64 位小写十六进制；NULL 表示未提供该类来源清单证据。 |
| `source_html_sha256` | `text` | 是 | 来源 HTML SHA-256；可空，非空时须为 64 位小写十六进制；NULL 不代表正文不存在。 |
| `news_content_synthetic` | `boolean` | 否 | 正文合成标记；当前 CHECK 固定 false，表示正文来自真实公开内容。 |
| `metric_data_synthetic` | `boolean` | 否 | 行为数据的合成属性；当前 CHECK 固定 true，仅表示数据来源属性，不等于本文章存在指标行。 |
| `metric_available` | `boolean` | 否 | 本地数据包是否为该新闻提供行为指标；false 时只能用于内容检索，不能用 0 值或合成值补榜单。 |
| `lifecycle_data_synthetic` | `boolean` | 否 | 生命周期状态／事件为本地假设场景；当前 CHECK 固定 true，不表示真实新闻被撤回或受限。 |
| `lifecycle_scenario` | `text` | 否 | 本地假设场景枚举 none / withdraw / restrict / correct；none 也不把生命周期来源改为企业真实事件。 |
| `status` | `text` | 否 | 发布状态枚举 published / withdrawn / restricted；主展示租户固定 published，其余状态仅用于隔离测试。 |
| `lifecycle_revision` | `integer` | 否 | 同一新闻的生命周期修订号，非正文版本；维表要求 > 0，事件与墓碑要求 > 1。 |
| `status_effective_at` | `timestamptz` | 否 | 当前生命周期状态的生效时刻，带时区；≥ publish_time；无场景时生成器取发布时间。 |

## 4. `news_content_feature`：内容特征表

粒度：每租户每新闻一行，关联正文哈希。主键：`tenant_id` + `news_id`。

外键 `(tenant_id, news_id)` → dim_news；内容特征覆盖有指标与无指标新闻。DDL 仅约束 keyword_top 为数组，生成器另行确保字符串、去重与数量限制。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 45 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `content_sha256` | `text` | 否 | 正文 UTF-8 内容的 SHA-256，64 位小写十六进制；用于内容一致性与版本证据。 |
| `metric_available` | `boolean` | 否 | 本地数据包是否为该新闻提供行为指标；false 时只能用于内容检索，不能用 0 值或合成值补榜单。 |
| `title_chars` | `integer` | 否 | Python len(title)，单位字符，> 0；包含空白及标点，不是模型 token 数。 |
| `content_chars` | `integer` | 否 | Python len(content)，单位字符，> 0；包含空白及标点，不是字节数或词数。 |
| `paragraph_count` | `integer` | 否 | 按一个或多个换行分割后的非空段落数，≥ 0。 |
| `sentence_count` | `integer` | 否 | 正文中句末符号组 [。！？!?]+ 的匹配次数，≥ 0；不是语义模型判定的句子数。 |
| `digit_token_count` | `integer` | 否 | 正文正则数字片段数量，≥ 0；匹配整数、小数及可选百分号，不是经事实核验的数值个数。 |
| `keyword_top` | `jsonb` | 否 | JSON 字符串数组；由标题加正文前 8,000 字符的重复 2–4 字 CJK 片段确定性筛选，去停用词及包含重复；不是 LLM 标签或用户兴趣标签。 最多 8 项。 |

## 5. `window_snapshot`：小时窗口快照表

粒度：每租户每小时每修订一行。主键：`tenant_id` + `event_time` + `snapshot_revision`。

外键 `(tenant_id, policy_version)` → dim_behavior_policy。先选最大修订，再检查完整性与质量；不能在最新修订不完整时偷偷回退旧完整版本。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 58 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `event_time` | `timestamptz` | 否 | 小时窗口起点；CHECK 要求 date_trunc('hour', event_time) = event_time。 |
| `snapshot_revision` | `integer` | 否 | 该租户、该小时的指标快照修订号，整数 > 0；修订之间不能相加，必须选择同一版本。 |
| `window_end` | `timestamptz` | 否 | 小时窗口终点，带时区；CHECK 严格等于 event_time + 1 hour，终点不含。 |
| `watermark_at` | `timestamptz` | 否 | 此快照声明的数据处理水位；complete/corrected 时须 ≥ window_end，incomplete 时须 < window_end。代表本地模型约定，不自动证明企业数据齐全。 |
| `snapshot_at` | `timestamptz` | 否 | 快照生成／落盘时刻；CHECK ≥ watermark_at。 |
| `completeness` | `text` | 否 | 窗口完整性枚举 complete / corrected / incomplete；corrected 是修订后完整，不能与旧版本相加。 |
| `quality_status` | `text` | 否 | 质量状态枚举 passed / pending；本地完整窗口须 passed，不完整窗口须 pending。 |
| `policy_version` | `text` | 否 | 指标定义版本；关联同 tenant_id 的 dim_behavior_policy。当前生成值为 sim-click-attributed-reading-v8.1。 |
| `metric_data_synthetic` | `boolean` | 否 | 行为数据的合成属性；当前 CHECK 固定 true，仅表示数据来源属性，不等于本文章存在指标行。 |

## 6. `news_metric_5min`：5 分钟行为聚合表

粒度：每租户每新闻每 5 分钟每小时快照修订一行，已跨来源渠道合并。主键：`tenant_id` + `news_id` + `event_time` + `snapshot_revision`。

外键到 dim_news；触发器验证所在小时的同修订 window_snapshot 存在，并禁止早于真实发布时间所属的 5 分钟桶。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 72 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `event_time` | `timestamptz` | 否 | 5 分钟桶起点；CHECK 要求秒为 0、分钟可被 5 整除；关联所在小时同修订的 window_snapshot。 |
| `snapshot_revision` | `integer` | 否 | 该租户、该小时的指标快照修订号，整数 > 0；修订之间不能相加，必须选择同一版本。 |
| `impressions` | `bigint` | 否 | 曝光事件数，单位次，≥ 0；本表粒度内的事件计数，不是曝光人数。 |
| `clicks` | `bigint` | 否 | 去重后的点击事件数，单位次，0 ≤ clicks ≤ impressions；主动重复点击可计多次，不等于 UV。 |
| `detail_page_views` | `bigint` | 否 | 成功打开详情的次数，单位次，≥ 0；含点击进入与直接访问，因此可大于 clicks。 |
| `direct_page_views` | `bigint` | 否 | 未归因于点击的直接详情访问次数，单位次；0 ≤ 值 ≤ detail_page_views。 |
| `total_duration_seconds` | `bigint` | 否 | 详情访问累计时长，单位秒，≥ 0；不是平均阅读时长，当前独立 v8 用整数秒。 |
| `effective_consumptions` | `bigint` | 否 | 归因于点击的有效消费次数；0 ≤ 值 ≤ clicks，且 ≤ detail_page_views − direct_page_views。具体有效阅读阈值未在本地策略中定义。 |
| `interactions` | `bigint` | 否 | 正向互动事件总数，单位次；严格等于 likes + comments + shares + favorites，不包含 negative_feedback。 |
| `likes` | `bigint` | 否 | 点赞事件数，单位次，≥ 0；不是点赞用户数。 |
| `comments` | `bigint` | 否 | 评论事件数，单位次，≥ 0；不是评论用户数或评论质量分。 |
| `shares` | `bigint` | 否 | 分享事件数，单位次，≥ 0；不是分享触达人数。 |
| `favorites` | `bigint` | 否 | 收藏事件数，单位次，≥ 0；不是收藏人数。 |
| `negative_feedback` | `bigint` | 否 | 负反馈事件数，单位次，≥ 0；独立于正向 interactions，不能减掉后仍把结果称作互动次数。 |
| `unique_users` | `bigint` | 否 | 本表统计范围内的点击 UV，单位去重人数；0 ≤ 值 ≤ clicks。不能跨小时、渠道或新闻直接 SUM。 |
| `detail_visitors` | `bigint` | 否 | 本表统计范围内的详情访问 UV，单位去重人数；0 ≤ 值 ≤ detail_page_views。与点击 UV 独立，不能互相替代。 |

## 7. `news_metric_channel_hourly`：渠道小时行为聚合表

粒度：每租户每新闻每小时每修订每渠道一行。主键：`tenant_id` + `news_id` + `event_time` + `snapshot_revision` + `channel_id`。

外键分别到 dim_news、dim_channel、window_snapshot；只在渠道归因互斥且版本一致时汇总可加指标；渠道 UV 必须另做并集。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 95 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `event_time` | `timestamptz` | 否 | 小时桶起点；通过外键关联 window_snapshot，从父表继承小时对齐及修订身份。 |
| `snapshot_revision` | `integer` | 否 | 该租户、该小时的指标快照修订号，整数 > 0；修订之间不能相加，必须选择同一版本。 |
| `channel_id` | `text` | 否 | 访问来源渠道编码，关联同 tenant_id 的 dim_channel；必须明确归因规则，不能与栏目 category 混用。 |
| `impressions` | `bigint` | 否 | 曝光事件数，单位次，≥ 0；本表粒度内的事件计数，不是曝光人数。 |
| `clicks` | `bigint` | 否 | 去重后的点击事件数，单位次，0 ≤ clicks ≤ impressions；主动重复点击可计多次，不等于 UV。 |
| `detail_page_views` | `bigint` | 否 | 成功打开详情的次数，单位次，≥ 0；含点击进入与直接访问，因此可大于 clicks。 |
| `direct_page_views` | `bigint` | 否 | 未归因于点击的直接详情访问次数，单位次；0 ≤ 值 ≤ detail_page_views。 |
| `total_duration_seconds` | `bigint` | 否 | 详情访问累计时长，单位秒，≥ 0；不是平均阅读时长，当前独立 v8 用整数秒。 |
| `effective_consumptions` | `bigint` | 否 | 归因于点击的有效消费次数；0 ≤ 值 ≤ clicks，且 ≤ detail_page_views − direct_page_views。具体有效阅读阈值未在本地策略中定义。 |
| `interactions` | `bigint` | 否 | 正向互动事件总数，单位次；严格等于 likes + comments + shares + favorites，不包含 negative_feedback。 |
| `likes` | `bigint` | 否 | 点赞事件数，单位次，≥ 0；不是点赞用户数。 |
| `comments` | `bigint` | 否 | 评论事件数，单位次，≥ 0；不是评论用户数或评论质量分。 |
| `shares` | `bigint` | 否 | 分享事件数，单位次，≥ 0；不是分享触达人数。 |
| `favorites` | `bigint` | 否 | 收藏事件数，单位次，≥ 0；不是收藏人数。 |
| `negative_feedback` | `bigint` | 否 | 负反馈事件数，单位次，≥ 0；独立于正向 interactions，不能减掉后仍把结果称作互动次数。 |
| `unique_users` | `bigint` | 否 | 本表统计范围内的点击 UV，单位去重人数；0 ≤ 值 ≤ clicks。不能跨小时、渠道或新闻直接 SUM。 |
| `detail_visitors` | `bigint` | 否 | 本表统计范围内的详情访问 UV，单位去重人数；0 ≤ 值 ≤ detail_page_views。与点击 UV 独立，不能互相替代。 |

## 8. `news_metric_hourly`：新闻小时行为聚合表

粒度：每租户每新闻每小时每修订一行，跨渠道汇总。主键：`tenant_id` + `news_id` + `event_time` + `snapshot_revision`。

外键到 dim_news、window_snapshot 和 dim_behavior_policy；曝光等事件数相加，UV 使用生成器临时区间并集；按最新合格小时供热点读视图使用。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 120 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `event_time` | `timestamptz` | 否 | 小时桶起点；通过外键关联 window_snapshot，从父表继承小时对齐及修订身份。 |
| `snapshot_revision` | `integer` | 否 | 该租户、该小时的指标快照修订号，整数 > 0；修订之间不能相加，必须选择同一版本。 |
| `impressions` | `bigint` | 否 | 曝光事件数，单位次，≥ 0；本表粒度内的事件计数，不是曝光人数。 |
| `clicks` | `bigint` | 否 | 去重后的点击事件数，单位次，0 ≤ clicks ≤ impressions；主动重复点击可计多次，不等于 UV。 |
| `detail_page_views` | `bigint` | 否 | 成功打开详情的次数，单位次，≥ 0；含点击进入与直接访问，因此可大于 clicks。 |
| `direct_page_views` | `bigint` | 否 | 未归因于点击的直接详情访问次数，单位次；0 ≤ 值 ≤ detail_page_views。 |
| `total_duration_seconds` | `bigint` | 否 | 详情访问累计时长，单位秒，≥ 0；不是平均阅读时长，当前独立 v8 用整数秒。 |
| `effective_consumptions` | `bigint` | 否 | 归因于点击的有效消费次数；0 ≤ 值 ≤ clicks，且 ≤ detail_page_views − direct_page_views。具体有效阅读阈值未在本地策略中定义。 |
| `interactions` | `bigint` | 否 | 正向互动事件总数，单位次；严格等于 likes + comments + shares + favorites，不包含 negative_feedback。 |
| `likes` | `bigint` | 否 | 点赞事件数，单位次，≥ 0；不是点赞用户数。 |
| `comments` | `bigint` | 否 | 评论事件数，单位次，≥ 0；不是评论用户数或评论质量分。 |
| `shares` | `bigint` | 否 | 分享事件数，单位次，≥ 0；不是分享触达人数。 |
| `favorites` | `bigint` | 否 | 收藏事件数，单位次，≥ 0；不是收藏人数。 |
| `negative_feedback` | `bigint` | 否 | 负反馈事件数，单位次，≥ 0；独立于正向 interactions，不能减掉后仍把结果称作互动次数。 |
| `unique_users` | `bigint` | 否 | 本表统计范围内的点击 UV，单位去重人数；0 ≤ 值 ≤ clicks。不能跨小时、渠道或新闻直接 SUM。 |
| `detail_visitors` | `bigint` | 否 | 本表统计范围内的详情访问 UV，单位去重人数；0 ≤ 值 ≤ detail_page_views。与点击 UV 独立，不能互相替代。 |
| `uv_method` | `text` | 否 | UV 去重算法标识；当前 CHECK 固定 temporary_interval_union_v8.1，表示合成数据生成器的临时区间并集，不是企业真实 UV 算法。 |
| `policy_version` | `text` | 否 | 指标定义版本；关联同 tenant_id 的 dim_behavior_policy。当前生成值为 sim-click-attributed-reading-v8.1。 |

## 9. `news_metric_daily`：新闻自然日行为聚合表

粒度：每租户每新闻每北京时间自然日一行。主键：`tenant_id` + `news_id` + `stat_date`。

外键到 dim_news 和 dim_behavior_policy；仅纳入完整合格小时。主键不含 snapshot_revision，当前不可保存同一天的多个历史版本。complete 要求 excluded_hours = 0，且 coverage_end 为北京时间次日零点。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 145 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `stat_date` | `date` | 否 | Asia/Shanghai 本地自然日，类型 date；不是 UTC 日期。 |
| `impressions` | `bigint` | 否 | 曝光事件数，单位次，≥ 0；本表粒度内的事件计数，不是曝光人数。 |
| `clicks` | `bigint` | 否 | 去重后的点击事件数，单位次，0 ≤ clicks ≤ impressions；主动重复点击可计多次，不等于 UV。 |
| `detail_page_views` | `bigint` | 否 | 成功打开详情的次数，单位次，≥ 0；含点击进入与直接访问，因此可大于 clicks。 |
| `direct_page_views` | `bigint` | 否 | 未归因于点击的直接详情访问次数，单位次；0 ≤ 值 ≤ detail_page_views。 |
| `total_duration_seconds` | `bigint` | 否 | 详情访问累计时长，单位秒，≥ 0；不是平均阅读时长，当前独立 v8 用整数秒。 |
| `effective_consumptions` | `bigint` | 否 | 归因于点击的有效消费次数；0 ≤ 值 ≤ clicks，且 ≤ detail_page_views − direct_page_views。具体有效阅读阈值未在本地策略中定义。 |
| `interactions` | `bigint` | 否 | 正向互动事件总数，单位次；严格等于 likes + comments + shares + favorites，不包含 negative_feedback。 |
| `likes` | `bigint` | 否 | 点赞事件数，单位次，≥ 0；不是点赞用户数。 |
| `comments` | `bigint` | 否 | 评论事件数，单位次，≥ 0；不是评论用户数或评论质量分。 |
| `shares` | `bigint` | 否 | 分享事件数，单位次，≥ 0；不是分享触达人数。 |
| `favorites` | `bigint` | 否 | 收藏事件数，单位次，≥ 0；不是收藏人数。 |
| `negative_feedback` | `bigint` | 否 | 负反馈事件数，单位次，≥ 0；独立于正向 interactions，不能减掉后仍把结果称作互动次数。 |
| `unique_users` | `bigint` | 否 | 本日已纳入小时的点击受众并集人数；生成器在聚合前对临时区间做并集，不是小时 UV 之和。 |
| `detail_visitors` | `bigint` | 否 | 本日已纳入小时的详情访问受众并集人数；不是小时详情 UV 之和。 |
| `coverage_start` | `timestamptz` | 否 | 本日有效统计覆盖起点；生成器取数据集开始、当天零点及真实发布时间的最大值。 |
| `coverage_end` | `timestamptz` | 否 | 本日统计覆盖终点，不含；生成器取数据集结束和次日零点的较小值，须 > coverage_start。 |
| `observed_hours` | `integer` | 否 | 实际纳入日聚合的完整合格小时桶数，范围 1–24；当日发布新闻无需在发布前补桶。 |
| `completeness` | `text` | 否 | 日覆盖完整性 complete / partial；complete 表示覆盖当天应观察的发布后时段，不一定观察 24 小时。 |
| `excluded_hours` | `integer` | 否 | 本日应观察但未纳入的小时数，≥ 0；完整日须为 0。 |
| `snapshot_revision` | `integer` | 否 | 参与日聚合的小时行中最大的修订号，> 0；当前日表主键不含它，不可当作独立日快照版本。 |
| `uv_method` | `text` | 否 | UV 去重算法标识；当前 CHECK 固定 temporary_interval_union_v8.1，表示合成数据生成器的临时区间并集，不是企业真实 UV 算法。 |
| `policy_version` | `text` | 否 | 指标定义版本；关联同 tenant_id 的 dim_behavior_policy。当前生成值为 sim-click-attributed-reading-v8.1。 |

## 10. `news_metric_baseline_hourly`：历史同小时基线表

粒度：每租户每新闻每当前小时一行。主键：`tenant_id` + `news_id` + `event_time`。

外键到 dim_news。sample_count = 0 时四个 baseline_* 必须全部 NULL；大于 0 时必须全部非空。缺失基线不是 0；没有 snapshot_revision，当前不支持同桶多版本基线台账。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 176 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `event_time` | `timestamptz` | 否 | 被评估当前小时的起点；基线取其前 1、2、3 天相同时刻的可用合格小时，绝不使用当前小时或未来值。 |
| `sample_count` | `integer` | 否 | 实际可用历史同小时样本数，0–3；不是样本天数固定为 3，也不是事件行数。 |
| `baseline_version` | `text` | 否 | 基线算法版本；当前生成值 sim-prior-3-available-days-same-hour-v8.1。 |
| `baseline_impressions` | `numeric(20,2)` | 是 | 可用历史同小时曝光次数的算术均值，保留 2 位小数，≥ 0；sample_count = 0 时 NULL。 |
| `baseline_clicks` | `numeric(20,2)` | 是 | 可用历史同小时点击次数均值，保留 2 位小数；0 ≤ 值 ≤ baseline_impressions；无样本时 NULL。 |
| `baseline_effective_consumptions` | `numeric(20,2)` | 是 | 可用历史同小时有效消费次数均值，保留 2 位小数；0 ≤ 值 ≤ baseline_clicks；无样本时 NULL。 |
| `baseline_interactions` | `numeric(20,2)` | 是 | 可用历史同小时正向互动次数均值，保留 2 位小数，≥ 0；无样本时 NULL。 |

## 11. `content_change_event`：内容变更事件表

粒度：每租户每事件一行，同时每新闻每生命周期修订唯一。主键：`tenant_id` + `event_id`。

外键到 dim_news；另有唯一键 `(tenant_id, news_id, lifecycle_revision)` 及 `(tenant_id, event_id, news_id)`。这是合成场景事件，不是企业实际审核日志。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 191 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 仅隔离生命周期演示租户 33333333-3333-4333-8333-333333333333，DDL CHECK 固定；不得视为企业租户常量。 |
| `event_id` | `text` | 否 | 生命周期事件稳定 ID；当前生成器由租户、新闻、场景计算 SHA-256 文本，用于幂等关联。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `event_type` | `text` | 否 | 事件类型 withdraw / restrict / correct；分别对应撤回、限制访问、纠正。 |
| `from_version` | `integer` | 否 | 事件作用前的内容版本，整数 > 0。 |
| `to_version` | `integer` | 否 | 事件作用后的场景版本，≥ from_version；correct 时必须严格增加。 |
| `lifecycle_revision` | `integer` | 否 | 同一新闻的生命周期修订号，非正文版本；维表要求 > 0，事件与墓碑要求 > 1。 |
| `new_status` | `text` | 否 | 事件后状态 withdrawn / restricted / published；与 event_type 绑定，correct 必须为 published。 |
| `effective_at` | `timestamptz` | 否 | 生命周期事件／封禁在业务上生效的时刻，带时区；与接收时间分开。 |
| `received_at` | `timestamptz` | 否 | 系统收到事件的时刻；CHECK ≥ effective_at，用于区分生效与到达延迟。 |
| `reason_code` | `text` | 否 | 变更原因码；本地生成 isolated_simulated_<event_type>，不是企业审核原因字典。 |
| `lifecycle_data_synthetic` | `boolean` | 否 | 生命周期状态／事件为本地假设场景；当前 CHECK 固定 true，不表示真实新闻被撤回或受限。 |

## 12. `content_tombstone`：内容不可见标记表

粒度：每租户每新闻一行。主键：`tenant_id` + `news_id`。

外键到 dim_news，并通过 `(tenant_id, event_id, news_id)` 关联同一新闻的变更事件；当前可读资格视图以是否存在墓碑阻断整篇新闻。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 208 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `event_id` | `text` | 否 | 生命周期事件稳定 ID；当前生成器由租户、新闻、场景计算 SHA-256 文本，用于幂等关联。 |
| `blocked_through_version` | `integer` | 否 | 语义为封禁覆盖到的版本上界，整数 > 0；注意当前 eligible_news 只判断墓碑是否存在，并未按该上界放行更高版本。 |
| `lifecycle_revision` | `integer` | 否 | 同一新闻的生命周期修订号，非正文版本；维表要求 > 0，事件与墓碑要求 > 1。 |
| `status` | `text` | 否 | 封禁状态枚举 withdrawn / restricted；与事件引用共同说明限制原因。 |
| `effective_at` | `timestamptz` | 否 | 生命周期事件／封禁在业务上生效的时刻，带时区；与接收时间分开。 |
| `lifecycle_data_synthetic` | `boolean` | 否 | 生命周期状态／事件为本地假设场景；当前 CHECK 固定 true，不表示真实新闻被撤回或受限。 |

## 13. `index_cleanup_task`：索引清理计划表

粒度：每租户每任务一行，每事件每目标最多一个任务。主键：`tenant_id` + `task_id`。

外键到 dim_news 和同一新闻的 content_change_event；唯一键 `(tenant_id, event_id, target)`。记录待映射的清理计划，当前数据包没有执行消费者。

DDL 来源：[real-news-warehouse-v8.sql](sql/real-news-warehouse-v8.sql)，第 218 行起。

| 字段 | 当前物理类型 | 可空 | 含义、单位与约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` | 否 | 租户／隔离域 ID；来自服务端身份及批准映射，不能由用户问题或模型任意指定。当前本地数据仅使用两个固定演示 UUID。 |
| `task_id` | `text` | 否 | 清理任务稳定 ID；当前由 event_id + target 的 SHA-256 生成，支持每目标幂等。 |
| `event_id` | `text` | 否 | 生命周期事件稳定 ID；当前生成器由租户、新闻、场景计算 SHA-256 文本，用于幂等关联。 |
| `news_id` | `text` | 否 | 新闻稳定标识；同租户内唯一，关联内容、行为、基线与生命周期；不是文章标题或数据库自增行号。 |
| `target` | `text` | 否 | 目标枚举 postgres_knowledge / milvus / bge_snapshot / retrieval_cache；只是模拟任务描述，不能证明这些目标已连接或执行。 |
| `action` | `text` | 否 | 清理动作文本；当前分别为 deactivate_chunks / delete_versioned_chunks / denylist_then_rebuild / invalidate；DDL 本身未限定枚举。 |
| `through_version` | `integer` | 否 | 清理覆盖的旧版本上界，> 0；correct 取 from_version，其余取 to_version。 |
| `status` | `text` | 否 | 任务状态；本地 CHECK 固定 pending，表示未执行，不能描述为生产清理成功。 |
| `attempt_count` | `integer` | 否 | 尝试次数；本地 CHECK 固定 0，单位次。 |
| `mapping_required` | `boolean` | 否 | 需要真实目标映射后才能执行；本地 CHECK 固定 true。 |
| `lifecycle_data_synthetic` | `boolean` | 否 | 生命周期状态／事件为本地假设场景；当前 CHECK 固定 true，不表示真实新闻被撤回或受限。 |
| `created_at` | `timestamptz` | 否 | 任务生成时刻，带时区；生成器取对应变更事件 received_at。 |

## 4 个只读视图的投影与使用边界

### `latest_window_snapshot`：10 列

投影 `window_snapshot` 的全部 10 列，字段顺序及类型继承原表。按 `(tenant_id, event_time)` 分组，以 `snapshot_revision DESC` 选择最新一条；**此视图本身不筛质量和完整性**。因此最新版本为 incomplete 时，结果仍是该不完整版本。

### `eligible_news`：28 列

投影 `dim_news` 的全部 28 列；要求 `status='published'`，且不存在同租户同新闻的 `content_tombstone`。它表示当前本地内容状态可见，不代表该文章有行为指标，也不代表过去任意时点都可见。当前实现没有利用墓碑中的 `blocked_through_version` 进行按版本放行。

### `news_behavior_aggregate`：18 列

从 `news_metric_hourly` 取行为，关联同租户、同小时、同修订的最新快照，再关联 `eligible_news`；历史基线用 LEFT JOIN。最后筛选 `completeness IN ('complete','corrected')` 且 `quality_status='passed'`。

| 投影字段（按实际顺序） | 来源 | 可空说明 |
| --- | --- | --- |
| tenant_id, news_id, event_time | news_metric_hourly | 源表非空 |
| title, content_type, category, source, publish_time | eligible_news → dim_news | 源表非空 |
| impressions, clicks, unique_users, total_duration_seconds, effective_consumptions, interactions | news_metric_hourly | 源表非空 |
| baseline_impressions, baseline_clicks, baseline_effective_consumptions, baseline_interactions | news_metric_baseline_hourly | 无样本或 LEFT JOIN 未匹配时 NULL |

该视图不包含修订、水位、UV 方法、策略版本、PV、详情 UV、渠道或生命周期字段。独立数仓这个同名视图与 Agent `dw.news_behavior_aggregate` 仍须分别验证。Agent 固定 18 列合同见 [sql-warehouse-schema-v7.md](sql-warehouse-schema-v7.md)。

### `news_operational_hourly`：39 列

用于检查窗口、生命周期和指标口径。其投影顺序是：

| 列组 | 数量 | 来源与空值 |
| --- | ---: | --- |
| news_metric_hourly 全部字段 | 20 | `m.*`，源字段非空 |
| title, source_url, publish_time, category, status, lifecycle_data_synthetic | 6 | dim_news，源字段非空 |
| watermark_at, snapshot_at, completeness, quality_status | 4 | latest_window_snapshot，源字段非空 |
| sample_count, baseline_version, baseline_impressions, baseline_clicks, baseline_effective_consumptions, baseline_interactions | 6 | LEFT JOIN 基线；全部可能因未匹配而 NULL |
| ctr | 1 | `clicks::numeric / NULLIF(impressions,0)`，分母为 0 时 NULL |
| click_attributed_effective_rate | 1 | `effective_consumptions::numeric / NULLIF(clicks,0)`，分母为 0 时 NULL |
| average_duration_per_detail_view | 1 | `total_duration_seconds::numeric / NULLIF(detail_page_views,0)`，单位秒，分母为 0 时 NULL |

该视图虽然选择最新修订，**没有过滤 incomplete / pending，也没有采用 eligible_news 过滤撤回或受限内容**。它适合受控诊断，不能直接作为运营榜单或模型可查的内容视图。

## 当前文档与实现需要区分的细节

- 冻结 v7 文档提到 `dw.news_content_feature`，但当前 Agent `warehouse.py` 的 `_DDL` 与 `scaled_profiles.py` 的 `TABLES` 未创建或导入该表；独立 `news_sim_v8.news_content_feature` 则确实定义了 10 列。不能把文档中的名字当作已接入事实。
- 独立 v8 的 `total_duration_seconds` 是 `bigint`；Agent `dw.news_metric_hourly` 为 `numeric(20,2)`。单位仍为秒，但物理类型不同。
- 独立指标表保存 `snapshot_revision`，Agent 导入流程先选择最新合格小时，再投影六项聚合量；Agent 的小时表及 18 列视图没有暴露这一版本字段。
- `news_metric_baseline_hourly` 的样本来自前 1、2、3 天同小时中存在的合格观测，不是无限向前补足 3 个样本；0 个样本保留 NULL。
- DDL 的整数计数、策略文字与约束不足以说明企业真实埋点定义。有效阅读阈值、事件去重键、归因时窗、跨端用户去重、时长截断方式仍待确认。
- 13 张表中包含用于演练的生命周期与清理计划。企业实施可以由内容平台及应用状态库提供这些能力，不要求全部落在数仓；本字典记录现状，不要求照搬物理部署。

## 来源、调用链与核对方式

资料来源：

- [独立 v8 DDL](sql/real-news-warehouse-v8.sql)：字段、类型、NULL、主外键、CHECK、触发器、视图定义。
- [源码结构审计清单](../../output/warehouse-schema-audit-20261022/inventory.json)：表字段计数和字段来源；这是源码解析结果，不是运行库 schema dump。
- [模拟聚合生成器](../simulation/warehouse_simulation/real_news.py)：`iter_rows`、`_dimension_rows`、`_article_rows`、`_hour_packet`、`_aggregate`、`_baseline` 和生命周期样本生成。
- [确定性内容特征](../simulation/warehouse_simulation/content_features.py)：`derive_content_features`、`validate_content_features`。
- [Agent 读模型映射](../app/sql_assistant/real_news_profiles.py)：`_package_identity`、`_valid_hours` 与版本化数据包校验；[Agent DDL/初始化](../app/sql_assistant/warehouse.py) 中的 `initialize_demo_warehouse`。
- [持久背景](../../DemoMD/PROJECT_CONTEXT.md) 第 26 节：真实内容与模拟行为边界。

当前上游与下游关系：

```text
公开内容缓存／冻结目录 + RealNewsSimulationConfig
→ real_news_catalog 目录与来源校验
→ derive_content_features 内容特征
→ iter_rows / _hour_packet / _aggregate / _baseline 生成本地聚合
→ 独立 v8 数据包 + DDL 定义的 13 表 / 4 视图
→ real_news_profiles._package_identity / _valid_hours 校验并选择合格小时
→ Agent 初始化投影到 dw 新闻维、小时指标与基线
→ dw.news_behavior_aggregate 固定列视图
→ SqlAssistantService / compile_query / SQL 护栏
→ 只读查询结果 → Python 指标与热度计算 → 热点报告／写作证据
```

外部依赖：当前本地读模型使用 PostgreSQL；企业 TDSQL-A 已确认 PostgreSQL 兼容，其连接方式、具体方言支持范围、权限与聚合接口仍需真实合同映射，本文没有新增驱动或连接。LLM 不能生成权威指标值。

配置：独立包由 `RealNewsSimulationConfig(start_at, closed_hours, seed)` 固定生成范围；Agent 数据包路径由 `REAL_NEWS_WAREHOUSE_DATASET` 覆盖；Agent profile 为 `real-news-v8`，冻结视图契约标识为 `news-warehouse-v7`。本文只增加说明，不修改这些配置或运行流程。

验证入口：现有 `agent/tests/test_real_news_warehouse_simulation.py`、`test_real_news_warehouse_extension.py`、`test_real_news_warehouse_queries.py`、`test_real_news_profiles.py` 可检查对应实现；本次文档工作仅静态核对字段完整性，不运行模型、数据库或评测。

尚待企业确认：TDSQL-A 具体内核版本与连接方式、库表与权限、租户映射、指标定义与去重规则、窗口关闭/补数协议、各字段真实 NULL 语义、基线版本来源、生命周期事件来源，以及本地每个逻辑字段到企业字段的对应关系。产品分支与 PostgreSQL 兼容方向已确认。

