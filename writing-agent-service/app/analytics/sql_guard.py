"""在 SQL 进入数仓前执行的确定性只读白名单护栏。

护栏只做结构和权限校验，不负责执行，也不理解业务口径。真正的最后一道防线
是企业数仓的只读账号；本模块让模板 SQL 和模型生成的候选 SQL 走同一套规则，
避免大模型直接接触任意表、任意函数或写操作。
"""

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.domain.errors import Text2SqlGuardError

_PLACEHOLDER_PATTERN = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_FORBIDDEN_EXPRESSIONS = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.Merge,
    exp.Command,
)
_DEFAULT_FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "lo_import",
        "lo_export",
        "dblink",
        "dblink_exec",
        "xp_cmdshell",
        "load_file",
        "benchmark",
        "sleep",
    }
)


@dataclass(frozen=True, slots=True)
class SqlGuardPolicy:
    """一次取数会话允许的只读边界。"""

    allowed_tables: frozenset[str]
    allowed_columns: frozenset[str] | None
    tenant_column: str
    window_column: str
    required_placeholders: frozenset[str]
    allowed_placeholders: frozenset[str]
    max_rows: int
    dialect: str = "postgres"
    forbidden_functions: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_FORBIDDEN_FUNCTIONS
    )


class SqlGuard:
    """校验 SQL 是否为单条、只读、白名单内且带必要租户/窗口条件。"""

    def __init__(self, policy: SqlGuardPolicy) -> None:
        if policy.max_rows <= 0:
            raise ValueError("max_rows must be positive")
        if not policy.allowed_tables:
            raise ValueError("allowed_tables cannot be empty")
        self.policy = policy

    def validate(self, sql: str) -> str:
        policy = self.policy
        stripped = sql.strip() if isinstance(sql, str) else ""
        if not stripped:
            raise Text2SqlGuardError("generated SQL is empty")
        if ";" in stripped:
            raise Text2SqlGuardError("generated SQL must contain exactly one statement")
        if "--" in stripped or "/*" in stripped:
            raise Text2SqlGuardError("generated SQL must not contain comments")

        try:
            statements = sqlglot.parse(stripped, read=policy.dialect)
        except SqlglotError as exc:
            raise Text2SqlGuardError(f"generated SQL does not parse: {exc}") from exc

        if len(statements) != 1:
            raise Text2SqlGuardError("generated SQL must contain exactly one statement")
        statement = statements[0]
        if not isinstance(statement, exp.Select):
            raise Text2SqlGuardError("generated SQL must be a single SELECT statement")

        self._validate_tables(statement)
        self._validate_no_star(statement)
        self._validate_columns(statement)
        self._validate_forbidden_expressions(statement)
        self._validate_forbidden_functions(stripped)
        self._validate_placeholders(stripped)
        self._validate_limit(statement)

        return stripped

    def _validate_tables(self, statement: exp.Expression) -> None:
        for table in statement.find_all(exp.Table):
            name = self._qualified_name(table)
            if name not in self.policy.allowed_tables:
                raise Text2SqlGuardError(f"SQL references a non-whitelisted table: {name!r}")

    def _validate_no_star(self, statement: exp.Select) -> None:
        if any(isinstance(item, exp.Star) for item in statement.expressions):
            raise Text2SqlGuardError("SQL must not select *")

    def _validate_columns(self, statement: exp.Expression) -> None:
        allowed = self.policy.allowed_columns
        if allowed is None:
            return
        for column in statement.find_all(exp.Column):
            if column.name not in allowed:
                raise Text2SqlGuardError(
                    f"SQL references a non-whitelisted column: {column.name!r}"
                )

    def _validate_forbidden_expressions(self, statement: exp.Expression) -> None:
        for node in statement.walk():
            if isinstance(node, _FORBIDDEN_EXPRESSIONS):
                raise Text2SqlGuardError(
                    f"SQL contains a forbidden statement: {type(node).__name__}"
                )

    def _validate_forbidden_functions(self, sql: str) -> None:
        lowered = sql.lower()
        for function in self.policy.forbidden_functions:
            if re.search(rf"\b{re.escape(function)}\s*\(", lowered):
                raise Text2SqlGuardError(f"SQL calls a forbidden function: {function!r}")

    def _validate_placeholders(self, sql: str) -> None:
        found = set(_PLACEHOLDER_PATTERN.findall(sql))
        unknown = found - self.policy.allowed_placeholders
        if unknown:
            raise Text2SqlGuardError(f"SQL uses unknown parameters: {sorted(unknown)}")
        missing = self.policy.required_placeholders - found
        if missing:
            raise Text2SqlGuardError(
                f"SQL is missing required parameters: {sorted(missing)}"
            )

    def _validate_limit(self, statement: exp.Select) -> None:
        limit = statement.args.get("limit")
        if limit is None:
            raise Text2SqlGuardError("SQL must include a LIMIT clause")
        expression = limit.expression
        if isinstance(expression, exp.Placeholder):
            return
        if isinstance(expression, exp.Literal) and expression.is_int:
            if int(expression.this) > self.policy.max_rows:
                raise Text2SqlGuardError(
                    f"SQL LIMIT exceeds max_rows={self.policy.max_rows}"
                )
            return
        raise Text2SqlGuardError("SQL LIMIT must be an integer literal or a parameter")

    @staticmethod
    def _qualified_name(table: exp.Table) -> str:
        parts = [part for part in (table.catalog, table.db, table.name) if part]
        return ".".join(parts) if parts else table.name
