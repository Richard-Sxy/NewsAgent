# 腾讯新闻业务背景模拟数仓 v7

日期：2026-10-14。数据集版本：`tencent-news-business-simulation-v7-20261014`。

这份说明记录本项目新增的独立模拟数仓：以腾讯新闻公开产品概念为业务背景，用 Python 确定性生成聚合数据，补充渠道、内容形态、事件变化、窗口修订、内容生命周期和原榜快照。所有标题、来源、指标、事件和榜单均为合成数据，不是腾讯新闻内部数据，不代表腾讯新闻内部技术实现或真实业务规模。

当前交付包括生成器、13 张表的 PostgreSQL DDL、5 个查询视图和独立数据包。它尚未接入现有热点 Agent、Text2SQL 白名单、正文库或向量库。已有 v1–v6 的冻结契约、运行数据和配置不因这个数据包改变。文中“待接入”与“建议合同”不能理解为已经运行。

## 1. 公开业务依据和未知边界

| 公开来源 | 业务事实与核对范围 | 可以用于模拟的设计依据 |
| --- | --- | --- |
| [腾讯官网：腾讯新闻](https://www.tencent.com/zh-cn/products/tencent-news/) | 官方定位包括事件驱动、结构化信息知识图谱、UGC 创作与互动及个性化需求；本次于 2026-10-14 查阅。 | 增加新闻事件、主题、供给主体和互动维度。公开定位不能推出内部表名或图谱实现。 |
| [腾讯可持续社会价值报告 2024](https://static.www.tencent.com/attachments/ssv/2025/tencent-ssv-report-2024.pdf) | 本次只核对到该官方 PDF 的来源检索摘要，摘要介绍腾讯新闻与优质创作者协作，扶持高质量、专业内容及垂直领域专家；PDF 正文抓取超时，未做全文核对。 | 仅作为供给主体模拟的补充背景；报告期信息不是本项目的当日运营数据。 |
| [腾讯大数据：腾讯推荐——让您的系统瞬间个性化](https://developer.cloud.tencent.com/article/1030012) | 原始发表日期为 2015-06-25，社区同步日期为 2018-01-26；以腾讯新闻为例介绍新闻信息、点击、浏览、评论的实时上报及推荐请求，也讨论曝光、点击和效果监控。 | 模拟分发、消费和互动链路。属于历史说明，不能据此认定 2026 年仍使用文中平台。 |
| [腾讯较真实时辟谣](https://vp.fact.qq.com/home) | 官网样例有查证者、日期和“谣言”“有失实”“尚无定论”“分情况”等结论，提供搜索和真/假筛选；可见样例主要来自 2022–2023 年。 | 给事实核查专题留出主题入口。v7 只生成专题维度，尚未生成主张、查证结论或证据表。 |
| [腾讯云 Oceanus：CREATE TABLE](https://cloud.tencent.com/document/product/849/18034) | 文档更新时间为 2023-06-21；区分数据自带时间戳与处理时间，说明 Watermark 容差及等待时间、晚到数据的权衡。 | 使用事件时间、快照时间、水位和修订号表达窗口状态。这里只借鉴通用工程语义。 |
| [腾讯云 WeData：数仓分层配置](https://cloud.tencent.com/document/product/1267/81342) | 文档更新时间为 2025-12-12；说明 ODS 原始层、DWD 清洗明细层、DWS 汇总层、ADS 应用视图等逻辑分层。 | 以层次职责组织模拟数据，不引入 WeData 或其他云产品运行时。 |

腾讯新闻的真实埋点协议、现有内部表名、技术栈、事件主键、热榜公式、实验分流、推送归因窗口、数据规模和生产 SLA 仍未知。`recommend/search/hot_board/push` 等名称是本项目的模拟场景；4 类内容形态、4 个板块和 12 个主题也是本项目选择，不能作为官方当前产品清单。腾讯云产品文档、腾讯游戏广告实践、腾讯看点或音乐架构都不能当成腾讯新闻内部实现的证据。

## 2. 数据包范围与默认配置

默认数据包已生成，位于仓库根目录的 `output/warehouse/tencent-news-v7-20261014/`。实际生成参数、文件行数和校验摘要以其中的 `manifest.json` 为准。

| 项目 | 当前实现 |
| --- | --- |
| 独立 PostgreSQL Schema | `news_sim_v7` |
| 租户 A | `11111111-1111-4111-8111-111111111111` |
| 租户 B | `33333333-3333-4333-8333-333333333333` |
| 每租户新闻数 | 默认 120；要求为 12 的倍数，范围 12–1200 |
| 历史天数 | 默认 7；范围 1–30 |
| 历史起点 | 默认 `2026-10-07`，北京时间 `00:00:00+08:00` |
| 历史终点 | 起点加 `days`；默认 `2026-10-14T00:00:00+08:00`，右边界不包含在历史完整小时内 |
| 开放小时 | 从历史终点开始，再生成一个进行中的小时 |
| 随机种子 | 默认 `20261014`；整数范围 0–2147483647 |
| 数据方法 | SHA-256 确定性扰动、整数聚合、Decimal 历史均值 |
| 用户行为明细 | 不生成或保存用户、设备、会话标识和原始行为记录 |
| 企业连接与模型调用 | 生成器没有企业数据库、推理、Embedding 或向量库连接 |

同一 `news_id` 可以出现在两个租户中，主键和关联必须包含 `tenant_id`。仅按 `news_id` 查询或删除，会把跨租户碰撞误认为同一内容。这个设置专门用于验证租户边界。

数据包包含：

```text
manifest.json                  配置、版本、时间范围、各表行数与 SHA-256
preview.json                   每张表最多前三条示例
scenarios.json                 业务和质量案例目录
13 个 <table_name>.jsonl        各表聚合记录
warehouse.sql                  DDL、COPY 数据和事务提交组成的可导入文件
schema.sql                     本次导出的 DDL 副本
generator.py                   本次导出的生成器源文件副本
```

Manifest 另记录 DDL、生成器源文件及 `warehouse.sql` 的 SHA-256，标注 `synthetic=true` 和 `standalone_fixture_not_connected_to_agent_or_vector_store`。`schema.sql/generator.py` 是该次产物的来源副本；当前仓库源码以后变化时，仍可据此核对原包。`generator.py` 的包内副本用于追溯，不是已装配好依赖路径的独立 CLI，应使用仓库的 CLI 入口重建。摘要用于核对数据包身份和复现；不代替权限校验或生产来源认证。

本次默认包的清单核对值：

| 表 / 投影 | 记录数 |
| --- | ---: |
| `dim_publisher` / `dim_channel` | 12 / 12 |
| `dim_news` | 240 |
| `window_snapshot` | 340 |
| `news_metric_channel_hourly` | 122400 |
| `news_metric_hourly` | 40800 |
| `news_metric_5min` | 22320 |
| `news_metric_baseline_hourly` | 40560 |
| `content_change_event` | 60 |
| `content_tombstone` | 40 |
| `index_cleanup_task` | 240 |
| `board_snapshot` / `board_item` | 2 / 24 |
| `news_behavior_aggregate`，完整且可用的 18 列投影 | 33600 |

数据集摘要为 `67ad183c97df43318564fa8bb8ca541ba138f04e1a7b41469f205051146db91a`，生成器摘要为 `9434164476df8a512cc19a0113f94c07e01dc3f773b6bb1e615a5f631ce84638`，DDL 摘要为 `30c4bde75e3b4f5da6f2d2be48a58d726050281dff7c1554fecc1429d6198560`。33600 是独立 PostgreSQL 导入后的视图结果，不是另一个持久表或额外用户行为数据。

验收后另保存 [validation.json](../../output/warehouse/tencent-news-v7-20261014/validation.json) 与 [postgres-observed.json](../../output/warehouse/tencent-news-v7-20261014/postgres-observed.json)，记录验收范围、数据集摘要及数据库只读观察结果。这两个文件属于验收旁证，由验收过程写入，不是生成器每次自动产生的文件。

## 3. 业务维度

### 3.1 渠道、来源与内容形态

| channel_id | 名称 | platform |
| --- | --- | --- |
| `recommend` | 推荐信息流 | `app` |
| `channel` | 栏目页 | `app` |
| `search` | 搜索结果 | `app` |
| `hot_board` | 热榜入口 | `app` |
| `push` | 消息推送 | `app` |
| `web` | 网页入口 | `web` |

每篇新闻确定性选择 3 个渠道，整套数据覆盖上述 6 个渠道。渠道表不是曝光或推送日志，`push` 也不意味着真实发出通知；发送、到达、打开和归因事件尚未实现。

来源类型为 `newsroom`、`wire`、`regional_media`、`vertical_media`、`creator`、`fact_check`，每租户生成 6 个合成来源。它们只表达供给主体差异，没有真实机构身份、作者认证、采购合同或版权授权。

`content_format` 支持 `article`、`video`、`gallery`、`live_text`。兼容旧契约的 `content_type` 仍只用 `article/video`：视频映射为 `video`，其他三种形态映射为 `article`。`live_text` 是文字直播形态，不生成实时直播流；`gallery` 不生成图片文件；整个 v7 数据包也不含新闻正文、视频转写或向量。

### 3.2 4 个板块与 12 个主题

| category | primary_topic |
| --- | --- |
| 科技 | 人工智能、数字生活、航天科普 |
| 财经 | 宏观经济、产业公司、消费市场 |
| 体育 | 球类赛事、全民健身、综合赛事 |
| 社会 | 城市民生、天气出行、事实核查 |

标题统一带 `【合成】` 标记。`dim_news.event_id` 是模拟新闻主线标识；`content_change_event.event_id` 则是内容生命周期变更的幂等事件 ID，二者职责不同，不能混用。

主题、形态、来源和案例当前按新闻序号取模分配，存在模板相关性，并非独立采样或真实业务分布。所有新闻均设定为历史窗口开始前发布，尚未模拟窗口中新增发布、作者增长、跨主题迁移和主题多标签。

## 4. 13 张表与逻辑职责

v7 不保存用户级 ODS/DWD。逻辑上，内容变更和原榜快照表达来源侧输入，维表提供公共维度，5 分钟和小时表保存聚合结果，查询视图承担应用投影。不能因借用了分层名称就把聚合表宣称为真实埋点明细层。

| 表 | 一行粒度 / 主键 | 主要字段和职责 |
| --- | --- | --- |
| `dim_publisher` | 租户 × 来源；`tenant_id,publisher_id` | `publisher_name,publisher_kind,synthetic`，供给主体维度。 |
| `dim_channel` | 租户 × 渠道；`tenant_id,channel_id` | `channel_name,platform,synthetic`，渠道字典。 |
| `dim_news` | 租户 × 新闻；`tenant_id,news_id` | 标题、旧 `content_type`、新 `content_format`、板块、主主题、事件、来源、发布时间、正文版本、生命周期修订、当前状态与状态生效时间。 |
| `window_snapshot` | 租户 × 小时 × 快照修订 | `window_end,watermark_at,snapshot_at,completeness,quality_status`，迟到修订统计及口径/数据版本。 |
| `news_metric_channel_hourly` | 租户 × 新闻 × 渠道 × 小时 × 修订 | 各渠道小时计数和单渠道小时 UV，关联同租户新闻、渠道及窗口。 |
| `news_metric_hourly` | 租户 × 新闻 × 小时 × 修订 | 全渠道小时计数和跨渠道去重 UV；`uv_method=synthetic_interval_union_v1`。 |
| `news_metric_5min` | 租户 × 新闻 × 渠道 × 5 分钟桶 × 修订 | 供最后闭合小时及开放小时核对的细粒度聚合；不是原始用户明细。 |
| `news_metric_baseline_hourly` | 租户 × 新闻 × 小时 | 四项历史同小时均值、`sample_count` 和 `baseline_version`。 |
| `content_change_event` | 租户 × 生命周期事件；新闻修订唯一 | `withdraw/restrict/correct`、`from_version,to_version,lifecycle_revision,new_status,effective_at,received_at` 与原因码。 |
| `content_tombstone` | 租户 × 新闻 | 撤稿/受限屏蔽台账，含 `blocked_through_version,lifecycle_revision,event_id,status,effective_at,policy_version`。 |
| `index_cleanup_task` | 租户 × 任务；同事件 × 目标唯一 | 来源、目标、动作、待清理版本上界、生命周期修订、状态、重试次数和 `mapping_required`。 |
| `board_snapshot` | 租户 × 榜单 × 快照 | 模拟原榜快照时间、榜单口径及 `synthetic_original_board` 来源标记。 |
| `board_item` | 租户 × 榜单 × 快照 × 新闻 | `source_rank,source_position,source_score`，保留并列排名、空缺及原始位置。 |

DDL 通过复合主键和外键约束同租户关联。计数必须非负，点击不能超过曝光，有效消费与 UV 不能超过点击，视频完成不能超过视频开始，互动总次数必须等于点赞、评论、分享、收藏和关注之和。5 分钟桶要求秒为 0 且分钟能被 5 整除。非完整窗口可以保留，但不能被完整小时应用视图消费。

## 5. 5 分钟到小时的指标口径

`_channel_buckets()` 先生成渠道内 5 分钟聚合，`_hour_metrics()` 将其归并到渠道小时和新闻小时。默认闭合小时有 12 个桶，开放小时只生成 7 个桶，起点依次为 00、05、10、15、20、25、30 分。

完整历史小时的小时指标都由相同的 5 分钟计算逻辑生成，但为控制产物体积，`news_metric_5min.jsonl` **只导出最后一个闭合小时的两次修订及开放小时**。不能把这个文件当成全部 7 天的 5 分钟数据，也不能要求历史所有小时都能直接从已导出的桶文件回算。其余历史小时可按同参数调用生成逻辑复算。

可加指标包括：

```text
impressions, clicks, total_duration_seconds, effective_consumptions,
interactions, likes, comments, shares, favorites, follows,
negative_feedback, video_starts, video_completions
```

这些是次数或总量，不是人数。互动不包含负反馈，负反馈另列。`effective_consumptions` 是按合成比例计算的有效消费次数：普通案例约为点击的 72%，消费退化案例约为 35%；视频完成约为有效消费的 75%。消费秒数使用视频每次点击 65 秒、其他形态 32 秒的模拟规则。它们不是腾讯新闻真实有效阅读阈值或视频完成定义。

每日 8、9、12、19、20、21 时增加消费波峰，体育周末加成，部分案例在历史中点后升温或回落。曝光升高、低样本、零点击和负反馈升高均通过确定性规则生成。LLM 不参与这些数字计算。

### 5.1 UV 必须按并集计算，禁止 SUM

生成器临时构造半开整数区间 `[left,right)`，用 `union_size()` 计算重叠区间并集长度，模拟同一新闻同一小时内的重复消费及跨渠道重叠。区间不是企业用户 ID，不写入 JSONL、SQL 或 Manifest；产物只保存聚合人数。

正确关系是：

```text
渠道小时 UV = 该渠道各 5 分钟临时区间的并集大小
新闻小时 UV = 该新闻所有渠道、所有 5 分钟临时区间的并集大小
```

禁止用 `SUM(5分钟 unique_users)` 得到渠道小时 UV，禁止用 `SUM(渠道 unique_users)` 得到新闻小时 UV，也禁止把新闻小时 UV 跨小时相加当作整日人数。现有区间在每个新闻/小时重新开始，不能跨新闻或跨小时恢复去重。扩大人数统计范围需要新的企业聚合合同、合规去重能力或专门的模拟口径，而不是对已有 UV 求和。

### 5.2 比率从总量计算，零分母保留 NULL

`news_operational_hourly` 使用 SQL `NULLIF(分母,0)`：

| 字段 | 计算式 |
| --- | --- |
| `ctr` | `clicks / impressions` |
| `effective_consumption_rate` | `effective_consumptions / clicks` |
| `average_duration_per_click` | `total_duration_seconds / clicks` |
| `interaction_event_rate` | `interactions / clicks` |
| `video_completion_rate` | `video_completions / video_starts` |
| `negative_feedback_event_rate` | `negative_feedback / clicks` |

多桶比率应先汇总分子、分母再相除，不能直接平均各桶 CTR。零点击是观测到的 0；分母为 0 导致的比率不可定义是 NULL；没有历史样本的基线也是 NULL。三种状态不能混为一谈。

## 6. 水位、修订与基线

`event_time` 表示指标小时或 5 分钟桶的业务时间起点，`watermark_at` 表示合成窗口水位，`snapshot_at` 表示该修订形成时间。这些字段模拟实时数仓语义；生成器是一次性离线出包，不是正在消费 Kafka/Flink 的流任务。

默认时间范围下的特殊窗口：

| 窗口 | snapshot_revision | 状态 | 模拟水位 |
| --- | --- | --- | --- |
| 普通历史闭合小时 | 1 | `complete/passed` | 小时终点后 10 分钟 |
| `2026-10-13 23:00+08:00` 最后闭合小时 | 1 | `incomplete/pending` | 起点后 45 分钟 |
| 同一最后闭合小时 | 2 | `corrected/passed` | 小时终点后 10 分钟 |
| `2026-10-14 00:00+08:00` 开放小时 | 1 | `incomplete/pending` | 起点后 30 分钟 |

快照时间均在水位后 5 分钟。最后闭合小时 rev1 仍有 12 个桶，但水位尚未闭合，因此“记录已存在”不能代替“窗口完整”。rev2 对每个新闻的 3 个渠道、每渠道最后 3 个桶分别补 7 次点击，共每新闻补 63 次点击；`late_corrected_bins` 和 `late_added_clicks` 是当前租户本次修订统计。

查询先选同租户同小时最高 `snapshot_revision`，再判断 `completeness` 和 `quality_status`。如果最新修订不完整，应阻断该小时，不能回退旧完整修订。当前应用视图只接受 `complete/corrected` 且 `passed` 的最新修订；开放小时不进入这个视图。

基线版本为 `sim-prior-3-days-same-hour-v1`：对同租户、同新闻、当前小时之前 1、2、3 天的相同时段求算术均值，只使用数据范围内已有历史。`sample_count` 为 0–3；0 个样本时四项基线均为 NULL，有 1–2 个样本时保存实际均值和样本数，不补造第三个样本。均值使用 Decimal，四舍五入到两位小数，JSONL 用十进制字符串，SQL 用 `numeric(20,2)`。

当前只生成曝光、点击、有效消费和互动的四项基线，不生成基线 UV 或时长。计算历史基线使用历史 rev1；v7 的修订仅出现在最后闭合小时，不影响包内当前三日基线取样。未来存在更早窗口修订时，基线必须绑定历史快照修订和截至时间，不能直接沿用这个简化规则。

## 7. 5 个视图与旧 18 字段合同

| 视图 | 当前行为与用途 |
| --- | --- |
| `latest_window_snapshot` | 每租户每小时只选最高修订，不自动剔除不完整状态。 |
| `eligible_news` | 当前 `status=published` 且不存在同租户新闻 tombstone；即时资格判断与物理清理独立。 |
| `news_behavior_aggregate` | eligible 新闻 × 最新且完整/更正的小时指标，LEFT JOIN 基线，提供旧 18 字段名称与形状。 |
| `news_operational_hourly` | 最新窗口的运营指标、比率、状态、形态和版本；保留不完整窗口与受限/撤稿指标，供隔离运维核对，不是内容服务入口。 |
| `current_board_item` | 按当前内容资格过滤原榜，保留 `source_rank/source_position/source_score`，不重新编号。 |

18 字段为：

```text
tenant_id, news_id, event_time, title, content_type, category, source, publish_time,
impressions, clicks, unique_users, total_duration_seconds, effective_consumptions, interactions,
baseline_impressions, baseline_clicks, baseline_effective_consumptions, baseline_interactions
```

这个兼容只表示列形状，**不表示现有 Agent 已可查询**。新视图位于 `news_sim_v7.news_behavior_aggregate`，旧白名单允许的是既有 `dw.news_behavior_aggregate`；不得通过改名或替换现有 schema 绕过冻结版本/哈希验证。将来接入需要新的数仓契约注册、场景配置、SQL 编译/AST 白名单、身份与窗口绑定、MetricSource 适配和端到端测试。

兼容视图不暴露 `content_format`、`sample_count` 或修订元数据，新的 Agent 适配仍需从可信 Port 获得这些质量证据，避免将缺基线当成 0。当前 `eligible_news` 使用最终当前状态，撤稿和受限内容会从整个历史应用投影排除；它不是“截至某历史时点的资格”查询，历史审计应读取受控台账并另建时点合同。

模拟原榜来源为 `synthetic_original_board`，不是项目 CTR/热度计算榜，也不是腾讯新闻真实原榜。每租户生成一个快照，取最前 12 个案例反向放置；保留并列排名和排名空缺。生命周期过滤造成位置缺口时仍保留原始排名，不能用 `row_number()` 改写为一个看似完整的新原榜。双来源查询 API 尚未接线。

## 8. 场景目录与当前覆盖

每 12 个新闻案例重复一组：常规消费、撤稿后屏蔽、权限受限、正文更正与旧版本清理、事件升温、事件回落、低样本、零点击、曝光升高但点击不同比增长、有效消费退化、负反馈升高、事实核查专题。

`scenarios.json` 另外列出最后闭合小时迟到修订、开放窗口、跨渠道 UV、缺失基线及原榜并列/空缺，共 17 类案例。案例目录用于核对既有生成逻辑，不是已经接入 Evaluation Gate 的正式评测集。升温、回落、有效消费退化等是确定性模式覆盖；本次没有据此声明全部业务质量或全部场景的统计真实性都已验收。

撤稿与受限案例当前 `content_version=1,lifecycle_revision=2`；更正案例 `content_version=2,lifecycle_revision=2,status=published`。`corrected` 是窗口完整性状态，不是新闻当前状态。生命周期事件在历史终点前 10 分钟生效，前 9 分钟接收；此处只有合成事件台账，没有真实 CMS 事件消费、签名验证或按接收时间推进的 Worker。

## 9. 撤稿、更正与向量清理边界

### 9.1 推荐执行合同：先禁止使用，再异步清理

向量删除不是撤稿生效的前提。以后真实接入应使用如下有界链路：

```text
可信 CMS/内容台账变更事件
→ 验证租户、来源、事件身份及生命周期修订
→ 事务内更新权威内容状态与 tombstone，登记清理任务/Outbox
→ 所有读取入口立即检查当前资格，禁止将已撤稿内容送入证据或模型
→ 异步按新闻、来源、版本映射查找知识文档和 chunk
→ 删除/停用向量与文本投影，失效缓存，处理派生产物
→ 验证不存在被禁版本、登记审计，任务才标记 verified
```

其中第一段权威状态及 tombstone 的即时读屏蔽必须持续生效，即使向量服务故障、删除超时或任务重试仍如此。后台物理清理失败不能让前台恢复使用。检索资格应在召回之后和模型上下文提交之前再次核验，处理“召回时有效、生成前撤稿”的竞态；内容详情、直接 chunk 查询、聊天追问、缓存命中、报告引用及视频读取也要经过同一规则。

### 9.2 正文版本与生命周期修订必须分开

`content_version` 表示正文版本，`lifecycle_revision` 表示内容状态事件顺序。撤稿可以只提升生命周期修订而不改正文，因此不能只比较正文版本；晚到的旧 `published` 事件必须被更高生命周期修订拒绝，防止撤稿后复活。

复刊应是可信来源明确发出的新生命周期事件，包含较高修订和确认后的可用正文版本，经过批准的状态迁移后解除当前屏蔽。当前 v7 没有复刊事件类型，也没有自动解除 tombstone 的实现；较新 `content_version` 自身不足以解除撤稿。

异步任务绑定 `tenant_id,content_source,news_id,event_id,lifecycle_revision,through_version`。更正任务只清理 `from_version`，撤稿/受限任务清理到事件确认的 `to_version`。执行前需重新核对映射及当前版本：旧任务可以移除它绑定的旧版本投影，不能因为只匹配 `news_id` 就删除更正/复刊后的新版；也不能由旧任务改写较新的权威状态。跨租户、跨来源及未知映射须失败关闭。

### 9.3 各存储当前能力和缺口

| 存储/能力 | 现有实现核对 | 接入撤稿仍需完成 |
| --- | --- | --- |
| PostgreSQL 知识台账 | `PostgresKnowledgeStore` 有文档状态查询、入库更新和搜索；当前没有撤稿停用/删除入口。 | 绑定数仓 `news_id/content_source/content_version` 到知识文档、chunk 和租户；事务更新可用状态、tombstone 与清理任务；所有检索加入最终资格核验。 |
| Milvus | `MilvusVectorIndex.delete()` 已可按 tier 和 chunk IDs 删除，`contains()` 可验证。当前删除/验证接口本身不接收租户，不能直接拿一个新闻 ID 去删。 | 先查可信且版本化的 chunk 映射，校验租户、来源、collection/tier 和版本；补租户约束或严格的调用边界，再删除并验证。 |
| BGE/HNSW 快照 | `BgeSnapshotVectorIndex` 为只读，`upsert/delete` 明确抛出 `PermissionError`。 | 在服务资格层立即应用权威 denylist；从已排除内容的可信语料重建新版本快照，验证哈希和覆盖后切换。当前 v7 未新增运行时 denylist，也未重建快照。 |
| 保留治理 | `retention.py` 已有 `RetentionDryRunService`、`RetentionPolicy` 和 `NewsRetentionService`；默认 dry-run，真实归档/删除要求 `approved_by`，删除向量后用 `contains()` 核验，再调用文本、缓存和对象 Port 并记审计。 | 仍缺与 main/Worker 的执行接线、具体候选仓库和各清理 Port Adapter、状态事件与版本映射、失败恢复及生产权限验收。不能因领域服务存在就宣称撤稿端到端已经完成。 |

`RetentionPolicy` 将合法保全置于撤稿/过期规则之前。保全限制物理删除，不应解除内容服务层的撤稿屏蔽；这两项职责需要在真实接入中明确分开，不能把“保留供审计”理解成“可继续推荐”。

### 9.4 模拟任务不会删除真实库

v7 为每个 `withdraw/restrict/correct` 事件生成四类任务：

| target | action |
| --- | --- |
| `postgres_knowledge` | `deactivate_chunks` |
| `milvus` | `delete_versioned_chunks` |
| `bge_snapshot` | `denylist_then_rebuild` |
| `retrieval_cache` | `invalidate` |

新任务全部 `status=pending,attempt_count=0,mapping_required=true`。任务状态枚举还允许 `running/retry/verified/failed`，但生成器不推进状态，不调用删除 API，不申请企业连接，也不猜测真实文档或 chunk ID。`pending` 表示模拟待办，不是已经清理；`mapping_required` 表示缺映射，不能自行当作 false。

### 9.5 清理范围不能停在向量

以后应建立基于新闻/内容版本的派生血缘，并给下列目标定义处理合同：

| 目标 | 推荐边界 |
| --- | --- |
| 检索和正文缓存 | 失效同租户新闻缓存，禁止旧缓存复用；缓存加载仍核对当前资格。 |
| QA 和关联摘要 | 知识 QA、摘录和摘要引用被撤稿来源时，停用/重算对应版本，不保留为未注明出处的事实。 |
| 视频派生内容 | 转写、字幕、摘要、分段向量及媒体对象均绑定原内容版本，按同一资格处理。 |
| 分析/写作报告 | 保留必要审计记录，标记引用失效或更正，阻止把失效引用继续用作当前证据；已发布内容的更正/撤回需要独立人工流程。 |
| 会话与 Memory | 历史回答与压缩摘要保留来源引用和失效标记；后续追问重新核对，不将旧会话摘要当作当前权威事实。物理清除范围需另有保留策略。 |
| 备份/对象副本 | 即时服务拒绝与备份到期清理分开登记。定义恢复时重新应用 tombstone、保留期限与保全策略，不承诺即时抹除所有备份。 |

这些派生清理目标尚未建入 v7 的四类模拟任务，属于后续范围，不能声称数据包已完成整个传播链的删除。

### 9.6 已知读取缺口，尚未修复

`HotNewsEnrichmentService._hydrate_related_news()` 在关联候选缺少正文时直接返回原候选，可能保留召回中的旧摘要；当前流程缺少统一最终生命周期资格校验。聊天 `ConversationTools` 也可以直接查询知识索引，其租户/工具权限检查不等于撤稿状态检查。只让数仓视图排除撤稿新闻，不能覆盖这两条读取路径。

后续需对关联召回、候选 hydration、聊天检索、BGE `get_article()`、缓存命中及模型提交建立同一可信状态门禁，并用“缺正文但有旧向量摘要”“并发撤稿”“旧任务晚到”“新版不得误删”验证。此次新增文档和模拟包没有修改这些现有读取流程。

## 10. 从仓库根目录生成与导入

生成只依赖 Python 标准库，无须启动数据库、Temporal、模型服务或读取 `.env`：

```bash
python3 agent/tools/generate_tencent_news_warehouse.py \
  --output output/warehouse/tencent-news-v7-20261014 \
  --news-per-tenant 120 \
  --days 7 \
  --start-date 2026-10-07 \
  --seed 20261014
```

输出目录必须不存在；已有目录会拒绝生成。需要再生成时选新的目录，不能通过删除旧目录或覆盖已有数据来掩盖版本变化。`SimulationConfig` 检查数字范围、12 的倍数及严格 `YYYY-MM-DD`；非法配置返回 CLI 退出码 2。

`export_dataset()` 在输出目录父级创建临时目录，写完 JSONL、SQL、Manifest、预览和场景后重命名为目标目录。JSONL 使用确定性序列化，SQL 使用 PostgreSQL text COPY 转义，数据字符串不能变成 SQL 指令。

导入仅使用新建的独立数据库，并使用数据包中的 `warehouse.sql`。不要导入正在运行 Agent 的数据库，也不要将新 schema 改成 `dw`：

```bash
createdb newsagent_sim_v7_20261014
psql -X -v ON_ERROR_STOP=1 -d newsagent_sim_v7_20261014 \
  -f output/warehouse/tencent-news-v7-20261014/warehouse.sql
psql -X -v ON_ERROR_STOP=1 -d newsagent_sim_v7_20261014 \
  -f agent/docs/sql/tencent-news-simulation-v7-checks.sql
```

这些命令是独立本地库的示例，不包含连接密钥。远端数据库须采用用户已有的合规连接方式，不在文档或聊天中复制 `.env` 敏感值。

生成 SQL 开启 `ON_ERROR_STOP`，以一个 `BEGIN/COMMIT` 事务导入，设置事务时区 `Asia/Shanghai`。DDL 使用 `CREATE SCHEMA news_sim_v7`，没有 `IF NOT EXISTS` 或 `DROP`；已有同名 schema 会报错，整笔导入不会覆盖旧数据。已有数据库或目录冲突应更换独立目标，不通过删库、删 schema 解决。

导入后的只读核对示例：

```sql
SELECT event_time, snapshot_revision, completeness, quality_status
FROM news_sim_v7.latest_window_snapshot
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'
ORDER BY event_time DESC
LIMIT 3;

SELECT news_id, title, clicks, unique_users, baseline_clicks
FROM news_sim_v7.news_behavior_aggregate
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'
  AND event_time = '2026-10-13T23:00:00+08:00'
ORDER BY clicks DESC, news_id
LIMIT 10;

SELECT target, status, mapping_required, count(*)
FROM news_sim_v7.index_cleanup_task
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'
GROUP BY target, status, mapping_required;
```

## 11. 完整调用链、依赖和测试入口

当前已实现的生成调用链：

```text
上游：根目录 CLI 参数
→ agent/tools/generate_tencent_news_warehouse.py: main()
→ SimulationConfig：类型/范围/日期验证，生成北京时间起止边界
→ export_dataset()：加载独立 DDL，建立临时输出目录
→ iter_rows()：按 13 张表及两租户生成
  → _content() / CHANNELS / TOPICS：新闻与公共维度
  → _channel_buckets()：5 分钟合成计数和临时区间
  → union_size() / _hour_metrics()：渠道/总小时聚合与 UV 并集
  → _versions() / _snapshot()：窗口修订及质量状态
  → _changes()：内容变更、tombstone 与 pending 模拟清理任务
  → 基线分支：前三日同小时 Decimal 均值与样本数
  → board 分支：模拟原榜和原始排名位置
→ copy_value()：SQL COPY 安全转义
→ _file_sha256() / scenario_catalog()：文件摘要和案例目录
→ 下游：JSONL、warehouse.sql、manifest.json、preview.json、scenarios.json、来源副本
→ 可选 psql：独立 PostgreSQL 库 → news_sim_v7 的 13 表与 5 视图
```

核心生成器为 `agent/app/warehouse_simulation/tencent_news.py`，DDL 为 `agent/docs/sql/tencent-news-simulation-v7.sql`。外部依赖只在可选导入/验证时涉及 `psql/PostgreSQL`；生成本身仅用 `argparse/json/hashlib/tempfile/datetime/decimal/pathlib/dataclasses` 等标准库。测试依赖 pytest，运行时没有 LLM、Embedding、Milvus、BGE、Redis、Temporal、腾讯云客户端或企业服务依赖。

CLI 配置项是 `--output/--news-per-tenant/--days/--start-date/--seed`；Schema、租户、频道、主题、案例和口径版本在生成器/DDL 中版本化。没有为了 v7 新增企业凭证环境变量或切换现有 Agent 配置。

专项测试入口：

```bash
cd agent
python3 -m pytest tests/test_tencent_news_warehouse_simulation.py -q
```

新增专项测试覆盖表合同、同租户关联、确定性重建、最后小时桶与小时计数对账、UV 重叠、零值与空基线、窗口修订、生命周期任务版本、COPY 转义及导出冲突/失败清理。2026-10-14 的当前源码专项运行 41 项通过；相关既有数仓、语料和 retention 回归通过，其中 3 项依赖型测试跳过。

本次正式 `warehouse.sql` 已在全新的隔离 PostgreSQL 16 数据库成功导入，`agent/docs/sql/tencent-news-simulation-v7-checks.sql` 实际执行通过，核对了渠道/小时与已导出 5 分钟的可加指标、UV 边界、内容/窗口门禁、基线历史覆盖、清理任务范围及 18 列投影。检查脚本用 `BEGIN READ ONLY` 执行可信运营检查，含 `DO` 块，不属于模型可执行 SQL 白名单。重复导入在任何数据写入前因 `schema news_sim_v7 already exists` 拒绝，`psql` 退出码为 3；随后再次只读核对行数不变，兼容视图中撤稿/受限内容泄漏数为 0，记录见 [验收文件](../../output/warehouse/tencent-news-v7-20261014/validation.json)。临时 PostgreSQL 容器已停止并自动移除，现有服务与真实向量库未改动。

上述结果验证合成数据的确定性、关系约束、聚合与隔离导入，不代表企业真实数据口径、全部业务场景统计、模型质量、生产流式能力或真实撤稿删除验收。

现有删除能力的专项入口包括 `tests/test_news_retention.py`、`tests/test_milvus_vector_index.py`、`tests/test_bge_snapshot_index.py`，读取链路入口包括 `tests/test_hot_news_enrichment.py`。这些旧测试不能代替新的撤稿端到端验收；真实依赖测试需要独立环境和明确启用。

## 12. 未完成 TODO

1. **Agent 接入**：注册 v7 的独立 schema/哈希/场景，更新受限编译与白名单，绑定租户、窗口和修订证据，复用现有热点 Workflow/MetricSource；验证同一次运行的 SQL、候选、权威指标、排行和报告一致。
2. **原榜接入**：新增可信榜单 Adapter 与双来源查询合同，区分模拟原榜/企业原榜/项目计算榜，保留来源排名及快照，补源侧覆盖和过期规则。
3. **内容映射**：建立数仓新闻到正文、知识文档、chunk、Embedding 版本及视频产物的同租户版本映射。v7 目前只有合成标题，不能直接用来验证正文或向量质量。
4. **撤稿即时门禁**：实现可信状态事件消费、修订比较、tombstone、显式复刊、模型提交前终检，以及关联召回和聊天的状态校验；修复缺正文仍返回旧摘要的读取缺口。
5. **异步清理**：在现有 retention 领域边界上补具体 Adapter、审批、Outbox/Worker、幂等重试、租户校验、旧版本清理和验证；Milvus 补映射与调用边界，BGE 补运行时 denylist 和新快照重建切换。
6. **派生血缘**：补缓存、QA、视频、报告、会话摘要、Memory、对象副本和备份的失效/更正/保留策略，保证恢复备份后不能复活撤稿内容。
7. **实时扩充**：目前不消费流，不提供持续时钟；未来模拟乱序、重复事件、上游断流、水位停滞、多次修订和恢复时，需明确事件幂等和截至时间，不把离线出包当作生产实时链路。
8. **业务扩充**：按真实匿名聚合合同补新发布内容、搜索查询聚合、推送漏斗、作者/媒体质量、主题多标签和实验版本；减少当前序号模板相关性。
9. **统计口径**：企业有效阅读/播放、曝光可见性、归因窗口和跨小时去重仍待定义；历史基线绑定修订，缺失/低样本保留质量证据。真实口径与规则变更需版本化并遵守人工审批边界。
10. **验收与权限**：独立 PostgreSQL 导入和生成契约测试已通过；继续完成 Agent 接入后的租户隔离、竞态、故障恢复、生产只读权限和删除验证。尚未取得的结果不写成已通过，也不将合成模型 Port 的链路测试称作企业模型质量验收。

长期架构继续由本项目 Python 实现。这里借鉴 `DemoMD/TAOTIAN.md` 的有界执行、状态事实源、幂等、失败阻断、版本化验证与人工决策原则，不复制其电商组件或自动训练权限。普通模拟设计不会自动修改持久项目上下文，也不授予真实推送、生产配置变更、自动发布或真实库删除权限。
