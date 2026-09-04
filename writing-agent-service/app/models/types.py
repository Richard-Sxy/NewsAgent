from enum import Enum as PythonEnum

from sqlalchemy import Enum as SqlEnum


def postgres_enum(enum_type: type[PythonEnum], name: str) -> SqlEnum:
    """用枚举值而非成员名创建稳定的 PostgreSQL 原生枚举。"""
    return SqlEnum(
        enum_type,
        name=name,
        native_enum=True,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )
