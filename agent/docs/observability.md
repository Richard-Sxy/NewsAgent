# NewsAgent 全链路可观测与模型故障定位

适用范围：本仓库 Python Agent 的 API、Temporal Worker、业务工具、知识链路与模型 Port。
本次提供可运行的本地诊断栈和经过测试的埋点契约；企业生产部署、真实模型质量与企业告警渠道尚未验收。
不引入外部 Agent/知识库框架，不改权威指标计算和人工审批边界。

## 执行计划与验收层次

| 步骤 | 实施结果 | 验收方式 |
| --- | --- | --- |
| 1. 统一技术契约 | 进程级 SDK、受控属性、JSON 日志、错误阶段、低基数指标、W3C context | 内存 exporter 与真实本地 OTLP HTTP 合同测试 |
| 2. 串起上游到下游 | HTTP/SSE → Temporal → Activity → 工具/DB/知识/模型 → Artifact/Outbox | 父子 span、重试、普通返回值失败、流式生命周期测试 |
| 3. 拆解模型错误 | Prompt、HTTP、Envelope、JSON、Schema、业务可信性分别定位 | HTTP 200 但 JSON/Schema/业务校验失败、429、超时等测试 |
| 4. 建立可查询后端 | Collector、Tempo、Loki、Prometheus、Grafana 看板/双向跳转、告警 | Compose + 组件自身校验 + 纯合成三信号查回 |
| 5. 交付定位方法 | 覆盖矩阵、配置、运行命令、查询、已知边界、一页 PDF | 本文与 `evaluation/render_observability_pdf.py` |

一次合成 smoke 只能证明三信号传输与查回。单元/合同测试证明埋点行为。
两者均不能代替企业推理/Embedding 接口、真实数据规模、业务质量、生产 SLO 验收。

## 完整调用链

```text
API / SSE / Worker / Schedule / 批处理入口
  → configure_telemetry（读取 OBSERVABILITY_*；进程 lifespan 启停）
  → TelemetryMiddleware（提取 W3C traceparent/tracestate；返回 x-trace-id）
  → conversation turn / tool 或热点/写作/知识服务
  → Temporal TracingInterceptor（跨 Client → Workflow → Activity 传播，不传 baggage）
  → Activity 逐次尝试 → 取数 / SQL / 正文 / 检索 / 重排 / 指标 / 基线 / 排行
  → model.structured
      → model.prompt_render（版本绑定）
      → model.inference → model.http.request → 已批准企业模型 Port 或隔离本地替身
      → model.response_validate（Envelope、模型绑定、Embedding 形状）
      → model.response_decode（JSON）→ model.schema_validate（Pydantic Schema）
  → model.business_validate（新闻证据/因果/指标或写作契约）
  → PostgreSQL / S3 Artifact / Outbox（私有 _telemetry → relay → Redis）
  → 用户响应 / SSE / 持久化报告 / 查询投影

上面每个受控 operation
  ├─ Span + 安全技术属性（版本、ID、阶段、错误分类）
  ├─ Counter/Histogram（仅低基数维度）
  └─ operation.completed / operation.failed 结构化日志（trace_id/span_id）
       ↓ OTLP HTTP /v1/traces、/v1/metrics、/v1/logs
本地 gateway（固定目标，宿主仅 127.0.0.1）
  → OTel Collector（memory_limiter → batch）
      ├─ OTLP HTTP → Tempo → TraceQL / Trace 瀑布
      ├─ OTLP HTTP /otlp → Loki → LogQL / Logs ↔ Trace
      └─ Prometheus exporter :8889 → Prometheus → PromQL / 规则求值
  → Grafana 预置三数据源与中文模型/链路看板
```

业务字段中的 `trace_id` 沿用原业务含义，不覆盖为 OpenTelemetry ID。
真正分布式跟踪 ID 来自当前 OTel span context，HTTP 响应头是 `x-trace-id`。
`model_request_id` / provider request ID 是诊断关联信息，不是业务幂等键。
跨进程依赖入口实际使用新的环境配置/版本；仅启动栈不会给旧容器自动加埋点。

