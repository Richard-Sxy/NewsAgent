# 统一新闻内容存储与后续评测素材

更新时间：2026-10-23。本文说明已经落地的公开新闻内容存储，不将本地 PostgreSQL 验证描述为企业 TDSQL-A 验收。第 1–6 节保留首次内容统一的结果；第 7–10 节说明随后增加的内容主题标签与历史模拟聚合关联。

## 1. 现在可以直接使用什么

已将以前采集的公开新闻缓存、历史检索切片和公开标题汇总为同一个冻结内容版本，并实际导入本地 PostgreSQL。

| 项目 | 实际值 |
|---|---|
| 数据库 | `127.0.0.1:25432/newsagent_unified_news_20261023`，PostgreSQL 16.15 |
| 运行方式 | 共用现有 `native-e2e` PostgreSQL 实例及数据卷，通过独立 database 隔离；没有新建独立数据库容器 |
| 逻辑租户 | `public-news-local` |
| 冻结版本 | `news-0c74530f2864db20a73d7a96` |
| 完整版本身份 SHA-256 | `0c74530f2864db20a73d7a960cec3649085e9db5fdb054bbf0bfbfc21a58375e` |
| 新闻 ID 数 | **21,435** |
| 内容记录版本数 | **21,828**；同一新闻的缓存正文与旧切片作为不同记录版本保留 |
| 来源观察数 | **22,369**；相同内容来自不同采集包时，保留各自来源 |
| 选定记录构成 | **8,441** 篇有缓存正文、**11,794** 篇只有历史切片、**1,200** 条只有标题 |
| 已导出正文素材 | **8,144** 篇；排除不足 100 字符的 290 篇和元数据待复核的 7 篇 |

直接使用以下现成产物，无需先重新采集或重建：

- 冻结包：[var/news-storage/unified-public-news-20261023](../../var/news-storage/unified-public-news-20261023)。
- 正文素材：[body-candidates.jsonl](../../var/news-storage/exports/20261023/body-candidates.jsonl)。
- 实际验收：[report.json](../../output/news-storage-unification-20261023/report.json)。

素材文件 SHA-256：`9e638bcbae404f1ad7a3b51e6bb7e1654c94fb8d19499590a518cec5acef63fb`。

**这些是制作评测集的输入素材，不是已标注评测集。** `body_available` 只表示保存了抓取到的正文，不代表人工认证其完整性或事实准确性；100 字符是本次导出过滤条件，不是质量合格线。后续仍需选择任务、审阅正文、建立标准答案和证据，并按事件或新闻来源划分开发集与测试集。

## 2. 数据来源及统一规则

| 来源包 | 仓库内路径 | 候选数 | 优先级 | 保留方式 |
|---|---|---:|---:|---|
| 公开新闻增量 v2 | `var/news/data/incremental/2026-10-07_2026-10-14-v2/` | 8,000 | 10 | 原始文章 JSON，核对 manifest、文件 SHA、标题与正文联合哈希、ID 和时间 |
| 公开正文缓存 | `news/data/local_recall/articles-v1/` | 441 | 20 | 按 collection manifest 顺序校验整个缓存集合；来源的无时区时间按已知上海时区解释并留标记 |
| 公开新闻增量 v1 | `var/news/data/incremental/2026-10-07_2026-10-14-v1/` | 541 | 30 | 全部已在 v2 出现，仍保留 v1 来源证据 |
| 历史 BGE 新闻切片 | `var/knowledge-snapshots/rag-recall-demo/data/experiments/offline_recall_fg/chunks.jsonl` | 12,187 个 ID | 100 | 保存 36,379 个原始切片；不读取向量文件，不拼接成全文 |
| 公开新闻标题 | `agent/data/public-headlines/2026-10-07/catalog.json` | 1,200 | 200 | 348 条 article、852 条 video，保留原始类型及标题元数据，正文为空 |

`news/data/incremental` 是指向 `var/news/data/incremental` 的兼容链接，不作为另一份来源重复导入。旧 v8 所引用的 `catalog-extension-v2.json` 当前缺失，本次直接验证现存的原始缓存，不依赖旧目录的历史统计或缺失文件。

新增 BGE v2 索引中的 8,000 篇文章已有原始缓存，本次不复制该派生索引。旧 BGE 中只有 393 个 ID 能与上述旧缓存对应，其他 11,794 个 ID 保持 `fragments_only`。不从片段里的文字猜测结构化标题、发布时间或 URL。

同一 `news_id` 的选定版本按以下元组取最小值，与 `model.build_package` 和 `sources.source_summary` 一致：

```text
内容状态排序（body_available=0，fragments_only=1，title_only=2）
→ 来源优先级（数字越小越优先）
→ 标准化 record_sha256（确定性处理并列）
```

所有观察到的内容版本及来源均保留。不同新闻 ID 即使正文完全相同也不合并 ID；当前有 6 组重复正文、涉及 8 个额外 ID。对于同一 ID 的不同正文，选定记录另加 `conflicting_body_variants`，不覆盖历史版本；本批没有这种正文冲突。

发布时间在标准记录中统一为 UTC ISO 时间，SQL 使用 `TIMESTAMP WITH TIME ZONE`。缺失信息保持 `NULL`，不从新闻 ID、采集时间或切片猜测发布时间。正文、标题和外部来源内容始终是数据，不能作为系统指令。

## 3. 四张表逐字段字典

实际定义见 [store.py](../app/modules/news_storage/store.py)。以下类型为 PostgreSQL 类型；SQLite 仅用于隔离测试。主键自动非空；枚举、哈希及内容状态关系由 Python 校验，不能将其误称为数据库已经配置的所有 `CHECK` 约束。

### 3.1 `news_content_versions`：不可变内容记录

