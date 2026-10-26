# SQL 数仓统一说明

日常阅读以本页为入口。原先散落在 `docs/` 根目录的 7 份契约已集中到本目录，
正文和 SHA-256 保持不变，用于兼容已有模拟数据集、数据库台账和运行快照。
本页说明当前共用接口、配置及各数据集的区别；选择 profile 时仍使用对应冻结契约。

## 当前共用接口

Agent 只读取 `dw.news_behavior_aggregate`，粒度为租户、新闻、小时桶，共 18 列：

```text
tenant_id, news_id, event_time, title, content_type, category, source, publish_time,
impressions, clicks, unique_users, total_duration_seconds, effective_consumptions,
interactions, baseline_impressions, baseline_clicks,
baseline_effective_consumptions, baseline_interactions
```

- 自然语言先转成受限意图，SQL 由 Python 编译并绑定租户、半开时间窗口和行数上限。
- 热点完整运行只处理一个整点小时；小时 UV 不能相加后称为跨窗口去重人数。
- `ctr` 和 `hot_score` 是查询表达式，最终热点排行仍由批准版本的 Python 规则计算。
- 公开新闻、合成行为和企业真实数据须区分；这些本地 profile 不代表企业生产数据或模型质量。
- 数据集、规模和契约身份不能直接覆盖已有数据库；既有查询快照继续绑定原版本与摘要。

## 冻结契约的对应关系与勘误

下表对应当前 `warehouse_contract` 的选择逻辑。Schema 版本、数据集 profile、数据包版本是不同的身份；
例如 `real-news-v8` 使用 v7 Agent 视图契约，不能只按文件名中的数字重命名或合并。

| 冻结文件 | `SQL_ASSISTANT_DATASET_PROFILE` | 适用数据 |
| --- | --- | --- |
| [v1](sql-warehouse-schema-v1.md) | `classic-v1`、`enterprise-v1` | 固定日期的小规模合成样本 |
| [v2](sql-warehouse-schema-v2.md) | `enterprise-v2` | 可选规模的企业情形合成样本 |
| [v3](sql-warehouse-schema-v3.md) | `public-headlines-v3` | 公开标题与合成行为，标题时间和行为时间分开 |
| [v4](sql-warehouse-schema-v4.md) | `timeline-v4` | 30 天合成时间线 |
| [v5](sql-warehouse-schema-v5.md) | `timeline-v5` | 扩充主题和场景的合成时间线 |
| [v6](sql-warehouse-schema-v6.md) | `corpus-v6` | 120 篇缓存新闻与合成行为 |
| [v7](sql-warehouse-schema-v7.md) | `real-news-v8` | 固定 8,120 篇公开新闻，661 篇关联既有模拟指标 |

冻结文件保留原始字节，下列文字差异在此说明，不能据此绕过哈希或扩展查询白名单：

- v6 文档的字段表同时列了派生量。当前 `get_text2sql_schema` 和 `dw.news_behavior_aggregate`
  实际只提供固定 18 列；`ctr`、`hot_score` 是 `compile_query` 生成的表达式，
  `baseline_ctr` 不是该视图的可查询列。Agent 候选 SQL 的零曝光 CTR 返回 0；
  不能将文档中的 NULL 描述套用到所有查询入口。
- v7 文档中的“真实存在的历史样本/指标”指已存在且通过资格校验的固定数据包记录，
  其中行为仍是合成数据，不是企业测量。没有指标的内容不能补造榜单行。
- 这些固定 profile 不会自动切换到统一新闻库的当前 release。
  当前正文、标签和自动标注应查 [数据库手册](../news-storage-database-reference.md)。

## 配置、调用链与验证

配置入口为 `SQL_ASSISTANT_DATASET_PROFILE`、`SQL_ASSISTANT_NEWS_PER_TENANT`、
`SQL_ASSISTANT_SCENARIOS_PATH`；模型配置沿用 `MODEL_RUNTIME_CONFIG_PATH`。
本次仅移动契约文件并更新引用，没有改变配置值、字段、数据集身份或摘要常量。

页面/CLI → 查询理解与 Python 意图校验 →
[`warehouse_contract` / `verify_schema_contract` / `get_text2sql_schema`](../../app/sql_assistant/warehouse.py)
选择契约、读取原字节、校验 SHA-256 并提供 18 列结构 → `compile_query` / SQL 护栏 →
保存并绑定查询计划 → Temporal Activity → PostgreSQL 只读候选查询、补齐指标/基线 →
Python 排行与模型解释 → PostgreSQL/MinIO 保存 → API/SSE 页面和报告。
Redis 提供缓存与事件推送；模型和知识检索使用各自 Port。
Docker 通过 `agent/Dockerfile.e2e` 将本目录复制到容器的 `/app/docs/sql-warehouse/`。

检查入口：

- [数仓契约与篡改拒绝](../../tests/test_sql_assistant_warehouse.py)
- [扩量数据集与 v1/v2 兼容](../../tests/test_scaled_profiles.py)
- [真实新闻演示预检](../../tests/test_real_news_demo_deploy.py)

企业真实字段、权限、跨窗口去重与模型质量仍需独立验收。若需要真正淘汰某个数据集版本，
还须迁移对应数据库台账和运行快照；本次目录整理没有执行数据迁移。
