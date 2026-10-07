# 独立固定操作数据分析服务

该服务把新闻聚合计算从 NewsAgent API 进程移到专用执行进程和容器。API 只调用固定
HTTPS Port；服务只接收版本化聚合 JSON，运行随镜像交付的 `worker.py`，不接收 Python、
SQL、脚本、文件路径或模块名。服务不加载业务 Settings、数据库、模型、Embedding 或企业
用户事件，不持有 Docker socket。此代码和部署模板已经实现，企业集群、真实数据和生产
容量尚待验收；不能把本地验证当作生产部署完成。

## 完整调用链

```text
/chat / 会话 API
→ ConversationAgentService 的获准分析工具
→ ConversationTools 从同租户已完成热点 run 读取快照
→ project_dataset 数值白名单、版本和窗口转换
→ RemoteAnalysisRunner.run：validate_request、总行数和输入字节限制
→ 固定 /v1/analyze HTTPS 请求（独立 Bearer token，无代理、无重试）
→ 开发 Compose 的 loopback Nginx 固定路由网关（生产 K8s 直接 TLS Service）
→ create_app：常时鉴权、立即并发准入、有界分块读取、严格 JSON
→ validate_request：拒绝未知字段，规范化版本化聚合请求
→ AnalysisRunner.run
→ 新建临时目录，最小环境，python -I -S 固定 worker.py 的 stdin
→ worker Linux CPU / AS / NOFILE / FSIZE / CORE 限制
→ engine.analyze 标准库确定性计算
→ AnalysisRunner._validate_output + execution 请求 SHA-256 和限额
→ 服务复核结果并限制输出字节
→ RemoteAnalysisRunner 再次校验结果、来源、请求 SHA-256、执行限额
→ execution 保留 process 后端并加 transport=service
→ 既有 Python 答复渲染、PostgreSQL ToolTrace、会话持久化与 SSE
```

镜像仅复制 `app/__init__.py` 和 `app/data_analysis` 下的 `__init__.py`、`engine.py`、
`runner.py`、`worker.py`、`service.py`。构建上下文白名单在
`Dockerfile.analysis-service.dockerignore`；业务代码、`.env`、模型配置、数据库配置和
新闻正文均不会进入镜像。运行依赖是锁定版本的 FastAPI、Uvicorn、Pydantic Settings
及其传递依赖；固定 worker 仍只用 Python 标准库。

## 服务配置

`AnalysisServiceSettings` 只读取 `ANALYSIS_SERVICE_*` 进程环境，明确 `env_file=None`；
错误信息隐藏配置原始值。token 使用 `SecretStr`，不会出现在默认配置表示或接口描述中。

| 环境变量 | 默认值及约束 |
| --- | --- |
| `ANALYSIS_SERVICE_TOKEN` | 必须通过秘密系统注入；16–4096 个 ASCII 可见字符，无空白 |
| `ANALYSIS_SERVICE_ENVIRONMENT` | `development`；生产设为 `production` |
| `ANALYSIS_SERVICE_REQUIRE_LINUX_LIMITS` | `true`；生产必须为 true 且运行于 Linux，否则启动拒绝 |
| `ANALYSIS_SERVICE_TIMEOUT_SECONDS` | 3；固定 worker wall budget，范围 (0, 60] |
| `ANALYSIS_SERVICE_REQUEST_TIMEOUT_SECONDS` | 5；收包、校验、执行和输出整体预算，必须不小于 worker 预算 |
| `ANALYSIS_SERVICE_MAX_ROWS` | 200；所有 dataset/reference_dataset 合计，范围 1–1000 |
| `ANALYSIS_SERVICE_MAX_INPUT_BYTES` | 262144；原始流和规范化 JSON 均限制，上限 1048576 |
| `ANALYSIS_SERVICE_MAX_OUTPUT_BYTES` | 262144；完整报告含 execution，上限 1048576 |
| `ANALYSIS_SERVICE_MAX_CONCURRENCY` | 2；范围 1–32，包含收包中的请求，无排队 |
| `ANALYSIS_SERVICE_MEMORY_MB` | 128；每个 Linux worker AS 限制，范围 32–4096 |
| `ANALYSIS_SERVICE_TLS_CERTFILE` / `ANALYSIS_SERVICE_TLS_KEYFILE` | 开发默认不配置；生产必须配置成对的非空路径，否则启动拒绝；生产模板从证书 Secret 只读挂载 |

