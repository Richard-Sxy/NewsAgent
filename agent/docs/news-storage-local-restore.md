# 本机统一新闻与受众数据库恢复记录（2026-10-23）

本文保留 2026-10-23 迁移和验收事实，路径、对象数量、测试数量与服务状态均属于当时记录。2026-10-26 仅复核源码及已有清单，没有重新恢复、连接数据库或运行验收。

后续本地模拟已增加公开正文目录、实时合成指标和实时读者兴趣的 SQL 投影；10 月 25 日又完成内容子集清理与自动标注。当前 release 与数量见 [数据库查询手册](news-storage-database-reference.md#connection)，实时模拟实现见 [实时 SQL 投影](news-storage-database-reference.md#realtime)。不能用本页 14 表/4 视图和旧 release 代表整个当前数据库。

2026-10-23 已完成桌面 `NewsAgent-WSL-transfer-20261023.zip` 的代码合并、SQL 恢复和本机验收。
这是当时落库的历史冻结包投影；历史受众与后续实时模拟仍采用不同来源和窗口，不能把历史画像改名当成实时画像。

## 迁移时的本机位置

| 内容 | 位置 |
| --- | --- |
| Windows 项目 | `E:\NewsAgent` |
| WSL 项目 | `/home/shi/project/NewsAgent`（Ubuntu-20.04） |
| 正文、标签和导出素材 | 当时两份项目各自的 `var/news-storage/`；当前工作区为上述 WSL 项目 |
| 原始 SQL | `E:\NewsAgent\var\imports\newsagent-transfer-20261023\database\newsagent_unified_news_20261023.sql` |
| 本机验收证据 | `E:\NewsAgent\output\setup-reports\news-storage-import-20261023\` |
| PostgreSQL 容器 | `news-agent-native-e2e-postgres-1` |
| 持久化卷 | `news-agent-native-e2e_native-postgres-data` |
| Windows 连接 | `127.0.0.1:25432` |
| 数据库 | `newsagent_unified_news_20261023` |
| 本地演示用户 | `newsagent_native`；凭据按本机显式配置注入 |
| Schema | `public`、`news_audience` |

迁移时两份项目连接同一个 Docker 数据库，没有分别恢复两份数据库；原应用库 `newsagent_native` 保留。
当时容器已启动、重启策略为手动。本次没有检查它当前是否运行；需要时先检查容器状态，再使用既有启动命令：

```powershell
docker start news-agent-native-e2e-postgres-1
```

50 个归档文件的 SHA256 全部匹配，压缩包 SHA256 为
`74bae9845e229d536521c6e1aed454d39bcb3c05afbc1528e64311cea1940131`。
既有 reader_profiles 的七个代码文件与迁入版本一致，未改写。
WSL 中旧版数据库说明仅包含与包内版本不同的公开演示凭据，已备份到
`E:\NewsAgent\var\imports\newsagent-transfer-20261023\backups\wsl\agent\docs\news-storage-database-reference.md`。
其他已有修改保持原状。批量 JSONL、SQLite、SQL 中转文件已通过局部 `.gitignore` 排除。

## 2026-10-23 数据和验证

独立空库使用 PostgreSQL 16.15 的 `psql -X -v ON_ERROR_STOP=1 --single-transaction` 恢复，
49.08 秒成功提交。恢复前检查了 SQL 结构与 psql 指令，没有执行归档中的任意 shell 命令。
14 张表、4 个视图逐项对照源库，全部一致，数据库约 1.33 GiB（验收时测量值）。

| 主要对象 | 行数 / 含义 |
| --- | --- |
| `public.news_content_versions` | 21,828 个内容版本 |
| `public.news_content_selected` | 21,435 篇选定新闻 |
| `public.news_content_tags` | 21,435 条内容标签 |
| `public.news_content_topic_memberships` | 35,888 条内容主题关系 |
| `news_audience.reader_snapshot` | 1,488 个租户窗口快照 |
| `news_audience.news_reader_population` | 105,436 条聚合，24h/720h 分开，不能相加 |
| `news_audience.news_reader_topic` | 1,282,053 条读者兴趣主题记录 |
| `news_audience.unified_news_binding` | 1,266 条业务域绑定，覆盖 661 个不同新闻 ID |

校验覆盖：所有表行数、视图名称、约束有效性、全部受众投影行的摘要、原始本地画像包身份、
正文与标签冻结包的文件摘要和语义绑定、真实 PostgreSQL Store 查询及同 JDBC 驱动的只读查询。
样例新闻 `20260718A03B9V00` 在北京时间 2026-09-24 22:00–23:00 有 6 位合成详情读者，
长期兴趣中 AI 主题为 5 位；未知新闻返回 unavailable，人数为 null。
公开新闻、规则推断标签、synthetic 受众的来源属性保持不变。

245 项 Python 测试通过：168 项 unified_news 测试、22 项 warehouse 测试、42 项画像契约测试、
13 项本地画像客户端测试。当时使用已有 `newsagent-streaming-base:local` 镜像和 Windows 源码的
只读挂载执行，没有使用项目里从 Mac 拷贝过来的 `.venv`。
测试入口为 `agent/tests/test_unified_news_*.py`、`test_reader_profile_warehouse.py`、
`test_reader_profile_contracts.py`、`test_reader_profile_local.py`。

`local-verification.json` 保存本机数据库验收结果，`acceptance.log` 保存逐阶段结果，
`python-tests.json` 保存测试数量，`jdbc-verification.log` 保存每个表/视图的可读性。
`source-verification.json` 是归档内的原机器证据，与本机验收证据分开保存。
`verify_restore.py` 是本次可重复执行的只读验收脚本：在现有 agent 镜像中，挂载 agent 到
`/source`、本机 `var/news-storage` 到 `/news-storage`、现有
`var/reader-profiles/local-readers-v1-20261017` 到 `/reader-package`，以及证据目录到 `/report`；
设置 `PYTHONPATH=/source` 和下述两项连接变量后运行 `python /report/verify_restore.py`。
前三项挂载应只读，报告目录可写；整个脚本不导入、不更新数据库。

验收时实时链路 `/ready` 正常，动态供数开启，sequence=882，已完成报告 3,566，失败 0。
该数字是 2026-10-23 22:07 左右的快照；运行状态见 `streaming-health.json`，后续仍会变化。

## DBeaver 查看

迁移验收时，Windows 客户端工作区已增加独立连接文件：
`E:\NewsAgent\var\database-client\workspace\General\.dbeaver\data-sources-unified-news.json`。
连接名为 **NewsAgent Unified News + Audience**，使用现有 PostgreSQL JDBC 驱动和只读设置。
已通过相同驱动执行认证、枚举、逐表计数及取一行读取，未声称已在 GUI 中点击验收。
若已打开的 DBeaver 没出现新连接，保存当前工作后重新打开客户端，使其重新加载配置。

展开新连接的 `Schemas → public / news_audience → Tables / Views` 查看完整表。
查询脚本位于工作区 `General/Scripts/03-Unified-News-Audience.sql`，仓库对应入口是
`agent/docs/sql/news-audience-local-query.sql`。

## 完整调用链与配置

1. 本次恢复：桌面 ZIP → 逐文件 SHA256 检验与目录比较 → 增量文件合并 →
   原生 pg_dump SQL 经 psql 单事务恢复 → PostgreSQL 14 表/4 视图 → 只读验收与 DBeaver。
2. 后续正文/标签工具：`agent/tools/unify_news_storage.py` → `load_package` /
   `load_tag_package` 校验冻结身份、版本和内容摘要 → `FrozenNewsStore` /
   `FrozenNewsTagStore` → public 表/视图 → summary/get/list/topics/导出。
   应用可通过 `ReleaseNewsContentRepository` 读取明确版本，当时未替换在线应用的内容源；后续 streaming lab 已通过 `PublicContentRepository` 读取绑定的统一正文目录。
3. 后续受众工具：`agent/tools/import_reader_audience.py` → `ReaderWarehouseSource`
   从本地合成画像包校验窗口、租户与来源 → `population_rows` / `topic_rows` 拆分期限与主题 →
   `import_package` COPY、不可变身份检查和 `verify_projection` 摘要核对 → news_audience。
   `bind_unified_content` 按 news_id + 非空正文 SHA 绑定内容域；`query_audience` 按明确来源、
   租户和窗口进行只读一致性查询 → JSON 或 SQL 视图结果。本次恢复后只读验证，没有再次执行全量导入。
4. 依赖：Python、SQLAlchemy、psycopg、Pydantic、SQLite 与 PostgreSQL；DBeaver 使用 JDBC。
   本次没有模型、Embedding、企业服务或个人行为采集调用。

两个 CLI 只接受显式环境变量，不自动读取 `.env`：

```text
UNIFIED_NEWS_DATABASE_URL=postgresql+psycopg://YOUR_USER@127.0.0.1:25432/newsagent_unified_news_20261023
AUDIENCE_DATABASE_URL=postgresql://YOUR_USER@127.0.0.1:25432/newsagent_unified_news_20261023
```

以上是连接格式示例，需要使用本机已配置的认证方式；未设置凭据时不能视为可直接执行的完整连接。主机地址沿用 Windows 本机约定。在 Docker 临时验收容器中使用 `host.docker.internal:25432`；
不要把 Mac 虚拟环境或文档中的历史 Mac 绝对路径当成本机执行路径。
查询必须指定 release、租户和窗口，内容域 `public-news-local` 不能当作业务租户 UUID。

## 后续实现与仍待完成的对接

- 本地实时模拟已有公开正文目录、分钟指标与读者兴趣投影；不再仅限于早期三个人造新闻 ID。目录必须绑定内容/标签 release、正文哈希与实验租户，历史小时受众不能直接 JOIN 到实时分钟窗口。源码为 `agent/simulation/streaming_lab/public_news.py` 和 `realtime_readers.py`，查询入口是 `news_realtime` 视图。
- 通用 Reader factory 仍仅装配 `local_package` / `enterprise_http`；`news_audience` 的 PostgreSQL 查询尚未成为该 factory 的独立在线后端。独立 streaming lab 已有实时聚合接线，不能把两者混为“全部未接入”或“全部生产可用”。
- 本地模拟已有暂定/定稿、水位、迟到修订及来源检查；企业聚合服务的同义合同、快照有效期、权限/RLS、TDSQL-A 兼容性和生产负载仍待联调。本地能力不代表企业模型质量或真实业务效果。
- 冻结磁盘包按身份保留，数据库保留策略另行管理。10 月 25 日清理后，原内容/标签 release 已归档且部分旧数据库行已删除，当前指针为 `var/news-storage/current-selection.json`。恢复本页原始 SQL 只能重建迁移基线，不能代表清理后的当前状态。恢复清理前数据应使用 `var/backups/news-category-prune-20261025/unified-news.before.dump`，先在独立库核验；后续标注/实时状态还须分别按版本恢复。

本文恢复记录的调用链、配置和测试入口保留在上文；当前数据、自动标注及实时模拟的调用链、配置、查询与测试入口见 [数据库查询手册](news-storage-database-reference.md)。本次只修改文档，没有执行恢复、模型推理或数据库写入。
