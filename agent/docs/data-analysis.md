# 热点数据分析与隔离执行

2026-10-07：同一分析引擎已接入运营配置 DataLoop 的固定 Activity，独立于聊天开关；完整调用链、范围、报告回执和验收入口见[DataLoop 数据分析接入](data-loop/dataset-analysis-integration.md)。默认开关关闭，启用方式见该文档。

模型只选择批准的分析操作，Python 计算权威数字。输入来自已经保存的热点聚合快照，
不执行模型生成的 Python、SQL、文件路径或 shell 命令。

可操作的 `/chat` 六种分析、双窗口准备、独立服务接线和 CLI 见
[聊天数据分析演示](data-analysis-demo.md)；演示保留已有业务项目、数据卷和历史记录。
新演示使用独立业务项目、API `28010` 和前端 `5175`，不改已有 `28000` 演示。

## 框架与完整调用链

news/ 负责新闻发现、抓取、清洗与知识入库；agent/ 用原生 Python 实现 Agent、知识
运行时和业务校验。FastAPI 提供入口，Temporal 编排热点与写作，PostgreSQL 保存运行、
会话和工具证据，Redis 推送进度，S3/MinIO 保存业务 Artifact。Data Loop 处理反馈、
回放评测与人工晋升。根目录 sandbox/run.py 是开发 REPL，与本执行器没有调用关系。

~~~text
/chat → conversationsApi.sendStream / send
→ /api/v1/conversations/{id}/messages[/stream]
→ Gateway tenant/user + hot-news:read → ConversationRepository claim
→ ConversationAgentService.run_turn → 结构化模型 Port → ConversationPlan
→ list/read/query 成功轨迹建立同会话已完成 run 来源
→ analyze_hot_news_data(run_id, operation, metric, reference_run_id?)
→ ConversationTools._analyze → HotNewsQueryService.get_run_detail(可信 tenant_id)
→ analysis_runs.result_payload 的完整 ranked_news
→ projection.project_dataset：身份/完整性/窗口/基线白名单与可比范围指纹
→ Settings → factory.create_analysis_runner → process / docker / RemoteAnalysisRunner
→ validate_request → 规范 JSON / SHA-256 → 固定 worker.py 的 stdin
→ engine.analyze：标准库 Decimal 确定性计算
→ 全版本来源/数字/限制说明按同版本引擎复核
→ render_analysis_result：Python 渲染权威数字与限制说明
→ PostgreSQL ToolTrace / 会话终态 → SSE answer_delta/done → 页面
~~~

service 后端经过固定 HTTPS /v1/analyze、独立 Bearer 鉴权、并发和收包预算，再在
专用容器中启动相同固定 worker；客户端再次校验返回的来源、请求哈希、数字和执行限额。
详见[独立服务、权限合同与部署模板](analysis-service.md)。

新取数继续复用 query_hot_news → Text2SQL 护栏 → Temporal 热点 Workflow。分析工具不
另建 SQL 查询器，不修改热度规则、生产 Bundle 或发布边界。趋势两个 run 均须由本会话
成功工具提供，服务端重新按可信租户读取。模型不能指定租户、端点、凭证或执行权限。
报告复用会话 JSON ToolTrace，无需改变业务 Schema。

## 六种操作与口径

| operation | 输出 | 口径 |
| --- | --- | --- |
| overview | 曝光、点击、有效消费、互动、时长总计，加权 CTR | CTR = 总点击 / 总曝光；零曝光为 null |
| distribution | count、min、max、mean、median、P90 | 同一榜单指定指标的分布，P90 用最近秩法 |
| compare | 最高与最低新闻、绝对差、相对变化 | 同一榜单比较；相对变化 = 差值 / 最低值；零分母为 null |
| quality | 零曝光、CTR 不一致、点击高于曝光列表 | 提示核对事件口径，不直接认定业务错误 |
| baseline | 每条新闻当前值、已保存基线、差值、样本量与参考版本 | 缺失基线/指标保留 null，不生成或补零 |
| trend | 共同新闻逐条与聚合变化、新增/退出榜单和窗口间隔 | 两个可比运行的交集；加法指标求和，CTR 加权，hot_score 求均值 |

metric 白名单为 impressions/clicks/ctr/hot_score/effective_consumptions/interactions/
total_duration_seconds。跨新闻 UV 不能相加得到去重人数，不在指标白名单中。所有结果只
描述运行的完整已上榜子集，不代表全部候选、数仓总体或实时新闻。超过行数/字节预算直接
失败，不截取后称完整分析；趋势行数预算为两个完整榜单的总行数。

