# NewsAgent 企业 TDSQL 数仓接入与字段设计

整理日期：2026-10-23。用途：确认企业字段映射、准备 SQL 与指标评测、说明数据驱动分析的输入。

**已确认背景：用户于 2026-10-23 确认企业侧使用 TDSQL-A、PostgreSQL 兼容。具体内核版本、物理表名、连接方式和企业指标定义尚未提供。本文不是腾讯内部数据字典，也不是已经执行的建表方案。**

本文给出 Agent 需要的业务合同和建议补充项；[当前结构逐字段字典](enterprise-warehouse-current-fields.md) 列出本地 v8 的 13 张表、183 个物理字段及 4 个视图。两份文档配合使用：先确认核心 18 列，再按业务需要补齐快照、基线和内容状态。

## 1. TDSQL 背景如何影响方案

腾讯云数据库团队介绍的 TDSQL-A 面向实时分析，采用 PostgreSQL 技术体系和 MPP 架构，提供列式及行列混合存储。用户已确认产品分支和兼容方向；具体内部内核版本、启用特性和性能仍以实际实例为准。[腾讯云数据库团队介绍](https://cloud.tencent.com/developer/article/1943222)

接入设计沿 PostgreSQL 方言推进，可优先复用现有 SQL 编译、AST 校验和参数化查询合同；不再保留 MySQL 方言的并行方案。实际通过 PostgreSQL 协议直连还是企业 SQL 网关/SDK，由内部接入规范决定。兼容方向确定不等于本地 DDL 的所有触发器、约束和分布式执行行为已验证。

| 层次 | 本项目的职责与状态 |
| --- | --- |
| 企业 TDSQL-A 数仓 | 已确认 PostgreSQL 兼容；企业维护采集、清洗、去重和聚合，项目消费批准的聚合视图或指标接口。具体企业 DDL 待映射 |
| 数仓接入层 | 将企业字段映射为 NewsAgent 的指标合同，校验身份、窗口、版本、类型、质量和重复行；当前仓库尚未提供经企业验收的 TDSQL Adapter |
| Agent 应用库 | 保存运行、审批、报告、证据引用等业务状态；使用 PostgreSQL 的本地实现不等于企业数仓也已迁移为该实例 |
| 本地独立模拟数仓 | `news_sim_v8.*`，13 张表、183 列；真实公开正文配合明确标记的合成行为数据 |
| 本地 Agent 查询库 | `dw.*`，3 张业务表加 1 张 Schema 台账表；对 Agent 暴露 18 列只读视图。它是裁剪后的接口结构，不是独立 v8 的全部物理表 |

不必为了接入 Agent 重建企业的 13 张表。已有企业表或接口能提供同样的可信语义即可；字段改名、单位换算和版本筛选可以由受控视图或 Adapter 完成。

## 2. 首先对齐的 18 个字段

当前合同：`news-warehouse-v7`，视图：`dw.news_behavior_aggregate`。

**唯一粒度：一个租户、一篇新闻、一个完整小时。** 当前实际主键对应 `(tenant_id, news_id, event_time)`；`content_type` 是新闻属性，不允许以同一新闻出现两种类型来规避重复行校验。`event_time` 表示小时开始时间，不是原始行为时间，也不是入库时间。区间统一使用 `[window_start, window_end)`。

以下类型是当前 Agent 本地 PostgreSQL 合同；企业源字段名全部待核实。表中“非空”指对外合同要求，不能用默认值掩盖企业源字段缺失。

| 字段 | 当前类型 / 可空 | 含义、单位与来源 | 计算及评测约束 |
| --- | --- | --- | --- |
| `tenant_id` | `uuid` / 非空 | 业务访问域，由可信服务身份映射；不是读者 ID | 不由模型填写；所有关联、筛选与缓存都带此维度。企业使用 app_id/business_id 时需显式映射 |
| `news_id` | `text` / 非空 | 企业统一新闻标识，关联指标、正文、证据和报告 | 不用标题作主键；跨来源 ID 冲突必须先做映射，不能静默合并 |
| `event_time` | `timestamptz` / 非空 | 小时桶起点 | 必须有时区并整点对齐；当前热点榜只支持完整单小时 |
| `title` | `text` / 非空 | 新闻标题，来自内容维度 | 用于显示和关联检索；标题内文字不能改变 SQL 或系统指令 |
| `content_type` | `text` / 非空 | 内容类型 | Agent 表允许 `article/video`；当前 v8 数据和场景只允许 `article`，不能声称已有视频行为验收 |
| `category` | `text` / 非空 | 栏目 | v8 的科技、财经、体育、社会、汽车、教育、娱乐是演示标签；需映射企业正式栏目字典 |
| `source` | `text` / 非空 | 内容来源标记 | 不等同推荐/搜索/消息等流量渠道；不能用它计算渠道贡献 |
| `publish_time` | `timestamptz` / 非空 | 新闻发布时间 | 解析后保留时区；用于确定新闻何时具有统计资格，不能让发布前的行为进入结果 |
| `impressions` | `bigint` / 非空 | 合同定义下的曝光事件次数，单位“次” | 非负；相同规则、非重叠时间及互斥来源下可加；同一窗口不同修订不可相加 |
| `clicks` | `bigint` / 非空 | 点击事件次数，单位“次” | 非负；当前本地约束 `clicks <= impressions`，企业是否满足取决于点击与曝光归因，必须先核实 |
| `unique_users` | `bigint` / 非空 | **单新闻、单小时点击 UV**，单位“人” | 不是详情访问 UV；不得跨小时、渠道或新闻直接求和。当前本地还约束其不超过 clicks |
| `total_duration_seconds` | `numeric(20,2)` / 非空 | 阅读/消费时长合计，单位“秒” | 企业若提供毫秒，只能在一个确定的转换位置除以 1000；按合同过滤异常/重复时长。v8 原表是整数秒，导入此表转 Decimal |
| `effective_consumptions` | `bigint` / 非空 | 当前 v8 为点击归因的有效阅读次数，单位“次” | 不包含直接入口有效阅读；不是有效阅读人数。阈值与过滤规则由企业定义，不能自行填“读满 N 秒” |
| `interactions` | `bigint` / 非空 | 当前 v8 为点赞、评论、分享、收藏事件次数之和 | 不是互动 UV；不包含负反馈。不应重复叠加总互动和子项 |
| `baseline_impressions` | `numeric(20,2)` / 可空 | 曝光历史基线 | 当前 v8 使用过去最多 3 天的可用同小时样本均值；无样本为 NULL |
| `baseline_clicks` | `numeric(20,2)` / 可空 | 点击历史基线 | 不能用未来窗口或不同指标版本；0 与 NULL 意义不同 |
| `baseline_effective_consumptions` | `numeric(20,2)` / 可空 | 有效消费历史基线 | 与当前值使用相同单位、过滤与归因规则 |
| `baseline_interactions` | `numeric(20,2)` / 可空 | 互动历史基线 | 不混用“次”和“人”，不可把缺基线当零基线计算增长 |

当前 `dw.news_metric_baseline_hourly` 的物理基线列为 NOT NULL；v8 没有基线的新闻不导入基线行，通过视图 LEFT JOIN 得到上述 NULL。这与独立 v8 基线表“有行、sample_count=0、四列 NULL”的实现不同，输出语义一致。

另外三处当前实现差异必须单独处理：

1. `total_duration_seconds` 的数据库列支持两位小数，但 `NewsMetricSnapshot` 和通用指标源目前要求整数秒。企业返回 12.34 秒时不能默默截断；需要批准保留 Decimal、改用整数毫秒或明确取整规则，并同步更新合同和测试。
2. v7 的 `unique_users` 指点击 UV；旧 `NewsMetricCalculator` 的事件聚合对全部事件的 user_id 去重，读者画像又使用详情访问 UV。三条路径尚不能当作同一口径比较，评测要固定入口并标注用户集合定义。
3. 源包的 `sample_count` 是实际历史样本数；当前 SQL 热点补充链对存在基线写入 `sample_count=1`，仅表示一份合成参考。它不能替代企业历史样本数，也不能据此评价真实基线覆盖率。

**时间约定建议：** 接口传 ISO 8601 带偏移时间，内部比较统一 UTC；北京时间自然日按 Asia/Shanghai 明确换算。若企业使用不带时区的时间列，Adapter 必须按已确认源时区解释，禁止依赖服务器默认时区。历史新闻回放还需冻结当时可见的内容状态；当前 v8 的资格视图主要反映包内当前状态，不能自动证明具备任意历史时点查询能力。

## 3. 需要单独确认的指标公式

| 指标 | 建议合同 | 当前实现和限制 |
| --- | --- | --- |
| 点击率 CTR | `clicks / impressions`；输出比例，展示时乘 100% | 独立 v8 运营视图分母为 0 返回 NULL；Agent 部分现有路径使用 0 回退。接入与评测须明确所测入口，不能混用 |
| 点击归因有效阅读率 | `effective_consumptions / clicks` | 仅在二者使用相同归因口径时成立；0 分母返回不可计算。现有独立 v8 运营视图已有该值，18 列视图没有 |
| 每次详情访问平均时长 | `total_duration_seconds / detail_page_views` | 当前 v8 的时长是详情阅读时长，应用详情 PV 作分母；18 列视图缺少 detail_page_views，不能换用点击 UV 后仍叫“平均阅读时长” |
| UV | 在目标新闻、目标时间范围内对批准身份去重 | 小时 UV 不能推算日 UV；需要上游提供目标窗口去重结果。企业若采用近似算法，还需给出方法及误差说明 |
| 基线相对变化 | `(current - baseline) / baseline`，仅 baseline > 0 时计算 | baseline=0 标记零基线，NULL 标记无基线；不生成无穷增长率，不自行用 1 替换分母 |
| 互动率 | 先指定分母是点击、详情访问还是曝光，再定义公式 | 三者不是同一指标。当前合同未统一批准这一新增派生指标 |
| 热度 | 使用批准版本的 Python 热度规则，记录输入与版本 | SQL `hot_score` 是候选选择用的演示分数，不等于最终 Python 权威热度；不是 TDSQL 原生字段 |

数值型指标在 Python/SQL 中计算，模型负责解释和表达。不能让模型根据标题猜测缺失曝光、热度或历史基线。

## 4. 企业接入建议增加的字段

下列内容是**待实现的生产合同建议**。部分概念已经在独立 `news_sim_v8` 中存在，但没有随 18 列 `dw` 视图完整传递。不要直接给冻结 v7 视图加列，应另建合同版本或增加服务端结果元数据。

### 4.1 快照与口径元数据

建议随查询结果返回 `snapshot_metadata`；如一次包含多个小时，则按租户和小时返回多条元数据，不用一个 revision 冒充所有小时的统一版本。元数据不一定需要重复到每行。

| 建议字段 | 逻辑类型 / 必需性 | 精确定义与用途 | 当前对应情况 |
| --- | --- | --- | --- |
| `source_system` | string / 必需 | 实际来源系统与数据产品标识，区分企业、公开内容、模拟数据 | 查询结果合同需补齐；不能只靠文件名推断 |
| `schema_version` | string / 必需 | 字段和输出结构版本 | 本地有 `news_schema_contract.version`，企业版另建 |
| `metric_policy_version` | string / 必需 | 曝光、点击、有效消费、时长、UV 的口径版本 | 对应 v8 `policy_version`，18 列不含 |
| `dataset_version` | string / 回放必需 | 数据发布/冻结包身份；与 Schema 版本区别开 | 本地包 manifest 有身份信息，企业需提供对应引用 |
| `window_start` | timestamp / 必需 | 查询目标桶起点，与行中 event_time 一致 | 已有查询绑定参数 |
| `window_end` | timestamp / 必需 | 排他的桶终点 | v8 window_snapshot 已有 |
| `bucket_seconds` | integer / 必需 | 本次桶宽，当前热点合同为 3600 | 通用指标模板已有该配置；不等于源库字段 |
| `business_timezone` | string / 必需 | 解释自然日和同期基线的时区，例如 Asia/Shanghai | 当前依赖场景/代码，建议显式返回 |
| `snapshot_revision` | integer / 必需 | 同租户、同桶的单调修订号；每次修正产生新版本 | v8 已有，导入 dw 后丢失逐桶 revision 列 |
| `snapshot_at` | timestamp / 必需 | 本快照生成时间，不能等同查询执行时间 | v8 window_snapshot 已有 |
| `watermark_at` | timestamp / 必需 | 上游按其合同确认处理推进到的事件时间边界 | v8 已有；具体语义及迟到预算由上游定义，仅水位过窗不能自行证明所有来源齐全 |
| `completeness` | enum / 必需 | complete、corrected、incomplete；企业状态需显式映射 | v8 已有；热点完整窗口仅接受前两类且质量通过 |
| `quality_status` | enum / 必需 | passed、pending、failed 等质量判定；允许值需另定版本 | v8 仅 passed/pending；failed 是建议扩展，不是已有枚举 |
| `uv_method` | string / 返回 UV 时必需 | 实际去重对象、窗口、方法或其版本引用 | v8 的 `temporary_interval_union_v8.1` 仅为模拟方法，不能用于企业数据 |
| `data_origin` | enum / 必需 | enterprise_measured、synthetic 等指标来源 | 建议新增；v8 metric_data_synthetic 恒为 true，不能切为 false 后宣称已接企业 |
| `query_id` | string / 必需 | 企业执行记录或服务端查询追踪 ID | 用于定位超时、扫描量、资源消耗；不是原始用户标识 |
| `evidence_ref` | string / 回放必需 | 经授权聚合快照或结果摘要引用 | 供报告和评测追溯；不嵌入凭证或用户明细 |

**一致性要求：** 先确定最新修订，再判断其完整性和质量；最新修订不合格时应阻断，不回退较旧的 passed 版本。同一轮的候选查询、UV/时长补充和基线必须绑定一致快照。需要企业在单一一致性读取中返回结果，或提供可重读的快照令牌；仅增加 revision 字段而分多次读取仍不能消除修订竞态。

### 4.2 与新闻绑定的基线和内容信息

| 建议字段 | 逻辑类型 / 空值 | 精确定义与校验 | 当前对应情况 |
| --- | --- | --- | --- |
| `baseline_version` | string / 有基线时必需 | 基线方法版本，包含回看范围、样本筛选与聚合方法 | v8 基线表已有，18 列不含 |
| `baseline_sample_count` | integer / 非空 | 实际采用的历史样本数；0 表示没有基线 | 对应 v8 `sample_count`；不是配置的计划天数 |
| `baseline_reference_start` | timestamp / 无样本可空 | 基线候选历史区间起点 | 建议新增；不能仅从均值倒推 |
| `baseline_reference_end` | timestamp / 无样本可空 | 基线候选历史区间排他终点，不能超出目标窗口起点 | 建议新增；与起点共同定义可见历史范围 |
| `baseline_source_revisions_ref` | string / 有基线时必需 | 实际入选样本的时间与修订清单引用 | 建议新增，便于迟到修正后重算与复现 |
| `content_version` | integer/string / 非空 | 实际内容版本，类型服从源合同 | v8 已有整数版；企业若使用字符串版本不要强转整数 |
| `content_sha256` | string / 非空 | 对明确规范化正文计算的 SHA-256 | v8 已有；需冻结编码和清洗规则，否则相同正文也会有不同摘要 |
| `content_status` | enum / 非空 | published、withdrawn、restricted 的映射值 | 对应 v8 status；企业真实撤稿/授权状态必须由内容源提供 |
| `content_status_effective_at` | timestamp / 非空 | 内容状态生效时间 | 对应 v8 status_effective_at；不同于指标时间 |
| `lifecycle_revision` | integer / 非空 | 内容状态更新序号，用于拒绝乱序覆盖 | v8 已有；不是指标 snapshot_revision |
| `metric_available` | boolean / 非空 | 是否具备该数据版本下的指标证据 | v8 dim_news 已有，但 true 不代表任意小时都有数据，仍需检查目标窗口 |
| `source_url` | string / 可空 | 经批准可展示的来源 URL | v8 已有；企业无公开 URL 可返回授权内容引用，不能拼造网址 |

内容正文、关键词和索引任务不必全部放进 Text2SQL 视图。数仓给 news_id，内容服务再按 ID 提供正文；知识库按内容版本检索。受众画像也优先消费现有聚合 Port，不把 user_id、设备 ID 或逐用户行为加入上述接口。[画像合同说明](news-reader-profile-warehouse.md)

## 5. TDSQL 物理适配：先确认再落地

| 项目 | 建议 | 落地前需要确认 |
| --- | --- | --- |
| 类型 | 沿 PostgreSQL 类型体系映射，保留计数整数、时长/均值精确小数及带时区接口时间 | 核对实际源列是否使用 uuid/text、timestamptz/timestamp、numeric，以及精度、源时区和驱动返回类型；源列无需与本地同名同类型 |
| 访问方式 | 优先企业批准的只读 SQL 网关/SDK 或现有指标 API | 协议、驱动、连接池、鉴权、取消查询和超时错误映射 |
| 时间分区 | 小时事实按事件时间做日期范围分区是候选方案 | 数据量、保留周期、迟到补数范围、分区裁剪能力与实际语法 |
| 数据分布 | 按真实连接键和热点分布评估 tenant/news 组合的分布方案 | 只按 tenant 分布在少量大业务域下可能倾斜；只按 news 分布会影响全站 TopK 的跨节点合并，需 EXPLAIN/实测 |
| 行列存储 | 宽范围聚合可评估列存；小维表、频繁单行更新单独评估 | 以该内部版本的可用能力为准，不把公开产品能力直接写成已上线优化 |
| 查询代价 | 对批准视图限制租户、时间、列、排序和结果量，并设执行预算 | LIMIT 只限制返回行，不能保证少扫描；还要检查扫描量、执行计划、并发和取消效果 |
| 权限 | 服务账号只读批准视图，按业务域隔离，记录查询审计 | 企业数据库/网关的实际权限机制；不能仅靠 Prompt 或 WHERE 条件证明隔离 |
| 修订 | 上游发布新快照，业务运行绑定版本，缓存包含版本 | 对迟到更新、撤稿和缓存失效进行同一次运行的一致性验证 |

当前聊天 SQL 编译器及 `SqlAssistantGuard` 明确使用 PostgreSQL 方言，与已确认的 TDSQL-A 兼容方向一致。通用 `Text2SqlSchema` 也可按 postgres 校验；连接执行、日期函数、小数除法、参数绑定、只读事务、超时和错误处理仍需企业合同测试。若允许 PostgreSQL 协议直连，现有 SQLAlchemy/psycopg 是可验证的适配起点；若要求网关/SDK，则实现同一 `SqlWarehouseClient` Port。**本文未新增驱动、TDSQL 连接或数据库迁移。**

## 6. 用这份字段设计准备评测

今天先完成一份“核心字段映射 + 口径确认”。不必等所有扩展字段上线后才开始本地合同回归；但企业业务指标验收必须使用已确认的企业映射和批准聚合快照。

每个字段补齐以下内容：

| 记录项 | 填写示例或规则 |
| --- | --- |
| 逻辑字段 | 如 `unique_users` |
| 企业库 / 表 / 列 | 待内部确认；不可把本地表名当作真实企业表名 |
| 数据类型与转换 | 如 bigint；是否可能为字符串数字、是否有小数、时长是否要换算 |
| 业务定义 | 如点击 UV 或详情访问 UV，必须选择明确的一种 |
| 原始粒度 | 租户 × 新闻 × 小时 × 渠道？是否包含修订历史？ |
| 目标粒度与规则 | 多渠道计数如何合并，UV 在哪里重新去重，最新修订如何选定 |
| NULL / 0 / 缺行 | 分别代表未统计、确实为零、没有该窗口，不能混为一谈 |
| 版本与证据 | 指标口径版本、数据快照引用和确认人/确认日期 |

准备如下最小测试场景，逐例确定正确结果；这是覆盖清单，不是充分样本量的统计结论。

| 场景 | 输入需要覆盖 | 正确结果如何判断 |
| --- | --- | --- |
| 单小时点击 TopK | 同租户、多新闻、点击并列 | 与独立确认的聚合结果一致；并列按 news_id 稳定排序 |
| CTR 与零曝光 | 正常曝光及 0 曝光样本 | 精确核对分子/分母；预先固定所测入口的 NULL/0 策略 |
| 点击 UV 与访问 UV | 两个 UV 不同的样本 | 问题用词与选择字段一致，不能拿访问人数填点击人数 |
| 跨小时 UV | 同一合成读者在两个桶重复出现，或上游已批准的窗口去重答案 | 无批准跨窗源时应拒绝/说明不可计算；不可将小时 UV 求和当正确答案 |
| 缺基线与零基线 | sample_count=0；另一个样本基线真实为 0 | 前者无基线，后者零基线；都不输出普通百分比增长 |
| 修订与水位 | rev1 完整、rev2 不完整，以及后续合格修订 | 先选最新再判质量，不回退旧合格版，不重复统计两个修订 |
| 缺指标内容 | 只有正文和元数据、没有该窗口指标 | 不得补 0 后进入榜单，更不能用模型补造指标 |
| 租户与时间边界 | 同 news_id 的另一租户、恰在窗口终点的行 | 排除其他租户和右边界，保留左边界 |
| 单位转换 | 业务确认的秒/毫秒转换样本 | 只换算一次；聚合时长与批准参考相等 |
| 撤稿/受限内容 | 当前不具备展示资格的新闻 | 按所测合同过滤或阻断；历史行为保存与当前展示资格分开判断 |

可保留三项清晰指标：**允许查询的结果正确数 / 允许查询总数**、**违规查询在访问数仓前被拦截数 / 违规查询总数**、**固定条件下成功查询的 SQL 耗时 P95，同时单列超时/失败率**。报告必须标明样本数、数据版本、环境、并发和耗时边界；同输入比较基线与候选。模型理解准确率应另测自然语言到意图，不能用手写 SQL 安全样本替代。

已有开发集是 [metric_contract_business_v1.json](../evaluation/datasets/metric_contract_business_v1.json)，包含 14 个合成案例。它用于通用指标 SQL 合同；不要将其中的简化列集当作全部 18 列视图或实际企业 DDL。增加样本后先做人工核对，另留冻结验证集，不能在调参集上填写最终泛化提升。本文没有修改或运行任何评测。

## 7. 调用链、配置、验证入口与待办

**上游入口与转换：** 热点聊天/页面提供问题、可信身份和时间窗口 → QueryUnderstanding 提议意图 → 服务端解析并确认范围 → `SqlAssistantService.preview` → `compile_query` 固定公式 → `SqlAssistantGuard` 校验 AST 与绑定参数 → SQL 计划绑定热点运行 → Temporal 执行 → `execute_for_hot_news` 重验计划与身份 → `LocalPostgresSqlWarehouseClient` 参数化只读查询 → 同小时 UV、时长、基线补充并交叉检查 → `NewsMetricSnapshot` → Python 热度与排名 → 正文/关联检索 → 模型解释和可信校验 → 报告、SQL 轨迹和界面输出。

**另一路通用指标入口：** `create_text2sql_metric_source` 装配 `Text2SqlNewsMetricSource.fetch_batch` → `HotNewsMetricSqlTemplate` 验证单桶并生成批准 SQL → 固定模板优先，必要时模型提议候选 → `SqlGuard` 与 `validate_metric_sql_contract` 校验完整查询 → `SqlWarehouseClient.execute` → 行类型/非负/重复/截断校验 → 指标快照。此入口不等于聊天的自然语言理解入口，也不因本文而自动接上 TDSQL。

**本地数据转换：** 独立 v8 冻结包 → `real_news_profiles._valid_hours` 先取最新修订再判质量 → `_iter_metrics` 取对应小时指标、缺基线不导入 → `initialize_demo_warehouse` 写入本地 `dw` → 18 列视图 LEFT JOIN → 上述只读查询。独立 v8 的生命周期过滤与裁剪后的 dw 不可视为完全相同；具体差异见逐字段字典。

**外部依赖：** 当前本地执行依赖 PostgreSQL、SQLAlchemy/psycopg、sqlglot、Pydantic、YAML；完整热点运行还涉及 Temporal、模型/Embedding Port、Redis 与对象存储。企业 TDSQL 网关/SDK 是待确认外部依赖，不是此次新引入的运行时依赖。

**现有配置：** `SQL_ASSISTANT_DATASET_PROFILE=real-news-v8`、`SQL_ASSISTANT_NEWS_PER_TENANT=8120`、`SQL_ASSISTANT_SCENARIOS_PATH` 指向 `deploy/text2sql-scenes.real-news-v8.yml`；场景绑定 `news-warehouse-v7`，SQL 默认超时 10000 ms、排行最多 100 行。上述是本地场景值，不是企业 SLA。企业端点、驱动、账号注入和快照合同需另行装配，本文不提供虚构配置键。

**本次验证范围：** 文档对照当前 DDL、视图、导入逻辑与字段清单做静态核对；不连接数据库、不读取凭证、不修改冻结 Schema、不执行模型评测或生产操作。

静态核对结果：13 张物理表的 183 个字段名称、顺序、类型和可空性全部与当前 DDL/源码清单一致；Agent 的 18 个视图字段全部有对应说明；两份文档的本地链接均有效。这是文档一致性核对，不是企业数据库验收。

后续实现变更的测试入口（在 `agent/` 目录、已配置的项目测试环境中运行；本次未执行）：

```bash
python -m pytest -o addopts='' -q \
  tests/test_sql_assistant_planner.py tests/test_sql_assistant_guard.py \
  tests/test_real_news_profiles.py tests/test_text2sql_metric_source.py \
  tests/test_metric_sql_contract.py tests/test_real_news_warehouse_queries.py
```

待办：取得 TDSQL-A 内核版本与批准连接方式 → 对齐 18 列实际来源与口径 → 确认质量/水位/修订及一致性读取 → 实现并验证企业 Adapter → 冻结经过人工核对的聚合评测快照 → 分别评测查询正确性、拒绝行为、时延和自然语言理解。产品分支及 PostgreSQL 兼容方向已确认；跨小时 UV、画像、渠道分析和任意历史时点回放各有独立合同，不能由单小时榜单验收自动推导通过。

源码依据：[冻结 v7 合同](sql-warehouse-schema-v7.md)、[v8 DDL](sql/real-news-warehouse-v8.sql)、[本地查询库定义](../app/sql_assistant/warehouse.py)、[v8 导入](../app/sql_assistant/real_news_profiles.py)、[通用指标源](../app/modules/hot_news/scoring/text2sql_metric_source.py)、[指标 SQL 合同](../app/modules/hot_news/scoring/metric_sql_contract.py)。
