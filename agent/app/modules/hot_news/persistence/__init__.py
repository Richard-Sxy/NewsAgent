"""ORM 模型聚合：导入包即完成全部模型注册（alembic/建表测试依赖）。"""
from app.modules.hot_news.persistence import hot_event  # noqa: F401
from app.modules.hot_news.persistence import hot_news  # noqa: F401
from app.modules.hot_news.persistence import hot_news_decision  # noqa: F401