baseline 投影已保存的六个参考指标、sample_count/reference_version，排除 UV 与未知
字段。hot_score 没有保存的基线值，报告缺失。样本量不自动解释为历史窗口数。当前曝光
为零或基线明确保存曝光为零时，CTR 与差值保持未知；相对变化零分母同样为 null。实际
基线生成策略、watermark 和参考版本含义仍需企业验收。

trend 要求不同 run、相同 Bundle/Workflow、相等窗口长度、参考窗口在当前之前且不
重叠、共同新闻类型一致，以及非空且相同的选择范围指纹。间隔可大于零并明确显示 gap；
两个观测窗口不能推断连续走势或因果。聚合只在共同新闻上计算，新增/退出新闻不参与
变化；CTR 用共同新闻总点击/总曝光，零曝光保留 null。

范围指纹由已保存 SQL 和补充指标映射的哈希、Schema 版本/哈希、场景、内容类型/分类/
row_limit 等参数计算。租户与窗口另做可信身份校验，不进入跨窗口指纹；问题文本、模型
解释和 query_id 不进入指纹；SQL 文本不进入 worker。身份/哈希不一致、截断、候选与
榜单不符、榜单计数不完整或筛选类型不符均拒绝。缺完整 SQL 证据的历史运行范围为
unknown，拒绝趋势比较，不能用相似新闻 ID 猜测可比性。

## 版本与证据

原四操作保持 schema_version=1.0 / news-analysis-v1；baseline/trend 使用
2.0 / news-analysis-v2。v2 dataset 带 Workflow 和范围指纹；baseline 行含显式基线；
trend 带 reference_dataset。窗口与 gap 以整秒定义，拒绝微秒窗口。

请求拒绝未知字段、重复新闻/rank、异常或非有限数值、原始用户行为、正文、标题、身份和
执行代码。逐新闻窗口/news_id 与源运行一致。Decimal 使用固定精度，结果数字按字符串
传输。快照哈希覆盖规范化 run、窗口、Bundle、全榜单及 v2 证据/基线；参考快照单独带
来源哈希。执行输入哈希还包含操作、指标及两个 dataset。哈希提供一致性追溯，不代替
生产签名式审计。

## 三种执行边界

process 使用当前解释器 -I -S 固定脚本、空临时目录、清洗环境和有界 stdio。父进程限制
总行数、字节、并发和墙钟时间，超时/取消后 kill/wait；Linux worker 另设 CPU、地址
空间、文件描述符、文件大小和 core dump 限制。macOS 不具备本实现的 Linux CPU/内存
硬限制。此后端只允许 local/development/test/e2e，不能承载任意不可信代码。

docker 启动批准的本地镜像与同一固定 worker：无网络、只读根、全部 capability 丢弃、
no-new-privileges、非 root、PID/内存/CPU 配额，无宿主挂载或秘密环境，关闭日志且不
自动拉镜像。失败/超时/取消后有界清理；Docker 不可用直接失败。普通 API 不应挂载宿主
Docker socket，生产采用独立服务与获准执行节点。

service 是专用容器、固定 HTTPS Port 和独立 SecretStr token，API 无需 Docker 权限。
服务只装载分析代码，不连接 DB、模型或企业事件源。独立 Compose 使用 internal 网络、
loopback 端口、只读根、非 root、资源限制和 tmpfs；K8s 模板声明专用 ServiceAccount、
TLS Secret、seccomp、入站允许名单与全部出站拒绝。生产启动要求 TLS 成对非空且具备
Linux 资源限制。实际集群控制仍须平台验收。

~~~bash
cd agent
docker build -f Dockerfile.analysis-sandbox -t news-agent/data-analysis:local .
docker build -f Dockerfile.analysis-service -t news-agent/analysis-service:local .
~~~

基础镜像使用固定 digest，各 Dockerfile 专用 .dockerignore 仅放行必需分析代码。
服务启动/关闭与 TLS/网络/Secret 要求见服务文档，不重启当前业务栈。

## 配置与使用

app/config.py → app/main.py → factory.create_analysis_runner 装配。Remote Port 在
shutdown 时关闭。功能默认关闭；本地 native Compose 显式开启 process。密钥与 .env
内容不会传给固定 worker。

