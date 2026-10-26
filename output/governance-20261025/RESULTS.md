# 2026-10-25 本地部署与运行治理评测

本地实现与实测已完成；本轮严格全通过门槛未通过。不能表述为“已完成企业正式环境部署”。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| 环境 | Docker Desktop 12 CPU、约 15.2 GiB 可用总内存；与既有演示服务共享本机 |
| 应用 | 两个 API 副本，同一当前工作区代码；Python 原生确定性模型 Port |
| 流量 | 10 虚拟用户，600.005 秒，完成每轮后思考 5 秒；实测 HTTP 峰值在途 10 |
| 总尝试 | 1175；含创建测试会话阶段失败 |
| 正确完成 | 1174/1175 = **99.9149%**；校验终态、持久化、跨副本重放、用户/租户隔离 |
| 已完成热点报告查询 | **583/583**；实际执行 list_hot_news 和 read_hot_news；P95 **0.236583 秒** |
| 问候 | 591/592；已取得 POST 延迟样本的 P95 0.087569 秒 |
| 消息 POST 总体 P95 | 0.215 秒；会话创建/验证请求不计入该延迟 |
| 业务请求 Trace | 1174/1174 查回；失败的会话创建没有进入消息 POST，不伪造模型链路 |
| 跨进程探针 | HTTP → Temporal Workflow → 独立 Worker Activity → 配额 → 模型 → 业务校验全部通过 |
| 专项测试 | 105 个不同用例通过；最终查回器单独复核 20 项通过 |
| 修复后短测 | 19/19 业务及 Trace 检查通过；不能替代上方 10 分钟原始失败统计 |
| Token | 本地替身不返回真实 Token 用量，记为未知；供应端用量映射已有合同测试 |

唯一客户端失败：`request_id=474674e2-008e-4739-a0c2-7e0968b89921`，错误类 `ReadError`，
发生在创建会话读取响应时，未得到 conversation_id、未发送消息 POST。不能计作模型失败，
也不能从总尝试分母删除。尚未复现该连接读取错误，继续列为稳定性复测项。

本轮不是新 SQL 查询、完整热点 Workflow、真实模型、SSE 或 24/72 小时长稳测试。
5 秒思考时间的 10 个虚拟用户也不等于一直占满 10 个模型并发。

## 配置核对与修复

- 本机原有 API/Worker、PostgreSQL、Redis、Temporal、MinIO 和完整观测栈；既有 API 与
  热点 Worker 的镜像 ID 不同，不能直接声称同一代码版本已验收。
- 新增 Redis 原子共享并发/RPM：全池并发 4、交互保留 2，RPM 1200、交互保留 200；
  排队 10 秒、调用总期限 120 秒。真实 Redis 六进程竞争测试验证全局容量不被副本数放大。
- 增加每个 SDK 进程的 service.instance.id；Prometheus 实际以 instance 标签区分副本，
  修复累计计数覆盖风险。
- Prometheus 原 512 MiB tmpfs 被 WAL/head 用满。保留约 512 MiB 原始归档后迁入命名卷，
  配置 24h/2GB 数据块保留、WAL 压缩、2 GiB 内存上限。
- 初始 Trace 批量查回没有时间范围，随后在受限 Tempo 内存下产生查询和采集超时。
  已增加逐请求 JSONL 检查点、时间范围、单路查回、20 秒请求与 120 秒总预算，批量不自动
  重试。Tempo 在线扩容至 3 GiB 后恢复 ready 和低 CPU；原业务请求的 1174 条 Trace 随后
  全部补查，期间采集失败记录保留。扩容与后测不能把早先观测故障变成“从未发生”。
- 企业 K8s 基础、迁移 Job、TKE 入口模板已离线渲染；没有连接或部署企业集群。

原始与各轮补查报告均保留。补查只读取遥测，不重发业务、不改成功率、不改耗时。
短测和主压测后的源代码摘要分开保存；持续测量使用 conversation 场景。随后修正了交互
场景列表中的 Text2SQL 名称并区分“排队超时/模型执行超时”，经专项回归和恢复后短测验证，
没有用短测替换原持续负载的结果。

## 证据入口

- `summary.json`：小体积汇总、错误样本和分场景 P95。
- `soak-10-users-600s.json`：首次原始报告，含当时不完整的 Trace 查回。
- `soak-final.json`：全部已提交业务请求 Trace 的复核报告，严格 passed 仍为 false。
- `cross-process-trace-final.json`：完整跨进程脱敏链路；TraceID
  `647666b912a5f19c0a1a910d81bcced4`。
- `recovered-config-smoke.json`：恢复后 19 条全通过短测；过载期间的短测也单独保留。
- `tests-final.xml`、`load-tests-final.xml`：测试用例级结果。
- `runtime-audit.json`、`runtime-audit-final.json`：测量时与最后代码 SHA-256、镜像和进程信息。
- `metrics-final.json`：副本指标和累计 exporter 失败计数，不能解释为失败已经归零。
- `k8s-*.rendered.yaml`：离线渲染输出，企业占位符仍需配置。

全量复核 JSON 约 25 MiB，批量报告/逐请求 JSONL 和恢复归档保存在本地并由 `.gitignore`
排除；小体积汇总与源码配置可进入版本控制。Prometheus 归档位于
`var/observability-backup-20261025/prometheus.tar`，未删除原始证据。

完整调用链、核心类/函数、外部依赖、配置、启动/复跑命令、测试入口与 TODO 见
[`local-runtime-governance.md`](../../agent/docs/local-runtime-governance.md)。

## 当前服务与未完成项

本地验收 API 在 `127.0.0.1:38101`、`127.0.0.1:38102`；Grafana 为
`http://127.0.0.1:23000/d/newsagent-observability`。独立诊断 API/Worker 仅用于本轮 e2e
基础设施测试，可用文档中的 diagnostic profile 复跑，不可部署到企业环境。

企业正式发布、真实模型语义质量/Token/TPM、Embedding 配额、队列公平性、全业务混合负载、
连接故障复测、观测容量告警/完整持久化、Redis/Temporal 故障恢复及 24/72 小时长稳仍待验收。
应用模型治理默认关闭，原有业务服务没有自动切换到本轮隔离副本的配置。

可采用的准确表述：

> 完成本地双副本部署与运行治理验证，支持 API—Workflow—Activity—模型跨进程追踪、
> 分阶段诊断和 Redis 共享并发/RPM及交互容量预留。在确定性本地模型、10 个虚拟用户、
> 持续 10 分钟、5 秒思考时间下，1175 次尝试的业务合同正确完成率为 99.915%，其中
> 583 次已完成热点报告查询全部完成，P95 为 0.237 秒。企业正式部署和真实模型 SLO 待验收。
