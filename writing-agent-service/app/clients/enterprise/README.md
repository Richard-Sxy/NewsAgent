# 企业服务 RPC 接口与防腐层

> 状态：**仓库适配层已实现，真实 RPC 待联调**。本目录已有强类型契约、领域 Adapter、
> 集中装配与契约测试；没有接入腾讯内部 SDK、地址、鉴权或真实数据，因此不能表述为
> 企业链路已经联调或上线。

## 分层方式

企业接入采用 Ports and Adapters，而不是让数仓字段或生成的 RPC Stub 直接进入业务代码：

```text
HotNews Workflow / Activity
→ HotNewsOrchestrationService
→ 现有领域 Port
→ Enterprise Adapter
→ 本目录的强类型 RPC Client
→ 企业生成 SDK / 服务治理框架
```

- `analytics`、`retrieval`、`services` 保存稳定业务对象和规则。
- 本目录的 Request/Response 描述企业服务边界，并对返回数据做第一层严格校验。
- Adapter 负责企业枚举、时间、时长、分页、缺失值和错误码到领域对象的显式转换。
- 企业生成的 Stub、注册中心和鉴权代码只允许出现在 Adapter/Client 内部。

## 已实现内容

- `common.py`：可信调用上下文、响应版本和统一 RPC 错误分类。
- `behavior_data.py`：行为数据 watermark、分页查询、企业枚举映射、窗口/版本/游标校验。
- `baseline.py`：按 `news_id + content_type` 批量查询聚合基线并校验策略与数据版本。
- `news_content.py`：按 `news_id` 分批读取正文，校验返回集合完整性并过滤受限内容。
- `related_news.py`：批量关联报道召回，校验策略/索引版本、去重并排除热点自身。
- `model_gateway.py`：企业模型网关的结构化请求和响应。
- `dependencies.py`：真实 Client、版本配置和领域 Adapter 的集中依赖装配。
- `tests/test_enterprise_hot_news_adapters.py`：不依赖企业环境的传输契约与防腐层测试。

所有 RPC 都要求显式 `tenant_id`、`request_id`、`trace_id` 和 deadline。访问凭据不得放入
业务 Request，必须由真实 Client 从企业凭据系统或运行环境获取。

## 与现有领域接口的映射

以下 Adapter 已实现，热点计算和 Agent 业务规则不感知企业字段：

1. `RpcBehaviorDataSource`：分页调用 `BehaviorWarehouseRpc`，把
   `anonymous_user_key`、企业事件枚举和毫秒时长映射为 `BehaviorRecord`。
2. `RpcHotNewsBaselineProvider`：把 `MetricBaselineRow` 映射为
   `NewsMetricBaseline`，并验证 Bundle 中的基线策略版本。
3. `RpcNewsContentRepository`：批量读取正文，将 `NewsContentRow` 映射为
   `NewsContent`，拒绝 removed/restricted 内容。
4. `RpcRelatedNewsSearchClient`：把召回结果映射为 `RelatedNews`；企业服务只负责召回，
   主体、事件、关键词和时间等最终重排继续由 `RelatedNewsReranker` 完成。
模型侧当前继续使用已实现的 `FastGPTClient.run_structured`。`model_gateway.py` 仅预留
企业统一模型 RPC 契约；若后续替换模型基础设施，再实现对应 Runner，之后仍必须经过
Pydantic Schema 与 `HotNewsAnalysisValidator`，不能信任模型网关返回。

`BehaviorDataSource.fetch`、`HotNewsBaselineProvider.get_baselines`、
`NewsContentRepository.batch_get_by_news_ids` 和
`KnowledgeSearchClient.batch_search_related_news` 均为异步领域 Port；编排与富化服务采用
显式 `await` 和批量调用，不存在 sync-over-async 桥接。

真实 RPC SDK 如果只提供同步方法，应在 Adapter 的受控线程池中执行；不要在异步
Temporal Activity 的事件循环中直接调用阻塞 Stub。分页和批量查询必须设置总 deadline，
不能为每一页重新获得完整超时预算。

## 装配入口

拿到企业 IDL/SDK 后，由部署入口创建真实 Client，再调用现有 Factory：

```text
Settings / 企业服务发现
→ 创建行为/基线/内容/检索真实 RPC Client
→ EnterpriseHotNewsRpcClients
→ build_enterprise_hot_news_adapters
→ 注入 HotNewsOrchestrationService 与 HotNewsAnalysisAgentRunner
```

`create_hot_news_orchestration_service` 接收稳定领域 Port；
`create_hot_news_runtime` 继续组装当前 FastGPT Runner，并可通过 `knowledge_search` 注入企业
检索 Adapter（此时不要求 FastGPT Dataset 配置）。业务 Service 不创建企业 Client。
热点 Window 的重试由 Temporal 控制；真实 Client 只执行单次调用并将错误转换为
`EnterpriseRpcError` 子类。

## 已覆盖与待完成

仓库测试已覆盖 watermark 不完整、分页字段映射、基线策略漂移、内容静默缺失、检索自身
排除与切片去重。真实联调仍需企业 IDL/SDK、字段枚举、服务发现、鉴权方式、错误码、
限流与测试环境；所需信息模板见 `docs/enterprise-rpc-integration-checklist.md`。获得这些信息后，
只需实现四个 Protocol 的 Client，不再改动热点计算、重排或 Agent 主链路。
