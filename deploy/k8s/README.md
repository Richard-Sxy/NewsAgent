# NewsAgent / 腾讯云 TKE 基础部署

这套配置部署本项目的 Python API、Vue 前端和 Worker，复用企业已有 PostgreSQL、Redis、
Temporal、S3 兼容存储与模型服务。没有在集群内创建数据库、模拟模型、演示数仓或企业网关。
所有 `example.invalid`、`replace-me` 均需替换；本说明交付的是配置基础，不代表企业联调已通过。

## 1. 文件与部署顺序

| 入口 | 用途 | 默认行为 |
| --- | --- | --- |
| `kustomization.yaml` + `production.yml` | 基础服务、ConfigMap、ServiceAccount、PDB、API 入站策略 | API/前端各 2 副本；写作/Outbox 各 1；热点/Data Loop 各 0 |
| `config/application.env.example` | 实际由 Kustomize 读取的普通配置源 | 不含密码；功能开关与调度默认关闭 |
| `config/model-runtime.yml` | 原生模型 Port 的 URL、模型、Prompt 与版本绑定 | 来自已有企业模板，Prompt 文本未改，仍需企业审批与合同测试 |
| `config/knowledge-search.yml` | 检索后端选择 | PostgreSQL；不附带本地 BGE 权重或样本数据 |
| `config/frontend.json` | 前端启动时加载的运行配置 | 同源、空租户、无浏览器密钥 |
| `secret.example.yml` | 运行密钥字段模板 | 不被 Kustomize 应用 |
| `migration-secret.example.yml` | 独立迁移账号字段模板 | 不被 Kustomize 应用 |
| `migrate/` | 独立 Alembic Job | 发布前显式执行一次，不随 Pod 启动重复运行 |
| `tke/` | 可选 CLB 入口附加清单 | 独立应用，复用已有 CLB，不包含基础应用 |
| `../docker/api.Dockerfile`、`../docker/work.Dockerfile`（现位于 `deploy/docker/`） | API / Worker 镜像 | Worker 从本次 API 镜像继承相同代码与依赖 |

```text
企业提供地址、模型与凭据 → 构建/推送镜像 → 创建命名空间和 Secret
→ 单独迁移并确认成功 → 应用基础服务 → 接好鉴权 Gateway、CLB 与 TLS
→ 应用 TKE 入口 → 健康/业务/SSE 联调 → 按审批结果启用额外模块
```

在仓库根目录执行下文命令。`production.yml` 依赖生成的 ConfigMap，不能单独 `apply -f`。
原 `agent/deploy/k8s/` 是历史前端/独立分析服务清单，本套不会修改它们；不要把两套同时部署
到同一个环境。

## 2. 你需要填写的位置

| 需要填写 | 位置 | 说明 |
| --- | --- | --- |
| TCR 地址、镜像版本或 digest | `kustomization.yaml`；迁移镜像另在 `migrate/kustomization.yaml` | API 与迁移用同一镜像；Worker 由该 API 镜像构建；推荐固定 digest |
| Temporal 地址、namespace | `config/application.env.example` | namespace 由平台预创建；3 个队列与相应 Worker 一致 |
| S3 兼容地址、region、bucket | 同上 | 桶由平台创建，校验版本保留和 SSE 支持；不自动创建或清空桶 |
| 推理、Embedding 完整 URL | `config/model-runtime.yml` | 分别包含 `/v1/chat/completions`、`/v1/embeddings`，本项目不会补路径 |
| 模型名与向量维度 | 同上 | 替换所有 `replace-me-*-model`，同步 `agent_scenes`；1024 仅为模板维度，必须核对真实模型 |
| PostgreSQL/Redis DSN 与密钥 | 企业密钥系统，字段见两个 Secret 模板 | DSN 中用户名/密码做 URL 编码，按企业要求配 TLS/CA |
| CLB ID、域名、TLS Secret | `tke/ingress.yml` | `kubernetes.io/ingress.existLbId` 复用现有 CLB；不是只读回填的 `qcloud-loadbalance-id` |
| Gateway Pod selector、端口 | `tke/ingress.yml` 和 `production.yml` 中的 NetworkPolicy | 默认同命名空间 `app.kubernetes.io/name=newsagent-gateway`，端口 8080 |

