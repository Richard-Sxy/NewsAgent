# syntax=docker/dockerfile:1
# 复用本次发布的 API 镜像，避免 API/Worker 的代码和依赖版本漂移。
# 必须先构建 API_IMAGE，再从仓库根目录构建本文件。
ARG API_IMAGE=newsagent-api:build
FROM ${API_IMAGE} AS runtime

# 热点、Outbox、Data Loop 在 K8s 中仅覆盖 command，使用相同 Worker 镜像。
CMD ["python", "-m", "app.worker"]
