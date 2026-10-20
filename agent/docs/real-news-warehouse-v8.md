# 真实新闻模拟数仓 v8

本版先完成可导入、可查询、可复现的数仓底座，供后续 NewsAgent 接入。它模拟新闻运营业务的
维度和统计链路，不描述腾讯新闻的内部实现，也没有连接企业行为库。

## 2026-10-19 真实内容扩展（原地 v8）

用户确认在 v8 上原地扩充真实内容，不伪造新文章的行为指标。当前数据包包含：

- **8,120 篇真实公开新闻**：661 篇冻结目录 + 7,459 篇 v2 公开增量；正文、URL、发布时间和
  内容哈希均来自公开缓存，不生成模拟标题或模拟正文。
- **7,459 篇内容-only 文章**：写入 `dim_news` 和新增的 `news_content_feature`，但没有曝光、
  点击、UV、时长、基线或榜单行。它们不能被表述为热点候选。
- **661 篇仍有 v8 行为指标**：这些指标仍是确定性模拟值，继续标记
  `metric_data_synthetic=true`，不能代表真实热度。
- 新增 `news_content_feature`（标题/正文字数、段落数、句子数、数字 token 数和确定性
  CJK 关键词），只由真实正文计算，不包含用户级数据。
- 原地重建后的数据包摘要为
  `e139126c4bdc49f885aa6f08681de48f7da4d445de85442207a77c3706d7c904`；
  扩展清单为 `catalog-extension-v2.json`，摘要
  `6b7e65df3590ce84974adcf77d4505bc494e05ecbd5a0b1e7c5eec1732ad766f`。
- 旧数据包已备份到仓库外，新的 `warehouse.sql` 使用 JSONB 文本导入关键词数组，不再使用
  Python 列表的 `repr`。

### 扩展生成与校验

```bash
# 1. 生成 7,459 篇真实文章的元数据/派生特征清单；正文仍留在公开缓存
PYTHONPATH=agent python3.11 agent/tools/freeze_real_news_warehouse_extension.py --overwrite

# 2. 导出新的 v8 数据包（需要新的目录；旧包先移出或备份）
PYTHONPATH=agent python3.11 -c "from simulation.warehouse_simulation.real_news import export_dataset, RealNewsSimulationConfig; export_dataset(RealNewsSimulationConfig(), 'var/warehouse/real-news-v8-new')"

# 3. 校验并替换
PYTHONPATH=agent python3.11 agent/tools/validate_real_news_warehouse.py \
  var/warehouse/real-news-v8-new --report var/warehouse/real-news-v8-new/validation.json
```

### 热点 Agent 接入（`real-news-v8`）

新增 `agent/app/sql_assistant/real_news_profiles.py` 和冻结契约
`agent/docs/sql-warehouse-schema-v7.md`。该 profile：

- 在 Agent 侧导入 8,120 篇真实内容的 `dim_news`（每租户）；
- 只导入 661 篇有 v8 指标的文章到 `dw.news_behavior_aggregate`；
- 对缺失基线保留为空（视图 LEFT JOIN），不补 0；
- 7,459 篇内容-only 文章只存在于 `dim_news`，没有指标行，因此不会进入热度排行；
- 聊天/热点运行使用 `deploy/text2sql-scenes.real-news-v8.yml`，允许 7 个演示栏目；
- 独立 Compose 叠加文件 `deploy/docker-compose.real-news-v8-agent.yml` 使用新的项目名、
  端口和卷（API `38000`、Postgres `35432`、Temporal `37233`、MinIO `39000/39001`），
  不修改默认 native-e2e 演示。

启动：

```bash
docker compose -f deploy/docker-compose.native-e2e.yml \
  -f deploy/docker-compose.real-news-v8-agent.yml up -d --build
```

验证过的链路：`/api/v1/conversations/runtime` 返回 `dataset_profile=real-news-v8`、
`metric_available_article_count=661`、`content_only_article_count=7459`；
本地热点运行在 `2026-10-13 20:00` 窗口完成 5 篇排名和分析；首条内容来自真实公开正文，
不再使用“本地合成新闻样本”占位。新增内容-only 文章在同一运行中不可见。

