"""热点上游 Text2SQL 取数的稳定输入输出契约。

采用"模板优先 + 模型兜底"：参数化模板负责可复现的周期取数；只有当请求超
出模板声明的能力时，才让大模型基于只读 Schema 生成候选 SQL。候选 SQL 必须
先通过 ``app.analytics.sql_guard`` 的确定性护栏，才允许送入数仓执行。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Text2SqlDialect = Literal["postgres", "mysql", "hive", "spark", "trino"]


class Text2SqlColumn(BaseModel):
    """白名单视图中的一列；只向模型暴露名称、类型和业务含义。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=128)
    data_type: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


class Text2SqlTable(BaseModel):
    """可被查询的只读聚合视图；``name`` 使用 schema.table 全限定名。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=500)
    columns: tuple[Text2SqlColumn, ...] = Field(min_length=1)


class Text2SqlSchema(BaseModel):
    """发送给 SQL 生成模型的只读白名单，不包含任何真实数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dialect: Text2SqlDialect = "postgres"
    tables: tuple[Text2SqlTable, ...] = Field(min_length=1)
    tenant_column: str = Field(default="tenant_id", min_length=1, max_length=128)
    window_column: str = Field(default="event_time", min_length=1, max_length=128)
    max_rows: int = Field(default=1000, ge=1, le=100000)

    def table_names(self) -> frozenset[str]:
        return frozenset(table.name for table in self.tables)

    def column_names(self) -> frozenset[str]:
        names: set[str] = set()
        for table in self.tables:
            names.update(column.name for column in table.columns)
        return frozenset(names)

    def render_ddl(self) -> str:
        """渲染成紧凑的 DDL 文本，作为模型提示的一部分。"""

        lines: list[str] = []
        for table in self.tables:
            lines.append(f"-- {table.description}" if table.description else "")
            lines.append(f"CREATE TABLE {table.name} (")
            for column in table.columns:
                suffix = f"  -- {column.description}" if column.description else ""
                lines.append(f"  {column.name} {column.data_type},{suffix}")
            lines.append(");")
        return "\n".join(line for line in lines if line)


class Text2SqlGenerationInput(BaseModel):
    """一次 SQL 生成请求；只包含结构和规范，不包含企业真实数据。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    dialect: Text2SqlDialect
    schema_ddl: str = Field(min_length=1, max_length=20000)
    metric_columns: tuple[str, ...] = Field(min_length=1, max_length=32)
    content_types: tuple[str, ...] = Field(default=(), max_length=8)
    ranking_limit: int = Field(ge=1, le=100000)
    tenant_column: str = Field(min_length=1, max_length=128)
    window_column: str = Field(min_length=1, max_length=128)
    required_placeholders: tuple[str, ...] = Field(min_length=1, max_length=16)
    row_limit_placeholder: str = Field(min_length=1, max_length=128)


class Text2SqlPlan(BaseModel):
    """模型输出的候选 SQL；执行前仍需通过确定性护栏。"""

    model_config = ConfigDict(extra="forbid")

    sql: str = Field(min_length=1, max_length=20000)
    explanation: str = Field(default="", max_length=2000)
    referenced_tables: list[str] = Field(default_factory=list, max_length=16)