粒度：`tenant_id + news_id + record_sha256`。不同来源若标准内容完全相同，共用一条记录；同一新闻的不同记录并存。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | VARCHAR(128) | 否，主键 | 逻辑数据域，当前为 `public-news-local` |
| `news_id` | VARCHAR(160) | 否，主键 | 原始新闻关联 ID，不因重新导入而变更 |
| `record_sha256` | VARCHAR(64) | 否，主键 | 标准化内容记录的 SHA-256；覆盖正文、元数据、切片和质量标记，排除哈希字段自身 |
| `title` | TEXT | 是 | 来源标题；历史切片没有可信结构化标题时为空 |
| `body` | TEXT | 是 | 抓取正文，只在 `body_available` 状态保存；标题和切片不冒充正文 |
| `url` | TEXT | 是 | 已知原始 HTTPS 页面链接；不根据 ID 拼造缺失链接 |
| `publish_time` | TIMESTAMP WITH TIME ZONE | 是 | 来源发布时间，规范化为 UTC；未知时为空 |
| `content_type` | VARCHAR(16) | 否 | `article` 或 `video`；标题目录中的视频类型保留 |
| `content_state` | VARCHAR(24) | 否 | `body_available`、`fragments_only`、`title_only` |
| `body_sha256` | VARCHAR(64) | 是 | 正文 UTF-8 字节 SHA-256；无正文为空。与整条记录哈希不同 |
| `record` | JSONB | 否 | 只存 `fragments`、`quality_flags` 两个补充子项；不重复保存正文 |

索引：`ix_news_content_versions_state(tenant_id, content_state)`。主键还提供租户、新闻 ID 与记录哈希的联合索引。

`record` 的两项结构：

| 子项 | 类型 | 规则 |
|---|---|---|
| `fragments` | 对象数组 | 正文/标题记录通常为空；每项含非负整数 `chunk_index`、非空字符串 `text`，历史导入额外含 `chunk_id` |
| `quality_flags` | 去重、排序的字符串数组 | 内容记录本身的缺失、短文本、元数据待复核等标记；参与记录哈希 |

历史 `chunk_id` 按源文件行号和行哈希生成，用于区分重复 `chunk_index` 的观察，不宣称是原发布方的内容版本。重复序号原样保留，不据此重建全文。

### 3.2 `news_content_releases`：冻结版本清单

粒度：`tenant_id + release_id`。一个 release 绑定一组内容版本、来源和选定记录。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | VARCHAR(128) | 否，主键 | 冻结版本所属逻辑数据域 |
| `release_id` | VARCHAR(160) | 否，主键 | `news-` 加版本身份哈希前 24 位；读取必须显式指定 |
| `identity_sha256` | VARCHAR(64) | 否 | 完整身份哈希，绑定租户、schema、选择策略、内容记录、来源及选定关系 |
| `schema_version` | VARCHAR(64) | 否 | 当前 `unified-news-content-v1` |
| `selection_policy` | VARCHAR(128) | 否 | 当前 `prefer-cached-body-source-priority-v1` |
| `status` | VARCHAR(16) | 否 | 当前发布状态为 `frozen`；读取只接受该状态 |
| `manifest` | JSONB | 否 | 版本完整清单：租户、版本 ID、完整哈希、schema、选择策略和 `counts` |
| `created_at` | TIMESTAMP WITH TIME ZONE | 否 | 本地数据库首次导入时间，UTC；不作为新闻发布时间或内容版本身份 |

`manifest.counts` 包含 `news_count`、`version_count`、`provenance_count`、三种 `*_count` 内容状态计数以及 `conflicting_body_news_count`。

### 3.3 `news_content_provenance`：来源证据

粒度：`tenant_id + release_id + news_id + record_sha256 + source_sha256`。分别通过联合外键关联冻结版本表和内容版本表。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | VARCHAR(128) | 否，主键 | 与版本及内容记录相同的逻辑域 |
| `release_id` | VARCHAR(160) | 否，主键 | 该来源被哪个冻结版本引用 |
| `news_id` | VARCHAR(160) | 否，主键 | 来源观察对应的新闻 ID |
| `record_sha256` | VARCHAR(64) | 否，主键 | 来源观察绑定的标准内容版本 |
| `source_sha256` | VARCHAR(64) | 否，主键 | **整个 `source` 对象的规范 JSON 哈希**，不是源文件哈希 |
| `source` | JSONB | 否 | 原始包、文件、manifest、源内容版本、采集时间及选择优先级 |

`source` 对象当前字段：

| 字段 | 类型/可空 | 含义 |
|---|---|---|
| `package` | 字符串，非空 | 来源包名称，如 `incremental-20261014-v2` |
| `path` | 字符串，非空 | 相对于仓库根的源文件路径；拒绝绝对路径和上级目录逃逸 |
| `file_sha256` | 64 位十六进制字符串，非空 | 原始源文件字节 SHA-256 |
| `manifest_path` | 字符串，可空 | 相对路径；BGE 为 `SHA256SUMS`，其他来源为相应 JSON 清单 |
| `manifest_sha256` | 64 位十六进制字符串，可空 | 对应清单文件的字节 SHA-256 |
| `source_content_version` | 字符串，可空 | 来源明确提供的内容版本；未知保持空，不自动写成 1 |
| `fetched_at` | 带时区 ISO 时间字符串，可空 | 原始采集/目录收集时间，不代替发布时间 |
| `selection_priority` | 非负整数，非空 | `build_package` 将候选的 `priority` 写入来源对象，保证选择规则可复核 |

### 3.4 `news_content_release_items`：每篇新闻的选定版本

粒度：`tenant_id + release_id + news_id`。联合外键关联 release 和选中的内容版本。

| 字段 | 类型 | 可空 | 含义 |
|---|---|---|---|
| `tenant_id` | VARCHAR(128) | 否，主键 | 逻辑数据域 |
| `release_id` | VARCHAR(160) | 否，主键 | 固定读取版本 |
| `news_id` | VARCHAR(160) | 否，主键 | 每个 release 内每个新闻 ID 恰好一条选定关系 |
| `record_sha256` | VARCHAR(64) | 否 | 按版本化策略选出的记录哈希 |
| `quality_flags` | JSONB 字符串数组 | 否 | 选定记录的标记，并补充跨版本正文冲突标记；不修改原记录 |

## 4. 统一读取视图与质量标记

视图 `news_content_selected` 联接上述 release、items、versions，仅返回 `status='frozen'` 的版本。**它包含所有冻结版本，查询必须同时限定 `tenant_id` 和 `release_id`**，不代表“自动选择最新版本”。

| 视图字段 | 来源/类型 | 含义 |
|---|---|---|
| `tenant_id` | release，VARCHAR(128) | 逻辑域 |
| `release_id` | release，VARCHAR(160) | 读取版本 |
| `identity_sha256` | release，VARCHAR(64) | 版本完整身份 |
| `news_id` | versions，VARCHAR(160) | 新闻 ID |
| `record_sha256` | versions，VARCHAR(64) | 选定内容记录哈希 |
| `body_sha256` | versions，VARCHAR(64)，可空 | 正文哈希 |
| `title` | versions，TEXT，可空 | 标题 |
| `body` | versions，TEXT，可空 | 缓存正文 |
| `url` | versions，TEXT，可空 | 公开链接 |
| `publish_time` | versions，TIMESTAMPTZ，可空 | 发布时间 |
| `content_type` | versions，VARCHAR(16) | article/video |
| `content_state` | versions，VARCHAR(24) | 正文/切片/标题状态 |
| `item_quality_flags` | items，JSONB | 本 release 选定记录的质量标记 |
| `record` | versions，JSONB | `fragments` 和记录级 `quality_flags` |

SQL 查询示例：

```sql
SELECT news_id, title, url, publish_time, body_sha256, item_quality_flags
FROM news_content_selected
WHERE tenant_id = 'public-news-local'
  AND release_id = 'news-0c74530f2864db20a73d7a96'
  AND content_state = 'body_available'
ORDER BY news_id
LIMIT 20;
```

主要标记的处理含义：

| 标记 | 含义及后续处理 |
|---|---|
| `cached_body_completeness_unreviewed` | 缓存有正文但尚未逐篇人工确认完整性；全部缓存正文保留此标记 |
| `body_under_100_chars`、`short_body_lt_100` | 分别来自源加载和标准记录校验的短正文标记；本次素材导出按实际长度排除不足 100 字符的记录 |
| `body_under_300_chars` | 来源正文不足 300 字符，仅为审阅提示，不等于无效新闻 |
| `metadata_review_required` | 7 个既有明确待复核 ID；本次素材导出及内容 Port 排除 |
| `source_naive_time_assumed_asia_shanghai` | 旧缓存没有时区；根据该采集来源的已知规则解释为上海时区后转 UTC |
| `fragment_only`、`title_only`、`body_missing` | 只有片段或标题，不进入本次正文素材导出 |
| `source_*_missing`、`missing_title/url/publish_time` | 来源或标准记录缺失对应字段；保留空值和标记 |
| `duplicate_chunk_index`、`missing_chunk_index` | 切片序号重复或有缺口；不据此宣称可恢复全文 |
| `conflicting_body_variants` | 同一新闻存在不同正文哈希，需要审阅选择；本批计数为 0 |

质量标记非空并不一概表示不可用，应按任务明确过滤条件。主键、联合外键和应用查询共同限定租户范围；当前 `tenant_id` 是**逻辑隔离字段，没有启用数据库 RLS，也没有实现数据库用户级授权**。拥有底层全库权限的账号仍能直接查询其他逻辑域。

## 5. 命令行操作

以下命令从仓库根执行，使用 `agent/.venv/bin/python`。数据库地址只从显式环境变量读取，默认名称为 `UNIFIED_NEWS_DATABASE_URL`；所有子命令都不自动读取 `.env`，不初始化应用 `Settings`。

本地现有 `native-e2e` 使用仓库 Compose 中公开声明的演示账号，以下命令可直接用于本机独立新闻数据库，不需要查找或读取 `.env`：

```bash
export UNIFIED_NEWS_DATABASE_URL='postgresql+psycopg://YOUR_USER@127.0.0.1:25432/newsagent_unified_news_20261023'
```

账号来源为 [docker-compose.native-e2e.yml](../../deploy/docker-compose.native-e2e.yml) 中的 `POSTGRES_USER` / `POSTGRES_PASSWORD`；这是公开本地演示账号，不是企业凭据。连接可通过 `--database-url-env YOUR_EXPLICIT_VARIABLE` 改为另一个显式环境变量。数据库不存在时需要先在允许使用的 PostgreSQL 实例创建空数据库；`--initialize` 只建缺失表/视图，不创建 database。

先查看现成版本：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py summary \
  --tenant-id public-news-local \
  --release-id news-0c74530f2864db20a73d7a96

agent/.venv/bin/python agent/tools/unify_news_storage.py get \
  --tenant-id public-news-local \
  --release-id news-0c74530f2864db20a73d7a96 \
  --news-id 20260718A03B9V00
```

离线核对来源和包，无需数据库连接：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py inventory --root .

agent/.venv/bin/python agent/tools/unify_news_storage.py verify-package \
  --package var/news-storage/unified-public-news-20261023
```

需要重建包时使用**不存在的新目录**；下面的示例目录不覆盖已交付冻结包：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py build \
  --root . --tenant-id public-news-local \
  --output var/news-storage/unified-public-news-20261023-rebuild-01
```

恢复或导入现成包：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py import \
  --package var/news-storage/unified-public-news-20261023 --initialize
```

首次导入会在一个事务中写入、从 SQL 重建整个包并复核身份后提交。同一包重复导入返回 `already_present`，不增加行数；已存在但内容不同的版本会被拒绝。`initialize` 不清空现有表，也不升级不兼容表结构。若本地数据库或卷被重建，可以在空数据库中使用冻结包恢复；无需重新采集、调用模型或计算 Embedding。

正文素材已经存在。需要再次导出时给出新文件名，避免覆盖已被评测引用的输入：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py export \
  --tenant-id public-news-local \
  --release-id news-0c74530f2864db20a73d7a96 \
  --body-only --min-body-chars 100 --exclude-metadata-review \
  --output var/news-storage/exports/20261023/body-candidates-copy-01.jsonl
