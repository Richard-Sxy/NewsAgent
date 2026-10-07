"""Text2SQL 只读白名单护栏的确定性单元测试。"""

import pytest
from datetime import datetime, timedelta, timezone

from app.analytics.sql_guard import SqlGuard, SqlGuardPolicy
from app.domain.errors import Text2SqlGuardError


ALLOWED_TABLES = frozenset({"dw.news_behavior_aggregate"})
ALLOWED_COLUMNS = frozenset(
    {
        "news_id",
        "content_type",
        "tenant_id",
        "event_time",
        "impressions",
        "clicks",
        "unique_users",
        "total_duration_seconds",
        "effective_consumptions",
        "interactions",
    }
)
REQUIRED = frozenset({"tenant_id", "window_start", "window_end"})


def guard(*, max_rows: int = 1000) -> SqlGuard:
    return SqlGuard(
        SqlGuardPolicy(
            allowed_tables=ALLOWED_TABLES,
            allowed_columns=ALLOWED_COLUMNS,
            tenant_column="tenant_id",
            window_column="event_time",
            required_placeholders=REQUIRED,
            allowed_placeholders=REQUIRED | {"row_limit", "content_type"},
            max_rows=max_rows,
            dialect="postgres",
        )
    )


VALID_SQL = (
    "SELECT news_id, content_type, impressions, clicks "
    "FROM dw.news_behavior_aggregate "
    "WHERE tenant_id = :tenant_id "
    "AND event_time >= :window_start AND event_time < :window_end "
    "LIMIT :row_limit"
)


def test_accepts_whitelisted_read_only_query() -> None:
    assert guard().validate(VALID_SQL) == VALID_SQL