测试：

```bash
python3.11 -m pytest -o addopts='' -q \
  tests/test_real_news_warehouse_extension.py tests/test_real_news_profiles.py \
  tests/test_real_news_warehouse_catalog.py tests/test_real_news_warehouse_simulation.py \
  tests/test_real_news_warehouse_package.py
```

仍待完成：企业真实行为指标、内容-only 文章的真实榜单资格、企业聚合接口、生产负载和
权限联调。当前只证明隔离本地环境和真实公开内容链路。

2026-10-15 已补 [SQL 查询与热点全链路说明](hot-news-sql-chain.md)：包含 PostgreSQL 方言、7类固定
参数化只读查询、可运行 CLI、最新水位门禁、现有 Python 权威热度和后续 Agent 适配清单。

## 内容、规模与时间

- 8,120 篇不同的真实新闻：冻结目录 661 篇（corpus-v6 120 + 增量 v1 541），加上 v2 公开
  增量 7,459 篇；ID 去重后交集为 0。
- 原始 `news_id`、标题、完整正文、URL、发布时间、正文版本和来源摘要保留，标题没有合成前缀。
- 7 个演示栏目；v2 新增文章来自公开发现入口，全部映射为社会栏目，不冒充原始编辑分类。
- 2 个隔离租户共 16,240 条新闻维记录；它们使用相同真实内容。661 篇有模拟行为数据，
  7,459 篇内容-only 文章没有指标行。**不同真实新闻的数量是 8,120，不把租户复制算成新增新闻。**
- 默认闭合时段为北京时间 `2026-09-30 00:00` 至 `2026-10-14 20:00`，共 356 小时。
  另有 20:00 开始的开放小时，模拟观测至 20:35。实际可查询的小时还受最新修订质量门禁限制。
- 新闻发布时间在统计期内时，只对发布后的桶内时段建模；发布前没有行为桶、小时指标或基线。
  所有采集正文均以文本文章建模，不虚构视频播放指标。