`secret.example.yml` 中每个值都是字段示意，不能把占位值作为可用凭据。由平台创建
`newsagent-secrets`、`newsagent-migration-secrets` 和 `newsagent-tls`；真实值不写入受版本控制
的 YAML。运行账号给应用所需 DML 权限，迁移账号单独持有 DDL 权限。采用短期 S3 凭据时
额外注入 `AWS_SESSION_TOKEN`；采用平台身份时，应先验证 boto3 凭据链，再移除静态密钥。

本模板关闭 Pod 的 Kubernetes API Token 自动挂载，未授予 RBAC 权限。若企业工作负载身份
需要专用投影 Token，应按平台要求配置，不能假设关闭自动挂载后仍有默认 Token。

TCR 拉取优先使用集群已配置的免密能力；没有该能力时，对所有 Deployment 及迁移 Job 的
Pod 添加 `imagePullSecrets`，并由平台创建相应 Secret。不要填写不存在的 Secret 名称。

## 3. 构建镜像

```bash
# 修改为企业允许的仓库、发布版本与节点架构；Mac 默认架构可能与 TKE 节点不同。
REGISTRY=registry.example.invalid/newsagent
RELEASE=replace-me-release
PLATFORM=linux/amd64
API_IMAGE="$REGISTRY/api:$RELEASE"
WORKER_IMAGE="$REGISTRY/worker:$RELEASE"
FRONTEND_IMAGE="$REGISTRY/frontend:$RELEASE"

docker build --platform "$PLATFORM" -f docker/api.Dockerfile -t "$API_IMAGE" .
docker build --platform "$PLATFORM" --build-arg API_IMAGE="$API_IMAGE" \
  -f docker/work.Dockerfile -t "$WORKER_IMAGE" .
docker build --platform "$PLATFORM" -f agent/frontend/Dockerfile \
  -t "$FRONTEND_IMAGE" agent/frontend

docker push "$API_IMAGE"
docker push "$WORKER_IMAGE"
docker push "$FRONTEND_IMAGE"
```

把构建结果填回两处 Kustomize 的 `images`。基础镜像/包源可由企业 CI 配置；API 的
`PYTHON_IMAGE` 构建参数支持替换基础镜像。Docker 专用 ignore 文件只发送后端源代码和迁移，
不会发送本地 `.env`、抓取数据、模型权重、演示 examples 或整个仓库。
前端镜像仅复制 Vite 所需的配置解析器，不复制 `dev-server/local.yml` 中的开发身份。

## 4. 离线预检、迁移与发布

```bash
# 仅在本地渲染，不连接集群，不生成真实 Secret。
kubectl kustomize k8s > /tmp/newsagent-base.yaml
kubectl kustomize k8s/migrate > /tmp/newsagent-migrate.yaml
kubectl kustomize k8s/tke > /tmp/newsagent-tke.yaml

# 查漏：这些占位项只应在填写前出现。
rg -n 'example\.invalid|replace-me' k8s/config k8s/kustomization.yaml \
  k8s/migrate/kustomization.yaml k8s/tke/ingress.yml
```

集群操作应在你确认 kube-context、企业参数、Secret 和发布审批后执行。本次配置编写没有
连接或修改 TKE，也没有执行数据库迁移。

```bash
kubectl config current-context
kubectl apply -f k8s/namespace.yml
# 由企业密钥系统在该命名空间创建两个应用 Secret，确认 TCR 拉取配置就绪。

kubectl apply --dry-run=server -k k8s/migrate
kubectl apply -k k8s/migrate
kubectl -n news-agent wait --for=condition=complete job/newsagent-migrate --timeout=600s
# 迁移失败则停止发布；通过企业受控日志平台定位，不把含凭据的异常贴到公开渠道。

kubectl apply --dry-run=server -k k8s
kubectl diff -k k8s
kubectl apply -k k8s
kubectl -n news-agent rollout status deployment/newsagent-api --timeout=600s
kubectl -n news-agent rollout status deployment/newsagent-writing-worker --timeout=600s
kubectl -n news-agent rollout status deployment/newsagent-outbox-worker --timeout=600s
kubectl -n news-agent rollout status deployment/newsagent-frontend --timeout=600s
```

`kubectl diff` 返回 1 表示存在差异，不等于执行失败。迁移 Job 固定名称、失败不自动重试，
完成记录保留 24 小时。后续发布若旧 Job 仍存在，先确认它已结束、保留迁移日志，再删除
`job/newsagent-migrate` 并重新应用；不要删除正在执行的迁移。每次只允许一条迁移发布流水线。