启动入口是 `python -m app.data_analysis.service`，固定监听 `0.0.0.0:8100`，仅一个
Uvicorn worker，关闭 access log、代理头信任、Server header 和交互式 API 文档。
生产服务自身拒绝未配置 TLS 的明文启动，不仅依赖 API 客户端或部署模板。
`GET /health` 也必须鉴权。token 与业务模型密钥独立；worker 的环境仍由现有 Runner
固定为 `{"LANG":"C.UTF-8"}`，不会继承服务 token、TLS key 路径或业务进程环境。

`production` 的 Linux 校验只能证明 OS 具备 worker 资源限制，容器与网络策略仍必须由
部署端实际执行；Python 进程本身不是运行任意不可信 Python 的安全沙箱。

## API 侧 Port

业务 API 的 `create_analysis_runner(settings)` 根据 `DATA_ANALYSIS_BACKEND=service`
装配 Remote Port；`main.py` 在 shutdown 时关闭该 Port。启用需设
`CONVERSATION_DATA_ANALYSIS_ENABLED=true`、`DATA_ANALYSIS_SERVICE_URL` 和独立秘密
`DATA_ANALYSIS_SERVICE_TOKEN`，沿用 `DATA_ANALYSIS_TIMEOUT_SECONDS/MAX_ROWS/MAX_INPUT_BYTES/
MAX_OUTPUT_BYTES/MAX_CONCURRENCY/MEMORY_MB`。两个策略变量为
`DATA_ANALYSIS_SERVICE_ALLOW_INSECURE_HTTP=false` 与 `DATA_ANALYSIS_SERVICE_REQUIRE_OS_LIMITS=true`。
生产启用时缺 URL/token、允许 HTTP 或关闭 OS 限制均由业务 Settings 拒绝。

装配 `RemoteAnalysisRunner` 时，URL 和 token 由服务端配置提供，模型和用户请求无法改变。
URL 默认要求 HTTPS，路径必须严格为 `/v1/analyze`，拒绝 userinfo、query、fragment、
反斜杠和重定向。正常 HTTP 客户端固定 `trust_env=False`、`follow_redirects=False`，
因此不会从进程代理环境隐式转发 token，也不会因 307/308 重发分析请求。

```python
RemoteAnalysisRunner(
    url=approved_url,
    token=injected_token,
    timeout_seconds=5,
    max_rows=200,
    max_input_bytes=262144,
    max_output_bytes=262144,
    max_concurrency=2,
    memory_mb=128,
    allow_insecure_http=False,
    require_os_limits=True,
)
```

`description()` 只返回 `execution_backend=service` 和资源限额，不返回 URL、token 或路径。
`run()` 规范化输入，并把远程回包当作不可信输出重新经过现有 `_validate_output`。
服务声明的执行限额必须有效、不得超过客户端批准的限额，且能容纳此次请求；默认拒绝
未声明 OS 限制的结果。完整实际回包的字节数也必须不超过服务声明的输出限额，不能
以错误的限额声明形成审计记录。审计中的 `execution.backend` 保留真实 `process` 或 `docker`，
另加 `execution.transport=service`。应用关闭时调用 `await close()`，关闭自己创建的
HTTP 客户端；测试注入的外部 client 由调用者负责关闭。

本地开发可显式 `allow_insecure_http=True`。本机 macOS 进程模式还需显式
`require_os_limits=False`；这仅用于开发验证，生产装配应保持默认 true。
内部 CA 必须安装到镜像的标准信任库，不能以关闭 TLS 证书验证绕过验收。

## 失败与取消

- 鉴权失败发生在收包和启动 worker 之前；Authorization 必须恰有一个精确 Bearer 值，
  使用 `hmac.compare_digest` 比较。缺少、错误、重复和格式不符返回 401。
- 并发槽在鉴权后、收包前立即占用，饱和返回 429；服务和远程 Port 均没有无限等待队列。
- JSON 重复 key、非有限常量、未知字段、原始明细或任意程序参数均拒绝。chunked body
  不依赖 Content-Length，也在每次分块时做字节限制。
