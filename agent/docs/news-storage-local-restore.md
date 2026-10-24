# 本机统一新闻与受众数据库恢复记录

2026-10-23 已完成桌面 `NewsAgent-WSL-transfer-20261023.zip` 的代码合并、SQL 恢复和本机验收。
这是已落库的历史冻结快照；实时模拟流水线继续独立运行，本次没有把历史画像接成实时画像。

## 本机位置

| 内容 | 位置 |
| --- | --- |
| Windows 项目 | `E:\NewsAgent` |
| WSL 项目 | `/home/shi/project/NewsAgent`（Ubuntu-20.04） |
| 正文、标签和导出素材 | 两份项目各自的 `var/news-storage/` |
| 原始 SQL | `E:\NewsAgent\var\imports\newsagent-transfer-20261023\database\newsagent_unified_news_20261023.sql` |
| 本机验收证据 | `E:\NewsAgent\output\setup-reports\news-storage-import-20261023\` |
| PostgreSQL 容器 | `news-agent-native-e2e-postgres-1` |
| 持久化卷 | `news-agent-native-e2e_native-postgres-data` |
| Windows 连接 | `127.0.0.1:25432` |
| 数据库 | `newsagent_unified_news_20261023` |
| 用户 / 密码 | `newsagent_native` / `newsagent_native`，现有 Compose 的公开本地演示配置 |
| Schema | `public`、`news_audience` |

两份项目连接同一个 Docker 数据库，没有分别恢复两份数据库。原应用库 `newsagent_native` 保留。
容器已启动；当前重启策略为手动，Docker 重启后若未运行，可执行：

```powershell
docker start news-agent-native-e2e-postgres-1
```

50 个归档文件的 SHA256 全部匹配，压缩包 SHA256 为
`74bae9845e229d536521c6e1aed454d39bcb3c05afbc1528e64311cea1940131`。
既有 reader_profiles 的七个代码文件与迁入版本一致，未改写。
WSL 中旧版数据库说明仅包含与包内版本不同的公开演示凭据，已备份到
`E:\NewsAgent\var\imports\newsagent-transfer-20261023\backups\wsl\agent\docs\news-storage-database-reference.md`。
其他已有修改保持原状。批量 JSONL、SQLite、SQL 中转文件已通过局部 `.gitignore` 排除。

## 数据和验证

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
13 项本地画像客户端测试。使用已有 `newsagent-streaming-base:local` 镜像和当前 Windows 源码的
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

现有客户端工作区已增加独立连接文件：
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
   应用可通过 `ReleaseNewsContentRepository` 读取明确版本，此次未替换在线应用的内容源。
3. 后续受众工具：`agent/tools/import_reader_audience.py` → `ReaderWarehouseSource`
   从本地合成画像包校验窗口、租户与来源 → `population_rows` / `topic_rows` 拆分期限与主题 →
   `import_package` COPY、不可变身份检查和 `verify_projection` 摘要核对 → news_audience。
   `bind_unified_content` 按 news_id + 非空正文 SHA 绑定内容域；`query_audience` 按明确来源、
   租户和窗口进行只读一致性查询 → JSON 或 SQL 视图结果。本次恢复后只读验证，没有再次执行全量导入。
4. 依赖：Python、SQLAlchemy、psycopg、Pydantic、SQLite 与 PostgreSQL；DBeaver 使用 JDBC。
   本次没有模型、Embedding、企业服务或个人行为采集调用。

两个 CLI 只接受显式环境变量，不自动读取 `.env`：

```text
UNIFIED_NEWS_DATABASE_URL=postgresql+psycopg://newsagent_native:newsagent_native@127.0.0.1:25432/newsagent_unified_news_20261023
AUDIENCE_DATABASE_URL=postgresql://newsagent_native:newsagent_native@127.0.0.1:25432/newsagent_unified_news_20261023
```

以上主机地址用于 Windows 本机。在 Docker 临时验收容器中使用 `host.docker.internal:25432`；
不要把 Mac 虚拟环境或文档中的历史 Mac 绝对路径当成本机执行路径。
查询必须指定 release、租户和窗口，内容域 `public-news-local` 不能当作业务租户 UUID。

## 仍待完成的实时对接

- 当前实时模拟使用三个人造新闻 ID、分钟窗口与独立租户集合；本次冻结受众使用历史真实新闻
  ID、小时窗口及另一组租户。必须先建立明确的新闻、正文版本、租户与事件时间映射，不能直接 JOIN。
- Reader 的在线 factory 尚未装配 PostgreSQL Port；现阶段本库由独立 CLI/SQL 查询。
  原应用仍使用已有本地包/企业接口 Port 配置。本次未切换运行中的 v8 应用。
- 持续更新需发布新的快照与来源版本，区分点击 UV 和详情去重读者，保证访问前兴趣、迟到修订、
  TopK 缺失含义与质量门槛；不得向现有冻结包身份追加实时数据。
- 企业模型、真实聚合服务、TDSQL-A 兼容性、授权/RLS 与生产负载验收仍待企业联调。

详细字段、键和查询口径继续以 `agent/docs/news-storage-database-reference.md` 为参考。
