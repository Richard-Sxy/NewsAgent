# NewsAgent 企业 RPC 联调信息清单

这份清单可以直接发给数仓、内容中心和检索平台负责人。仓库已经实现领域 Port、防腐层、
批量/分页、数据质量校验与依赖装配；真实联调阶段只补企业 SDK Client 和字段映射。

不要在文档或代码评审中提供密钥、Token、Cookie 等凭据值，只需要说明凭据获取方式和
运行身份申请流程。

## 每个服务都需要确认

| 类别 | 需要的信息 |
|---|---|
| 服务定位 | 服务名、命名空间、负责人、调用方 AppKey/服务身份、测试与生产环境 |
| 协议 | RPC 框架、IDL/生成 SDK 包、方法全名、SDK 版本、异步或同步调用方式 |
| 请求 | 完整字段、必填项、枚举、时间单位与时区、批量上限、分页语义 |
| 响应 | 完整字段、空值语义、缺失记录表达、数据/索引版本、响应示例 |
| 一致性 | 数据水位、分区完成标志、同一分页快照是否稳定、延迟 SLA |
| 治理 | 服务发现、鉴权/租户隔离、超时建议、QPS、限流、熔断和降级规则 |
| 错误 | 错误码表、哪些可重试、Retry-After、超时与部分成功的表达方式 |
| 可观测性 | request_id/trace_id 透传字段、监控指标、日志检索与告警负责人 |

## 1. 行为数仓或行为查询服务

请确认能否按左闭右开窗口 `[window_start, window_end)` 查询以下最小字段：

| 本项目字段 | 企业字段/枚举（待填写） | 说明 |
|---|---|---|
| `event_id` |  | 全局或窗口内唯一性；是否可能重复上报 |
| `anonymous_user_key` |  | 只需不可逆匿名标识，不要原始账号/设备明细 |
| `news_id` |  | 是否与内容中心统一；历史 ID 是否需要转换 |
| `event_type` |  | 曝光、点击、阅读/播放、点赞、评论、分享映射 |
| `event_time` |  | 单位、时区、事件时间还是入库时间 |
| `content_type` |  | 图文/视频枚举 |
| `duration_milliseconds` |  | 阅读或播放时长；无效值和封顶规则 |
| `channel` / `device_type` |  | 是否获准用于聚合，可为空 |

还需询问：

- 是否有“分区已完成/数据已追平”的 watermark 接口，能否证明数据覆盖到 `window_end`；
- 支持游标分页还是 offset 分页，单页/单次最大记录数，同一查询的数据版本如何保持稳定；
- 数据源是否保证 `event_id` 唯一；若保证，需要正式口径才能启用快速路径；
- 是否已有聚合指标 RPC。若只能提交离线 SQL，需要任务提交、状态查询和结果读取三个接口。

对应仓库接口：`BehaviorWarehouseRpc.get_watermark/query_events`；企业枚举通过
`build_enterprise_hot_news_adapters` 的 `behavior_event_type_mapping` 和
`behavior_content_type_mapping` 参数注入。

## 2. 历史基线/指标服务

需要按 `news_id + content_type` 批量返回可比历史窗口的聚合均值：曝光、点击、UV、总消费
时长、有效消费、互动、CTR 和样本窗口数。请确认：

- 基线口径（自然日/同星期/同时段）、有效消费阈值、异常值处理和回补规则；
- `baseline_policy_version` 与数据分区版本如何获得，分页期间是否固定；
- 没有历史数据时是显式 missing 还是不返回；Decimal 精度和除零口径；
- 批量键上限、分页方式、数据可用延迟和超时建议。

对应仓库接口：`MetricBaselineWarehouseRpc.query_baselines`。

## 3. 新闻内容中心

需要按统一 `news_id` 批量读取标题、摘要、正文、内容类型、发布时间、规范 URL、内容版本和
状态。请确认：

- `news_id` 是否可直接关联行为数仓；若不是，需要权威 ID 映射服务；
- 批量上限、正文最大长度、富文本/纯文本格式、编码和发布时间时区；
- 已删除、下架、权限受限、未找到分别如何表达；是否可能部分成功；
- 内容修改后的版本字段、缓存 TTL 和数据合规限制。

对应仓库接口：`NewsContentRpc.batch_get_news`。返回集合必须完整：每个请求 ID 要么出现在
`items`，要么出现在 `missing_news_ids`，静默遗漏会被拒绝。

## 4. 关联新闻检索服务

需要批量输入 `query_id/source_news_id/title/summary/exclude_news_ids/candidate_limit`，返回候选
新闻 ID、文档/切片 ID、标题、摘要片段、URL、发布时间、召回通道和归一化相关度。请确认：

- 支持向量、关键词还是混合召回；分数方向、范围和是否已归一化；
- 批量 query 上限、每个 query 候选上限、超时与 QPS；
- 能否服务端排除 `source_news_id`，切片如何归并为新闻；
- `retrieval_policy_version`、`index_version` 和索引数据延迟；
- 元数据缺失的比例与权威补全来源。

对应仓库接口：`RelatedNewsRetrievalRpc.batch_search_related_news`。企业服务负责召回，本项目的
`RelatedNewsReranker` 继续负责主体、事件、关键词、时间和基础相关度的确定性重排。

## 5. 调用端落地模板

拿到生成 SDK 后，为四个 Protocol 各写一个很薄的 Client：组装企业请求、调用 Stub、把
错误码转换为 `EnterpriseRpcError`，再把响应构造成本目录 Pydantic DTO。随后按下面顺序装配：

```text
企业服务发现/运行身份
→ 四个真实 SDK Client
→ EnterpriseHotNewsRpcClients
→ build_enterprise_hot_news_adapters
→ create_hot_news_orchestration_service 或 create_hot_news_runtime
→ HotNewsActivities
→ HotNewsWorkflow
```

真实环境验收至少准备：一个有完整数据的窗口、一个水位未完成窗口、缺少正文/受限内容、
重复切片/自身召回、限流、超时、服务不可用和分页版本漂移。联调通过后再执行 PostgreSQL、
Temporal 和模型服务的端到端验证。
