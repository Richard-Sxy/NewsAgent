"""ORM 模型聚合：导入包即完成全部模型注册（alembic/建表测试依赖）。"""
from app.modules.writing.persistence import agent_run  # noqa: F401
from app.modules.writing.persistence import artifact  # noqa: F401
from app.modules.writing.persistence import job  # noqa: F401
from app.modules.writing.persistence import outbox  # noqa: F401
from app.modules.writing.persistence import step  # noqa: F401
