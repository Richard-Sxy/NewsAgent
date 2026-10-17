"""ORM 模型聚合：导入包即完成全部模型注册（alembic/建表测试依赖）。"""
from app.modules.memory.persistence import user_memory  # noqa: F401
