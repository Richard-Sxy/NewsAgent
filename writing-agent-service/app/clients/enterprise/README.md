# 企业服务适配器预设目录

> 状态：**PRESET ONLY**。本目录没有可执行实现，也不代表已经接入任何腾讯内部系统。

这里预留热点主链路与企业基础设施之间的防腐层。未来实现应遵循现有领域协议，
避免让数仓或 RPC SDK 的字段直接泄漏到业务计算层。

计划适配器：

- `behavior_data.py`：实现 `BehaviorDataSource`，对接行为数仓或数据查询 RPC。
- `baseline.py`：实现 `HotNewsBaselineProvider`，读取已授权的历史聚合快照。
- `news_content.py`：实现 `NewsContentRepository`，按 `news_id` 查询新闻正文。
- `related_news.py`：实现 `KnowledgeSearchClient`，调用企业检索服务召回关联报道。
- `model_gateway.py`：替换本地 FastGPT 适配器，调用企业内部统一模型服务。

完成条件：

1. 明确真实 RPC 契约、鉴权、超时、重试和批量查询能力。
2. 完成企业字段到领域 Schema 的显式转换和数据质量校验。
3. 不在本地落企业原始用户行为明细或凭据。
4. 使用 Mock/Fake 完成契约测试，并通过获授权环境的集成测试。

