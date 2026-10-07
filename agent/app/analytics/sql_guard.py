"""定义可配置的 SQL 护栏，检查只读查询、表和字段白名单、租户、时间边界、参数及行数。

护栏只做结构和权限校验，不负责执行，也不理解业务口径。真正的最后一道防线
是企业数仓的只读账号；本模块让模板 SQL 和模型生成的候选 SQL 走同一套规则，
避免大模型直接接触任意表、任意函数或写操作。
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.tokens import TokenType

from app.domain.errors import Text2SqlGuardError

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
_DEFAULT_ALLOWED_FUNCTIONS = frozenset(
    {"sum", "count", "avg", "min", "max", "coalesce", "nullif", "round",
     "abs", "greatest", "least", "cast"}
)
_ALLOWED_AST_TYPES = frozenset(
    {
        exp.Select, exp.From, exp.Table, exp.Identifier, exp.Column,
        exp.Literal, exp.Placeholder, exp.Alias, exp.Paren, exp.Where,
        exp.Group, exp.Having, exp.Order, exp.Ordered, exp.Limit, exp.Distinct,
        exp.And, exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
        exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.Pow, exp.Neg,
        exp.Null, exp.Boolean, exp.DataType, exp.DataTypeParam,
    }
)
_SAFE_CAST_TYPES = frozenset(
    {"TINYINT", "SMALLINT", "INT", "BIGINT", "DECIMAL", "FLOAT", "DOUBLE",
     "BOOLEAN", "CHAR", "VARCHAR", "TEXT", "DATE", "TIME", "TIMESTAMP",
     "TIMESTAMPTZ"}
)


def reject_quoted_function_calls(sql: str, dialect: str = "postgres") -> None:
    """Reject function identifiers whose quotes SQLGlot drops during parsing.

    PostgreSQL distinguishes ``\"SUM\"`` from the built-in ``SUM``. An AST-only
    function check cannot make that distinction after SQLGlot normalizes both
    to ``exp.Sum``. Token types preserve it without treating string contents as
    executable syntax.
    """
    try:
        tokens = sqlglot.tokenize(sql, read=dialect)
    except (SqlglotError, RecursionError) as exc:
        raise Text2SqlGuardError("generated SQL does not tokenize") from exc
    for current, following in zip(tokens, tokens[1:]):
        if current.token_type == TokenType.IDENTIFIER and following.token_type == TokenType.L_PAREN:
            raise Text2SqlGuardError("SQL quoted function calls are not allowed")


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
    allowed_functions: frozenset[str] = field(
        default_factory=lambda: _DEFAULT_ALLOWED_FUNCTIONS
    )
    max_window: timedelta = timedelta(days=7)
    content_type_column: str = "content_type"


class SqlGuard:
    """校验 SQL 是否为单条、只读、白名单内且带必要租户/窗口条件。"""

    def __init__(self, policy: SqlGuardPolicy) -> None:
        if isinstance(policy.max_rows, bool) or not isinstance(policy.max_rows, int) or policy.max_rows <= 0:
            raise ValueError("max_rows must be a positive integer")
        if not policy.allowed_tables:
            raise ValueError("allowed_tables cannot be empty")
        if policy.max_window <= timedelta(0):
            raise ValueError("max_window must be positive")
        self.policy = policy

    def validate(self, sql: str, params: Mapping[str, Any] | None = None) -> str:
        policy = self.policy
        stripped = sql.strip() if isinstance(sql, str) else ""
        if not stripped:
            raise Text2SqlGuardError("generated SQL is empty")
        if len(stripped) > 20_000:
            raise Text2SqlGuardError("generated SQL exceeds the size limit")
        if ";" in stripped:
            raise Text2SqlGuardError("generated SQL must contain exactly one statement")
        if any(token in stripped for token in ("--", "/*", "*/")):
            raise Text2SqlGuardError("generated SQL must not contain comments")
        reject_quoted_function_calls(stripped, policy.dialect)

        try:
            statements = sqlglot.parse(stripped, read=policy.dialect)
        except (SqlglotError, RecursionError) as exc:
            raise Text2SqlGuardError(f"generated SQL does not parse: {exc}") from exc

        if len(statements) != 1:
            raise Text2SqlGuardError("generated SQL must contain exactly one statement")
        statement = statements[0]
        if not isinstance(statement, exp.Select):
            raise Text2SqlGuardError("generated SQL must be a single SELECT statement")

        self._validate_no_star(statement)
        self._validate_tables(statement)
        self._validate_columns(statement)
        placeholders = self._validate_placeholders(statement)
        self._validate_shape(statement)
        self._validate_scope_predicates(statement, placeholders)
        self._validate_limit(statement)
        if params is not None:
            self._validate_bindings(placeholders, params)

        return stripped

    def _validate_tables(self, statement: exp.Expression) -> None:
        tables = list(statement.find_all(exp.Table))
        for table in tables:
            name = self._qualified_name(table)
            if name not in self.policy.allowed_tables:
                raise Text2SqlGuardError(f"SQL references a non-whitelisted table: {name!r}")
        if len(tables) != 1:
            raise Text2SqlGuardError("SQL must read exactly one approved view")
        table = tables[0]
        if any(value for key, value in table.args.items() if key not in {"this", "db", "catalog"}):
            raise Text2SqlGuardError("SQL table aliases and view modifiers are not allowed")
        from_clause = statement.args.get("from_") or statement.args.get("from")
        if from_clause is None or from_clause.this is not table or from_clause.expressions:
            raise Text2SqlGuardError("SQL must read the approved view directly")

    def _validate_no_star(self, statement: exp.Select) -> None:
        if statement.find(exp.Star) is not None:
            raise Text2SqlGuardError("SQL must not select *")

    def _validate_columns(self, statement: exp.Expression) -> None:
        allowed = self.policy.allowed_columns
        for column in statement.find_all(exp.Column):
            if column.table or column.db or column.catalog:
                raise Text2SqlGuardError("SQL column qualifiers are not allowed")
            if allowed is not None and column.name not in allowed:
                raise Text2SqlGuardError(
                    f"SQL references a non-whitelisted column: {column.name!r}"
                )

    def _validate_shape(self, statement: exp.Expression) -> None:
        allowed_clauses = {"expressions", "from", "from_", "where", "group", "having", "order", "limit", "distinct"}
        for key, value in statement.args.items():
            if value and key not in allowed_clauses:
                raise Text2SqlGuardError(f"SQL clause is not supported: {key}")
        if sum(1 for _ in statement.find_all(exp.Select)) != 1:
            raise Text2SqlGuardError("SQL subqueries are not allowed")
        nodes = list(statement.walk())
        if len(nodes) > 1000:
            raise Text2SqlGuardError("SQL exceeds the AST complexity limit")
        for node in statement.walk():
            if type(node) in _ALLOWED_AST_TYPES:
                pass
            elif isinstance(node, exp.Func):
                name = node.name if isinstance(node, exp.Anonymous) else node.sql_name()
                name = name.lower()
                if isinstance(node, exp.Anonymous) or name in self.policy.forbidden_functions or name not in self.policy.allowed_functions:
                    raise Text2SqlGuardError(f"SQL calls a forbidden function: {name!r}")
            else:
                raise Text2SqlGuardError(
                    f"SQL contains an unsupported AST expression: {type(node).__name__}"
                )
            if isinstance(node, exp.DataType):
                cast_type = node.this.name if hasattr(node.this, "name") else str(node.this)
                if cast_type not in _SAFE_CAST_TYPES:
                    raise Text2SqlGuardError("SQL cast type is not approved")

    def _validate_placeholders(self, statement: exp.Expression) -> set[str]:
        found = {placeholder.name for placeholder in statement.find_all(exp.Placeholder)}
        unknown = found - self.policy.allowed_placeholders
        if unknown:
            raise Text2SqlGuardError(f"SQL uses unknown parameters: {sorted(unknown)}")
        missing = self.policy.required_placeholders - found
        if missing:
            raise Text2SqlGuardError(
                f"SQL is missing required parameters: {sorted(missing)}"
            )
        return found

    def _validate_scope_predicates(self, statement: exp.Select, placeholders: set[str]) -> None:
        """Scope must be three exact comparisons in the outer AND chain."""

        where = statement.args.get("where")
        if where is None:
            raise Text2SqlGuardError("SQL must include tenant and time predicates")

        expected = {
            "tenant_id": (exp.EQ, self.policy.tenant_column),
            "window_start": (exp.GTE, self.policy.window_column),
            "window_end": (exp.LT, self.policy.window_column),
        }
        if "content_type" in placeholders:
            expected["content_type"] = (exp.EQ, self.policy.content_type_column)
        matched: set[str] = set()

        def conjuncts(node: exp.Expression):
            pending = [node]
            while pending:
                current = pending.pop()
                while isinstance(current, exp.Paren):
                    current = current.this
                if isinstance(current, exp.And):
                    pending.extend((current.expression, current.this))
                else:
                    yield current

        for predicate in conjuncts(where.this):
            left, right = predicate.this, predicate.args.get("expression")
            if not isinstance(left, exp.Column) or not isinstance(right, exp.Placeholder):
                continue
            required = expected.get(right.name)
            if required is not None and type(predicate) is required[0] and left.name == required[1]:
                if right.name in matched:
                    raise Text2SqlGuardError("SQL scope predicates must not be duplicated")
                matched.add(right.name)

        if "tenant_id" not in matched:
            raise Text2SqlGuardError(
                f"SQL must bind {self.policy.tenant_column} in a WHERE predicate"
            )
        if not {"window_start", "window_end"} <= matched:
            raise Text2SqlGuardError(
                f"SQL must bind {self.policy.window_column} to both exact window predicates"
            )
        if "content_type" in expected and "content_type" not in matched:
            raise Text2SqlGuardError("SQL must bind content_type in an exact WHERE predicate")

    def _validate_limit(self, statement: exp.Select) -> None:
        limit = statement.args.get("limit")
        if limit is None:
            raise Text2SqlGuardError("SQL must include a LIMIT clause")
        expression = limit.expression
        if isinstance(expression, exp.Placeholder):
            if expression.name == "row_limit":
                return
            raise Text2SqlGuardError("SQL LIMIT must use the row_limit parameter")
        if isinstance(expression, exp.Literal) and expression.is_int:
            if not 1 <= int(expression.this) <= self.policy.max_rows:
                raise Text2SqlGuardError(
                    f"SQL LIMIT must be between 1 and max_rows={self.policy.max_rows}"
                )
            return
        raise Text2SqlGuardError("SQL LIMIT must be an integer literal or a parameter")

    def _validate_bindings(self, placeholders: set[str], params: Mapping[str, Any]) -> None:
        if set(params) != placeholders:
            raise Text2SqlGuardError("SQL parameters must exactly match AST placeholders")
        tenant = params.get("tenant_id")
        if not isinstance(tenant, str) or re.fullmatch(r"[A-Za-z0-9_.:@-]{1,256}", tenant) is None:
            raise Text2SqlGuardError("SQL tenant binding must be a nonempty trusted identifier")
        if "row_limit" in placeholders:
            row_limit = params["row_limit"]
            if isinstance(row_limit, bool) or not isinstance(row_limit, int) or not 1 <= row_limit <= self.policy.max_rows:
                raise Text2SqlGuardError(f"SQL row_limit must be between 1 and {self.policy.max_rows}")
        if "content_type" in placeholders:
            content_type = params["content_type"]
            if not isinstance(content_type, str) or content_type not in {"article", "video"}:
                raise Text2SqlGuardError("SQL content_type binding is not whitelisted")

        def timestamp(name: str) -> datetime:
            value = params.get(name)
            try:
                if isinstance(value, str):
                    value = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("not an aware timestamp")
            except (ValueError, TypeError, OverflowError) as exc:
                raise Text2SqlGuardError("SQL time bindings must be timezone-aware timestamps") from exc
            return value

        start, end = timestamp("window_start"), timestamp("window_end")
        if not timedelta(0) < end - start <= self.policy.max_window:
            raise Text2SqlGuardError("SQL time window must be positive and within the configured bound")

    @staticmethod
    def _qualified_name(table: exp.Table) -> str:
        parts = [part for part in (table.catalog, table.db, table.name) if part]
        return ".".join(parts) if parts else table.name