冻结目录摘要为 `1af51f4fba5f190829d255fe8ee2ce2328811f09102af6d9e814e2b49d4353f0`。
一个核验案例是 [WorkBuddy开放平台上线，AI办公迎来硬件入口之争](https://news.qq.com/rain/a/20260903A05YQE00)：
原文发布时间 `2026-09-03 12:18:15+08:00`，3,856 字，页面正文与本地缓存完整一致。
全量目录来自已冻结缓存，只有这个参考案例及两篇备选做了本次在线复核；不是逐篇重新抓取。

本次默认数据包生成 **2,508,059 行**，约 1.5 GiB，核心数据集摘要为
`e139126c4bdc49f885aa6f08681de48f7da4d445de85442207a77c3706d7c904`。
其中五分钟 1,739,966 行、渠道小时 438,036 行、新闻小时 146,012 行、新闻日表 6,980 行、
基线 143,368 行，其余为维度、窗口修订和生命周期演练记录。行数包括修订历史，不代表曝光/点击事件数。

实际验收通过：包内每表摘要与行数；PostgreSQL 16.15 的全量导入、16,240 条
实际新闻正文与元数据核验、只读跨粒度与基线 SQL 对账；localhost:28438 的参数化只读查询。
库内有 13 张表、4 个视图；每租户 354 个完整小时、1 个纠正小时、2 个不完整小时。
参考新闻在 `2026-10-14 19:00` 模拟窗口：曝光 1,323、点击 81、点击 UV 41、访问 PV 401、访问 UV 245，
其中直接访问 PV 341，CTR 6.12%，基线样本数 3。以上行为数字均为模拟值。
证据文件为数据包内的 `validation.json`、`deployment.json`、`postgres-checks.log`、
`postgres-observed.json`、`host-query.json` 与 `tests.log`；对应架构 PDF 已确认仅一页且中文渲染正常。

## 13 张表和统计颗粒度

| 表 | 颗粒度与用途 |
| --- | --- |
| `dim_behavior_policy` | 租户 / 指标规则版本，保存 PV、UV、点击和有效阅读定义 |
| `dim_channel` | 租户 / 入口：推荐、栏目、搜索、热榜、消息、网页 |
| `dim_news` | 租户 / 真实 news_id，含正文、URL、发布时间、内容摘要和状态来源 |
| `news_content_feature` | 租户 / 真实新闻，标题/正文字数、段落、句子、数字 token 和确定性关键词 |
| `window_snapshot` | 租户 / 小时 / 修订号，水位、观测时间、完整性、质量状态 |
| `news_metric_5min` | 租户 / 新闻 / 五分钟 / 修订号，所有历史有效桶都有数据 |
| `news_metric_channel_hourly` | 租户 / 新闻 / 入口 / 小时 / 修订号，每篇选取 3 个模拟入口 |
| `news_metric_hourly` | 租户 / 新闻 / 小时 / 修订号，跨入口重新去重 |
| `news_metric_daily` | 租户 / 新闻 / 日期，最新合格小时聚合、跨小时重新去重、覆盖范围及缺失小时 |
| `news_metric_baseline_hourly` | 租户 / 新闻 / 目标小时，过去 3 天可用同小时平均和真实样本数 |
| `content_change_event` | 隔离租户 / 事件 / 新闻，模拟撤稿、受限、更正 |
| `content_tombstone` | 隔离租户 / 新闻，演练立即屏蔽与版本范围 |
| `index_cleanup_task` | 隔离租户 / 事件 / 索引目标，全部 pending 的清理任务样本 |

小时统计同时有五分钟总表与渠道小时表两条对账路径，曝光、点击、PV、时长、有效阅读和互动等
可加量在它们之间守恒。模拟有时效衰减、早晚峰、短时升温、低样本、零点击、直接访问、阅读退化、
负反馈升高和迟到修订，避免只用匀速数据验证查询。

## 指标口径

| 字段 | 含义与聚合边界 |
| --- | --- |
| `impressions` | 模拟去重后的曝光次数，可跨桶相加 |
| `clicks` | 模拟去重后的点击事件次数，用户主动再次点击计新次数 |
| `unique_users` | **单新闻窗口点击 UV**，沿用未来 18 字段投影名称，不等于访问 UV |
| `detail_page_views` | 成功打开详情的 PV，包含点击归因和直接入口，允许大于点击次数 |
| `direct_page_views` | 直接入口产生的详情 PV，是上述 PV 的子集 |
| `detail_visitors` | **单新闻窗口详情访问 UV**，与点击 UV 独立计算 |
| `total_duration_seconds` | 模拟详情阅读时长总和 |
| `effective_consumptions` | 点击归因的有效阅读数，不包含直接入口阅读；受点击归因 PV 约束 |
| `interactions` | 点赞 + 评论 + 分享 + 收藏次数，非人数 |
| `negative_feedback` | 模拟负反馈事件次数 |

CTR 为 `clicks / impressions`；分母为 0 时运营视图保留 NULL。平均阅读时长按详情 PV 为分母。
这是聚合数据生成器，按已去重事件的统计结果建模，没有实际 SDK、日志消费或 event_id 去重服务。
不能用确定性生成代替端到端投递语义验收。

UV 使用临时数学区间的集合并集，五分钟、渠道、小时、每日使用同一套临时人群范围重新计算。
没有用户、设备、session、view 标识或区间明细落盘。**渠道 UV、小时 UV、不同新闻的 UV 都不能直接相加。**
日表仍是单新闻日 UV，不能用 `SUM(detail_visitors)` 声称全站 DAU。全站跨新闻人群去重是后续扩展项。

## 查询门禁与生命周期

`latest_window_snapshot` 先选择最大的修订号，再检查完整性，不能先挑合格旧版本。
最后一个闭合小时 rev1 仍待水位，rev2 已纠正；倒数第二小时专门构造 rev1 完整、rev2 不完整，
验证新快照失效后必须阻断旧快照回退。开放小时仅观测 7 个桶，也不能进入完整小时榜。
日表排除最新不合格小时，保留 `excluded_hours`，末日标记 partial；不会把一天的部分数据说成整日数据。

`news_behavior_aggregate` 提供与现有 Agent 相同名称和顺序的 18 字段，过滤最新修订质量和内容资格，
通过 LEFT JOIN 保留没有历史基线的新新闻。`sample_count=0` 时四个基线都为 NULL，不能填 0。
`news_operational_hourly` 展示最新修订的访问统计和质量状态，诊断时可包含 pending；做完整小时业务查询
必须明确过滤 `completeness IN ('complete','corrected') AND quality_status='passed'`。

主租户所有内容均为 published 演示状态，没有虚构某篇真实新闻实际撤稿。
隔离租户才演练撤稿、受限、更正，`lifecycle_data_synthetic=true`；原始正文及原始 `content_version`
始终保留，假设更正使用 `fixture_content_version`。历史行为不会因为当前屏蔽而物理消失，资格视图会过滤它。
清理任务覆盖 PostgreSQL 知识、Milvus、BGE 快照和检索缓存，全部 pending、attempt_count=0，
没有执行器、外部连接或实际向量删除。三元外键绑定 tenant / event / news，避免清理串到另一篇文章。

## 生成、查询与复现

```bash
# 1. 生成到全新目录，已有目录会阻断，所有表流式写出
PYTHONDONTWRITEBYTECODE=1 python3.11 agent/tools/generate_real_news_warehouse.py \
  --output var/warehouse/real-news-v8-20261014

# 2. 全包摘要、真实内容和隐私字段校验
PYTHONDONTWRITEBYTECODE=1 python3.11 agent/tools/validate_real_news_warehouse.py \
  var/warehouse/real-news-v8-20261014 \
  --report var/warehouse/real-news-v8-20261014/validation.json

# 3. 独立本地 PostgreSQL；不修改现有 Agent / 数据库 / Docker 服务
PYTHONDONTWRITEBYTECODE=1 python3.11 agent/tools/start_real_news_warehouse.py \
  --dataset var/warehouse/real-news-v8-20261014 --wait-seconds 300
```

公开本地演示连接为 `postgresql://news_demo:local-v8-only@127.0.0.1:28438/news_demo`。
该口令只用于新模拟库。Compose 使用独立 project、自定义 bridge 网络和新卷，并通过数据集摘要标签
阻断不同 seed、窗口或数据包悄悄复用旧卷。初始化 SQL 全事务导入，已有 schema 会报错而非覆盖。
启动后校验实际 localhost 端口和 PostgreSQL 协议应答，并只读核对 12 表实际数量、全部真实新闻元数据、
数据库正文 SHA 和窗口，不读取 `.env`。

```sql
BEGIN READ ONLY;
SET LOCAL TIME ZONE 'Asia/Shanghai';
SELECT news_id,title,event_time,clicks,unique_users AS click_uv,
       detail_page_views AS visit_pv,detail_visitors AS visit_uv,
       direct_page_views,ctr,snapshot_revision,completeness,sample_count
FROM news_sim_v8.news_operational_hourly
WHERE tenant_id='11111111-1111-4111-8111-111111111111'
  AND news_id='20260903A05YQE00'
  AND event_time='2026-10-14 19:00:00+08'
  AND completeness IN ('complete','corrected') AND quality_status='passed';
COMMIT;
```

包内有原始 catalog、扩展清单、生成器与 loader 源码快照、DDL、全事务 `warehouse.sql`、13 张表 JSONL、
manifest、少量 preview 与 scenarios。manifest 对每表行数、列和 SHA 以及 SQL、源码、正文目录分别绑定。
冻结新目录和已有数据库均拒绝覆盖，允许同一版本重启；重新构建内容或修改指标规则应使用新版本、新目录、新卷。

## 完整调用链与配置

1. 内容上游：`freeze_real_news_warehouse_catalog.py` → 两份冻结来源 manifest → 逐篇原文件与正文摘要校验
   → `build_catalog()` → `freeze_catalog()` → `agent/data/real-news-warehouse-v8/catalog.json`。
2. 数仓入口：`generate_real_news_warehouse.py` → `RealNewsSimulationConfig` → `load_catalog()` → `_catalog()`
   → `_dimension_rows()` / `_article_rows()` / `_hour_packet()` → 跨桶、入口、小时集合并集
   → `_baseline()` → `export_dataset()` 流式 JSONL / COPY / 版本摘要 → 新数据包。
3. 包验收：`validate_real_news_warehouse.py` → `validate_package()` → 独立冻结 catalog SHA 绑定
   → 每文件、每表流式校验及维表原文核对 → `validation.json`。
4. 数据库入口：`start_real_news_warehouse.py` → 同一包校验 → 卷标签身份核验 → 新 Compose 的 `warehouse`
   → PostgreSQL 16 原子 COPY → TCP 健康检查 → `verify_database_result()` 只读正文与数量核验。
5. 数据质量：`real-news-warehouse-v8-checks.sql` → 只读 DO 断言 → 发布边界、最新修订、跨粒度守恒、
   UV 边界、逐新闻基线、清理状态 → `real-news-warehouse-v8-observe.sql` 有界真实案例投影
   → `postgres-observed.json`。
6. PDF：`render_real_news_warehouse_pdf.py --dataset … --output …` → 同一摘要下的 manifest / validation /
   PostgreSQL 验收证据 → ReportLab 中文单页矢量架构图 → Poppler 渲染与 pypdf 页数检查。

生成器与校验器仅依赖 Python 3.11 标准库；部署依赖 Docker Compose / PostgreSQL 16；PDF 依赖 ReportLab、
系统中文字体和 Poppler。无模型、Embedding、企业接口或外部 Agent 框架依赖。
生成配置为 `--start-at`、`--closed-hours`、`--seed`、`--output`；内容目录版本及 SHA 硬绑定。
部署配置为 `--dataset`、`--wait-seconds`、`--skip-start`，新服务固定 localhost:28438。

测试入口：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=agent python3.11 -m pytest \
  agent/tests/test_real_news_warehouse_catalog.py \
  agent/tests/test_real_news_warehouse_simulation.py \
  agent/tests/test_real_news_warehouse_package.py \
  agent/tests/test_real_news_warehouse_deploy.py -p no:cacheprovider -o addopts=''
```

## 后续 Agent 接入和 TODO

当前 schema 是独立 `news_sim_v8`，**尚未注册到现有 Agent 的 profile / SQL 白名单，也未绑定热点 Workflow**。
18 字段兼容仅表示形状，不表示已经接入。下一步应新建版本化 profile、契约与摘要，使用固定参数 SQL
补充访问 PV/UV、水位及基线样本数，将统计旁车保存到同一次 `sql_tool_trace`，聊天与前端读取同一运行快照。
不得把访问 UV 偷换为现有 `unique_users`；无样本基线映射为不存在的 baseline，不补 0。
接入时还须核对旧指标语义：当前旧计算器的人数覆盖全部行为，旧时长、有效消费和分母也可能不同；
本版明确采用点击 UV、详情阅读总时长和点击归因有效阅读。字段形状一致不能代替这一步契约验收。

其他 TODO：全站跨新闻 UV / DAU；真实行为统计 Port；事件去重与投递验收；企业接口权限；数据库 RLS 和
最小只读角色；真实 CMS 事件、版本化清理执行器、重试与删除复核、BGE denylist/rebuild；生产性能与容量压测。
模拟数学人数不是可合并的生产 HLL / bitmap，也不能支撑任意窗口的真实用户查询。

生产参考中，事件时间水位表示处理进度，并不保证永远没有迟到事件，后续更新需要明确修订规则。
参见 [Apache Flink 时间与水位文档](https://nightlies.apache.org/flink/flink-docs-stable/docs/concepts/time/)。
日志幂等生产也不自动解决外部存储的端到端重复计算，参见
[Apache Kafka 投递语义设计](https://kafka.apache.org/42/design/design/)。本版本没有部署这些流式组件。