核心实现入口：

| 层 | 核心类/函数与输出 |
| --- | --- |
| SDK 与安全边界 | `app/observability/settings.py::ObservabilitySettings`；`telemetry.py::configure_telemetry/operation/record_error/record_metric/log_event`；provider 缓冲、flush、shutdown 与属性白名单 |
| HTTP/SSE | `app/observability/web.py::TelemetryMiddleware`；按路由模板记录，响应流结束后结束 span；探针不循环采样 |
| Temporal | `app/observability/temporal.py::temporal_interceptors`；官方 interceptor 与安全 facade；Activity 记录 workflow/run/type/attempt，重放不增加真实业务副作用 |
| DB 与边界装饰器 | `app/observability/database.py::instrument_engine` 仅 SQL 类型；`decorators.py::traced` 处理异常和普通领域失败返回值 |
| 模型 | `app/model_runtime/core.py` 的结构化调用/模型指标；`app/model_runtime/http.py::{OpenAICompatibleInferenceClient,OpenAICompatibleEmbeddingClient}` 的原始 HTTP 适配；模型 Port 的确定性本地替身沿用隔离测试语义 |
| 采集/存储/展示 | `deploy/observability/gateway.conf` → `otel-collector.yml` → `tempo.yml` / `loki.yml` / `prometheus.yml` / `alerts.yml` → `grafana/provisioning` 与 `grafana/dashboards/newsagent.json` |

## 覆盖矩阵与限制

| 链路 | 现在能定位 | 明确限制 |
| --- | --- | --- |
| HTTP 与 SSE | 模板 route、method、status、总体延迟、响应头 TraceID、流完整生命周期 | 不采集 query/body/header 原文；浏览器前端未加 RUM |
| Temporal | workflow/activity 父子关系、每次 attempt、跨队列跟踪与失败 | 不替代 Temporal server 自身的运维指标；生产调度流量未验收 |
| 热点与对话 | turn/tool、聚合取数、正文、知识召回、重排、指标/基线/排行、业务校验 | 未加每条 SQL/每个候选文档的内容级追踪；不保存用户行为明细 |
| 模型/Embedding | route/scene、模型/Prompt/Schema 版本或哈希、HTTP 状态、重试属性、供应端 ID、分阶段失败、Token 用量（已知时） | 不采集 Prompt/新闻/检索文本/回答原文；缺少 Token 时不伪造零；不据此推断企业质量 |
| DB | `db.query`、类型、耗时、错误类 | 不记录 SQL statement、参数、DSN；SQL cursor span 是 DB 边界，不能看到数据库内部执行计划 |
| Artifact、Outbox、Redis | S3 保存边界，Outbox 私有 W3C context、relay 推送边界 | 旧 Outbox 无 context 时新建 root；不改变既有 DB Schema，不把私有 telemetry 投影给订阅用户 |
| 知识入库与索引 | ingest/search/index 批次和操作、下游错误与耗时 | 向量服务内部、BGE 第三方库和分析服务子进程仅在调用边界观测；无内核/硬件剖析 |
| 可观测系统自身 | Prometheus `up`、Collector exporter 失败、受控队列/内存、日志滚动、后端容量/保留上限 | 本地 tmpfs 容量满会丢数据/失败；本地无持久化队列、HA 或外部通知 |

## 配置

应用默认 `OBSERVABILITY_ENABLED=false`，不创建 OTLP exporter、不发网络请求。
JSON 安全日志默认开启；关闭 JSON 选项会恢复应用原有日志行为，应自行确保原日志不含敏感内容。
配置独立读取进程环境，`env_file=None`，不读取 `.env`。

