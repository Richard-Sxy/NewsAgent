"""Compare candidate metric SQL with a server-owned, versioned query contract.

This is deliberately narrower than SQL equivalence checking. Only formatting,
projection order and redundant ``column AS column`` aliases may differ. A new
formula, filter, grouping, ordering or limit needs a new approved contract.
The generic SqlGuard must still validate both statements before this check.
"""

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.domain.errors import Text2SqlGuardError


def _canonical_query(sql: str, *, dialect: str) -> str:
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except (SqlglotError, RecursionError) as exc:
        raise Text2SqlGuardError("metric contract SQL does not parse") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise Text2SqlGuardError("metric contract requires one SELECT")
    statement = statements[0]
    projections: list[exp.Expression] = []
    for item in statement.expressions:
        if (
            isinstance(item, exp.Alias)
            and isinstance(item.this, exp.Column)
            and not item.this.table
            and item.alias == item.this.name
        ):
            item = item.this.copy()
        projections.append(item)
    # Ordering SELECT columns does not change the name-based warehouse mapping.
    # Preserve duplicates: a duplicate output must not disappear into a set/dict.
    statement.set("expressions", sorted(
        projections, key=lambda item: item.sql(dialect=dialect, normalize=True),
    ))
    return statement.sql(dialect=dialect, normalize=True)


def validate_metric_sql_contract(
    candidate_sql: str, *, approved_sql: str, dialect: str,
) -> None:
    """Reject any candidate that changes the approved metric query semantics."""
    if _canonical_query(candidate_sql, dialect=dialect) != _canonical_query(
        approved_sql, dialect=dialect,
    ):
        raise Text2SqlGuardError(
            "SQL differs from the approved metric contract "
            "(formula, columns, scope, grouping, ordering or limit)"
        )