```

`export` 每批最多 500 条，按 `news_id` 游标前进；文件排他创建，已有路径直接报错。每条导出记录包含标准内容字段，以及 `tenant_id`、`release_id`、`selection_quality_flags`、选定记录对应的 `provenance`。命令结果返回文件 SHA、过滤条件和排除计数；正文长度使用 Python 字符串长度，不是 UTF-8 字节数。原始包仍完整保留被素材过滤排除的记录。

## 6. 完整调用链、验证入口和剩余工作

```text
历史公开文章 JSON / manifest / BGE chunks / 标题 catalog
→ sources.load_existing_sources：来源哈希、路径边界、身份、时间及质量检查
→ model.normalize_record：UTC、内容状态、正文哈希、记录哈希
→ model.build_package：保留多版本与来源，确定性选定记录，冻结版本身份
→ package_io.save_package / load_package：JSONL 文件与清单哈希校验
→ tools/unify_news_storage.py：显式数据库环境变量和操作参数
→ SQLAlchemy Engine → FrozenNewsStore.initialize / import_release
→ PostgreSQL 四表 + news_content_selected 视图
→ get_record / get_records / list_records / summary
→ CLI get/export 或显式创建的 ReleaseNewsContentRepository
→ JSONL 素材或 NewsContent Port 返回值
```

依赖边界：来源、标准化和冻结文件处理只使用 Python 标准库；数据库操作使用 SQLAlchemy、PostgreSQL 驱动，适配器复用现有 `NewsContent`/`ContentType` 类型。来源清单路径和 SHA 是版本化常量；CLI 参数包括仓库根、租户、包路径、release、数据库变量名及导出过滤项。此链路无新闻抓取、LLM 或 Embedding 调用，也不读取企业行为数据。

`ReleaseNewsContentRepository` 绑定一个明确的租户和 release，通过 `asyncio.to_thread` 调用同步 SQL 读取。正文读取 Port 仅返回有正文、标题、URL、发布时间且不在元数据待复核名单的记录，`collection_id=None`，不冒充已激活的向量集合。该 Port 尚未接入在线 Worker；它本身也没有应用导出命令的 100 字符筛选规则，使用时需明确任务过滤政策。

首次内容存储验收记录见前述 `report.json`：四表行数与冻结包一致，21,435 条选定记录的哈希、元数据、来源均逐条比对；分页无重复或遗漏；错误租户被拒绝；重复导入返回 `already_present`；内容 Port 的正文与元数据过滤通过。当时 66 个针对性测试通过。首次验收的测试入口：

```bash
PYTHONPATH=agent agent/.venv/bin/python -m pytest -q \
  agent/tests/test_unified_news_model.py \
  agent/tests/test_unified_news_package_io.py \
  agent/tests/test_unified_news_store.py
