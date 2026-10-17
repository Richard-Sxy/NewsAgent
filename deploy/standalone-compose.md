# NewsAgent 独立 Compose

`docker-compose.standalone.yml` 启动 NewsAgent 自有的 PostgreSQL、Redis、Temporal、
MinIO、API 与 Worker。Agent 和知识库运行时均为本项目 Python 实现，外部只访问 YAML 中
配置的企业推理与 Embedding 原始接口。

## 启动

从 `writing-agent-service` 目录执行：

```bash
cp deploy/standalone.env.example deploy/.env.standalone
cp deploy/model-runtime.enterprise.example.yml deploy/model-runtime.enterprise.yml
# 编辑两个私有配置文件中的占位值，不要提交它们。
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml up -d --build
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml ps
```

默认 API 为 `http://127.0.0.1:28000`，Temporal UI 为
`http://127.0.0.1:28233`，MinIO 控制台为 `http://127.0.0.1:29001`。
热点与 Data Loop 需要企业数据 Adapter、运行清单和调度定义，准备好后启用 profile：

```bash
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml \
  --profile hot-news --profile data-loop up -d
```

## 链路与边界

```text
企业行为/内容 Adapter
  → Python 指标、热度和证据构造
  → Python Agent Runner
  → YAML 选择 Prompt 与企业模型路由
  → Schema/业务校验
  → PostgreSQL、向量索引与 MinIO
  → API/SSE/人工审批
```

`minio-init` 会创建 Artifact Bucket 并开启版本控制，`migrate` 会在 API/Worker 启动前
执行全部 Alembic 迁移。本 Compose 使用独立数据卷；切换已有环境前需备份并验证数据库
升级与回读。

```bash
docker compose --env-file deploy/.env.standalone \
  -f deploy/docker-compose.standalone.yml config --quiet
curl -fsS http://127.0.0.1:28000/ready
```

生产部署还必须替换本地凭据、限制网络、接入企业密钥系统和审计，并完成真实模型接口的
合同测试。