迁移后再更新应用是人工/CI 顺序，Kubernetes 不会自动替你等待独立 Job。升级前应完成数据库
备份及兼容性评估；镜像回滚不能撤销数据库变更，不自动执行 Alembic downgrade。

## 5. TKE 入口与可信身份

```text
浏览器 HTTPS → 现有 CLB
  /       → newsagent-frontend-ingress NodePort → Nginx:8080 → Vue
  /api    → newsagent-gateway NodePort → 企业鉴权 Gateway:8080
          → newsagent-api ClusterIP:8000 → FastAPI
```

企业 Gateway 必须先验证用户，再剥离客户端传入的身份/权限头，注入可信
`X-Tenant-ID`、`X-User-ID` 及对应模块的 `X-Hot-News-Roles`、`X-Data-Loop-Roles`、
`X-Writing-Roles`、`X-Memory-Roles`；需要作用域时注入 `X-Team-ID`、`X-Section-ID`、
`X-Role-ID`。受保护业务接口由网关注入 `Authorization: Bearer <DATA_LOOP_GATEWAY_TOKEN>`。
知识入库使用独立 `KNOWLEDGE_INGEST_TOKEN`，不能向浏览器暴露任一共享 Token。

网关转发保留 `/api/...` 原路径，不能统一剥离 `/api`。前端 `apiBaseUrl=""` 走同源，
浏览器配置中没有用户/租户伪造身份；Nginx 本身对 `/api/` 返回 404。

这里仅创建指向企业 Gateway Pod 的 Service，没有伪装成鉴权网关的示例 Deployment。
Gateway 尚未接入时 `/api` 无 Endpoint，访问不会进入 API。若现有 Gateway 位于其他
命名空间/集群，应由平台调整路由和网络策略；不能把 gateway Service selector 改成 API。

基础 API 是 ClusterIP，入站 NetworkPolicy 只放行同命名空间的 Gateway Pod。必须确认
TKE 网络插件实际执行 NetworkPolicy；节点/Pod 安全组、NodePort 来源范围由平台配置。
本模板未猜测企业出站网段，依赖访问白名单和 DNS 规则由平台侧治理。

先确认 CLB 复用、Gateway 标签/端口、TLS Secret 和 DNS，再执行入口发布：

```bash
kubectl apply --dry-run=server -k k8s/tke
kubectl apply -k k8s/tke
kubectl -n news-agent get ingress,service,endpointslices
```

TKE 附加清单采用 NodePort 后端、`direct-access=false`。如平台要求 VPC-CNI CLB-Pod 直连，
需按该集群规范调整。HTTPS/强制跳转、CLB 空闲超时、Gateway SSE 禁缓冲及读超时由入口
团队配置并验证，不混入 Nginx Ingress 专用 annotation。SSE 至少覆盖实际 150 秒前端预算，
验证心跳、断线重连与滚动发布行为。

