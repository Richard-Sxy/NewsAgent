"""检查生成的 SQL。用 SQL 语法树检查是否只读、是否查询批准的视图、是否带租户和时间规范，以及字段、排序、参数、行数是否符合限制。"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.analytics.sql_guard import reject_quoted_function_calls
from app.domain.errors import Text2SqlGuardError


_VIEW = "dw.news_behavior_aggregate"
_DIMENSIONS = ("news_id", "title", "content_type", "category", "source")
_MEASURES = ("impressions", "clicks", "effective_consumptions", "interactions")
_DERIVED = {
    "ctr": "COALESCE(ROUND(SUM(clicks) * 1.0 / NULLIF(SUM(impressions), 0), 4), 0)",
    "hot_score": (
        "COALESCE(ROUND((SUM(clicks) * 0.4 + SUM(effective_consumptions) * 0.35 "
        "+ SUM(interactions) * 0.25) / NULLIF(SUM(impressions), 0), 4), 0)"
    ),
}
_OUTPUT_ALIASES = frozenset((*_MEASURES, *_DERIVED))
_PARAMETERS = frozenset(
    {"tenant_id", "window_start", "window_end", "row_limit", "content_type", "category"}
)
_REQUIRED_PARAMETERS = frozenset({"tenant_id", "window_start", "window_end", "row_limit"})
_PREDICATES = {
    "tenant_id": (exp.EQ, "tenant_id"),
    "window_start": (exp.GTE, "event_time"),
    "window_end": (exp.LT, "event_time"),
    "content_type": (exp.EQ, "content_type"),
    "category": (exp.EQ, "category"),
}


def _canonical(expression: exp.Expression) -> str:
    return expression.sql(dialect="postgres", normalize=True)


def _plain_column(expression: exp.Expression) -> str | None:
    if not isinstance(expression, exp.Column):
        return None
    if expression.table or expression.db or expression.catalog:
        return None
    return expression.name


def _unparenthesize(expression: exp.Expression) -> exp.Expression:
    while isinstance(expression, exp.Paren):
        expression = expression.this
    return expression


class SqlAssistantGuard:
    """Validate compiler-owned ranking/trend SQL and its bound parameters."""

    allowed_view = _VIEW
    allowed_output_aliases = _OUTPUT_ALIASES

    def __init__(self, *, max_rows: int = 100) -> None:
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows must be a positive integer")
        self.max_rows = max_rows
        self._expected_projections = {
            name: _canonical(sqlglot.parse_one(f"SELECT SUM({name}) AS {name}", read="postgres").expressions[0])
            for name in _MEASURES
        }
        self._expected_projections.update({
            name: _canonical(sqlglot.parse_one(f"SELECT {expression} AS {name}", read="postgres").expressions[0])
            for name, expression in _DERIVED.items()
        })

    def validate(self, sql: str, params: Mapping[str, Any]) -> str:
        stripped = sql.strip() if isinstance(sql, str) else ""
        if not stripped or len(stripped) > 16_384:
            raise Text2SqlGuardError("SQL must be nonempty and within the size limit")
        if any(token in stripped for token in (";", "--", "/*", "*/")):
            raise Text2SqlGuardError("SQL must contain one statement without comments")
        reject_quoted_function_calls(stripped)
        try:
            statements = sqlglot.parse(stripped, read="postgres")
        except SqlglotError as exc:
            raise Text2SqlGuardError("SQL does not parse as PostgreSQL") from exc
        if len(statements) != 1 or not isinstance(statements[0], exp.Select):
            raise Text2SqlGuardError("SQL must be one plain SELECT")
        statement = statements[0]
        if sum(1 for _ in statement.walk()) > 1000:
            raise Text2SqlGuardError("SQL exceeds the AST complexity limit")
        self._validate_shape(statement)
        dimensions = self._validate_projection(statement)
        self._validate_grouping(statement, dimensions)
        self._validate_ordering(statement, dimensions)
        found = self._validate_scope(statement)
        self._validate_limit(statement)
        found.add("row_limit")
        self._validate_bindings(found, params)
        return stripped

    @staticmethod
    def _validate_shape(statement: exp.Select) -> None:
        allowed_clauses = {"expressions", "from", "from_", "where", "group", "order", "limit"}
        for key, value in statement.args.items():
            if value and key not in allowed_clauses:
                raise Text2SqlGuardError(f"SQL clause is not supported: {key}")
        if sum(1 for _ in statement.find_all(exp.Select)) != 1:
            raise Text2SqlGuardError("SQL subqueries are not allowed")
        tables = list(statement.find_all(exp.Table))
        if len(tables) != 1:
            raise Text2SqlGuardError("SQL must read exactly one approved view")
        table = tables[0]
        if table.catalog or table.db != "dw" or table.name != "news_behavior_aggregate" or table.args.get("alias"):
            raise Text2SqlGuardError("SQL references a non-whitelisted view or table alias")
        if any(value for key, value in table.args.items() if key not in {"this", "db"}):
            raise Text2SqlGuardError("SQL view modifiers are not allowed")
        from_clause = statement.args.get("from_") or statement.args.get("from")
        if from_clause is None or from_clause.this is not table or from_clause.expressions:
            raise Text2SqlGuardError("SQL must read the approved view directly")
        if any(isinstance(node, (exp.Subquery, exp.Join, exp.Union, exp.Intersect, exp.Except, exp.Star, exp.Window)) for node in statement.walk()):
            raise Text2SqlGuardError("SQL joins, sets, stars, windows and subqueries are not allowed")

    def _validate_projection(self, statement: exp.Select) -> tuple[str, ...]:
        dimensions: list[str] = []
        aliases: set[str] = set()
        for item in statement.expressions:
            column = _plain_column(item)
            if column is not None:
                dimensions.append(column)
                continue
            if not isinstance(item, exp.Alias) or item.alias not in self._expected_projections:
                raise Text2SqlGuardError("SQL projection is not an approved aggregate or dimension")
            if item.alias in aliases or _canonical(item) != self._expected_projections[item.alias]:
                raise Text2SqlGuardError("SQL aggregate formula differs from the frozen contract")
            aliases.add(item.alias)
        if tuple(dimensions) not in (_DIMENSIONS, ("event_time",)):
            raise Text2SqlGuardError("SQL dimensions must match ranking or trend contract")
        if aliases != _OUTPUT_ALIASES:
            raise Text2SqlGuardError("SQL must return the complete approved metric schema")
        return tuple(dimensions)

    @staticmethod
    def _validate_grouping(statement: exp.Select, dimensions: tuple[str, ...]) -> None:
        group = statement.args.get("group")
        if group is None or any(value for key, value in group.args.items() if key != "expressions"):
            raise Text2SqlGuardError("SQL requires plain GROUP BY dimensions")
        columns = tuple(_plain_column(item) for item in group.expressions)
        if columns != dimensions:
            raise Text2SqlGuardError("SQL GROUP BY must match selected dimensions")

    @staticmethod
    def _validate_ordering(statement: exp.Select, dimensions: tuple[str, ...]) -> None:
        order = statement.args.get("order")
        if order is None or any(value for key, value in order.args.items() if key != "expressions"):
            raise Text2SqlGuardError("SQL requires deterministic ORDER BY")
        items = order.expressions
        if dimensions == ("event_time",):
            if len(items) != 1 or _plain_column(items[0].this) != "event_time" or items[0].args.get("desc"):
                raise Text2SqlGuardError("Trend SQL must order event_time ascending")
            return
        if len(items) != 2 or _plain_column(items[0].this) not in _OUTPUT_ALIASES:
            raise Text2SqlGuardError("Ranking SQL must order an approved metric")
        if _plain_column(items[1].this) != "news_id" or items[1].args.get("desc"):
            raise Text2SqlGuardError("Ranking SQL requires news_id ascending tie-break")

    @staticmethod
    def _validate_scope(statement: exp.Select) -> set[str]:
        where = statement.args.get("where")
        if where is None:
            raise Text2SqlGuardError("SQL requires tenant and both window predicates")
        predicates: list[exp.Expression] = []

        def collect(node: exp.Expression) -> None:
            pending = [node]
            while pending:
                current = _unparenthesize(pending.pop())
                if isinstance(current, exp.And):
                    pending.extend((current.expression, current.this))
                else:
                    predicates.append(current)

        collect(where.this)
        found: set[str] = set()
        for predicate in predicates:
            parameter = predicate.expression if isinstance(predicate, (exp.EQ, exp.GTE, exp.LT)) else None
            if not isinstance(parameter, exp.Placeholder) or parameter.name not in _PREDICATES:
                raise Text2SqlGuardError("SQL WHERE only allows approved AND-connected scope predicates")
            expected_type, column = _PREDICATES[parameter.name]
            if type(predicate) is not expected_type or _plain_column(predicate.this) != column:
                raise Text2SqlGuardError("SQL scope predicate must bind the exact approved column")
            if parameter.name in found:
                raise Text2SqlGuardError("SQL scope predicates must not be duplicated")
            found.add(parameter.name)
        if not {"tenant_id", "window_start", "window_end"}.issubset(found):
            raise Text2SqlGuardError("SQL requires tenant and both window predicates")
        return found

    @staticmethod
    def _validate_limit(statement: exp.Select) -> None:
        limit = statement.args.get("limit")
        if limit is None or any(value for key, value in limit.args.items() if key != "expression"):
            raise Text2SqlGuardError("SQL requires LIMIT :row_limit without OFFSET")
        if not isinstance(limit.expression, exp.Placeholder) or limit.expression.name != "row_limit":
            raise Text2SqlGuardError("SQL LIMIT must be the approved row_limit binding")

    def _validate_bindings(self, found: set[str], params: Mapping[str, Any]) -> None:
        if not isinstance(params, Mapping) or set(params) != found or not _REQUIRED_PARAMETERS.issubset(found) or not found.issubset(_PARAMETERS):
            raise Text2SqlGuardError("SQL parameters must exactly match approved placeholders")
        tenant_id = params["tenant_id"]
        if not isinstance(tenant_id, str) or not tenant_id.strip() or len(tenant_id) > 128:
            raise Text2SqlGuardError("SQL tenant binding must be a nonempty trusted identifier")
        row_limit = params["row_limit"]
        if isinstance(row_limit, bool) or not isinstance(row_limit, int) or not 1 <= row_limit <= self.max_rows:
            raise Text2SqlGuardError(f"SQL row_limit must be between 1 and {self.max_rows}")
        if "content_type" in found and (
            not isinstance(params["content_type"], str)
            or params["content_type"] not in {"article", "video"}
        ):
            raise Text2SqlGuardError("SQL content_type binding is not whitelisted")
        if "category" in found and (
            not isinstance(params["category"], str)
            or params["category"] not in {"科技", "财经", "体育", "社会"}
        ):
            raise Text2SqlGuardError("SQL category binding is not whitelisted")
        try:
            start = self._datetime(params["window_start"])
            end = self._datetime(params["window_end"])
        except (TypeError, ValueError) as exc:
            raise Text2SqlGuardError("SQL time bindings must be timezone-aware timestamps") from exc
        if not timedelta(0) < end - start <= timedelta(days=7):
            raise Text2SqlGuardError("SQL time window must be positive and at most seven days")
        if any(value.minute or value.second or value.microsecond for value in (start, end)):
            raise Text2SqlGuardError("SQL hourly time bindings must align to whole hours")

    @staticmethod
    def _datetime(value: Any) -> datetime:
        result = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(result, datetime) or result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        return result
