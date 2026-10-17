# Mock CMS 发布网关

企业 CMS 暂未提供，本目录是一个自包含的模拟实现，用于打通写作链路末端的
「终稿审批 → CMS 发布 → 回执落库」全链路。

## 契约（与 `app/clients/cms.py::CmsPublisher` 对齐）

| 项 | 值 |
| --- | --- |
| 发布 | `POST /api/v1/publications` |
| 请求头 | `Idempotency-Key`（必需）、`X-Tenant-ID`（必需）、`Authorization: Bearer <token>` |
| 请求体 | `{"job_id": str, "channel": str, "article": object}` |
| 成功响应 | 201 `{"publication_id": str, "status": "published", "replay": false}` |
| 幂等重放 | 同一 `(X-Tenant-ID, Idempotency-Key)` 返回相同 `publication_id`，`replay: true` |
| 幂等键冲突 | 同键不同内容 → 409 |
| 验收查询 | `GET /api/v1/publications` / `GET /api/v1/publications/{id}` |

Bearer token 由环境变量 `MOCK_CMS_TOKEN` 配置，默认 `mock-cms-dev-token`（仅限本地）。

## 本地运行（不写 Docker）

```bash
cd writing-agent-service
/tmp/newsagent-memory-venv/bin/python -m uvicorn \
  --app-dir deploy/mock-cms app:app --host 127.0.0.1 --port 8080
```

然后在 `.env` 中指向它：

```
CMS_PUBLISH_URL=http://127.0.0.1:8080/api/v1/publications
CMS_PUBLISH_TOKEN=mock-cms-dev-token
```

## Docker Compose

`deploy/docker-compose.yml` 已注册 `mock-cms` 服务，`CMS_PUBLISH_URL` /
`CMS_PUBLISH_TOKEN` 的 compose 默认值已指向它；生产环境仍由真实 CMS 网关
地址与 Secret 覆盖。

## 边界

- 仅内存存储，重启即清空；不模拟审核流、撤回、定时发布等高级能力。
- 不得部署到生产；生产 `CMS_PUBLISH_URL` 必须指向企业真实 CMS 网关。
