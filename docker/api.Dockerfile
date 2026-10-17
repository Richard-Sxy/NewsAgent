
# syntax=docker/dockerfile:1
# 从仓库根目录构建；企业镜像源可通过 PYTHON_IMAGE 覆盖。
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

ARG APP_EXTRAS=""
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app app

COPY agent/pyproject.toml ./
COPY agent/app ./app
COPY agent/alembic ./alembic
COPY agent/alembic.ini ./
# APP_EXTRAS 留空使用 PostgreSQL 检索；接 Milvus 时传入 [milvus]。
RUN pip install --upgrade pip && pip install ".${APP_EXTRAS}"

USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