```

测试覆盖不可变版本、确定性选择、多来源、片段边界、篡改、文件落盘/失败清理、事务、重复导入、租户范围、分页和 Port 行为。存储自动测试使用临时 SQLite；本地 PostgreSQL 的实际验收是单独完成的，不等同于真实 TDSQL-A 的兼容验证。

后续 TODO：

1. 在实际 TDSQL-A／PostgreSQL 兼容环境验证驱动、JSONB、时区、事务、联合约束和视图语法；确认企业内容主键及真实字段映射后再适配，不将本地演示账号或公开新闻规模当作企业信息。
2. 为企业访问配置服务身份、最小权限以及适用的数据库行级权限策略；当前应用级 tenant 约束不能替代数据库权限。
3. 将冻结内容投影到现有在线内容生命周期时，显式处理撤稿、修订、权限、索引版本与人工门禁；本次没有替换在线 Worker、数仓行为视图或已有知识索引。
4. 以已导出的 8,144 篇为候选开展人工抽样、问题设计、答案及证据标注；后续带主题版本见第 7–10 节，其推断标签仍需人工审核。固定内容 release、标签 release、文件 SHA 和任务划分后再执行质量评测。本次没有产生模型质量指标。

## 7. 新闻内容与已有主题标签的关联

以前的标签并未丢失，保存在 [旧标签包](../../var/behavior-tags/local-merged-v1-20261016)。此前统一存储只收录新闻原文、来源和质量标记，未把该包的主题字段并入新内容表。本次在内容表之外增加独立、不可变的主题标签版本，原始内容 release 保持不变。

本次已生成冻结标签包并成功导入本地 PostgreSQL。[标签 Manifest](../../var/news-storage/tags/public-news-topics-20261023/manifest.json) 绑定以下结果：

| 项目 | 实际值 |
|---|---|
| 内容版本 | `news-0c74530f2864db20a73d7a96` |
| 标签版本 | `tags-33c4054fd01bd98cd5ef2826` |
| 标签身份 SHA-256 | `33c4054fd01bd98cd5ef2826555ecde7299205dd0d4f11996095112007479443` |
| 主题目录版本 | `news-behavior-topics-demo-v1` |
| 规则版本 | `news-behavior-topic-rules-v1` |
| 推导规则 | `verified-text-only-topics-v1` |
| 标签记录 | **21,435**，每个选定新闻 ID 恰好一条 |
| 有文本依据且命中主题 `tagged` | **7,566** |
| 有可用文本但未命中 `unclassified` | **2,068** |
| 只有历史片段且元数据不足 `insufficient_input` | **11,794** |
| 元数据待复核 `metadata_review` | **7** |
| 新闻与主题关联数 | **35,888**；单篇可以命中多个子主题及父主题 |
| 关联旧内容标签的新闻 | **781** |
| 重算结果与旧纯规则标签一致 `legacy_verified` | **670** |

单看 8,441 篇有缓存正文的新闻，7,212 篇命中主题、1,222 篇未命中、7 篇待元数据复核。主题总命中数 7,566 还包含 354 条仅有标题的新闻，不能将总命中数全都计作正文覆盖率。

旧包有 2,474 条按租户保存的内容标签、1,265 个唯一新闻 ID，其中有合成新闻。与本次公开内容相交的只有 **661 篇正文和 120 条标题**。661 篇正文的标题与正文 SHA 均精确一致，其中 625 篇为纯文本规则标签，36 篇依赖模拟栏目兜底；120 条标题中，45 条为纯规则、75 条依赖模拟栏目兜底。相交公开新闻的跨旧租户标签一致。

本次对统一内容重新运行同一版本的关键词规则，不向规则输入模拟栏目。`legacy_verified=670` 对应 625 篇正文及 45 条标题的规则结果一致；其余旧兜底标签只保留审计引用，不能作为当前新闻主题。新增的 7,780 篇正文也按同一规则计算，没有依靠旧标签覆盖率凑齐分类。

主题目录为 **12 个根主题、72 个细分主题，状态为 `draft`**。这是本项目版本化草案，不是腾讯正式分类规范。主题由标题、正文关键词以及父主题继承产生；规则分数是匹配权重，不是模型置信概率。无法分类时保留空主题，缺失标题的历史片段不拼成正文参与分类，7 条元数据待复核新闻保持阻断状态。标签器对标题超过 1,024 字符或正文超过 1,000,000 字符的记录返回 `insufficient_input`，不截断原文；本批没有这类超限记录，11,794 条输入不足记录全部来自历史片段。

**所有主题都是 `labels_are_inferred=true` 的推断标签，不是人工评测金标。** 旧行为记录则来自本地模拟数仓聚合，不能用来报告真实用户 CTR、真实推荐效果或企业运营收益。

## 8. 新增三张标签表、视图与输出字段

定义见 [tag_store.py](../app/modules/news_storage/tag_store.py)。标签表与内容表共享同一独立 database，不修改既有正文或原冻结包。

### 8.1 `news_content_tag_releases`：冻结标签版本

主键：`tenant_id + content_release_id + tag_release_id`；联合外键关联对应内容 release。

| 字段 | PostgreSQL 类型 | 含义 |
|---|---|---|
| `tenant_id` | VARCHAR(128) | 标签与内容的逻辑数据域 |
| `content_release_id` | VARCHAR(160) | 明确绑定的内容 release |
| `tag_release_id` | VARCHAR(160) | `tags-` 加标签身份哈希前 24 位 |
| `content_identity_sha256` | VARCHAR(64) | 完整内容版本身份，防止标签接到另一个内容集合 |
| `identity_sha256` | VARCHAR(64) | 标签包身份，绑定内容、规则、标签及旧来源摘要 |
| `schema_version` | VARCHAR(64) | 当前 `unified-news-topics-v1` |
| `derivation_version` | VARCHAR(128) | 本次采用的文本分类与旧标签比较规则版本 |
| `policy_version` | VARCHAR(128) | 关键词规则版本 |
| `taxonomy_version` | VARCHAR(128) | 主题目录版本 |
| `tagger_sha256` | VARCHAR(64) | 标签器、主题目录实现与推导实现的联合代码摘要 |
| `status` | VARCHAR(16) | 当前为 `frozen`，只读取已冻结版本 |
| `manifest` | JSONB | 完整清单、计数及 `legacy_source` 证据绑定 |
| `policy` | JSONB | 本版本实际使用的目录与规则，不依赖运行时随意更换的文件 |
| `created_at` | TIMESTAMPTZ | 首次导入时间，不参与标签内容身份 |

所有字段非空。`legacy_source` 保存旧包版本、Manifest 文件摘要与自摘要、policy/content_labels 文件摘要、目录与规则版本；Manifest 文件摘要同时用于后续模拟行为查询，不能自动切换到另一份旧包。

### 8.2 `news_content_tags`：选定新闻的主题及证据

主键：`tenant_id + content_release_id + tag_release_id + news_id`。外键分别指向标签版本、内容选定关系、精确内容记录。每个选定新闻都有状态记录，不只存命中分类的新闻。

| 字段 | PostgreSQL 类型 | 含义 |
|---|---|---|
| `tenant_id` | VARCHAR(128) | 逻辑数据域 |
| `content_release_id`、`tag_release_id`、`news_id` | VARCHAR(160) | 三个独立身份，查询时均明确绑定 |
| `record_sha256` | VARCHAR(64) | 精确内容记录哈希，覆盖标题与元数据，避免只核对正文而漏掉标题更新 |
| `body_sha256` | VARCHAR(64)，可空 | 正文哈希，与内容表一致；只有标题时为空 |
| `label_sha256` | VARCHAR(64) | 完整标签对象规范 JSON 哈希，不含自身 |
| `label_status` | VARCHAR(32) | `tagged` / `unclassified` / `insufficient_input` / `metadata_review` |
| `input_scope` | VARCHAR(24) | `title_body` / `title_only` / `none` |
| `provenance` | VARCHAR(32) | `rule_inferred`、`legacy_verified`，或相应未分类/阻断状态 |
| `topic_ids` | JSONB | 去重、排序的主题 ID 数组，可为空 |
| `label` | JSONB | 标签全量信息，包括分数、原文证据和旧标签引用 |

除 `body_sha256` 外，字段均非空。CLI `get/export` 返回原内容记录并增加完整 `tags` 对象；`list` 只返回其紧凑摘要。完整对象的主要子字段如下：

| 子字段 | 内容 |
|---|---|
| `topic_ids`、`topic_names` | 主题 ID 与目录中的中文显示名称，两数组顺序相同；`topic_names` 在读取时从本版本目录生成 |
| `tag_release_id`、`policy_version`、`taxonomy_version` | 本次读取所固定的三个版本 |
| `label_status`、`input_scope`、`provenance` | 分类结果、实际文本范围、推导或复核来源 |
| `topic_scores` | 每个主题的关键词权重；父主题继承命中的子主题最高分 |
| `matches` | 具体命中项：`topic_id/rule_id/field/term/start/end/score/source`；位置对应原始标题或缓存正文 |
| `legacy_refs` | 精确标题、正文相同的旧标签引用，含原租户、旧标签哈希、旧主题及 `compatible`，不把旧租户等同于新租户 |
| `labels_are_inferred` | 固定为 `true`，避免导出后被当成人工金标 |
| `record_sha256`、`body_sha256`、`label_sha256` | 内容记录、正文、标签三个不同哈希 |

`legacy_refs.compatible=true` 只表示旧纯规则主题、分数、原文命中、目录与规则版本都和本次重算一致。引用中的旧主题可能包括模拟栏目兜底结果；这种引用为不兼容，当前 `tags.topic_ids` 不使用它。

### 8.3 `news_content_topic_memberships`：主题过滤索引

| 字段 | PostgreSQL 类型 | 含义 |
|---|---|---|
| `tenant_id` | VARCHAR(128) | 逻辑域 |
| `content_release_id`、`tag_release_id`、`news_id` | VARCHAR(160) | 被索引的标签记录 |
| `topic_id` | VARCHAR(128) | 单个主题 ID，包括继承的父主题 |

五字段共同构成主键，均非空；前四字段通过外键关联 `news_content_tags`。索引 `ix_news_content_topic_lookup` 按租户、内容版本、标签版本、主题、新闻 ID 查找。SQL 主题过滤先于 `LIMIT`，不会因为某一页没有命中而遗漏后续新闻；多个 `--topic` 为任一匹配，单篇不会重复返回。

统一视图 `news_content_tagged_selected` 联接内容选定视图、标签记录、标签 release，仅返回精确内容哈希匹配且标签状态为 `frozen` 的记录。视图含原文及原质量标记，并增加 `content_release_id/tag_release_id/content_identity_sha256/tag_identity_sha256/policy_version/taxonomy_version/tagger_sha256/topic_ids/tag_status/input_scope/tag_provenance/label_sha256/label`。**查询必须限定逻辑租户、内容版本和标签版本，不存在隐式“最新标签”。**

```sql
SELECT news_id, title, body, topic_ids, tag_status, tag_provenance, label
FROM news_content_tagged_selected
WHERE tenant_id = 'public-news-local'
  AND content_release_id = 'news-0c74530f2864db20a73d7a96'
  AND tag_release_id = 'tags-33c4054fd01bd98cd5ef2826'
