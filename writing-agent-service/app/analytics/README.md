# 用户行为与热点分析代码骨架

## 模块边界

本模块消费企业已有行为数据库的查询结果，不负责埋点、日志采集、消息队列或数仓建设。真实数据库只需实现 `BehaviorDataSource`，其输出统一为 `list[BehaviorRecord]`；后续计算不依赖具体 SQL 方言和数据库类型。

```text
企业数据库适配器
  → BehaviorDataSource.fetch
  → list[BehaviorRecord]
  → NewsMetricCalculator.calculate
  → list[NewsMetricSnapshot]
  → HotScoreCalculator.calculate
  → 热点 news_id
  → 内容精确查询和知识库关联检索
```

## 基础包

第一阶段不需要安装新依赖，只使用 Python 3.11 标准库：

| 包 | 用途 |
|---|---|
| `dataclasses` | 领域对象和指标快照 |
| `datetime` | 带时区时间及左闭右开窗口 |
| `decimal` | 比例、权重和分数的精确计算 |
| `enum` | 事件类型和内容类型 |
| `typing` | `Protocol` 数据源接口 |
| `collections` | 分组聚合与去重辅助结构 |

项目已有且后续才使用：

| 包 | 使用阶段 |
|---|---|
| `pytest` | 从第一阶段开始编写单元测试 |
| `pydantic` | API、Agent 输入输出边界校验 |
| `sqlalchemy`、`psycopg` | 企业数据库适配器或聚合结果持久化 |
| `fastapi`、`uvicorn` | 暴露热点查询和分析接口 |
| `temporalio` | 热点/异常触发后的长流程编排 |
| `httpx` | 调用 FastGPT 或企业内部 HTTP 服务 |

第一阶段不要引入 Pandas、NumPy、Kafka、Flink、Spark 或 LangChain。

## 实现顺序

1. `BehaviorRecord.validate`
2. `BehaviorQuery.validate`
3. `InMemoryBehaviorDataSource.fetch`
4. `NewsMetricCalculator.calculate`
5. 启用并通过 `test_news_metric_calculator.py`
6. `HotScoreConfig.validate`
7. 设计并实现可解释的 `HotScoreCalculator.calculate`
8. `BaselineCalculator.calculate`
9. 启用并通过 `test_baseline.py`
10. `HotNewsRanker.rank`
11. 启用并通过 `test_ranking.py`
12. 最后才实现真实企业数据库适配器

完成一个 TODO 就启用对应测试，不要一次实现全部功能。

## 热点与知识库关联

当前已提供以下边界：

```text
RankedHotNews
  → NewsContentRepository.get_by_news_id（精确内容查询）
  → KnowledgeSearchClient.search_related_news（关联报道召回）
  → EnrichedHotNews（排行、内容和证据合并）
```

- `news_content.py` 定义内容模型、仓储协议和本地内存实现。
- `hot_news_enrichment.py` 负责按排行顺序组合内容与关联报道。
- `app/clients/knowledge_base.py` 实现 FastGPT 数据集检索适配器。
- 真实 FastGPT 检索需要配置 `FASTGPT_DATASET_ID`，并保证入库元数据包含
  `news_id`、`original_title`、`source_url` 和 `publish_time`。

本地测试使用假知识库，不需要启动 FastGPT；连接真实环境时替换
`KnowledgeSearchClient` 实现即可，不改变热点计算代码。
