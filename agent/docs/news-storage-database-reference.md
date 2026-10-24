# NewsAgent 本地统一数据库：库表、字段与查询手册

核对日期：**2026-10-23（北京时间）**。本文以本地 PostgreSQL 实际系统目录、实际行数、导入代码和验收报告为依据，汇总已经创建的对象。适合按表名、字段名、业务关键词检索。

**当前成果是一个独立 database、两个业务 Schema、14 张表、4 个视图。** 新闻正文、内容标签和新闻受众聚合已经可以用 SQL 联查。数据是冻结快照，不是持续更新中的企业实时数仓。

公开新闻内容来自已有采集包；受众人数全部为 `synthetic` 合成数据；内容标签为规则推断。三者分别用于内容读取、主题筛选和聚合链路验证，不能直接当成人工金标或企业真实业务效果。

## 目录与检索入口

- [1. 连接、位置与固定版本](#connection)
- [2. 按业务问题找表](#lookup)
- [3. 数据规模与关系](#relationships)
- [4. 14 张表的完整字段字典](#dictionary)
- [5. 4 个查询视图及指标口径](#views)
- [6. 直接复制执行：查结构、正文、标签与受众](#queries)
- [7. 原始包、导出素材与个人画像的位置](#files)
- [8. 导入与读取的完整调用链](#call-chain)
- [9. 已有验证证据与测试入口](#verification)
- [10. 使用边界与未完成事项](#todo)

关键词：PostgreSQL、TDSQL-A、新闻正文、全文、内容标签、tag、用户兴趣、读者画像、受众聚合、reader_count、share_all、share_profiled、lift、snapshot_id、news_id、release、哈希、SQL、表结构、对象存储、ES、评测素材。

<a id="connection"></a>
## 1. 连接、位置与固定版本

### 1.1 数据库连接

| 项目 | 实际值 |
|---|---|
| PostgreSQL 版本 | `16.15` |
| 主机与宿主机端口 | `127.0.0.1:25432` |
| 数据库名 | `newsagent_unified_news_20261023` |
| Docker 容器 | `news-agent-native-e2e-postgres-1` |
| 容器内端口 | `5432` |
| 本地演示用户名 | `newsagent_native` |
| 内容与内容标签 Schema | `public` |
| 新闻受众 Schema | `news_audience` |
| 容器内数据目录 | `/var/lib/postgresql/data`；由现有 Compose 的 `native-postgres-data` 卷挂载 |
| 与应用库的关系 | 共用 PostgreSQL 实例，独立于原应用 database `newsagent_native`；没有新建数据库容器 |

连接配置依据：[docker-compose.native-e2e.yml](../../deploy/docker-compose.native-e2e.yml)。Docker 卷由 Docker 管理；`var/news-storage/` 是冻结输入包和导出文件目录，不是 PostgreSQL 数据目录。

在任意终端进入数据库，无需在宿主机安装 `psql`：

```bash
docker exec -it news-agent-native-e2e-postgres-1 \
  psql -X -U newsagent_native -d newsagent_unified_news_20261023
```

进入 `psql` 后执行：

```psql
\conninfo
\pset pager off
\x auto
SET TIME ZONE 'Asia/Shanghai';
```

`\d` 等反斜杠命令只在 `psql` 内有效；图形数据库客户端应运行第 6 节的标准 SQL。退出使用 `\q`。

### 1.2 固定身份：查询时不要省略

| 身份 | 当前值 | 作用 |
|---|---|---|
| 内容域 / 内容租户 | `public-news-local` | 统一公开新闻的逻辑域，文本类型 |
| 内容 release | `news-0c74530f2864db20a73d7a96` | 冻结本次选定正文及来源 |
| 内容完整身份 SHA-256 | `0c74530f2864db20a73d7a960cec3649085e9db5fdb054bbf0bfbfc21a58375e` | 内容包完整身份；release ID 使用其前缀 |
| 标签 release | `tags-33c4054fd01bd98cd5ef2826` | 在指定内容 release 上计算的标签版本 |
| 受众来源 release | `03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5` | **受众包 manifest 原始字节的 SHA-256**，不是 manifest 内另一个身份字段 |
| 受众业务租户 A | `11111111-1111-4111-8111-111111111111` | 合成业务租户，UUID 类型 |
| 受众业务租户 B | `33333333-3333-4333-8333-333333333333` | 另一合成业务租户，UUID 类型 |
| 主题目录版本 | `news-behavior-topics-demo-v1` | 84 个主题的草案目录，非腾讯官方目录 |
| 画像策略版本 | `prewindow-exclude-trigger-interest-v1` | 使用访问窗口之前的兴趣，并排除目标新闻的历史贡献 |
| 受众 Schema 版本 | `news-reader-audience-v1` | 本地聚合投影结构版本 |

内容域与业务租户不能直接相等连接。两者通过 `news_audience.unified_news_binding` 显式关联，并校验 `news_id + body_sha256` 及固定正文版本。

<a id="lookup"></a>
## 2. 按业务问题找表

| 我想查什么 | 首选 SQL 对象 / 位置 | 关键条件或字段 |
|---|---|---|
| 新闻全文、标题、链接、发布时间 | `public.news_content_selected` | `tenant_id + release_id + news_id`；正文在 `body` |
| 某篇新闻所有历史内容版本 | `public.news_content_versions` | `tenant_id + news_id`；版本由 `record_sha256` 区分 |
| 原文从哪个包和文件来 | `public.news_content_provenance` | 固定 release、news ID、record SHA；`source` 为来源 JSON |
| 新闻讲什么，打了哪些 tag | `public.news_content_tagged_selected` | 内容及标签 release；`topic_ids / label / tag_status` |
| 只筛选 AI、财经等主题的新闻 | `public.news_content_topic_memberships` | `topic_id`，连回选定内容；这是内容主题 |
| 标签为什么命中 | `public.news_content_tags` | `label->'matches'`；有规则、原文位置和分数 |
| 一个窗口内有多少人读某篇新闻 | `news_audience.v_news_reader_population_status` | 业务租户、来源、快照、新闻、`lookback_hours` |
| 这些读者主要喜欢什么 | `news_audience.v_news_reader_topic_metrics` | `topic_name / topic_reader_count / share_profiled / lift` |
| 有哪些主题及父子关系 | `news_audience.topic_dictionary` | `taxonomy_version / topic_id / parent_topic_id` |
| 受众数据对应哪份正文 | `news_audience.unified_news_binding` | 业务租户、来源、内容域、内容 release、记录和正文哈希 |
| 数据窗口、修订、水位、质量 | `news_audience.reader_snapshot` | `window_start/end / watermark_at / source_revision / quality_basis` |
| 导入了哪份受众包、如何校验 | `news_audience.package_release` | `manifest / policy / row_counts / projection_digests` |
| 单个合成用户的画像 | 旧包 `var/behavior-tags/local-merged-v1-20261016/` | 未导入当前 PG 库；见第 7 节 |
| 曝光、点击、热度排行 | 既有热点指标链路中的 `dw.news_behavior_aggregate` | **不属于本次统一库新增的 14 张表**；受众人数不是热度分数 |

**内容标签回答“这篇新闻在讲什么”；受众标签回答“读这篇新闻的人此前偏好什么”。** 不能把新闻自身的 AI 标签直接当成读者都喜欢 AI。

<a id="relationships"></a>
## 3. 数据规模与关系

### 3.1 2026-10-23 实际行数

以下行数经数据库 `COUNT(*)` 核对。行数表示记录粒度，不表示用户规模。

| 表 | 行数 | 一行的含义 |
|---|---:|---|
| `public.news_content_versions` | 21,828 | 某内容域中的一篇新闻的一个标准化内容版本 |
| `public.news_content_releases` | 1 | 一个冻结的内容发布版本 |
| `public.news_content_release_items` | 21,435 | 某 release 为某 news_id 选定的一个内容版本 |
| `public.news_content_provenance` | 22,369 | 某发布中的内容版本的一条来源观察 |
| `public.news_content_tag_releases` | 1 | 绑定到内容 release 的一个冻结标签版本 |
| `public.news_content_tags` | 21,435 | 某选定新闻在指定标签版本下的标签与状态 |
| `public.news_content_topic_memberships` | 35,888 | 某标签版本中一篇新闻与一个主题的关系 |
| `news_audience.package_release` | 1 | 一个冻结受众源包及其导入审计信息 |
| `news_audience.content_binding` | 2,474 | 原业务租户的源包内一篇新闻及正文/主题绑定 |
| `news_audience.unified_news_binding` | 1,266 | 一个业务租户的源新闻与一个统一内容版本的精确映射 |
| `news_audience.reader_snapshot` | 1,488 | 某业务租户、来源 release 的一个覆盖窗口 |
| `news_audience.topic_dictionary` | 84 | 某目录版本中的一个主题 |
| `news_audience.news_reader_population` | 105,436 | 某租户、快照、新闻、兴趣回看期限的人群汇总 |
| `news_audience.news_reader_topic` | 1,282,053 | 上述人群中一个保留主题的读者数和基准人数 |

四个视图不另存一份数据。当前两份内容视图均对应 21,435 条选定新闻；受众人群视图对应 105,436 行，主题视图对应 1,282,053 行。

- **21,435 个新闻 ID**：8,441 篇有缓存正文、11,794 篇仅历史片段、1,200 条仅标题。`body_available` 不是人工完整性认证。
- **21,828 个版本**：同一新闻可能保留正文版本和片段版本，故多于选定新闻数。
- **21,435 条标签状态**：7,566 条 `tagged`，2,068 条 `unclassified`，11,794 条 `insufficient_input`，7 条 `metadata_review`。
- **52,718 条源新闻窗口记录**：拆成 24 小时、720 小时两个兴趣维度，形成 105,436 条人群记录。两个维度不是两批读者，不能相加。
- **1,266 条正文映射**：是业务租户与新闻的组合，去重后为 **661 个新闻 ID**。新增公开正文没有凭空补上受众数据。
- **84 个主题**：12 个一级主题、72 个二级主题；同一读者可命中多个主题。
- 受众覆盖区间：北京时间 **2026-09-13 20:00 至 2026-10-14 20:00**。`window_start` 包含、`window_end` 不包含；单个窗口为一小时。历史不足完整 720 小时时保留限制说明。

### 3.2 关联链路

```text
公开新闻源文件
  └─ news_content_versions：各内容版本
       ├─ news_content_provenance：原始来源
       └─ news_content_release_items + news_content_releases：选定版本
            └─ news_content_selected：正文统一读取
                 └─ news_content_tags + news_content_tag_releases
                      ├─ news_content_topic_memberships：主题筛选
                      └─ news_content_tagged_selected：正文 + 内容标签

冻结受众聚合包
  └─ package_release
       ├─ content_binding：源新闻资格
       │    └─ unified_news_binding ──精确新闻 ID + 正文 SHA──→ 统一正文
       └─ reader_snapshot：租户 + 窗口 + 修订 + 水位
            └─ news_reader_population：读者分母
                 └─ news_reader_topic + topic_dictionary：主题计数与名称
                      └─ v_news_reader_topic_metrics：占比 / lift / 低样本
```

关联受众与主题时必须同时使用 `tenant_id + snapshot_id + news_id + lookback_hours`。仅按 `news_id` JOIN 会混入其他租户、小时或长短期维度，造成重复与错误人数。主题目录 JOIN 还必须带 `taxonomy_version`。

<a id="dictionary"></a>
## 4. 14 张表的完整字段字典

字段、类型、是否可空和主外键来自本次数据库系统目录。`timestamptz` 表示带时区时间，显示由会话时区决定；文档示例统一使用北京时间。表中未列出的 CHECK 约束可使用 `\d+ 表名` 查看。

`public` 内容表的枚举、哈希和业务一致性还依赖 Python 导入校验；不能把代码校验全部视为数据库 CHECK。`unified_news_binding` 对统一正文的精确匹配由绑定程序校验，当前没有跨 Schema 的正文外键。

### 4.1 `public.news_content_versions`：不可变内容版本

一行代表：某内容域中的一篇新闻的一个标准化内容版本。当前 **21,828 行**。

- `PRIMARY KEY (tenant_id, news_id, record_sha256)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `varchar(128)` | 否 | 统一内容域，当前为 public-news-local |
| `news_id` | `varchar(160)` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `record_sha256` | `varchar(64)` | 否 | 标准化整条内容记录的 SHA-256，覆盖正文、元数据和片段等；不等于正文哈希 |
| `title` | `text` | 是 | 来源标题；缺失时保持空值 |
| `body` | `text` | 是 | 已保存的抓取正文；片段和仅标题记录不冒充全文 |
| `url` | `text` | 是 | 来源新闻 URL；未知时为空 |
| `publish_time` | `timestamptz` | 是 | 来源发布时间；不从 news_id 或抓取时间猜测 |
| `content_type` | `varchar(16)` | 否 | article 或 video；沿用来源类型 |
| `content_state` | `varchar(24)` | 否 | body_available / fragments_only / title_only |
| `body_sha256` | `varchar(64)` | 是 | 正文 UTF-8 字节的 SHA-256；无正文时为空 |
| `record` | `jsonb` | 否 | 扩展 JSON，仅含 fragments、quality_flags；不重复存正文。片段含 chunk_index、text，历史源另有 chunk_id |

内容版本全部保留；不同 news_id 即使正文相同也不合并。`record_sha256` 与 `body_sha256` 是两种不同身份。

### 4.2 `public.news_content_releases`：内容冻结发布

一行代表：一个冻结的内容发布版本。当前 **1 行**。

- `PRIMARY KEY (tenant_id, release_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `varchar(128)` | 否 | 统一内容域，当前为 public-news-local |
| `release_id` | `varchar(160)` | 否 | 冻结内容发布 ID；读取时显式指定 |
| `identity_sha256` | `varchar(64)` | 否 | 该发布的完整身份摘要，用于验证冻结内容 |
| `schema_version` | `varchar(64)` | 否 | 该数据包或投影的结构版本 |
| `selection_policy` | `varchar(128)` | 否 | 选定版本策略，当前为 prefer-cached-body-source-priority-v1 |
| `status` | `varchar(16)` | 否 | 记录状态；具体语义见表下说明 |
| `manifest` | `jsonb` | 否 | 内容包 manifest.json 的 release 子对象：租户、版本、schema、选择策略、identity 和 counts；不含磁盘外层 files/source_inventory |
| `created_at` | `timestamptz` | 否 | 首次导入时间；不是新闻发布时间 |

`status=frozen`。同一新闻的选定规则依次为：正文优于片段、片段优于标题；随后比较来源优先级和记录哈希。

### 4.3 `public.news_content_release_items`：发布选定项

一行代表：某 release 为某 news_id 选定的一个内容版本。当前 **21,435 行**。

- `PRIMARY KEY (tenant_id, release_id, news_id)`
- `FOREIGN KEY (tenant_id, news_id, record_sha256) REFERENCES news_content_versions(tenant_id, news_id, record_sha256)`
- `FOREIGN KEY (tenant_id, release_id) REFERENCES news_content_releases(tenant_id, release_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `varchar(128)` | 否 | 统一内容域，当前为 public-news-local |
| `release_id` | `varchar(160)` | 否 | 冻结内容发布 ID；读取时显式指定 |
| `news_id` | `varchar(160)` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `record_sha256` | `varchar(64)` | 否 | 标准化整条内容记录的 SHA-256，覆盖正文、元数据和片段等；不等于正文哈希 |
| `quality_flags` | `jsonb` | 否 | 发布选定项的质量标记数组，可能不同于内容记录自身标记 |

同一 release 每个 news_id 只选一条内容记录。查询选定全文使用视图，不必自己猜测“最新”。

### 4.4 `public.news_content_provenance`：原始来源证据

一行代表：某发布中的内容版本的一条来源观察。当前 **22,369 行**。

- `PRIMARY KEY (tenant_id, release_id, news_id, record_sha256, source_sha256)`
- `FOREIGN KEY (tenant_id, news_id, record_sha256) REFERENCES news_content_versions(tenant_id, news_id, record_sha256)`
- `FOREIGN KEY (tenant_id, release_id) REFERENCES news_content_releases(tenant_id, release_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `varchar(128)` | 否 | 统一内容域，当前为 public-news-local |
| `release_id` | `varchar(160)` | 否 | 冻结内容发布 ID；读取时显式指定 |
| `news_id` | `varchar(160)` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `record_sha256` | `varchar(64)` | 否 | 标准化整条内容记录的 SHA-256，覆盖正文、元数据和片段等；不等于正文哈希 |
| `source_sha256` | `varchar(64)` | 否 | 来源对象的规范化 JSON 哈希；原始文件哈希在 source.file_sha256 |
| `source` | `jsonb` | 否 | 来源对象：package、path、file_sha256、manifest_path、manifest_sha256、source_content_version、fetched_at、selection_priority（用于版本选择的来源优先级） |

相同内容在不同采集包出现时保留多条来源；不能把来源条数当作文章数。

### 4.5 `public.news_content_tag_releases`：内容标签发布

一行代表：绑定到内容 release 的一个冻结标签版本。当前 **1 行**。

- `PRIMARY KEY (tenant_id, content_release_id, tag_release_id)`
- `FOREIGN KEY (tenant_id, content_release_id) REFERENCES news_content_releases(tenant_id, release_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `varchar(128)` | 否 | 统一内容域，当前为 public-news-local |
| `content_release_id` | `varchar(160)` | 否 | 绑定的冻结正文 release |
| `tag_release_id` | `varchar(160)` | 否 | 独立的冻结内容标签发布 ID |
| `content_identity_sha256` | `varchar(64)` | 否 | 绑定的内容发布完整身份摘要 |
| `identity_sha256` | `varchar(64)` | 否 | 该发布的完整身份摘要，用于验证冻结内容 |
| `schema_version` | `varchar(64)` | 否 | 该数据包或投影的结构版本 |
| `derivation_version` | `varchar(128)` | 否 | 标签推导算法/转换版本 |
| `policy_version` | `varchar(128)` | 否 | 本次内容标签规则版本 |
| `taxonomy_version` | `varchar(128)` | 否 | 主题目录版本；关联 topic_id 时必须同时使用 |
| `tagger_sha256` | `varchar(64)` | 否 | 实际标签计算实现的摘要 |
| `status` | `varchar(16)` | 否 | 记录状态；具体语义见表下说明 |
| `manifest` | `jsonb` | 否 | 标签包 manifest.json 的 release 子对象：内容绑定、版本、规则摘要、counts 和 legacy_source；不含磁盘外层 files |
| `policy` | `jsonb` | 否 | 本版本实际规则和主题目录等配置 JSON |
| `created_at` | `timestamptz` | 否 | 首次导入时间；不是新闻发布时间 |

`status=frozen`。标签 release 与内容 release 绑定，不能不校验就混用。内容主题目录保存在 policy 中。

### 4.6 `public.news_content_tags`：新闻内容标签

一行代表：某选定新闻在指定标签版本下的标签与状态。当前 **21,435 行**。

- `PRIMARY KEY (tenant_id, content_release_id, tag_release_id, news_id)`
- `FOREIGN KEY (tenant_id, content_release_id, news_id) REFERENCES news_content_release_items(tenant_id, release_id, news_id)`
- `FOREIGN KEY (tenant_id, content_release_id, tag_release_id) REFERENCES news_content_tag_releases(tenant_id, content_release_id, tag_release_id)`
- `FOREIGN KEY (tenant_id, news_id, record_sha256) REFERENCES news_content_versions(tenant_id, news_id, record_sha256)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `varchar(128)` | 否 | 统一内容域，当前为 public-news-local |
| `content_release_id` | `varchar(160)` | 否 | 绑定的冻结正文 release |
| `tag_release_id` | `varchar(160)` | 否 | 独立的冻结内容标签发布 ID |
| `news_id` | `varchar(160)` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `record_sha256` | `varchar(64)` | 否 | 标准化整条内容记录的 SHA-256，覆盖正文、元数据和片段等；不等于正文哈希 |
| `body_sha256` | `varchar(64)` | 是 | 正文 UTF-8 字节的 SHA-256；无正文时为空 |
| `label_sha256` | `varchar(64)` | 否 | 标签对象规范化 JSON 哈希，不含自身 |
| `label_status` | `varchar(32)` | 否 | tagged / unclassified / insufficient_input / metadata_review |
| `input_scope` | `varchar(24)` | 否 | title_body / title_only / none，记录实际参与判断的文本 |
| `provenance` | `varchar(32)` | 否 | 规则推断或旧标签精确核对等来源状态，如 rule_inferred、legacy_verified |
| `topic_ids` | `jsonb` | 否 | 去重排序的内容主题 ID 数组，可为空；不是受众兴趣比例 |
| `label` | `jsonb` | 否 | 完整标签 JSON：topic_scores、matches、legacy_refs、labels_are_inferred 等；匹配证据引用原文位置 |

原始 `label` 不保证有展示字段 `topic_names`；CLI 在读取时从本版目录生成中文名。`matches` 含 topic_id/rule_id/field/term/start/end/score/source。规则标签不是人工金标。

### 4.7 `public.news_content_topic_memberships`：新闻与主题关系

一行代表：某标签版本中一篇新闻与一个主题的关系。当前 **35,888 行**。

- `FOREIGN KEY (tenant_id, content_release_id, tag_release_id, news_id) REFERENCES news_content_tags(tenant_id, content_release_id, tag_release_id, news_id)`
- `PRIMARY KEY (tenant_id, content_release_id, tag_release_id, news_id, topic_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `varchar(128)` | 否 | 统一内容域，当前为 public-news-local |
| `content_release_id` | `varchar(160)` | 否 | 绑定的冻结正文 release |
| `tag_release_id` | `varchar(160)` | 否 | 独立的冻结内容标签发布 ID |
| `news_id` | `varchar(160)` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `topic_id` | `varchar(128)` | 否 | 主题 ID，例如 ai 或 finance；父子主题分别保留 |

包含继承的父主题；一篇新闻可以有多个主题。通过此表过滤后再分页。

### 4.8 `news_audience.package_release`：受众来源发布

一行代表：一个冻结受众源包及其导入审计信息。当前 **1 行**。

- `PRIMARY KEY (source_release_sha256)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `source_release_sha256` | `text` | 否 | 受众 manifest 文件原始字节 SHA-256；区分于文件内的自摘要 |
| `manifest` | `jsonb` | 否 | 冻结清单 JSON：版本、来源、统计和文件校验等 |
| `policy` | `jsonb` | 否 | 本版本实际规则和主题目录等配置 JSON |
| `schema_version` | `text` | 否 | 该数据包或投影的结构版本 |
| `row_counts` | `jsonb` | 否 | 冻结来源投影的导入行数；后续 unified_news_binding 另有绑定报告 |
| `projection_digests` | `jsonb` | 否 | 按固定顺序计算的各来源投影表摘要，用于全量读回与重复导入核对 |
| `imported_at` | `timestamptz` | 否 | 受众源包首次导入时间 |

重复导入会比较来源身份及所有来源投影摘要，成功时返回 already_imported_verified，不追加重复行。

### 4.9 `news_audience.content_binding`：来源新闻资格

一行代表：原业务租户的源包内一篇新闻及正文/主题绑定。当前 **2,474 行**。

- `PRIMARY KEY (tenant_id, source_release_sha256, news_id)`
- `FOREIGN KEY (source_release_sha256) REFERENCES news_audience.package_release(source_release_sha256)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `uuid` | 否 | 业务租户 UUID；不等于内容域 public-news-local |
| `source_release_sha256` | `text` | 否 | 受众 manifest 文件原始字节 SHA-256；区分于文件内的自摘要 |
| `news_id` | `text` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `content_sha256` | `text` | 是 | 受众源包绑定的正文 SHA-256；无正文的源新闻可为空 |
| `content_topic_ids` | `jsonb` | 否 | 源包保存的新闻内容主题 ID 数组，不是读者兴趣列表 |
| `binding` | `jsonb` | 否 | 源绑定 JSON；当前只允许 content_sha256 和 topic_ids，禁止混入个人身份 |

用于判断新闻是否属于这个来源包。源资格存在，不等于已和统一正文匹配。标题源允许正文 SHA 为空。

### 4.10 `news_audience.unified_news_binding`：业务新闻与统一正文映射

一行代表：一个业务租户的源新闻与一个统一内容版本的精确映射。当前 **1,266 行**。

- `PRIMARY KEY (tenant_id, source_release_sha256, news_id, content_tenant_id, content_release_id)`
- `FOREIGN KEY (tenant_id, source_release_sha256, news_id) REFERENCES news_audience.content_binding(tenant_id, source_release_sha256, news_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `uuid` | 否 | 业务租户 UUID；不等于内容域 public-news-local |
| `source_release_sha256` | `text` | 否 | 受众 manifest 文件原始字节 SHA-256；区分于文件内的自摘要 |
| `news_id` | `text` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `content_tenant_id` | `text` | 否 | 统一内容域，当前为 public-news-local |
| `content_release_id` | `text` | 否 | 绑定的冻结正文 release |
| `record_sha256` | `text` | 否 | 标准化整条内容记录的 SHA-256，覆盖正文、元数据和片段等；不等于正文哈希 |
| `body_sha256` | `text` | 否 | 非空正文 UTF-8 字节 SHA-256；无正文源新闻不生成本表映射 |
| `binding_method` | `text` | 否 | 匹配方式；当前为 news_id_and_body_sha256 |
| `bound_at` | `timestamptz` | 否 | 本次业务新闻与统一正文映射写入时间 |

绑定函数只接受新闻 ID 与非空正文 SHA 同时匹配；固定内容租户、release、记录 SHA。不按标题猜测匹配，不将业务 UUID 伪装成 public-news-local。

### 4.11 `news_audience.reader_snapshot`：受众覆盖窗口快照

一行代表：某业务租户、来源 release 的一个覆盖窗口。当前 **1,488 行**。

- `PRIMARY KEY (tenant_id, snapshot_id)`
- `FOREIGN KEY (source_release_sha256) REFERENCES news_audience.package_release(source_release_sha256)`
- `UNIQUE (tenant_id, snapshot_id, taxonomy_version)`
- `UNIQUE (tenant_id, source_release_sha256, window_start, window_end)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `uuid` | 否 | 业务租户 UUID；不等于内容域 public-news-local |
| `snapshot_id` | `text` | 否 | 该租户来源窗口快照的确定性 ID；基于来源、租户、起止时间、修订，不基于请求新闻列表 |
| `window_start` | `timestamptz` | 否 | 新闻详情访问统计窗口起点（包含） |
| `window_end` | `timestamptz` | 否 | 新闻详情访问统计窗口终点（不包含） |
| `profile_cutoff` | `timestamptz` | 否 | 兴趣画像截止时点，当前与 window_start 一致；只使用之前的历史 |
| `knowledge_as_of` | `timestamptz` | 否 | 本地合成包的冻结生成时点；不是企业在历史窗口当时的知识可得时间 |
| `knowledge_as_of_basis` | `text` | 否 | 当前为 synthetic_package_frozen_at，说明上述时间的来源口径 |
| `history_start` | `timestamptz` | 否 | 来源包历史覆盖起点，决定回看历史是否不足 |
| `source_revision` | `bigint` | 否 | 源快照修订号，当前为 1 |
| `source_release_sha256` | `text` | 否 | 受众 manifest 文件原始字节 SHA-256；区分于文件内的自摘要 |
| `source_mode` | `text` | 否 | 来源类型；本次所有受众数据均为 synthetic |
| `taxonomy_version` | `text` | 否 | 主题目录版本；关联 topic_id 时必须同时使用 |
| `profile_policy_version` | `text` | 否 | 兴趣构建规则版本，当前排除目标新闻历史贡献 |
| `identity_policy_version` | `text` | 否 | 读者身份去重规则版本，当前为 synthetic-user-id-tenant-scoped-v1 |
| `reader_definition` | `text` | 否 | 读者定义，当前为 detail-view-distinct-v1（窗口内去重详情访客） |
| `count_method` | `text` | 否 | 计数方式，当前为 exact_synthetic_distinct（合成身份精确去重） |
| `generated_at` | `timestamptz` | 否 | 受众包生成时间 |
| `watermark_at` | `timestamptz` | 否 | 来源声明的数据覆盖水位；查询要求不早于 window_end |
| `completeness` | `text` | 否 | 窗口完整性状态，如 complete / corrected；不是模型回答质量 |
| `quality_status` | `text` | 否 | 质量状态；passed 的实际含义还需结合 quality_basis |
| `quality_basis` | `text` | 否 | 当前为 local_import_contract_validation，仅指本地导入合同校验 |
| `expires_at` | `timestamptz` | 是 | 可选失效时点；本次冻结包为空，未来在线使用需落实失效门禁 |
| `min_support` | `bigint` | 否 | 最小支持人数阈值，当前为 5，用于低样本判断与 lift 抑制 |

一行描述覆盖窗口；窗口存在不表示所有新闻都有访问。快照 ID 可供离线重现；quality_status=passed 不代表企业生产质量认证。

### 4.12 `news_audience.topic_dictionary`：受众主题字典

一行代表：某目录版本中的一个主题。当前 **84 行**。

- `FOREIGN KEY (taxonomy_version, parent_topic_id) REFERENCES news_audience.topic_dictionary(taxonomy_version, topic_id)`
- `PRIMARY KEY (taxonomy_version, topic_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `taxonomy_version` | `text` | 否 | 主题目录版本；关联 topic_id 时必须同时使用 |
| `topic_id` | `text` | 否 | 主题 ID，例如 ai 或 finance；父子主题分别保留 |
| `topic_name` | `text` | 否 | 目录中的中文显示名 |
| `parent_topic_id` | `text` | 是 | 父主题 ID，一级主题为空；自引用同一目录版本 |
| `topic_level` | `smallint` | 否 | 一级为 1，二级为 2 |

来自冻结包的草案目录。内容标签也使用目录版本，但不能不检查版本就假定永远相同。

### 4.13 `news_audience.news_reader_population`：新闻读者人群

一行代表：某租户、快照、新闻、兴趣回看期限的人群汇总。当前 **105,436 行**。

- `PRIMARY KEY (tenant_id, snapshot_id, news_id, lookback_hours)`
- `FOREIGN KEY (tenant_id, snapshot_id) REFERENCES news_audience.reader_snapshot(tenant_id, snapshot_id)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `uuid` | 否 | 业务租户 UUID；不等于内容域 public-news-local |
| `snapshot_id` | `text` | 否 | 该租户来源窗口快照的确定性 ID；基于来源、租户、起止时间、修订，不基于请求新闻列表 |
| `news_id` | `text` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `lookback_hours` | `integer` | 否 | 兴趣历史回看期限：24（短期）或 720（最长 30 天）；不是访问窗口长度 |
| `content_sha256` | `text` | 是 | 受众源包绑定的正文 SHA-256；无正文的源新闻可为空 |
| `source_has_body_hash` | `boolean` | 否 | 源人群记录是否有非空正文哈希；不能单独证明统一正文映射已存在 |
| `reader_count` | `bigint` | 否 | 该新闻访问窗口内的去重详情访客数；同一窗口在长短期两行里相同 |
| `profiled_reader_count` | `bigint` | 否 | 该回看期限中，有有效历史兴趣画像的新闻读者人数 |
| `unknown_reader_count` | `bigint` | 否 | 该期限无有效兴趣画像的读者人数；与 profiled_reader_count 相加等于 reader_count |
| `cold_start_reader_count` | `bigint` | 否 | 已初始化但回看期内没有非目标新闻有效证据的冷启动人数；是 unknown 的子集 |
| `baseline_profiled_count` | `bigint` | 否 | 同租户、截止时间和策略下，排除目标新闻历史贡献后的基准有画像人数 |
| `status` | `text` | 否 | 记录状态；具体语义见表下说明 |
| `trigger_article_excluded` | `boolean` | 否 | 构建历史兴趣与基准时是否排除了目标新闻自身历史贡献；避免用目标新闻给自己制造兴趣证据 |
| `limitations` | `text[]` | 否 | 历史不足、主题 TopK 截断等限制说明数组 |
| `source_payload_sha256` | `text` | 否 | SQLite 原始 payload_json 字符串 UTF-8 字节 SHA-256；同一新闻窗口的两个期限相同 |
| `source_topic_limit` | `integer` | 否 | 源包每个回看期限最多保留主题数，本次为 20 |
| `topics_are_top_k` | `boolean` | 否 | 来源有 TopK 限制；未出现的主题不能当作零人 |
| `source_topic_count` | `integer` | 否 | 该新闻窗口、期限实际保留的主题数 |

表内 `status` 只允许 `complete`（有访客）或 `empty`（0 人）。`unavailable` 是查询层对未知新闻或不可用窗口的返回语义，不是本表持久化状态。当前表只落已有源人群，不生成新闻×所有窗口的笛卡尔积。查询没有行时先确认源资格和覆盖，不能直接补零。

### 4.14 `news_audience.news_reader_topic`：新闻读者主题计数

一行代表：上述人群中一个保留主题的读者数和基准人数。当前 **1,282,053 行**。

- `FOREIGN KEY (taxonomy_version, topic_id) REFERENCES news_audience.topic_dictionary(taxonomy_version, topic_id)`
- `PRIMARY KEY (tenant_id, snapshot_id, news_id, lookback_hours, topic_id)`
- `FOREIGN KEY (tenant_id, snapshot_id, news_id, lookback_hours) REFERENCES news_audience.news_reader_population(tenant_id, snapshot_id, news_id, lookback_hours)`
- `UNIQUE (tenant_id, snapshot_id, news_id, lookback_hours, source_rank)`
- `FOREIGN KEY (tenant_id, snapshot_id, taxonomy_version) REFERENCES news_audience.reader_snapshot(tenant_id, snapshot_id, taxonomy_version)`

| 字段 | PostgreSQL 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | `uuid` | 否 | 业务租户 UUID；不等于内容域 public-news-local |
| `snapshot_id` | `text` | 否 | 该租户来源窗口快照的确定性 ID；基于来源、租户、起止时间、修订，不基于请求新闻列表 |
| `news_id` | `text` | 否 | 新闻关联 ID；保持原始 ID，不因导入变更 |
| `lookback_hours` | `integer` | 否 | 兴趣历史回看期限：24（短期）或 720（最长 30 天）；不是访问窗口长度 |
| `topic_id` | `text` | 否 | 主题 ID，例如 ai 或 finance；父子主题分别保留 |
| `topic_reader_count` | `bigint` | 否 | 当前新闻读者中，在指定期限具有该主题兴趣的人数 |
| `baseline_topic_count` | `bigint` | 否 | 基准有画像人群中，具有该主题兴趣的人数 |
| `taxonomy_version` | `text` | 否 | 主题目录版本；关联 topic_id 时必须同时使用 |
| `source_rank` | `integer` | 否 | 源包主题原始排名，从 1 开始；原排序为人数降序，再按主题 ID |

只保存来源保留的 Top20 主题。主题重叠，人数不能加总；没出现的主题不等于人数为零。

<a id="views"></a>
## 5. 4 个查询视图及指标口径

| 视图 | 组合来源 | 适合查询 |
|---|---|---|
| `public.news_content_selected` | frozen 内容发布、选定项、内容版本 | 当前固定 release 的标题、全文、片段和质量标记 |
| `public.news_content_tagged_selected` | 选定内容、标签记录、frozen 标签发布 | 正文及内容主题，固定内容/标签版本 |
| `news_audience.v_news_reader_population_status` | 人群表 + 精确快照 | 读者分母、画像覆盖率、窗口、来源和质量信息 |
| `news_audience.v_news_reader_topic_metrics` | 主题计数 + 人群 + 快照 + 字典 | 中文兴趣主题、人数、占比、基准、lift 和低样本 |

两个内容视图不会替你选择“最新”发布。两个受众视图保留状态字段，不自动筛选最新修订或只返回质量合格窗口，也不会为缺失新闻自动生成 0 人记录。

### 5.1 视图新增字段与别名

| 字段 | 定义 / 含义 |
|---|---|
| `item_quality_flags` | 内容选定项的质量标记，来自 release_items.quality_flags |
| `tag_identity_sha256` | 标签发布 identity_sha256 的视图别名 |
| `tag_status` | 标签记录 label_status 的视图别名 |
| `tag_provenance` | 标签记录 provenance 的视图别名 |
| `profile_coverage` | `profiled_reader_count / reader_count`；分母为 0 时 NULL |
| `share_all` | `topic_reader_count / reader_count`；占全部读者比例 |
| `share_profiled` | `topic_reader_count / profiled_reader_count`；占有画像读者比例 |
| `baseline_share` | `baseline_topic_count / baseline_profiled_count` |
| `low_sample` | 画像人数、基准画像人数、主题人数、基准主题人数四者任一小于 min_support |
| `lift` | `share_profiled / baseline_share`；低样本或基准占比为 0 时 NULL |
| `topic_list_may_be_truncated` | `topics_are_top_k AND source_topic_count >= source_topic_limit`；达到上限时提示可能截断 |

`lift > 1` 只表示该群体对主题的兴趣占比高于同口径基准，不证明因果、显著性或推荐收益。SQL 返回原始 NUMERIC 比例；既有 Python Reader 合同要求八位小数、半偶舍入，后续在线适配时需保持一致。

即使 `topic_list_may_be_truncated=false`，也不能用缺失主题推断整套目录里该主题一定为零。源包有 TopK 导出规则，当前每个长短期维度最多 20 个主题。

### 5.2 空值、0 和未知的区别

| 情形 | 正确处理 |
|---|---|
| 已知源新闻、覆盖完整且合格的窗口，没有访问人群行 | 可推导 `empty`、`reader_count=0` |
| 新闻不属于源包、窗口未覆盖或质量不满足 | `unavailable` 或拒绝查询；不能填 0 |
| 有访客，但无有效历史兴趣 | 保留 reader_count，计入 unknown，不等于无人阅读 |
| 无正文、只存标题/片段 | body 为 NULL，不能自动拼成完整正文 |
| 主题不存在于保留 TopK | 不代表该主题人数为 0 |
| lift 因低样本/零基准不可计算 | 保持 NULL，不补 0 |

`reader_count = profiled_reader_count + unknown_reader_count`；`cold_start_reader_count` 已包含在 unknown 中。24h 与 720h 人群共享同一新闻访问窗口，不可跨期限重复相加；跨小时去重人数和多个主题人数也不能直接相加。

<a id="queries"></a>
## 6. 直接复制执行：查结构、正文、标签与受众

以下 SQL 在 `newsagent_unified_news_20261023` 执行。时间字面量使用 `+08`，无需猜测数据库默认时区。

### 6.1 查看有哪些表、视图和字段

在 `psql` 内：

```psql
\dt public.news_content_*
\dt news_audience.*
\dv public.news_content_*
\dv news_audience.*
\d+ public.news_content_versions
\d+ public.news_content_tags
\d+ news_audience.reader_snapshot
\d+ news_audience.news_reader_population
\d+ news_audience.news_reader_topic
```

任何 PostgreSQL SQL 客户端都可使用：

```sql
SELECT table_schema, table_name, table_type
FROM information_schema.tables
WHERE table_schema = 'news_audience'
   OR (table_schema = 'public' AND table_name LIKE 'news_content_%')
ORDER BY table_schema, table_name;
```

查询人群表完整字段，替换两个名称即可查看其他表：

```sql
SELECT ordinal_position, column_name, data_type, udt_name,
       character_maximum_length, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'news_audience'
  AND table_name = 'news_reader_population'
ORDER BY ordinal_position;
```

### 6.2 查看一篇新闻的完整正文和片段

```sql
SELECT news_id, title, publish_time, url, content_state,
       length(body) AS body_chars, body,
       record->'fragments' AS fragments,
       item_quality_flags, record_sha256, body_sha256
FROM public.news_content_selected
WHERE tenant_id = 'public-news-local'
  AND release_id = 'news-0c74530f2864db20a73d7a96'
  AND news_id = '20260718A03B9V00';
```

`body` 是数据库保存的全部抓取正文，没有在查询中截断。若终端显示难读，可先执行 `\x on`。`fragments_only` 新闻请看 `record->'fragments'`；标签联合视图没有 `record` 字段。

### 6.3 查看内容标签及其原文证据

```sql
SELECT news_id, title, topic_ids, tag_status, tag_provenance,
       label->'topic_scores' AS scores,
       label->'matches' AS matched_text_evidence
FROM public.news_content_tagged_selected
WHERE tenant_id = 'public-news-local'
  AND content_release_id = 'news-0c74530f2864db20a73d7a96'
  AND tag_release_id = 'tags-33c4054fd01bd98cd5ef2826'
  AND news_id = '20260718A03B9V00';
```

查询带有 AI 内容主题的前 20 篇新闻：

```sql
SELECT c.news_id, c.title, c.publish_time, c.content_state
FROM public.news_content_selected c
JOIN public.news_content_topic_memberships m
  ON m.tenant_id = c.tenant_id
 AND m.content_release_id = c.release_id
 AND m.news_id = c.news_id
WHERE c.tenant_id = 'public-news-local'
  AND c.release_id = 'news-0c74530f2864db20a73d7a96'
  AND m.tag_release_id = 'tags-33c4054fd01bd98cd5ef2826'
  AND m.topic_id = 'ai'
ORDER BY c.news_id
LIMIT 20;
```

### 6.4 先看精确受众快照，再看主要兴趣

复现示例：业务租户 A，新闻 `20260718A03B9V00`，北京时间 2026-09-24 22:00–23:00。

```sql
SELECT snapshot_id, window_start, window_end, profile_cutoff,
       source_mode, source_revision, watermark_at,
       completeness, quality_status, quality_basis,
       knowledge_as_of, knowledge_as_of_basis, expires_at
FROM news_audience.reader_snapshot
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'::uuid
  AND source_release_sha256 = '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'
  AND window_start = '2026-09-24 22:00:00+08'::timestamptz
  AND window_end = '2026-09-24 23:00:00+08'::timestamptz;
```

下面固定该快照，读取一级兴趣主题的前五项，并明确要求完整性、质量、水位和未过期：

```sql
SELECT news_id, topic_id, topic_name,
       reader_count, profiled_reader_count, unknown_reader_count,
       topic_reader_count,
       round(100 * share_all, 2) AS share_all_pct,
       round(100 * share_profiled, 2) AS share_profiled_pct,
       baseline_topic_count, baseline_profiled_count,
       lift, low_sample, topic_list_may_be_truncated, source_mode
FROM news_audience.v_news_reader_topic_metrics
WHERE tenant_id = '11111111-1111-4111-8111-111111111111'::uuid
  AND source_release_sha256 = '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'
  AND snapshot_id = '90d919aee5b20f16d3bc3200e91f65d55120b69458388ba6c883a96d1db4954d'
  AND news_id = '20260718A03B9V00'
  AND lookback_hours = 720
  AND topic_level = 1
  AND completeness IN ('complete', 'corrected')
  AND quality_status = 'passed'
  AND watermark_at >= window_end
  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
ORDER BY topic_reader_count DESC, topic_id
LIMIT 5;
```

该样例为 **6 位模拟详情读者**，长期画像人数为 6，AI 兴趣人数为 5，全部读者占比约 83.33%。这些只是核验样例，不是企业真实访问指标。短期改为 `lookback_hours=24`，其余窗口条件保持不变。

### 6.5 联查“新闻全文 + 内容主题 + 读者人数”

```sql
SELECT c.news_id, c.title, c.body,
       c.topic_ids AS content_topic_ids, c.tag_status,
       p.window_start, p.window_end, p.lookback_hours,
       p.reader_count, p.profiled_reader_count,
       p.unknown_reader_count, p.profile_coverage,
       p.source_mode, p.limitations
FROM news_audience.v_news_reader_population_status p
JOIN news_audience.unified_news_binding b
  ON b.tenant_id = p.tenant_id
 AND b.source_release_sha256 = p.source_release_sha256
 AND b.news_id = p.news_id
JOIN public.news_content_tagged_selected c
  ON c.tenant_id = b.content_tenant_id
 AND c.content_release_id = b.content_release_id
 AND c.news_id = b.news_id
 AND c.record_sha256 = b.record_sha256
 AND c.body_sha256 = b.body_sha256
WHERE p.tenant_id = '11111111-1111-4111-8111-111111111111'::uuid
  AND p.source_release_sha256 = '03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'
  AND p.snapshot_id = '90d919aee5b20f16d3bc3200e91f65d55120b69458388ba6c883a96d1db4954d'
  AND p.news_id = '20260718A03B9V00'
  AND p.lookback_hours = 720
  AND p.completeness IN ('complete', 'corrected')
  AND p.quality_status = 'passed'
  AND p.watermark_at >= p.window_end
  AND (p.expires_at IS NULL OR p.expires_at > CURRENT_TIMESTAMP)
  AND c.tenant_id = 'public-news-local'
  AND c.content_release_id = 'news-0c74530f2864db20a73d7a96'
  AND c.tag_release_id = 'tags-33c4054fd01bd98cd5ef2826';
```

这里只展示有精确绑定且有实际人群行的交集。未返回行不代表这篇新闻无人阅读；需要进一步区分内容未绑定、窗口缺失、数据门禁未通过或确实空人群。

包含来源审计、正文联查、主要兴趣和“未知/0 人”判断的现有完整脚本：
[news-audience-local-query.sql](sql/news-audience-local-query.sql)。从仓库根目录执行：

```bash
docker exec -i news-agent-native-e2e-postgres-1 \
  psql -X -v ON_ERROR_STOP=1 -U newsagent_native -d newsagent_unified_news_20261023 \
  < agent/docs/sql/news-audience-local-query.sql
```

这里由宿主机重定向 SQL 文件；不要在容器内用 `\i /Users/...`，宿主机文件并未默认挂载到容器。

<a id="files"></a>
## 7. 原始包、导出素材与个人画像的位置

以下路径均相对仓库根 `/Users/shi/Project/myProject/NewsAgent`。

| 路径 | 内容与用途 |
|---|---|
| [var/news-storage/unified-public-news-20261023/](../../var/news-storage/unified-public-news-20261023/) | 冻结内容包；manifest、全部 versions、来源和选定项，可校验与重建 |
| [var/news-storage/tags/public-news-topics-20261023/](../../var/news-storage/tags/public-news-topics-20261023/) | 冻结主题规则、目录、标签结果与摘要 |
| [var/reader-profiles/local-readers-v1-20261017/](../../var/reader-profiles/local-readers-v1-20261017/) | 受众聚合源包：reader_profiles.sqlite、manifest.json、policy.json；没有个人事件明细 |
| [all-news-with-tags.jsonl](../../var/news-storage/exports/20261023/all-news-with-tags.jsonl) | 21,435 个新闻 ID 的选定记录和内容标签；不是全部历史版本 |
| [body-candidates-with-tags.jsonl](../../var/news-storage/exports/20261023/body-candidates-with-tags.jsonl) | 8,144 篇待选样、待标注的正文素材，带规则标签 |
| [body-candidates.jsonl](../../var/news-storage/exports/20261023/body-candidates.jsonl) | 同一批 8,144 篇候选的原始无新增 tags 版本 |
| [storage-preview.json](../../output/news-storage-unification-20261023/storage-preview.json) | 小体积预览：新闻原文、内容标签、旧合成个人画像示例 |
| [audience-sql-preview.json](../../output/news-storage-unification-20261023/audience-sql-preview.json) | 受众 SQL/CLI 返回预览 |
| [audience-sql-queries-result.json](../../output/news-storage-unification-20261023/audience-sql-queries-result.json) | 已有 5 段 SQL 的实际返回，包括正文联查 |

正文候选过滤了不足 100 字符和元数据待复核记录。8,144 是候选素材量，不是已完成标注的评测集数量。

### 7.1 “用户标签存在哪里”的明确回答

- **新闻内容标签**：已经在 PostgreSQL 的 `public.news_content_tags` 和关系表中。
- **新闻读者群体兴趣标签**：已经在 `news_audience.news_reader_topic` 中，用 SQL 直接查询。
- **逐个合成用户的兴趣画像**：保留在旧包 [var/behavior-tags/local-merged-v1-20261016/](../../var/behavior-tags/local-merged-v1-20261016/)，主要文件为 `synthetic_profiles.jsonl` 与 `label_index.sqlite` 的 `profiles` 表，**未导入本次 PostgreSQL**。历史核对为 1,200 个合成身份、39,600 条画像快照（1,200 × 33），不是企业用户。

旧个人画像以 `tenant_id + synthetic_user_id + as_of` 区分，完整数据在 `payload_json`；包含 topic_weights、behavior_label_ids 等。热点分析应优先使用已经聚合的新闻读者兴趣，避免把个人画像送入模型。

### 7.2 公开正文的上游来源

来源包括已有增量 v2/v1、旧本地正文缓存、历史 BGE 切片和公开标题目录；准确路径及校验方式见 [unified-news-storage.md 第 2 节](unified-news-storage.md#2-数据来源及统一规则)。历史片段只保留原切片，没有拼接伪全文；旧内容索引不因本次导入自动重建。

<a id="call-chain"></a>
## 8. 导入与读取的完整调用链

### 8.1 新闻正文

```text
已有公开 JSON / manifest / 历史 chunks / 标题 catalog
→ unify_news_storage.py inventory / build
→ sources.load_existing_sources：检查来源文件、ID、时间与哈希
→ model.normalize_record / build_package：UTC、内容状态、标准化哈希、保留多版本、确定性选定
→ package_io.save_package / load_package：冻结和校验文件
→ CLI import → FrozenNewsStore.initialize / import_release
→ SQLAlchemy / psycopg → public 四张内容表 + news_content_selected
→ FrozenNewsStore.get_record / list_records / summary
→ CLI get / list / export，或 ReleaseNewsContentRepository 返回 NewsContent
```

`ReleaseNewsContentRepository` 只接收绑定租户/版本，返回满足正文、标题、URL、时间且无需元数据复核的记录；它不自动执行 CLI 的 100 字符候选过滤。该 Port 已有代码，尚未将本次统一库替换为在线 Worker 的内容源。

### 8.2 新闻内容标签

```text
冻结正文包 + 旧标签包中的规则/目录
→ CLI build-tags → legacy_tags.load_legacy_tags
→ topic_labels.build_tag_package：在当前原文重算、核对旧证据、禁止模拟栏目兜底
→ save_tag_package / load_tag_package
→ CLI import-tags → FrozenNewsTagStore.import_release
→ public 三张标签表 + news_content_tagged_selected
→ CLI topics / get / list / export：中文主题名、原文命中、版本和带标签 JSONL
```

`legacy_verified` 表示原文、规则、目录和旧规则结果精确一致，不表示人工标注正确。

### 8.3 受众聚合、正文绑定与查询

```text
既有匿名合成聚合包（reader_profiles.sqlite + manifest + policy）
→ import_reader_audience.py import
→ ReaderWarehouseSource / LocalNewsReaderProfileClient：来源 SHA 与 Reader 合同校验
→ population_rows / topic_rows：将新闻窗口拆为 24h / 720h 人群和主题行
→ warehouse.sql 初始化 news_audience
→ import_package：事务锁、psycopg COPY、固定顺序全量回读摘要、单事务提交
→ package_release + 来源绑定/窗口/主题字典/人群/主题计数表

CLI bind-content
→ bind_unified_content：固定受众 release 与内容 release
→ news_id + 非空正文 SHA 精确比对
→ unified_news_binding

CLI query
→ query_audience：REPEATABLE READ / READ ONLY
→ 校验来源、租户、窗口完整性/质量/水位与新闻资格
→ 人群和主题视图 → 带版本、来源、占比与限制说明的 JSON
```

CLI 单批支持 1–20 个不同新闻 ID，回看期限仅 24/720，查询 top_k 为 1–50；源包最多保留 20 个主题，设置更大的 top_k 不能恢复未导出的主题。当前查询工具仅接受 synthetic 来源；`expires_at` 在当前包为空，未来在线适配仍需落实有效期与版本门禁。

### 8.4 配置与代码入口

| 作用 | 入口 / 配置 |
|---|---|
| 正文与标签 CLI | [unify_news_storage.py](../tools/unify_news_storage.py) |
| 正文核心存储 | [store.py](../app/modules/news_storage/store.py)，`FrozenNewsStore / ReleaseNewsContentRepository` |
| 标签核心存储 | [tag_store.py](../app/modules/news_storage/tag_store.py)，`FrozenNewsTagStore` |
| 受众 CLI | [import_reader_audience.py](../tools/import_reader_audience.py) |
| 受众投影与查询 | [warehouse.py](../app/modules/reader_profiles/warehouse.py)，`ReaderWarehouseSource / import_package / bind_unified_content / query_audience` |
| 受众实际 DDL | [warehouse.sql](../app/modules/reader_profiles/warehouse.sql) |
| 正文/标签连接变量 | `UNIFIED_NEWS_DATABASE_URL`，SQLAlchemy DSN |
| 受众连接变量 | `AUDIENCE_DATABASE_URL`，psycopg DSN |

从仓库根目录设置本地公开演示连接：

```bash
export UNIFIED_NEWS_DATABASE_URL='postgresql+psycopg://YOUR_USER@127.0.0.1:25432/newsagent_unified_news_20261023'
export AUDIENCE_DATABASE_URL='postgresql://YOUR_USER@127.0.0.1:25432/newsagent_unified_news_20261023'
```

这些独立 CLI 只使用显式连接变量，不读取 `.env`。依赖 Python、PostgreSQL、SQLAlchemy、psycopg、SQLite 和已有 Pydantic/规则代码；本次存储与查询不调用 LLM、Embedding、ES 或企业 HTTP 接口。

只读 CLI 示例：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py get \
  --tenant-id public-news-local \
  --release-id news-0c74530f2864db20a73d7a96 \
  --tag-release-id tags-33c4054fd01bd98cd5ef2826 \
  --news-id 20260718A03B9V00

agent/.venv/bin/python agent/tools/import_reader_audience.py query \
  --release 03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5 \
  --tenant 11111111-1111-4111-8111-111111111111 \
  --start 2026-09-24T22:00:00+08:00 \
  --end 2026-09-24T23:00:00+08:00 \
  --news-id 20260718A03B9V00 --lookback-hours 720 --top-k 5
```

已有库无需为浏览重复导入；需要重建时，完整 import / import-tags / bind-content 命令与事务说明见 [统一新闻存储说明](unified-news-storage.md)。本次文档整理没有重新导入或修改库中数据。

<a id="verification"></a>
## 9. 已有验证证据与测试入口

以下是此前导入和功能验收记录，不是模型效果或生产性能指标。本次文档整理另外只读核对了实际数据库结构、14 张表的行数和覆盖范围，并在 READ ONLY 事务中成功执行第 6 节的全部 8 段 SQL；相对文件链接均已检查存在。

| 验证 | 已有结果 | 证据 |
|---|---|---|
| 统一正文导入 | 全量选定记录与来源读回，重复导入幂等 | [report.json](../../output/news-storage-unification-20261023/report.json) |
| 正文及内容标签 | 168 项定向测试；21,435 条联合读回、内容/标签哈希和导出一致 | [tag-integration-report.json](../../output/news-storage-unification-20261023/tag-integration-report.json) |
| 受众初次导入 | `imported_verified`，全部来源投影摘要读回一致 | [audience-import-report.json](../../output/news-storage-unification-20261023/audience-import-report.json) |
| 受众重复导入 | `already_imported_verified`，没有追加行 | [audience-reimport-report.json](../../output/news-storage-unification-20261023/audience-reimport-report.json) |
| 正文精确绑定 | 1,266 组合、661 个不同新闻；重复绑定一致 | [audience-content-binding.json](../../output/news-storage-unification-20261023/audience-content-binding.json) |
| 受众合同与本地查询 | 77 项相关测试，10 项实际 PG 集成检查，现有 5 段 SQL 执行通过 | [audience-integration-validation.json](../../output/news-storage-unification-20261023/audience-integration-validation.json) |

测试入口（需要检查代码修改时再运行）：

```bash
agent/.venv/bin/python -m pytest -q agent/tests/test_unified_news_*.py

agent/.venv/bin/python -m pytest -q \
  agent/tests/test_reader_profile_warehouse.py \
  agent/tests/test_reader_profile_contracts.py \
  agent/tests/test_reader_profile_local.py
```

本次文档修改未重新运行上述功能测试；此前报告为可追溯证据。本地功能验证不等于 TDSQL-A 方言、生产负载、真实数据质量或模型质量验收。

<a id="todo"></a>
## 10. 使用边界与未完成事项

| 项目 | 当前状态 / TODO |
|---|---|
| 本地统一存储 | 正文、内容主题、受众聚合表和查询已落地；数据为冻结包，没有持续同步任务 |
| 个人行为 | 未将企业原始个人行为或个人画像导入本库；旧个人画像是独立合成包 |
| 企业实时数仓 | 用户确认背景为 TDSQL-A / PostgreSQL 兼容；企业连接、DDL/权限/性能尚需联调 |
| 企业正文对象存储 | 用户确认企业原文在对象存储；本地原文仍在 PostgreSQL，尚未落实企业对象键和读取适配 |
| ES 检索 | 未接入线上 ES。按 news_id 读取已知全文不依赖 ES；相关内容全文检索可单独评估 |
| 在线 Agent | 本次统一库没有自动替换 Worker 内容源；受众 PG 查询尚未装配成在线 SQL Reader Port |
| 热度与排行 | 由已有行为指标与 Python 确定性链路计算；本库受众占比不是权威热度，也不能把 reader_count 当作点击 UV |
| 租户权限 | 当前 tenant_id 为逻辑隔离；正式数据库角色授权与 RLS 尚待落实，不能声称已有生产隔离验收 |
| 更新与版本 | 冻结包可追溯；持续增量、迟到数据修订、多版本选择、有效期和线上发布仍待设计/接入 |
| 评测集 | 8,144 篇为输入候选；需人工审阅与标注、冻结任务和数据划分，不能把规则标签直接当金标 |
| 受众分布 | 源只保留 Top20；完整分布需上游重新提供，不可从当前表补造 |

旧文件 [news-reader-audience-proposal-v1.sql](sql/news-reader-audience-proposal-v1.sql) 是设计稿，验证时已回滚；**不要去查询 `news_audience_proposal_v1`**。当前实际对象以 `news_audience` 和本文字典为准。

完整的已有热点链路是：行为指标查询 → Python 热度与排行 → 新闻正文/相关证据 → ReaderProfileService 聚合兴趣 → HotNewsAnalysisInputBuilder → 模型解释 → 结构化校验与报告。本次库表为其中的正文、内容主题与受众查询提供离线可验证的数据基础；在线接线和企业真实数据仍属于后续工作。

在仓库根目录快速检索本手册：

```bash
rg -n 'news_reader_population|reader_count|lift|body|topic_ids' agent/docs/news-storage-database-reference.md
```