ORDER BY news_id
LIMIT 20;
```

## 9. 查看标签、筛选新闻与关联历史聚合

先在仓库根目录执行一次，设置公开本地演示连接和明确版本：

```bash
export UNIFIED_NEWS_DATABASE_URL='postgresql+psycopg://YOUR_USER@127.0.0.1:25432/newsagent_unified_news_20261023'
export NEWS_CONTENT_RELEASE='news-0c74530f2864db20a73d7a96'
export NEWS_TAG_RELEASE='tags-33c4054fd01bd98cd5ef2826'
```

查看主题目录和当前版本各主题的新闻数，再筛选财经新闻：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py topics \
  --tenant-id public-news-local --release-id "$NEWS_CONTENT_RELEASE" \
  --tag-release-id "$NEWS_TAG_RELEASE"

agent/.venv/bin/python agent/tools/unify_news_storage.py list \
  --tenant-id public-news-local --release-id "$NEWS_CONTENT_RELEASE" \
  --tag-release-id "$NEWS_TAG_RELEASE" --topic finance --limit 20
```

`list` 返回 `news_id/title/url/publish_time/content_state/record_sha256`，以及 `tags` 中的标签版本、主题 ID/名称、状态、输入范围和来源摘要；不返回正文、切片、完整质量标记或 `matches`。`next_after_news_id` 作为下一次的 `--after-news-id`。使用 `get` 查看单篇完整原文、质量信息及命中证据：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py get \
  --tenant-id public-news-local --release-id "$NEWS_CONTENT_RELEASE" \
  --tag-release-id "$NEWS_TAG_RELEASE" --news-id 20260718A03B9V00

agent/.venv/bin/python agent/tools/unify_news_storage.py summary \
  --tenant-id public-news-local --release-id "$NEWS_CONTENT_RELEASE" \
  --tag-release-id "$NEWS_TAG_RELEASE"
```

本次已经导出以下两个带标签文件：

| 产物 | 记录数 | SHA-256 |
|---|---:|---|
| [正文候选素材](../../var/news-storage/exports/20261023/body-candidates-with-tags.jsonl) | **8,144** | `f46befd413136176817881ef8eca15873dc58525ae80854d1cdd4105f025379f` |
| [全部选定新闻](../../var/news-storage/exports/20261023/all-news-with-tags.jsonl) | **21,435** | `6776c250fb14c5c82679e6b94e35d1933b0d351baf25f8f3d5e44067b6891ec8` |

后者保留片段、标题及未分类状态；“全量”指本 release 每个 ID 的选定记录，历史其他内容版本仍在原 `versions.jsonl`。8,144 篇正文候选与先前导出逐字段相同，只新增 `tags`。再次导出应使用新文件名：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py export \
  --tenant-id public-news-local --release-id "$NEWS_CONTENT_RELEASE" \
  --tag-release-id "$NEWS_TAG_RELEASE" \
  --body-only --min-body-chars 100 --exclude-metadata-review \
  --output var/news-storage/exports/20261023/body-with-tags-copy-01.jsonl

agent/.venv/bin/python agent/tools/unify_news_storage.py export \
  --tenant-id public-news-local --release-id "$NEWS_CONTENT_RELEASE" \
  --tag-release-id "$NEWS_TAG_RELEASE" --topic finance \
  --output var/news-storage/exports/20261023/finance-with-tags-copy-01.jsonl
```

`--topic` 必须同时指定 `--tag-release-id`。全量导出不传 `--body-only`、`--min-body-chars`、`--exclude-metadata-review`、`--topic`。100 字符仍只是一项素材筛选条件，不表示新闻或主题质量已经人工验收；本次仍排除 7 条元数据待复核新闻。导出结果会记录内容版本、标签版本、过滤条件、文件 SHA 以及 `artifact_kind=source_material_with_inferred_topics_not_evaluation_gold`。

历史行为关联必须显式指定原标签包、原租户和时间窗口。示例查询与这篇公开新闻绑定的旧**模拟新闻小时聚合**：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py behavior \
  --tenant-id public-news-local --release-id "$NEWS_CONTENT_RELEASE" \
  --tag-release-id "$NEWS_TAG_RELEASE" --news-id 20260718A03B9V00 \
  --legacy-package var/behavior-tags/local-merged-v1-20261016 \
  --source-tenant-id 11111111-1111-4111-8111-111111111111 \
  --window-start 2026-10-13T00:00:00+08:00 \
  --window-end 2026-10-14T20:00:00+08:00 --limit 20
```

旧包的两个租户是原模拟场景，不能从新逻辑域 `public-news-local` 推断映射。命令先查精确内容绑定，再核对旧 Manifest、SQLite 全文件摘要及内容 JSONL/索引一致性；以只读 SQLite 查询指定新闻、原租户、正文哈希和完整落在请求范围内的小时。结果保留每小时、`source_dataset_version`、`scenario_id`、`metric_definition_id`，不跨窗口或跨来源累加 UV；`source_mode` 明确为 `synthetic`。

无旧内容引用、原租户不匹配或指定时间没有聚合记录时，返回 `unavailable` 及原因，不编造零指标。元数据待复核会在访问旧库前拒绝；时间必须带时区并处于旧包覆盖范围内。旧包有约 3.7 GB SQLite，本实现每次实际聚合查询都会读取其文件校验摘要，可能比主题查询慢；内容标签加载本身不读取该大文件，也不读取个人 events/profiles。

需要在另一台本地 PostgreSQL 恢复时，先按第 5 节导入内容，再核对并导入现成标签包：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py verify-tags \
  --tags-package var/news-storage/tags/public-news-topics-20261023 \
  --package var/news-storage/unified-public-news-20261023

agent/.venv/bin/python agent/tools/unify_news_storage.py import-tags \
  --tags-package var/news-storage/tags/public-news-topics-20261023 --initialize
```

导入在同一事务中核对内容绑定、标签哈希和主题索引；同一标签包重复导入返回 `already_present`。重算时必须新建输出目录，不能改写现有标签 release：

