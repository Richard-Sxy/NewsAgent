# 企业数据与中间件接入现状

核对日期：2026-10-23。本文区分用户已确认的企业背景、本地实现和待接入能力。
此前关于 TCHouse-C/P、ES 热温索引和 COS 存储分层的描述属于早期设想，不再作为已确认架构。

## 已确认背景与当前实现

| 能力 | 已确认背景 / 当前实现 | 仍需确认或实现 |
| --- | --- | --- |
| 企业数仓 | 用户确认 TDSQL-A、PostgreSQL 兼容；项目已有 SQL 编译、AST 护栏及 `SqlWarehouseClient` Port | 内核版本、真实表与指标口径、只读授权、水位与修订；协议直连还是企业网关/SDK 尚待批准 |
| 新闻正文 | 用户确认企业新闻内容使用对象存储；本地统一正文当前保存在独立 PostgreSQL 数据库 | 对象存储产品、对象键、版本、内容服务接口及权限；已有 Artifact S3/MinIO 能力不等于正文读取已接通 |
| 模型与 Embedding | Python 原生运行时对接 OpenAI-compatible Chat Completions / Embeddings；隔离本地替身实现相同 Port | 企业端点的鉴权、限流、结构化输出、向量维度及合同验收 |
| 相关内容检索 | 已有分层向量检索与本地知识快照能力 | 企业向量平台接入；ES/BM25 仍是待评估方案，尚未确认实施 |
| 业务状态与 Artifact | PostgreSQL 保存运行、审批、版本、审计和记忆；S3/MinIO 保存 Artifact，Redis 用作缓存及事件推送 | 企业生产部署、容量、备份恢复及权限治理 |
| 受众与标签 | 本地已串联统一正文、规则主题标签及合成受众聚合；企业侧按聚合 Port 接入 | 企业真实主题目录、聚合权限、口径与时效合同；不获取个人接收者名单 |
| 编排与发布 | Temporal 编排有界任务；研究、写作、审批及 CMS Port 已存在 | CMS 真实接口与发布语义、企业生产部署验收；不自动发布或推送 |

已知 `news_id` 的正文精确读取不依赖 ES。企业埋点、日志传输和行为数仓建设不属于本项目职责；
本地不得保存企业原始用户行为明细，只保留必要聚合快照、版本及证据引用。

## 指标查询调用链

```text
热点工作台 / 对话 query_hot_news
→ 可信租户与用户身份、场景和窗口
→ 结构化理解与固定 SQL 编译 / 服务端批准的指标查询合同
→ SQL AST、绑定参数及指标口径校验
→ SqlWarehouseClient（企业 Adapter 待接；本地 PostgreSQL 仅用于模拟）
→ NewsMetricSnapshot
→ Python 指标、基线、热度与排行
→ 按 news_id 读取正文、关联检索、模型解释与确定性校验
→ 运行快照、审计、报告和页面
```

模型不计算权威数字。小时 UV 不得直接跨小时相加，缺失数据不得随意补零。
企业 Adapter 应复核可信身份与授权视图，并落实只读账号、参数绑定、超时、并发和行数上限。

## 本地统一内容与受众链路

公开缓存及历史素材 → `unify_news_storage.py` → 内容版本、来源证据、冻结发布 →
`ReleaseNewsContentRepository` 按明确租户和版本读取。标签按规则生成并绑定正文版本；
`import_reader_audience.py` → `ReaderWarehouseSource` → 来源与摘要校验 → PostgreSQL
`news_audience` 聚合表和视图。该链路尚未替换在线 Worker 内容源，受众仍明确标记为合成数据。

- 内容连接配置：`UNIFIED_NEWS_DATABASE_URL`；受众连接配置：`AUDIENCE_DATABASE_URL`。
- SQL 预算配置：`agent/app/config.py` 中的 `text2sql_max_rows`、`text2sql_timeout_ms`。
- 场景与模型配置：`deploy/text2sql-scenes.*.yml`、`deploy/model-runtime.*.yml`；企业凭据由受控环境注入，不写入文档。
- 分层检索配置与生命周期见[知识分层说明](agent/docs/tiered-knowledge-lifecycle.md)，不预设企业必须使用 TCHouse 或 ES。

## 验证入口与剩余工作

在 `agent/` 下可运行定向测试：

```bash
python -m pytest tests/test_metric_sql_contract.py tests/test_text2sql_metric_source.py \
  tests/test_sql_assistant_warehouse.py tests/test_unified_news_store.py \
  tests/test_reader_profile_warehouse.py -q
```

以上是测试入口，本次仅更新文档，没有重新执行测试。本地测试不替代企业 TDSQL-A、模型、
对象存储、权限或生产负载验收。后续优先完成企业合同映射、正文 Adapter、角色授权/RLS、
增量更新与线上生命周期接入，再验证生产部署和发布回执。

详细参考：

- [企业 TDSQL 数仓合同](agent/docs/enterprise-tdsql-warehouse-contract.md)
- [当前本地数仓字段](agent/docs/enterprise-warehouse-current-fields.md)
- [统一新闻存储](agent/docs/unified-news-storage.md)
- [本地统一数据库与查询手册](agent/docs/news-storage-database-reference.md)
- [企业受众接入](agent/docs/enterprise-audience-implementation.md)
