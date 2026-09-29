# NewsAgent 独立 Compose

`docker-compose.standalone.yml` 为 NewsAgent 单独创建 PostgreSQL、Redis、
Temporal 和 MinIO。它使用独立的 Compose 项目名与数据卷，不连接 `dev_fastgpt` 网络，
也不构建或导入 `FastGPT/` 源码。FastGPT 仍作为外部 HTTP 服务提供知识检索与模型应用。
独立镜像通过 `Dockerfile.standalone` 补齐 SQLAlchemy 异步迁移需要的 `greenlet`。
此配置面向本地联调；MinIO 使用本地凭据与 `ARTIFACT_SSE_ALGORITHM=none`，
生产部署需单独配置密钥管理、网络和审计。

## 启动

从 `writing-agent-service` 目录运行：

```bash
cp deploy/standalone.env.example deploy/.env.standalone
# 编辑 deploy/.env.standalone 中的占位值，不要提交这个文件。
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml up -d --build
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml ps
```

默认 API 为 `http://127.0.0.1:28000`，Temporal UI 为
`http://127.0.0.1:28233`，MinIO 控制台为 `http://127.0.0.1:29001`。
只有 API、Temporal、数据库、Redis、MinIO、写作 Worker、Outbox Worker 与本地
Mock CMS 默认启动。FastGPT 必须另行运行，并在 `FASTGPT_BASE_URL` 配置容器可访问的地址。
若 FastGPT 在宿主机运行，可用示例中的 `host.docker.internal`；远端部署则填写其 HTTPS 地址。

热点 Worker 要求 `FASTGPT_HOT_NEWS_APP_ID`、真实 `HOT_NEWS_DEPENDENCIES_FACTORY`
和受支持的 `HOT_NEWS_RUNTIME_MANIFEST_JSON`。数据闭环 Worker 也要求非空运行清单。
准备好这些配置后，再启用对应 profile：

```bash
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml --profile hot-news --profile data-loop up -d
```

`examples.hot_news_scenario_factory:build_from_scenario` 只能用于固定窗口的本地联调，
不能用于周期调度或企业数据。生产环境必须提供企业行为、基线和内容 Adapter。
前端继续按 `frontend/README.md` 使用本地 Vite 代理或企业网关；静态 nginx 镜像
不会代替身份网关。

## 边界与验证

本 Compose 的 `minio-init` 在 API/Worker 启动前创建并开启 Artifact Bucket 版本控制。
业务链路保持为：行为数据与新闻内容 Adapter → 确定性指标/热度/证据构造 →
`HotNewsAnalysisAgentRunner` → `FastGPTClient.run_structured` → 报告业务校验 →
PostgreSQL/MinIO 持久化 → API/SSE。只改变 Redis、MinIO 和网络的部署归属。

```bash
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml config --quiet
curl -fsS http://127.0.0.1:28000/ready
```

这套栈使用新数据卷，不会自动迁移旧 Compose/旧 FastGPT 栈中的 PostgreSQL、Redis
或 MinIO 数据。切换真实流量前，需要单独做数据迁移与回读验证。

## FastGPT 上游源码精简边界

NewsAgent 服务通过 HTTP 使用 FastGPT，独立 Compose 不依赖 `FastGPT/` 的源码或网络。
仓库中原先由 Git 跟踪的 FastGPT 上游源码已删除；原有本地 FastGPT 源码开发方式
因此不再可用。未纳入 Git 的本地配置和构建产物已移至 `FastGPT-local-backup/`，
并由根目录 `.gitignore` 忽略；此目录不是可运行的 FastGPT 源码。如需恢复上游源码，
可从删除前的 Git 历史检出。

运行真实热点链路时，需另行部署 FastGPT，并恢复同一知识库、App ID、Prompt 与
模型版本；在同一输入下比较检索证据、结构化报告和错误路径，才能确认结果一致。
旧 Compose 仍引用 `dev_fastgpt` 网络，仅用于兼容已有外部 FastGPT 部署；
新的独立 Compose 不依赖该网络。
