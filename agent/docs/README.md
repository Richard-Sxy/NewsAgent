# NewsAgent 文档入口

日常使用主要查看以下 7 份说明。本页按 2026-10-26 的源码与部署模板核对；各说明中的历史验收记录只代表记录日期的环境，不代表当前工作区已重新跑通。

| 需要做什么 | 说明 |
| --- | --- |
| 启动带本地 BGE 知识快照的模拟、检查与停止服务 | [本地运行](local-bge-simulation.md) |
| 配置聊天查询、模型接口与测试 | [聊天数仓与模型](conversation-warehouse-model.md) |
| 运行企业业务情形的合成数据场景与规模测试 | [企业模拟](enterprise-synthetic-data.md) |
| 查新闻正文、标签与读者聚合表 | [数据库查询手册](news-storage-database-reference.md) |
| 在本地恢复已有新闻数据库 | [数据库恢复](news-storage-local-restore.md) |
| 对接企业数仓字段、聚合口径与实时窗口设计 | [企业数仓合同](enterprise-tdsql-warehouse-contract.md)，含[大厂案例与时间窗口优化](enterprise-tdsql-warehouse-contract.md#realtime-window-design) |
| 查看运行监控与指标 | [监控说明](observability.md) |

项目定位与长期约束见 [PROJECT_CONTEXT](../../DemoMD/PROJECT_CONTEXT.md)，长期设计参考见
[TAOTIAN](../../DemoMD/TAOTIAN.md)。当前优先完成本地测试和企业情形模拟，企业接口保留为可替换边界。
本地确定性模型 Port 用于验证控制流和数据合同，不能代表企业模型质量。BGE 快照检索与确定性模型替身是不同的组件。

## 文档类型与保留原因

- [运营判断与持续跟进技术实施](operations-judgment-followup-implementation.md)：待实现设计与跨机交接，包含事实增量卡、持久跟进、事务派发、评测及部署步骤；不代表功能已部署。
- 上表7份是主要使用说明；其中数据库恢复记录有明确的数据集和恢复日期，不能当作适用于所有环境的重建命令。
- [查询诊断](hot-news-query-diagnosis.md)、[热点并发](hot-news-concurrency.md)、
  [多租户并发设计](multi-tenant-concurrency-design.md) 包含特定阶段的诊断、验证和设计记录，仍被PDF报告脚本检查。
  阅读时以各自日期和适用范围为准，不能将历史数字视为当前运行结果。
- [`sql-warehouse/`](sql-warehouse/README.md) 集中存放统一说明与冻结运行契约。
  `warehouse_contract` 根据数据集配置选择版本，`verify_schema_contract` 读取原始字节并校验
  SHA-256，再提供给查询链路；Docker 构建也会复制它们。它们不能当作重复说明合并或改名。
- `sql/` 是可执行SQL和查询模板，涉及数仓结构、模拟数据装载与查询示例；应先确认文件用途与目标数据库，再决定是否执行。

## SQL 数仓统一入口

共用字段、配置、数据集差异及冻结文字勘误统一见
[SQL 数仓说明](sql-warehouse/README.md)。各版本文件已收进该目录，`docs/` 根目录不再平铺。

## SQL 文件用途

| 文件 | 用途与执行前提 |
| --- | --- |
| [v7 DDL](sql/tencent-news-simulation-v7.sql)、[v8 DDL](sql/real-news-warehouse-v8.sql) | 分别由模拟数仓生成器读取，生成包再追加数据装载语句；单独执行 DDL 不会得到完整演示数据 |
| [v7 验收](sql/tencent-news-simulation-v7-checks.sql)、[v8 验收](sql/real-news-warehouse-v8-checks.sql)、[v8 统计](sql/real-news-warehouse-v8-observe.sql) | 对应隔离数仓的一致性检查及证据查询；完整样本验收不能直接套在缩小的单元测试样本上 |
| [Agent 数仓巡检](sql/news-warehouse-readonly-checks.sql) | 检查 `dw` 结构、数量和质量，结果需按所用 profile 解释 |
| [历史受众查询](sql/news-audience-local-query.sql) | 2026-10-23 的受众包与正文 release 示例；旧正文 release 已归档，不能直接当作当前数据查询 |
| [实时指标](sql/news-realtime-query.sql)、[墙钟读者](sql/news-wallclock-readers-query.sql)、[读者概览](sql/news-reader-overview-query.sql) | `news_realtime` 本地模拟查询；区分暂定/定稿、存储窗口和数据新鲜度，不表示持续在线采集 |
| [企业受众 DDL 草案](sql/news-reader-audience-proposal-v1.sql)、[热点受众查询示例](sql/news-hotspot-audience-query-example.sql) | 待确认的企业逻辑设计；DDL 会建对象，查询采用 SQLAlchemy 命名参数，不能直接当作 psql 脚本执行 |

## 调用链与检查入口

文档查阅顺序是根目录 README → 本页 → 对应使用说明。实际启动由
[`start_local_demo.py`](../tools/start_local_demo.py) 调用 `deploy_bge_demo.py`，或直接使用
仓库根目录 [`deploy/`](../../deploy/) 中的 Compose。前者需要已存在的 BGE 快照包；企业合成数据
模板使用另一组 Compose 与数据集，两者不是同一个演示实例。

本地热点查询主链为：页面或 CLI → `simulation.local_simulation:create_local_app` →
`build_local_hot_news_sql_service` 装配冻结契约、场景与数据集 → `SqlAssistantService`
解析受限意图 → `compile_query` 与 SQL 护栏 → 保存查询预览并将SQL计划绑定热点运行 →
Temporal Workflow/Activity → `SqlAssistantHotNewsMetricSource` 执行PostgreSQL只读候选查询并补齐指标、基线 →
Python 热度排序 → `SqlHotNewsContentRepository` 提供正文 → 知识检索补充证据 →
`NativeHotNewsAnalysisRunner` 调用模型 Port 并校验 →
PostgreSQL/MinIO 保存结果，API/SSE 输出页面与报告，Redis 承担缓存与事件推送。
企业合成数据的会话分析还会把授权运行投影为有界聚合数据，交给独立分析服务计算并保存工具轨迹。

主要配置是 `MODEL_RUNTIME_CONFIG_PATH`、`SQL_ASSISTANT_DATASET_PROFILE`、
`SQL_ASSISTANT_SCENARIOS_PATH`、`SQL_ASSISTANT_NEWS_PER_TENANT`、`CONVERSATION_*`、
`DATA_ANALYSIS_*` 及数据库、Temporal、Redis、Artifact 连接。具体取值和适用模板见各说明；
Compose 文件位于仓库根 `deploy/`，`agent/deploy` 是指向该目录的兼容符号链接；
容器内复制后的配置位于 `/app/deploy/`。Windows通过UNC访问时可能无法跟随Linux符号链接，相关检查应在WSL中执行。

测试入口包括 [`test_sql_assistant_warehouse.py`](../tests/test_sql_assistant_warehouse.py)、
[`test_scaled_profiles.py`](../tests/test_scaled_profiles.py)、
[`test_real_news_demo_deploy.py`](../tests/test_real_news_demo_deploy.py)，以及各说明列出的 HTTP、前端与监控检查。
需要数据库、API 或 Temporal 的测试须显式提供相应隔离环境；跳过不等于验收通过。

当前待办：独立分析服务 Dockerfile 仍有模块迁移前的 COPY 路径；并发 PDF 脚本仍引用此前已缺失的
`conversation-concurrency.md`。企业真实接口、模型质量和生产容量也未完成验收。
本轮只核对文档、源码路径和模板，没有启动服务或重跑业务验收。