```bash
agent/.venv/bin/python agent/tools/unify_news_storage.py build-tags \
  --package var/news-storage/unified-public-news-20261023 \
  --legacy-package var/behavior-tags/local-merged-v1-20261016 \
  --output var/news-storage/tags/public-news-topics-20261023-rebuild-01
```

## 10. 标签完整调用链、验收与剩余工作

```text
CLI build-tags
→ package_io.load_package：验证固定内容 release
→ legacy_tags.load_legacy_tags：仅验证旧 Manifest、policy、content_labels
→ topic_labels.build_tag_package：重算原文规则，禁止模拟栏目兜底，比较精确旧版本
→ topic_labels.save_tag_package / load_tag_package：独立冻结标签文件与身份
→ CLI import-tags → FrozenNewsTagStore.import_release
→ PostgreSQL 三张标签表 + news_content_tagged_selected 视图
→ CLI topics / summary / get / list / export：明确内容与标签版本，过滤后分页
→ tags 字段、中文主题名、原文依据与带标签 JSONL

CLI behavior
→ 当前标签记录的精确 legacy_refs + 显式 source_tenant_id + 时间范围
→ legacy_tags.query_legacy_behavior：Manifest / SQLite / 内容绑定校验
→ SQLite mode=ro、query_only=ON、trusted_schema=OFF
→ 按新闻、小时、来源、场景及指标口径分别返回模拟聚合与标签
```

依赖为 Python、已有 Pydantic 主题规则模型、SQLAlchemy、psycopg 和 SQLite。数据库地址仍仅通过显式 `UNIFIED_NEWS_DATABASE_URL` 读取；包路径、内容/标签版本、主题、原模拟租户和时间窗通过 CLI 传入。本次没有调用 LLM/Embedding、修改新闻原文、启动新采集或发布生产配置；也没有把统一库自动切换为在线 Worker 的内容与标签来源。

本次统一测试 **168 项通过**，覆盖内容、冻结文件、旧包读取、标签构建、数据库和 CLI。[全量 PostgreSQL 标签验收报告](../../output/news-storage-unification-20261023/tag-integration-report.json) 状态为 `passed`：逐条核对 21,435 条关联结果的正文、记录/标签哈希和中文主题名；财经过滤遍历全部 3,536 条且无分页遗漏；重复导入为 `already_present`；完整原内容包保持不变，8,144 条旧候选素材除新增 `tags` 外逐字段一致；旧公开新闻成功查询模拟聚合，新新闻缺旧引用明确返回 `unavailable`。统一测试入口：

```bash
agent/.venv/bin/python -m pytest -q agent/tests/test_unified_news_*.py
```

测试包含内容与标签版本绑定、禁止模拟栏目兜底、父主题继承、原文命中位置、源包篡改、跨租户隔离、旧聚合哈希和时间范围、事务回滚及幂等、主题过滤先于分页、空过滤页继续前进、导出不覆盖文件与失败清理。SQLite 测试验证代码边界，本地 PostgreSQL 导入和读回需要分别以验收报告为证；两者都不能代替企业 TDSQL-A 验收或主题质量评测。

剩余工作：人工审核主题目录和关键词规则；抽样标注真实主题，用人工金标计算分类指标；按任务冻结素材、标签版本与划分；正式接入企业授权聚合数据；明确线上内容生命周期与标签更新流程。关键词推断结果不能直接作为自身的评测答案，模拟行为不能作为简历中真实企业效果指标。

## 11. 新闻受众聚合正式导入本地 PostgreSQL（2026-10-23）

本节记录后续实际落地，前文保留原阶段的历史状态。已有的匿名新闻受众聚合已从冻结 SQLite 包导入同一个本地数据库 `127.0.0.1:25432/newsagent_unified_news_20261023`，使用独立 Schema **`news_audience`**。这次导入的是“某篇新闻在一个窗口内的读者群体及其兴趣分布”，没有导入个人用户画像或个人行为明细。

来源为 `var/reader-profiles/local-readers-v1-20261017/`；固定 `source_release_sha256` 为：

```text
03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5
```

所有受众记录仍明确标记 `source_mode=synthetic`。**52,718 是新闻与时间窗口的聚合记录数，不是用户数，也不是企业真实规模。** 原包每条记录包含长期和短期两个维度，入库后拆成 105,436 条人群记录。

### 11.1 已落地的 SQL 对象

| 对象 | 当前行数 | 一行的含义 |
|---|---:|---|
| `news_audience.package_release` | 1 | 来源包身份、Manifest、规则、入库行数及各表投影摘要 |
| `news_audience.topic_dictionary` | 84 | 12 个一级、72 个二级主题中的一个主题，带目录版本与父子关系 |
| `news_audience.content_binding` | 2,474 | 原业务租户中的一条新闻与原正文哈希、内容主题的绑定 |
| `news_audience.reader_snapshot` | 1,488 | 某业务租户的一个已覆盖时间窗口及其固定修订、版本、水位和质量状态 |
| `news_audience.news_reader_population` | 105,436 | 某租户、快照、新闻、兴趣回看期限的读者人数、画像覆盖及基准人数 |
| `news_audience.news_reader_topic` | 1,282,053 | 对应人群中一个已保留主题的读者人数、基准主题人数和原排序 |
| `news_audience.unified_news_binding` | 1,266 | 原业务租户中的新闻与统一正文版本的精确映射，覆盖 661 个不同公开新闻 ID |

可直接读取的两个视图：

- **`news_audience.v_news_reader_population_status`**：读者总数、画像人数及未知／冷启动人数，以及窗口、来源和状态信息。
- **`news_audience.v_news_reader_topic_metrics`**：主题中文名、主题人数、全体读者占比 `share_all`、有画像读者占比 `share_profiled`、基准占比、`lift` 和 `low_sample`。

视图保留原始状态，**不会替调用方选择最新修订或通过质量门禁**。直接执行 SQL 时也要固定租户、来源和快照，并检查完整性、质量与水位；低样本会抑制 `lift` 为 `NULL`，不能将其补零。示例 SQL 已按这些字段组织查询。

`lookback_hours=24` 表示短期兴趣、`720` 表示最长 30 天的长期兴趣；两者使用同一个新闻访问窗口。靠近来源包覆盖起点时，历史不足完整回看期限，记录会保留相应限制说明。多主题读者可以重叠，不能将主题人数相加当成总人数，也不能跨小时相加读者数当成去重 UV。

