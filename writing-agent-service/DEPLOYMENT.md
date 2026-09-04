# Writing Agent 企业部署基线

当前 Compose 用于单机集成验收；生产环境应将相同容器部署到 Kubernetes 或企业容器平台。

## 服务与探针

- `GET /health`：仅检查 API 进程存活，用作 liveness。
- `GET /ready`：检查 PostgreSQL、Redis、Temporal，用作 readiness。
- `GET /api/v1/jobs/{job_id}/research-metrics`：返回后端确定性计算的 RAG 质量指标。
- `GET /metrics`：Prometheus 文本格式的 Job 状态与 Outbox 可靠性指标。
- `POST /api/v1/jobs/{job_id}/publish`：将已批准终稿发送到固定 CMS 网关。
- API、Temporal Worker、Outbox Worker 必须使用同一个不可变镜像 digest。

## 安全

- 禁止在生产 `.env` 保存密码；使用 Vault、云 Secret Manager 或 Kubernetes Secret 注入。
- Artifact 存储必须启用 `AES256` 或 `aws:kms`；当前 WSL 的 `none` 只允许集成测试。
- API 身份 Header 必须由可信 API Gateway 注入，外部请求不得直接伪造租户和用户 ID。
- 容器默认只读根文件系统、丢弃 Linux capabilities，并启用 `no-new-privileges`。

## 高可用与恢复

- PostgreSQL 使用托管高可用实例，并开启 PITR；至少每日验证一次恢复演练。
- Temporal 使用官方生产拓扑，不使用 `auto-setup`；部署多个 Frontend、History、Matching、Worker 副本。
- API、Temporal Worker、Outbox Worker 至少各 2 个副本；Worker 共享同一 task queue。
- Redis 开启持久化或使用托管集群；Outbox 的事实源仍是 PostgreSQL，可在 Redis 丢失后重放。
- S3/MinIO 开启版本控制、生命周期和跨区域复制；Artifact 通过 SHA-256 校验完整性。

## 监控与告警

至少采集以下指标：

- Job 各状态数量、失败率、等待人工时长、端到端 P50/P95/P99。
- Activity 重试次数、FastGPT 延迟/429/5xx、Temporal task queue backlog。
- RAG `fact_count`、`unique_source_count`、`numeric_fact_count`、引用覆盖率和证据缺口数。
- Outbox pending/dead-letter 数量和最老事件年龄。
- PostgreSQL 连接池、慢查询、磁盘；Redis 内存；S3 请求错误率。

建议告警：连续 5 分钟 readiness 失败、任务失败率超过 5%、Outbox 最老事件超过 60 秒、Temporal backlog 持续增长、引用覆盖率低于 100%。

## CMS 发布网关

发布功能默认关闭。生产环境通过 Secret/Config 注入：

- `CMS_PUBLISH_URL`：企业 CMS 接收终稿的固定 HTTPS 地址。
- `CMS_PUBLISH_TOKEN`：Bearer Token，仅通过 Secret Manager 注入。
- `CMS_PUBLISH_TIMEOUT_SECONDS`：请求超时，默认 30 秒。

网关响应必须包含字符串字段 `publication_id`（兼容 `id`）。请求包含
`Idempotency-Key` 和 `X-Tenant-ID`，CMS 必须按幂等键去重。服务只允许
`final_approved → published`，未配置网关时返回 503 且不会修改任务。

## 发布流程

1. 运行全量单元测试和数据库迁移 dry-run。
2. 构建一次镜像并记录 digest，所有服务引用相同 digest。
3. 先运行 migration Job，再滚动 API/Worker；保留上一镜像用于回滚。
4. 创建一条合成 RAG 任务，验证 Research → Writer → Reviewer → Finalize。
5. 核对 `/research-metrics`、SSE、Temporal History、Artifact hash 和最终 Job 状态。
6. 在预发布 CMS 使用合成稿验证幂等发布，再放开生产发布权限。