- 服务总预算覆盖收包和 worker。超时返回 504；HTTP 断线、任务取消和 lifespan shutdown
  取消分析任务，并等待 Runner kill/reap worker、删除临时目录，释放槽位。
  超时先触发取消，等待清理可能增加返回耗时；5 秒处理预算不保证响应和清理都在 5 秒内完成。
- 远程 Port 使用总 deadline 与有界 response stream，只接受未压缩 JSON；拒绝压缩
  回包避免在字节限制之前发生解压扩张。取消关闭 HTTP response。
- 非 200、网络异常、错误来源/哈希、无效 execution、超量和 malformed 输出均失败关闭。
  所有公开异常只含固定 `error_code` 和 `retryable=False`；错误 body 不读取、不回显。
  不重试，不回退本地进程，不复用旧报告冒充新结果。

## 本地独立容器

从 `agent/` 构建，不需要业务栈：

```sh
docker build -f Dockerfile.analysis-service -t news-agent/analysis-service:local .
```

由秘密管理器或当前终端的隐藏输入预先注入 `ANALYSIS_SERVICE_TOKEN`，然后启动独立
Compose 项目。`--env-file /dev/null` 确保 Compose 也不隐式读取项目 `.env`：

```sh
docker compose --env-file /dev/null -f deploy/docker-compose.analysis-service.yml up -d --no-build
```

分析服务只连接 `analysis-private` internal bridge，自身不发布主机端口。Docker 的
internal-only bridge 不提供主机端口转发，因此专用 `analysis-gateway` 连接开发 ingress
bridge 与 internal bridge，仅由它发布 loopback `127.0.0.1:28100`。分析服务没有加入
可外网路由的网络，其固定 worker 仍处于私有执行容器内。