原包仅保留每个维度的 **Top 20 主题**，已入库 `source_topic_limit/topics_are_top_k/source_topic_count/source_rank`。**没有某个主题行并不表示该主题人数为零**；扩大查询 `LIMIT` 不能恢复原包未保留的主题。

### 11.2 与新闻正文、内容标签的连接

正文的逻辑租户 `public-news-local` 与受众源包中的业务 UUID 是不同概念。通过 `unified_news_binding` 显式映射，连接条件为 **`news_id` 与正文 SHA-256 同时一致**，并固定内容租户、内容 release 和记录哈希。不能把两个租户直接当作相同值，也不能仅凭标题或新闻 ID 强行连接不同正文版本。

本次 1,266 条映射是“业务租户—新闻”组合，去重后只有 661 个公开新闻 ID；未匹配的内容继续保留源绑定，不编造统一正文引用。正文和内容主题标签仍读取第 8 节的 `news_content_tagged_selected`，受众主题读取本节新视图。完整联查见 [本地受众 SQL 查询示例](sql/news-audience-local-query.sql)，实际返回示例见 [受众查询预览](../../output/news-storage-unification-20261023/audience-sql-preview.json)。

**本地正文仍保存在 PostgreSQL。** 企业背景中原文使用对象存储，不表示本次已将本地正文迁入 COS/S3/MinIO，也不表示已建立 ES 索引。

### 11.3 导入、映射与查询命令

入口为 [import_reader_audience.py](../tools/import_reader_audience.py)。只从显式环境变量 `AUDIENCE_DATABASE_URL` 读取连接，不加载 `.env`，不初始化应用 Settings。下列凭据仅为仓库中公开的本地演示配置：

```bash
export AUDIENCE_DATABASE_URL='postgresql://YOUR_USER@127.0.0.1:25432/newsagent_unified_news_20261023'
export READER_SOURCE_RELEASE='03fab1518c30f6822aedc73a67bbcede4831273b141db9a4a6aa1c8129ff19f5'

agent/.venv/bin/python agent/tools/import_reader_audience.py import \
  --package var/reader-profiles/local-readers-v1-20261017

agent/.venv/bin/python agent/tools/import_reader_audience.py bind-content \
  --release "$READER_SOURCE_RELEASE" \
  --content-tenant public-news-local \
  --content-release news-0c74530f2864db20a73d7a96

agent/.venv/bin/python agent/tools/import_reader_audience.py query \
  --release "$READER_SOURCE_RELEASE" \
  --tenant 11111111-1111-4111-8111-111111111111 \
  --start 2026-09-24T22:00:00+08:00 \
  --end 2026-09-24T23:00:00+08:00 \
  --news-id 20260718A03B9V00 \
  --lookback-hours 720 --top-k 5
```

该示例是北京时间 2026-09-24 22:00 至 23:00，返回 **6 位模拟读者**。固定快照 ID 为 `90d919aee5b20f16d3bc3200e91f65d55120b69458388ba6c883a96d1db4954d`。一个请求可重复传入 `--news-id` 查询 1 至 20 个不同新闻；`--lookback-hours` 只接受 24 或 720。

查询先检查明确来源 release、租户和窗口，确认完整性、质量与水位，再读取新闻人群及主题。**已知源新闻绑定 + 完整覆盖窗口 + 无访问聚合行**，返回 `empty`、读者数 0；未知新闻返回 `unavailable`，窗口未覆盖或未通过校验则拒绝查询。不能把缺失数据统一解释为零人。

`knowledge_as_of` 是冻结模拟包的生成时点，`knowledge_as_of_basis=synthetic_package_frozen_at`；它不证明企业在历史窗口当时已获得这些画像。`quality_status=passed` 仅指 `quality_basis=local_import_contract_validation` 所记录的本地合同校验，不代表企业数据质量认证或真实推荐效果验收。

### 11.4 完整调用链、验证与后续接入

```text
import_reader_audience.py import
→ ReaderWarehouseSource / LocalNewsReaderProfileClient
→ 冻结 Manifest、policy、SQLite 摘要和内容／窗口／画像合同校验
→ 原新闻窗口记录拆分为 24h / 720h 人群及 TopK 主题行
→ warehouse.sql 初始化独立 news_audience Schema
→ import_package：事务锁 + psycopg COPY + 全量投影摘要回读
→ package_release 和五类来源投影表提交

import_reader_audience.py bind-content
→ bind_unified_content：固定源 release、正文 release
→ news_id + body_sha256 精确关联
→ unified_news_binding

import_reader_audience.py query
→ query_audience：REPEATABLE READ / READ ONLY
→ reader_snapshot 与 content_binding 校验
→ 人群状态视图 + 主题指标视图
→ 带来源、版本、限制说明的人群及主要兴趣 JSON
```

核心实现为 [warehouse.py](../app/modules/reader_profiles/warehouse.py) 和 [warehouse.sql](../app/modules/reader_profiles/warehouse.sql)。外部依赖为本地 PostgreSQL、psycopg、SQLite 和已有 Pydantic 合同模型；没有模型、Embedding、ES 或企业 HTTP 调用。Schema 初始化拒绝复用未知结构；同一 release 重复导入会全量核对现有投影后返回 `already_imported_verified`，不会追加重复行。重复绑定也会核对完整映射，匹配后返回 `already_bound_verified`。

本轮相关 **77 项测试通过**，覆盖仓库投影、来源合同和本地读取边界；真实 PostgreSQL 导入后已成功逐表全量回读、校验行数与投影摘要。重复导入返回 `already_imported_verified`，重复正文绑定返回 `already_bound_verified`，均保持行数不变；5 段交付 SQL 和 10 项真实 PostgreSQL 集成检查通过，见 `output/news-storage-unification-20261023/audience-integration-validation.json`。测试入口：

```bash
agent/.venv/bin/python -m pytest -q \
  agent/tests/test_reader_profile_warehouse.py \
  agent/tests/test_reader_profile_contracts.py \
  agent/tests/test_reader_profile_local.py
```

后续工作：将 PostgreSQL 受众查询适配到 `ReaderProfileService` 并接入线上热点 Worker；实现或对接企业授权的聚合作业、迟到修订与版本发布；核对 TDSQL-A SQL 方言、权限和生产性能；按企业实际部署落实对象存储原文引用及可选的 ES 检索。**个人用户画像未导入本次 Schema，企业真实数仓和线上 Worker 尚未切换。** 本次存储与合同验证不能替代模型质量评测，也不能作为真实企业业务收益数字。
