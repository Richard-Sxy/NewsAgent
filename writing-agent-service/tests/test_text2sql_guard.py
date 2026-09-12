"""Text2SQL 只读白名单护栏的确定性单元测试。"""

import pytest

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
