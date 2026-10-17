"""ORM 模型聚合：导入包即完成全部模型注册（alembic/建表测试依赖）。"""
from app.modules.data_loop.persistence import analysis_feedback  # noqa: F401
from app.modules.data_loop.persistence import evaluation_dataset  # noqa: F401
from app.modules.data_loop.persistence import production_bundle  # noqa: F401