| 配置项 | 默认值 | 本地栈建议 |
| --- | --- | --- |
| `OBSERVABILITY_ENABLED` | `false` | 显式 `true` |
| `OBSERVABILITY_SERVICE_NAME` | `newsagent-api` | 每类独立入口使用稳定名称；已有 API/Worker 启动函数会传入自己的角色名 |
| `OBSERVABILITY_SERVICE_VERSION` | `0.1.0` | 实际构建版本/提交号 |
| `OBSERVABILITY_ENVIRONMENT` | `local` | `local` / 已批准部署环境 |
| `OBSERVABILITY_OTLP_ENDPOINT` | `http://127.0.0.1:4318` | **本栈是 `http://127.0.0.1:24318`**，填 base URL，不加 `/v1/...` |
| `OBSERVABILITY_SAMPLE_RATIO` | `1.0` | 本地复现保留 100%；ParentBased 采样继承上游决定 |
| `OBSERVABILITY_JSON_LOGS` | `true` | 保持开启 |
| `OBSERVABILITY_LOG_LEVEL` | `INFO` | INFO；控制 `operation.completed` 数量 |
| `OBSERVABILITY_EXPORT_TIMEOUT_SECONDS` | `5` | 5 秒，上界 60 秒 |
| `OBSERVABILITY_METRIC_EXPORT_INTERVAL_MS` | `15000` | 15 秒；短 smoke 可设 1000 |

Trace 采样与指标独立：采样为 0 或上游未采样时，指标与日志仍产生，日志的 TraceID 不保证有可查 Trace。
不得把采样后 Trace 数量当权威业务计数；指标由代码确定性记录。

本地默认端口全部只在 `127.0.0.1`：OTLP HTTP `24318`、Grafana `23000`、Prometheus `29090`、
Loki `23100`、Tempo `23200`。可分别通过 `NEWSAGENT_OTLP_HTTP_PORT`、`NEWSAGENT_GRAFANA_PORT`、
`NEWSAGENT_PROMETHEUS_PORT`、`NEWSAGENT_LOKI_PORT`、`NEWSAGENT_TEMPO_PORT` 覆盖。

Compose 仅包含独立诊断组件；不自动合并/重启现有应用服务。固定版本并锁定官方镜像 digest。
五个采集/存储组件只接 internal 网络；非 root Nginx gateway 接本地 ingress 桥，仅固定转发五个目标。
这样避开 Docker Desktop 对 internal-only 网络不发布宿主端口的行为，同时保留后端无公网出口。
关闭 Grafana/Tempo/Loki 使用统计、更新检查与自动插件安装，没有远程 exporter。

Prometheus/Tempo/Loki 均有 24 小时保留配置，每个数据目录使用最大 512 MiB 的 tmpfs；
Prometheus 的数据块额外限制 384 MiB。**24 小时是过期策略，不保证一定保留满 24 小时**；
容量先到上限时会失败/丢失，停止容器会清空暂存数据。Grafana 数据 128 MiB，各容器有内存限制，
Docker 日志每个最多 2 × 10 MiB。不向业务数据库新增遥测表。

Grafana 为本地匿名 Editor，以便 Explore/Trace 跳转；没有默认管理员密码，禁登录与注册。
这个本地模式不能直接部署生产。生产接入要另行批准 TLS、SSO/RBAC、租户隔离、存储、保留和告警渠道。

## 运行与验证

以下在仓库根目录执行。`--env-file /dev/null` 避免 Compose 隐式读取应用 `.env`。

```bash
docker compose --env-file /dev/null -f agent/deploy/docker-compose.observability.yml config --quiet
docker compose --env-file /dev/null -f agent/deploy/docker-compose.observability.yml up -d
python3 agent/deploy/observability/smoke.py
```

