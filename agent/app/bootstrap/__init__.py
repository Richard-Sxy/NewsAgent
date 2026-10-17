"""启动装配：配置加载、依赖注入与资源生命周期。

- worker：写作 Worker 的运行时装配（WorkerRuntime）。
- hot_news / hot_news_dependencies：热点 Worker 与依赖工厂装配。
- api_lifespan：FastAPI 业务 lifespan（数据库、Temporal、Redis、模型、知识索引、
  受众/画像、会话与 Data Loop 服务的构建与关闭顺序）。
"""

from app.bootstrap.worker import WorkerRuntime, create_worker_runtime

__all__ = ["WorkerRuntime", "create_worker_runtime"]