网关锁定官方 `nginx:1.28.0-alpine` manifest digest，仅把精确 `GET /health` 与
`POST /v1/analyze` 转发给 `analysis-service:8100`；拒绝其他路径、方法和 query，不含
token、数据库或模型配置，不提供任意代理地址。使用 Docker 内部 DNS 的动态 resolve，
后端重建地址改变时仍能重新解析。[Nginx 官方 upstream 文档](https://nginx.org/en/docs/http/ngx_http_upstream_module.html)
说明开源动态解析由 1.27.3 起支持。

网关 256KiB body 上限、6 秒 header/body/读写空闲预算、1 秒连接预算，并关闭重试、
access log 与缓冲。HTTP/1.1 + `proxy_request_buffering off` 将 chunked 和普通 body
立即交给后端，使服务的 5 秒绝对请求预算也覆盖收包。Nginx 的 read/send 超时是读写间
空闲预算，不能声称是整请求 deadline；绝对分析预算由后端与 Remote Port 执行。
[Nginx 官方 proxy 文档](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)
解释了不缓冲转发和上述超时语义。过大的 body 可能先在网关返回 413；进入分析服务的
请求仍先鉴权再启动 worker，网关不会持有或判断 token。

分析服务采用只读根、非 root、drop ALL capabilities、no-new-privileges、32 PID、384MiB
内存、1 CPU、8MiB 临时内存目录与关闭容器日志。网关同样只读/non-root/cap-drop，限制
16 PID、64MiB、0.25 CPU、64 FD 和 8MiB 临时目录；仅只读挂载固定 Nginx 配置。两个
容器均无 Docker socket、业务目录或秘密文件挂载；分析服务没有宿主挂载，网关仅绑定
上述固定配置文件。
这份 Compose 是独立开发服务，不连接、重启或清空当前业务栈及其数据卷；API 若运行在
另一个容器中，需由部署者选择获准的专用网络与固定服务地址，不能使用主机 loopback
作为另一个容器的服务发现地址。

可在同一终端做鉴权健康验证，不打印 token：

```sh
python - <<'PY'
import os
import httpx

with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
    result = client.get(
        "http://127.0.0.1:28100/health",
        headers={"Authorization": "Bearer " + os.environ["ANALYSIS_SERVICE_TOKEN"]},
    )
    print("health_status", result.status_code)
PY
```

结束该独立实例：

```sh
docker compose --env-file /dev/null -f deploy/docker-compose.analysis-service.yml down
```

## Kubernetes 模板

`deploy/k8s/analysis-service.yml` 提供独立 ServiceAccount、Deployment、ClusterIP
Service 和 NetworkPolicy。它不创建明文 Secret；需要部署端先注入
`newsagent-analysis-auth/token` 与 `newsagent-analysis-tls/tls.crt,tls.key`。
证书必须覆盖所选命名空间的服务 DNS，客户端仍执行 TLS 验证。

仅同命名空间中 `app=newsagent-api` Pod 可以访问 8100；服务所有 egress 默认拒绝。
ServiceAccount 不挂载 API token；容器非 root、只读根、RuntimeDefault seccomp、无
capability、禁止权限提升。CPU/内存 requests 与 limits、临时目录配额已声明。健康探针
用 TCP，因为 `/health` 必须鉴权。服务不需要 Kubernetes API 或 DNS 出站权限。

应用模板前必须选择命名空间、替换为批准的 registry `image@sha256` 并调整 pull policy，
确认 CNI 实际支持 NetworkPolicy、集群 PID 配额、证书信任和秘密轮换。当前没有自动
应用 Kubernetes 清单，也没有把示例 local tag 声称为生产不可变镜像。

## 测试入口与待验收项

```sh
python -m pytest tests/test_data_analysis_service.py tests/test_data_analysis_remote.py
```

独立硬化 Compose 已由部署者启动后，可用公开合成数据运行真实 Docker HTTP 验收。
向测试进程单独注入 `NEWSAGENT_ANALYSIS_SERVICE_E2E_TOKEN`，其值与服务的开发测试 token
相同；不打印 token，也不读取 `.env`。默认测试 URL 为 loopback
`http://127.0.0.1:28100/v1/analyze`，可由 `NEWSAGENT_ANALYSIS_SERVICE_E2E_URL` 显式覆盖。

```sh
NEWSAGENT_ANALYSIS_SERVICE_DOCKER_E2E=1 python -m pytest tests/test_data_analysis_service_docker.py
```

真实容器测试覆盖六个分析操作、每个报告的规范化请求 SHA-256 和 Linux 限额声明、
401、413 分块与声明长度边界、已鉴权健康检查、网关路径/方法/query 拒绝，以及未完成
body 在后端总 deadline 内返回 504。部署模板安全测试另外验证 worker internal-only、
仅网关发布 loopback、镜像 digest、权限与资源限制、固定代理目标和没有重试。

```sh
python -m pytest tests/test_analysis_service_deployment.py
```

测试覆盖真实固定 worker 的四个 v1 操作及两个 v2 比较操作，鉴权先于 worker、chunked 输入上限、严格 JSON、
立即并发拒绝、收包总超时、真实 OS worker 在断线/取消/超时/关闭时 kill/reap、错误回包
失败关闭；远程覆盖规范化请求、SHA-256 与限额绑定、OS 约束、输出上限、拒绝压缩、
总超时、并发、关闭、错误 body 隐藏和没有重试。v2 趋势/历史基线沿用同一 Port、引擎
与结果 Validator，所有窗口的行数共同计入限额，并验证完整 Remote Port → ASGI 服务 →
真实 worker 链路及 reference 快照哈希篡改拒绝。

尚需外部条件完成的 TODO：

- 企业实际聚合来源、窗口 watermark、去重 UV 和基线口径合同验收；只使用脱敏聚合
  Fixture，不能采集本地企业原始行为明细。
- 真实企业 Gateway/IdP 与 token Secret/TLS CA 联调；TLS 轮换、服务 token 轮换流程和
  发布 owner 由生产平台确认。
- 实际 K8s/CNI 网络阻断、只读根、seccomp、PID/CPU/内存压力、长稳、关闭与节点故障
  演练；测试模板存在不等于上述控制已在企业环境生效。
- 容量预算、可观测性、429/504 告警与审计保留；不应为收集日志添加正文、原始用户事件、
  URL 或 token。
- 任何生产镜像、权限与发布变化继续经过既有人工审批，不由聊天 Agent 自动部署。