打开 [Grafana 看板](http://127.0.0.1:23000/d/newsagent-observability) 与
[Prometheus 告警](http://127.0.0.1:29090/alerts)。第一批指标需等待 SDK 的 15 秒推送和 Prometheus 的 5 秒抓取；
`rate` 面板通常需至少两次抓取。没有数据不会自动填成“全绿”。

合成 smoke 将本地 `response_decode / invalid_json` 错误发送到 Collector，再按同一 TraceID 从 Tempo、
Loki 查回，按稳定 service 从 Prometheus 查回计数。脚本仅允许 loopback HTTP，最长查回等待 45 秒，
不启动业务 Workflow、不调用企业模型，不读取任何凭据。成功输出 `trace/log/metric: true`。

启动宿主 Python 进程时显式传入：

```bash
cd agent
OBSERVABILITY_ENABLED=true \
OBSERVABILITY_OTLP_ENDPOINT=http://127.0.0.1:24318 \
OBSERVABILITY_SERVICE_VERSION=local-development \
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

同样把配置传给实际运行的 Worker/Scheduler/批处理入口。容器中的 `127.0.0.1` 是该容器自身；
不得用 `host.docker.internal` 假设能访问只绑定宿主 loopback 的端口。
若要给现有 Docker 应用接入，需在**其显式部署配置**中接入这个栈的 internal 网络，
使用 `http://otel-collector:4318`，并仅重建你选择的应用角色。此操作不会由本文件自动执行。
生产保持企业批准的内部 Collector 地址，不能默认向个人电脑或公网发送遥测。

```bash
# 不依赖 Docker 的 SDK/模型/链路/静态部署合同测试（仓库根目录）
python3 -m pytest agent/tests/test_observability_telemetry.py agent/tests/test_observability_business.py \
  agent/tests/test_model_observability.py agent/tests/test_observability_chain.py agent/tests/test_observability_deploy.py

# 真实组件校验（仓库根目录；一次性容器不会启动依赖服务）
docker compose --env-file /dev/null -f agent/deploy/docker-compose.observability.yml \
  run --no-deps --rm otel-collector validate --config=/etc/otelcol/config.yml
docker compose --env-file /dev/null -f agent/deploy/docker-compose.observability.yml \
  run --no-deps --rm --entrypoint /bin/promtool prometheus check rules /etc/prometheus/alerts.yml

# 停止且只清理这个独立栈；tmpfs 遥测随之清空
docker compose --env-file /dev/null -f agent/deploy/docker-compose.observability.yml down
```

## 指标与查询契约

Collector 使用 `UnderscoreEscapingWithSuffixes`：属性点号转下划线、计数追加 `_total`、秒直方图追加 `_seconds`。

| SDK 名称 | Prometheus 名称 | 主要维度 |
| --- | --- | --- |
| `newsagent.model.calls` | `newsagent_model_calls_total` | `model_scene/model_route/gen_ai_operation_name/newsagent_stage/newsagent_outcome/error_type` |
| `newsagent.model.duration`（s） | `newsagent_model_duration_seconds_bucket/count/sum` | 模型场景/路由/阶段/结果 |
| `newsagent.model.tokens` | `newsagent_model_tokens_total` | 场景/路由、`gen_ai_token_type=input/output/total`；三个类型不能相加，total 本身已含 input/output |
| `newsagent.operation.calls` | `newsagent_operation_calls_total` | `newsagent_operation/newsagent_stage/newsagent_outcome/error_type/error_kind` |
| `newsagent.operation.duration`（s） | `newsagent_operation_duration_seconds_bucket/count/sum` | 操作/阶段/结果 |
| `newsagent.http.requests/duration` | `newsagent_http_requests_total` / `newsagent_http_duration_seconds_*` | HTTP method/模板 route/status |

模型专用计数 `error_type` 是静态 failure kind（如 `invalid_json`、`timeout`）；
通用 operation 的 `error_type` 是异常类，`error_kind` 才是受控分类。
版本、run/job/turn/provider request ID 只用于 span/日志，不能成为高基数指标标签。
Token 只记录供应端返回的有效非负整数；没有用量时没有该项指标。

定位查询示例：

```promql
# 模型结构/调用失败，按阶段分类
sum by (model_scene, model_route, newsagent_stage, error_type)
  (increase(newsagent_model_calls_total{newsagent_outcome="error"}[10m]))

# 推理成功之后的业务可信性失败
sum by (newsagent_operation, error_kind)
  (increase(newsagent_operation_calls_total{newsagent_stage="business_validation",newsagent_outcome="error"}[10m]))

# p95，单位秒
histogram_quantile(0.95, sum by (le, model_scene, model_route)
  (rate(newsagent_model_duration_seconds_bucket[5m])))
```

```logql
{service_name="newsagent-api"} | trace_id = "替换为32位OTelTraceID"
{service_name=~"newsagent-.+"} | newsagent_outcome = "error"
```

```traceql
{ status = error }
{ span.newsagent.stage = "schema_validation" }
{ span.newsagent.run_id = "替换为业务runID" }
```

OTLP LogRecord 的 TraceID 在 Loki 为结构化元数据 `trace_id`，不建索引标签。
Grafana Loki derived field 使用 `matcherType: label`，Tempo 自定义 Logs 查询按元数据过滤，
不对只有固定 event 名称的日志正文做 TraceID 正则。Metrics 的 exemplar（存在且被采样时）可跳到 Tempo。

## 故障定位

1. **确认观测没有丢失**：看 Prometheus `up` 和 Collector exporter 失败；查看本项目容器状态。
   无指标/Trace 先排查 `OBSERVABILITY_ENABLED`、base endpoint、进程版本、采样与队列/容量，不直接归因模型。
2. **从入口取 ID**：保留 HTTP 的 `x-trace-id`，或者从 `operation.failed` 日志找到 OTel TraceID；
   按 run/job/turn ID 也可在 span 属性检索。不要把业务 `trace_id` 或 provider ID 当 OTel ID。
3. **看最早失败的子 span**：先看取数/DB/检索/重排是否失败、为空或超时，再看模型阶段。
   父 span 的失败可能只是传播，多个 error span 不等于多次独立模型调用。
4. **按模型阶段归因**：
   - `prompt_render`：Prompt 模板与受控输入契约；记录实际 Prompt/Schema 版本。
   - `inference` / `model.http.request`：`timeout/connection/rate_limit/http_error`；检查 HTTP 状态、重试属性和 provider request ID。
   - `response_decode / invalid_json`：HTTP 200 的非 JSON、围栏等解析失败；不能以 HTTP 成功率替代模型成功率。
   - `schema_validation`：JSON 合法但 Pydantic 结构或字段类型不符；核对 schema 名称、版本/哈希与 Prompt/profile。
   - `model_binding`：返回模型与绑定版本不匹配；`response_validation`：Embedding 数量/向量值/维度或 Envelope 不合约。
5. **检查后置业务门禁**：`model.business_validate / business_validation` 的
   `news_id_mismatch/memory_reference/evidence_missing/evidence_reference/attention_reason/writing_contract`。
   模型结构指标全成功时，这里依然可能失败；看板和告警单独展示。
6. **核对下游保存与异步推送**：S3、DB、Outbox relay/Redis；旧消息没有 context 时独立 root 是预期兼容行为。
7. **比较版本并复现**：固定已批准 model/Prompt/Schema/Validator/Bundle，使用脱敏合成或已授权证据重跑。
   本地 telemetry 只保留安全属性，不含回答原文；需要全文复查时沿已有授权 Artifact/审计入口读取，不能临时打开通用 payload 采集。

默认告警包含模型错误率（5 分钟 ≥5 次且 >5%）、p95（>30 秒）、输出契约失败、后置业务校验失败、
后端抓取失败、Collector exporter 失败。阈值是待校准的本地初始值。
规则由 Prometheus 求值，Grafana 使用相同数据展示；**没有接 Alertmanager/短信/邮件/IM**，不会通知外部人员。

## 待完成 TODO

- 企业推理与 Embedding 端到端验收、真实 HTTP 错误/限流与Token协议、跨天长 Workflow/重放验证。
- 企业生产 TLS、鉴权、租户隔离、遥测数据域批准、SLO/告警阈值、外部通知渠道和 on-call 责任人。
- 生产持久队列、丢弃/队列积压、Collector 与应用心跳/卡死、CPU/内存/磁盘平台监控、持久存储/容量规划/HA。
- 浏览器 RUM 与前后端 trace context、模型 TTFT/流式 token 时延、成本定价版本（当前不凭空估价）。
- 第三方检索/向量服务与分析子进程内部 span；业务未埋点的脚本/临时入口仍需主动调用统一 SDK。
- 基于真实模型和已批准数据集的质量评测与脱敏 bad-case 闭环；不得把成功率当事实准确性，不自动改生产 Prompt/规则/模型。

本地 `tests/test_observability_temporal_e2e.py` 已用隔离 Temporal devserver 验证
Workflow → Activity 第 1/2 次尝试 → timer → Replayer replay 的传播与重放；
这不覆盖企业环境、跨天长流程、版本升级与历史大规模重放。

## 本次验证记录（2026-10-16）

- 后端全量回归：`4017 passed, 104 skipped, 4 warnings`（46.27 秒），跳过项不能视为企业验收通过。
- 新增部署配置合同测试：6 项通过；Compose 解析通过，六个官方镜像全部固定 patch tag 与 digest。
- 真实 Collector 自带 `validate` 通过；真实 Prometheus `promtool check rules` 验证六条告警通过。
- 使用独立 `newsagent-observe-check` 项目启动六个组件，纯合成 OTLP HTTP 数据经 Collector 后，
  Tempo/Loki/Prometheus 的 Trace/Log/Metric 均成功查回，同 TraceID 日志过滤有效。
- Grafana 健康检查通过，三个数据源全部 `status=OK`，中文 14 panel 看板已实际载入；
  看板 11 个 PromQL/LogQL 查询、2 个 TraceQL 查询由真实后端接受，六条告警求值 `health=ok`。
- 已读回 Grafana provisioned 数据源配置，确认日志 Label TraceID 与 Tempo Logs 查询宏转义正确。
  验证结束清理独立测试项目，不连接、不重启既有业务容器。
- SDK 本地 OTLP protobuf 三信号合同测试和隔离真实 Temporal 重试/重放验收另行显式通过。

这些结果证明本地实现与诊断数据链路可用；不代表企业真实模型效果、生产安全隔离、持久留存、
容量和 SLO 已完成验收。栈会按“运行与验证”中的命令显式启动，默认应用遥测仍关闭。

## PDF 图解与产物生成链

`evaluation/render_observability_pdf.py` → ReportLab + 本机中文字体 →
`../output/pdf/newsagent-observability-2026-10-16.pdf`（一页中文架构/定位图）。
依赖本机 Python ReportLab 与脚本指定中文字体，使用 PDF 渲染/页数检查验证排版。
该脚本是文档产物入口，不进入业务运行时、不调用模型或企业服务；脚本自身没有业务 TODO。

## 官方依据

- [OTel Collector 配置](https://opentelemetry.io/docs/collector/configuration/) 与
  [Collector 内部指标](https://opentelemetry.io/docs/collector/internal-telemetry/)：三信号 pipeline、组件遥测。
- [Loki 原生 OTLP 日志接入](https://grafana.com/docs/loki/latest/send-data/otel/)：使用 OTLP HTTP `/otlp`，开启 structured metadata；属性点号归一化。
- [Tempo 配置](https://grafana.com/docs/tempo/latest/configuration/) 与
  [Tempo 2.10.8 固定版本](https://github.com/grafana/tempo/releases/tag/v2.10.8)：本栈沿用 2.x 单机存储，不启用 3.x 的新部署架构。
- [Loki 数据源与 derived fields](https://grafana.com/docs/grafana/latest/datasources/loki/configure/) 和
  [Tempo 数据源](https://grafana.com/docs/grafana/latest/datasources/tempo/configure-tempo-data-source/)：日志/Trace 关联。
- [Prometheus exporter](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/v0.162.0/exporter/prometheusexporter)：指标归一化、exemplar、受控 resource labels。
- [Docker bridge 网络](https://docs.docker.com/engine/network/drivers/bridge/)：本地端口与网络隔离边界。