参考：[TKE Ingress Annotation](https://intl.cloud.tencent.com/document/product/457/40675?lang=en)、
[TKE Ingress 基础特性](https://intl.cloud.tencent.com/document/product/457/30673?lang=en)、
[Kustomize 配置生成](https://kubernetes.io/docs/tasks/manage-kubernetes-objects/kustomization/)。

## 6. 完整调用链与检查入口

| 链路 | 上游 → 数据转换/核心函数 → 外部依赖 → 下游 |
| --- | --- |
| 配置加载 | Kustomize `configMapGenerator` → 内容哈希与引用改写 → Pod `envFrom`/只读挂载 → `Settings`、`load_model_runtime_config`、`build_model_runtime_ports` |
| 前端 | 构建时 `npm run build`（类型检查+Vite）→ Nginx 静态资源；运行时 `loadRuntimeConfig()` 读取 `config.json` → 同源 `/api` |
| API | Gateway → `app.main:app` / `lifespan` → `Database`、Temporal `Client.connect`、Redis、`S3ArtifactStore`、原生模型 Port → PostgreSQL 状态、S3 产物、JSON/SSE |
| 写作执行 | API `OrchestratorService` → Temporal `NewsWritingWorkflow` → `app.worker.run_worker` / `create_worker_runtime` → `NewsStepHandler`、结构化模型、Schema/Validator、人工 Gate → checkpoint/报告 |
| 事件推送 | PostgreSQL Outbox → `app.outbox_worker.run` / `OutboxRelay` → Redis → API SSE → 浏览器 |
| 知识链路 | 经鉴权入库 API → `PostgresKnowledgeStore` 确定性切片 → 企业 Embedding → PostgreSQL 台账/索引 → 检索证据 |
| 迁移 | 独立 Job → `alembic upgrade head` / `alembic/env.py` → 专用 PostgreSQL 账号 → 业务 Schema；不会触发模型/Worker |
| 热点（待启用） | 企业行为/基线/正文 Adapter 工厂 → `app.hot_news_worker.main` → Python 计算、证据与模型解释/校验 → 数据库/S3/热点 API |
| Data Loop（待启用） | 反馈与已批准完整 Bundle → `create_data_loop_worker_runtime` → 回放/确定性门禁 → 候选与审批；不自动批准、激活或发布 |

配置项位置见第 2 节；三个队列、API 和各 Worker 的模型配置必须一致。默认不创建
Temporal Schedule。确需周期热点时，在 Adapter、Bundle 与租户激活均验收后填写
`HOT_NEWS_SCHEDULE_DEFINITIONS_JSON`，由受控单次运维 Job 执行
`python -m app.hot_news_scheduler ensure`，不能通过定时重启 Worker 代替调度。

部署契约测试（有项目开发依赖和 kubectl 的 Python 3.11+ 环境）：

```bash
cd agent
python -m pytest tests/test_k8s_deployment.py -q
# 已有相关测试可按变更范围选择，不需要连接企业：
python -m pytest tests/test_health.py tests/test_model_runtime_config_and_http.py -q
```

API `/health` 只检查进程，`/ready` 检查 PostgreSQL/Redis/Temporal；两者均不证明模型、S3、
企业数据或向量服务可用。内部排障可 `kubectl -n news-agent port-forward service/newsagent-api
8000:8000` 后检查这两个地址；正式业务访问仍经企业网关。
Worker 没有 HTTP 探针，不伪装成 API 探针；Deployment Ready 仅表示容器运行，实际消费
还需检查 Temporal poller/队列积压、活动执行结果、Outbox 延迟及失败重试。`/metrics`
由可信网关或明确授权的监控入口采集，按平台流程追加监控 NetworkPolicy。

ConfigMap 内容哈希会触发相关 Pod 更新；Secret 由外部管理，更新后应滚动重启消费它的
Deployment。API/前端 PDB 用于自愿驱逐；它不替代容量与故障演练。资源 requests/limits
是初始值，须压测调整；当前每个后端进程数据库池最多 40 连接，扩容前核对数据库总额度。

## 7. 尚未完成的企业 TODO

- 填企业 URL、真实模型路由/维度、TCR digest、DSN、桶与网关/CLB/TLS；完成真实接口
  的鉴权、超时/限流、JSON Schema、错误码、Embedding 维度和 S3 读写验收。
- 企业热点 SDK、依赖工厂和完整可执行 Bundle 尚需接入并打包；两类 Worker 维持 0 副本。
  完成后在受版本控制的清单中修改副本数，启用热点前端入口并执行业务验收。不要接入
  `examples.*` 工厂，也不要把本地演示冒充企业验收。
- 会话、共享知识读取、异步工具及数据分析需逐项批准后开启；生产数据分析应使用独立
  TLS service 和强制 OS 限额，不能打开本地 process 模式。CMS URL 未填写，发布仍关闭。
- 如 Temporal 企业端要求 TLS/mTLS/API Key，当前 `Client.connect` 仅配置 address/namespace，
  需要补真实 SDK 鉴权接入，不能通过虚构环境变量声称已支持。
- 接企业 Milvus 时切换知识配置并以 `APP_EXTRAS='[milvus]'` 构建；集合/维度/Embedding 版本、
  写入同步、删除/更正/重建及恢复语义需单独验收。基础 PostgreSQL 检索仍需规模压测。
- 企业 CI 镜像构建/扫描、包版本锁定、服务端 Schema 预检、NetworkPolicy 执行验证、Gateway
  身份伪造拒绝、SSE、迁移备份恢复、监控告警、负载与长稳演练尚未在真实 TKE 执行。