| 配置 | 默认值 |
| --- | --- |
| CONVERSATION_DATA_ANALYSIS_ENABLED | false |
| DATA_ANALYSIS_BACKEND | process；可选 docker/service |
| DATA_ANALYSIS_TIMEOUT_SECONDS | 3，范围 0.1–8 秒；service 建议 6 秒覆盖网络与服务预算 |
| DATA_ANALYSIS_MAX_ROWS | 200，上限 1000，两个 dataset 合计 |
| DATA_ANALYSIS_MAX_INPUT_BYTES / DATA_ANALYSIS_MAX_OUTPUT_BYTES | 各 262144，上限各 1048576 |
| DATA_ANALYSIS_MAX_CONCURRENCY | 2 |
| DATA_ANALYSIS_MEMORY_MB | 128 |
| DATA_ANALYSIS_DOCKER_IMAGE | news-agent/data-analysis:local |
| DATA_ANALYSIS_SERVICE_URL | 无默认，固定 HTTPS /v1/analyze |
| DATA_ANALYSIS_SERVICE_TOKEN | 无默认，秘密系统独立注入 |
| DATA_ANALYSIS_SERVICE_ALLOW_INSECURE_HTTP | false，仅显式本地测试可 true |
| DATA_ANALYSIS_SERVICE_REQUIRE_OS_LIMITS | true，生产不能关闭 |

API 环境示例见 [analysis-client.example.yml](../deploy/analysis-client.example.yml)；
ANALYSIS_SERVICE_* 配额、TLS 与 Secret 合同见[服务文档](analysis-service.md)。会话还受
CONVERSATION_MAX_TOOL_CALLS / CONVERSATION_TURN_TIMEOUT_SECONDS 总预算限制。

Prompt 新增并绑定 native-conversation-v4 / compatible-conversation-v4，保留 v2/v3；
未修改企业批准 Prompt。兼容模型的密钥配置使用环境变量名，不在 YAML 保存值。前端由
鉴权 runtime 获取启用状态并显示示例。多轮：“查看最近热点” → “比较当前热点与基线”；
趋势：“统计最近两个窗口的点击率趋势”。本地模型只验证控制流与契约，不代表企业模型
质量；需足够的已完成可比运行。

## 企业聚合合同与验收

~~~bash
cd agent
python tools/check_analysis_data_contract.py --config deploy/analysis-data-acceptance.example.yml
~~~

新增管理员显式 CLI：AcceptanceRequest → run_aggregate_acceptance →
NewsMetricSource.fetch_snapshots，一次有界调用，复核聚合身份、窗口、类型、计数与 CTR，
输出计数、固定检查状态和证据哈希，不输出新闻 ID/指标值/原始明细。默认合成来源无网络；
明确管理员 factory 才接入批准 Adapter，不自动发现地址或加载 .env。详见
[聚合 Port 合同验收准备](analysis-data-acceptance.md)。响应没有租户字段，报告明确将
真实租户隔离标为 not_verified，不能把参数传递或本地样本通过称为企业验收。

## 测试与剩余 TODO

~~~bash
cd agent
python -m pytest -q
python -m pytest tests/test_data_analysis_comparison.py \
  tests/test_conversation_analysis_comparison.py tests/test_data_analysis_output_integrity.py \
  tests/test_data_analysis_service.py tests/test_data_analysis_remote.py \
  tests/test_analysis_data_acceptance.py -q
NEWSAGENT_ANALYSIS_DOCKER_E2E=1 python -m pytest \
  tests/test_data_analysis_runner.py -k real_docker_analysis_contract -q
# 服务真实容器测试按服务文档显式启动独立实例并注入测试 token。
cd frontend
npm test
npm run lint
npm run build
~~~

覆盖计算、精确小数、零分母/缺失基线、共同新闻范围、来源与差值篡改拒绝、同租户/同会话
授权、完整榜单、严格输入、有界 IO/并发/超时、取消与 shutdown 清理、远程失败关闭与
管理员 CLI。实际容器和企业验收是不同证据，测试结果在交付说明中记录。

2026-10-05 实际验证：后端完整回归 1717 passed / 57 skipped；前端 155 passed，lint、
类型检查与生产构建通过。显式直接 Docker 六操作 6 passed；独立服务经本机固定网关的
真实 HTTP 验收 16 passed；真实本地 PostgreSQL 会话验收 6 passed。后端默认跳过包括
上述需要显式环境的用例，不将 skip 算作通过。独立服务镜像实际构建；inspect 与固定
容器探针确认非 root、只读根、分析服务 external egress 不可达及资源/挂载/端口限制。
CLI 合成合同报告为 local_contract_passed，真实租户隔离仍为 not_verified。以上不代表
企业模型、企业数仓或生产长稳验收；专用测试容器结束后清理，镜像与模板保留。

外部条件 TODO：获准企业聚合 Adapter、IDL/水位/UV/基线口径、独立二租户授权 fixture、
企业模型规划质量、生产 Secret/TLS/CNI、容量/长稳/节点故障验收。生产发布与模型/
Prompt/规则晋升仍受既有人工审批。本轮提供可审查实现与模板，不自动发布企业环境。
会话逐工具终态检查点和审计保留约束见[检查点与保留策略](conversation-tool-checkpoints.md)；自由 Python、图表文件和上传数据
需另建有界接口，当前不暴露这些能力。