def test_accepts_numeric_limit_within_bound() -> None:
    sql = VALID_SQL.replace("LIMIT :row_limit", "LIMIT 100")

    assert guard().validate(sql) == sql


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("", "empty"),
        (VALID_SQL + ";", "one statement"),
        ("SELECT news_id FROM dw.news_behavior_aggregate -- comment\n"
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit", "comment"),
        ("SELECT * FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit", "select *"),
        ("SELECT news_id FROM dw.other_table "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit", "non-whitelisted table"),
        ("SELECT secret FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit", "non-whitelisted column"),
        ("DELETE FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id LIMIT :row_limit", "single SELECT"),
        ("SELECT news_id FROM dw.news_behavior_aggregate "
         "WHERE event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit", "missing required parameters"),
        ("SELECT news_id FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id "
         "AND event_time < :window_end LIMIT :row_limit", "missing required parameters"),
        ("SELECT news_id FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit OFFSET :secret", "unknown parameters"),
        ("SELECT news_id FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end", "LIMIT"),
        ("SELECT news_id, :tenant_id AS requested_tenant "
         "FROM dw.news_behavior_aggregate "
         "WHERE event_time >= :window_start AND event_time < :window_end "
         "LIMIT :row_limit", "bind tenant_id in a WHERE predicate"),
        ("SELECT news_id FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT 100000", "max_rows"),
        ("SELECT pg_sleep(5) FROM dw.news_behavior_aggregate "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit", "forbidden function"),
        ("WITH c AS (SELECT news_id FROM dw.news_behavior_aggregate) "
         "SELECT news_id FROM c "
         "WHERE tenant_id = :tenant_id AND event_time >= :window_start "
         "AND event_time < :window_end LIMIT :row_limit", "non-whitelisted"),
    ],
)
def test_rejects_unsafe_or_incomplete_sql(sql: str, message: str) -> None:
    with pytest.raises(Text2SqlGuardError, match=message):
        guard().validate(sql)


def test_guard_policy_rejects_empty_whitelist() -> None:
    with pytest.raises(ValueError, match="allowed_tables"):
        SqlGuard(
            SqlGuardPolicy(
                allowed_tables=frozenset(),
                allowed_columns=None,
                tenant_column="tenant_id",
                window_column="event_time",
                required_placeholders=REQUIRED,
                allowed_placeholders=REQUIRED,
                max_rows=10,
            )
        )


def bindings(**overrides) -> dict:
    start = datetime(2026, 10, 3, tzinfo=timezone.utc)
    params = {"tenant_id": "tenant-1", "window_start": start,
              "window_end": start + timedelta(hours=1), "row_limit": 10}
    params.update(overrides)
    return params


def test_accepts_complete_trusted_bindings_and_safe_aggregate_formula() -> None:
    sql = VALID_SQL.replace(
        "news_id, content_type, impressions, clicks",
        "news_id, COALESCE(ROUND(SUM(clicks) * 1.0 / NULLIF(SUM(impressions), 0), 4), 0) AS clicks",
    ).replace("LIMIT", "GROUP BY news_id LIMIT")
    assert guard().validate(sql, bindings()) == sql


@pytest.mark.parametrize("sql", [
    VALID_SQL.replace("LIMIT", "OR 1 = 1 LIMIT"),
    VALID_SQL.replace("WHERE tenant_id = :tenant_id", "WHERE NOT (tenant_id = :tenant_id)"),
    VALID_SQL.replace("tenant_id = :tenant_id", "tenant_id <> :tenant_id"),
    VALID_SQL.replace("tenant_id = :tenant_id", "tenant_id IN (:tenant_id, 'other-tenant')"),
    VALID_SQL.replace("tenant_id = :tenant_id", "tenant_id = COALESCE(:tenant_id, tenant_id)"),
    VALID_SQL.replace("tenant_id = :tenant_id", "tenant_id = :tenant_id || ''"),
    VALID_SQL.replace("event_time >= :window_start", "event_time <= :window_start"),
    VALID_SQL.replace("event_time < :window_end", "event_time > :window_end"),
    VALID_SQL.replace("event_time < :window_end", ":window_end > :window_start"),
    VALID_SQL.replace("event_time < :window_end", "clicks >= :window_end"),
    VALID_SQL.replace("event_time >= :window_start", "event_time + INTERVAL '1 day' >= :window_start"),
    VALID_SQL.replace("SELECT news_id", "SELECT (SELECT news_id FROM dw.news_behavior_aggregate LIMIT 1) AS news_id"),
    VALID_SQL.replace("FROM dw.news_behavior_aggregate", "FROM dw.news_behavior_aggregate JOIN dw.news_behavior_aggregate AS other ON 1 = 1"),
    "WITH c AS (" + VALID_SQL + ") " + VALID_SQL,
    VALID_SQL.replace("news_id, content_type", "dw.news_behavior_aggregate.*, content_type"),
    VALID_SQL.replace("news_id, content_type", "ROW_NUMBER() OVER (ORDER BY clicks), content_type"),
    VALID_SQL.replace("FROM dw.news_behavior_aggregate", "INTO TEMP copied FROM dw.news_behavior_aggregate"),
    VALID_SQL + " FOR UPDATE",
    VALID_SQL.replace("news_id, content_type", "pg_advisory_lock(1), content_type"),
    VALID_SQL.replace("news_id, content_type", "set_config('application_name', 'injected', false), content_type"),
    VALID_SQL.replace("news_id, content_type", "IF(true, clicks, 0), content_type"),
    VALID_SQL.replace("news_id, content_type", "nextval('sequence'), content_type"),
    VALID_SQL.replace("news_id, content_type", "public.sum(clicks), content_type"),
    VALID_SQL.replace("news_id, content_type", "\"SUM\"(clicks), content_type"),
    VALID_SQL.replace("news_id, content_type", "\"sum\"(clicks), content_type"),
    VALID_SQL.replace("news_id, content_type", "CAST(news_id AS regclass), content_type"),
    VALID_SQL.replace("LIMIT :row_limit", "LIMIT :tenant_id"),
    VALID_SQL.replace("LIMIT :row_limit", "LIMIT 0"),
    VALID_SQL.replace("LIMIT :row_limit", "LIMIT -1"),
    VALID_SQL.replace("LIMIT :row_limit", "LIMIT :row_limit OFFSET 100"),
    VALID_SQL.replace("LIMIT :row_limit", "AND tenant_id = :tenant_id LIMIT :row_limit"),
])
def test_rejects_scope_bypasses_and_select_side_effects(sql: str) -> None:
    with pytest.raises(Text2SqlGuardError):
        guard().validate(sql, bindings())


def test_string_literal_cannot_supply_a_required_ast_placeholder() -> None:
    sql = VALID_SQL.replace("tenant_id = :tenant_id", "tenant_id = ':tenant_id'")
    with pytest.raises(Text2SqlGuardError, match="missing required parameters"):
        guard().validate(sql)


def test_postgres_cast_and_literal_text_do_not_create_phantom_parameters() -> None:
    sql = VALID_SQL.replace(
        "SELECT news_id", "SELECT news_id::text, ':not_a_parameter' AS label, '\"SUM\"(clicks)' AS quoted_text"
    )
    assert guard().validate(sql, bindings()) == sql


@pytest.mark.parametrize("overrides", [
    {"row_limit": 1001}, {"row_limit": 0}, {"row_limit": -1},
    {"row_limit": True}, {"row_limit": "10"}, {"row_limit": None},
    {"tenant_id": ""}, {"tenant_id": None}, {"tenant_id": "tenant-1' OR 1=1"},
    {"window_start": "2026-10-03T00:00:00"},
    {"window_end": datetime(2026, 10, 2, tzinfo=timezone.utc)},
    {"window_end": datetime(2026, 10, 11, tzinfo=timezone.utc)},
    {"unexpected": "value"},
])
def test_rejects_untrusted_or_over_budget_bindings(overrides: dict) -> None:
    with pytest.raises(Text2SqlGuardError):
        guard().validate(VALID_SQL, bindings(**overrides))


def test_rejects_missing_binding_and_accepts_aware_iso_timestamps() -> None:
    params = bindings()
    del params["tenant_id"]
    with pytest.raises(Text2SqlGuardError, match="exactly match"):
        guard().validate(VALID_SQL, params)
    assert guard().validate(VALID_SQL, bindings(
        window_start="2026-10-03T00:00:00Z", window_end="2026-10-03T01:00:00Z",
    )) == VALID_SQL


def test_literal_limit_does_not_require_an_unused_row_limit_binding() -> None:
    sql = VALID_SQL.replace("LIMIT :row_limit", "LIMIT 10")
    params = bindings()
    del params["row_limit"]
    assert guard().validate(sql, params) == sql


@pytest.mark.parametrize("content_type", ["audio", "", None, True, 1])
def test_rejects_unapproved_content_type_binding(content_type) -> None:
    sql = VALID_SQL.replace("LIMIT", "AND content_type = :content_type LIMIT")
    with pytest.raises(Text2SqlGuardError, match="content_type binding"):
        guard().validate(sql, bindings(content_type=content_type))


@pytest.mark.parametrize("sql", [
    VALID_SQL.replace("SELECT news_id", "SELECT :content_type AS news_id"),
    VALID_SQL.replace("LIMIT", "AND content_type <> :content_type LIMIT"),
    VALID_SQL.replace("LIMIT", "AND clicks = :content_type LIMIT"),
    VALID_SQL.replace("LIMIT", "AND content_type = COALESCE(:content_type, content_type) LIMIT"),
])
def test_content_type_placeholder_must_be_an_exact_scope_predicate(sql: str) -> None:
    with pytest.raises(Text2SqlGuardError, match="exact WHERE predicate"):
        guard().validate(sql, bindings(content_type="video"))


def test_accepts_bound_content_type_scope() -> None:
    sql = VALID_SQL.replace("LIMIT", "AND content_type = :content_type LIMIT")
    assert guard().validate(sql, bindings(content_type="video")) == sql
