"""Contract and adversarial checks for the local SQL assistant execution gate."""

from datetime import datetime, timezone

import pytest

from app.domain.errors import Text2SqlGuardError
from app.sql_assistant.guard import SqlAssistantGuard


_METRICS = """SUM(impressions) AS impressions,
SUM(clicks) AS clicks,
SUM(effective_consumptions) AS effective_consumptions,
SUM(interactions) AS interactions,
COALESCE(ROUND(SUM(clicks) * 1.0 / NULLIF(SUM(impressions), 0), 4), 0) AS ctr,
COALESCE(ROUND((SUM(clicks) * 0.4 + SUM(effective_consumptions) * 0.35 + SUM(interactions) * 0.25) / NULLIF(SUM(impressions), 0), 4), 0) AS hot_score"""
_SCOPE = "tenant_id = :tenant_id AND event_time >= :window_start AND event_time < :window_end"
_DIMENSIONS = "news_id, title, content_type, category, source"
RANKING_SQL = (
    f"SELECT {_DIMENSIONS}, {_METRICS} FROM dw.news_behavior_aggregate "
    f"WHERE {_SCOPE} GROUP BY {_DIMENSIONS} ORDER BY ctr DESC, news_id ASC LIMIT :row_limit"
)
TREND_SQL = (
    f"SELECT event_time, {_METRICS} FROM dw.news_behavior_aggregate "
    f"WHERE {_SCOPE} GROUP BY event_time ORDER BY event_time ASC LIMIT :row_limit"
)


@pytest.fixture
def params() -> dict:
    return {
        "tenant_id": "demo-tenant",
        "window_start": datetime(2026, 10, 3, tzinfo=timezone.utc),
        "window_end": datetime(2026, 10, 4, tzinfo=timezone.utc),
        "row_limit": 10,
    }


@pytest.mark.parametrize("sql", [RANKING_SQL, TREND_SQL, RANKING_SQL.replace("ctr DESC", "clicks ASC")])
def test_accepts_complete_approved_query_shapes(sql: str, params: dict) -> None:
    assert SqlAssistantGuard().validate(sql, params) == sql


def test_accepts_parameterized_content_and_category_filters(params: dict) -> None:
    sql = RANKING_SQL.replace(_SCOPE, _SCOPE + " AND content_type = :content_type AND category = :category")
    params.update(content_type="video", category="科技")
    assert SqlAssistantGuard().validate(sql, params) == sql


def test_accepts_safe_parentheses_in_scope_and_iso_time_bindings(params: dict) -> None:
    sql = RANKING_SQL.replace(_SCOPE, f"(({_SCOPE}))")
    params["window_start"] = params["window_start"].isoformat()
    params["window_end"] = params["window_end"].isoformat()
    assert SqlAssistantGuard().validate(sql, params) == sql


@pytest.mark.parametrize(
    "sql",
    [
        "",
        RANKING_SQL + ";",
        RANKING_SQL + " -- harmless",
        RANKING_SQL.replace("SELECT ", "SELECT /* bypass */ ", 1),
        RANKING_SQL.replace(_SCOPE, f"({_SCOPE}) OR 1 = 1"),
        RANKING_SQL.replace("tenant_id = :tenant_id", "tenant_id = :tenant_id OR tenant_id <> :tenant_id"),
        RANKING_SQL.replace(_SCOPE, f"NOT NOT ({_SCOPE})"),
        RANKING_SQL + " UNION " + RANKING_SQL,
        RANKING_SQL.replace("dw.news_behavior_aggregate", "dw.private_user_events"),
        RANKING_SQL.replace("dw.news_behavior_aggregate", "other.news_behavior_aggregate"),
        RANKING_SQL.replace("FROM dw.news_behavior_aggregate", "FROM dw.news_behavior_aggregate AS a"),
        RANKING_SQL.replace("FROM dw.news_behavior_aggregate", "FROM ONLY dw.news_behavior_aggregate"),
        RANKING_SQL.replace("FROM dw.news_behavior_aggregate", "FROM dw.news_behavior_aggregate, dw.news_behavior_aggregate"),
        RANKING_SQL.replace("FROM dw.news_behavior_aggregate", "FROM dw.news_behavior_aggregate JOIN dw.news_behavior_aggregate b ON true"),
        RANKING_SQL.replace("SUM(clicks) AS clicks", "(SELECT SUM(clicks) FROM dw.news_behavior_aggregate) AS clicks"),
        RANKING_SQL.replace("SUM(clicks) AS clicks", "(SELECT SUM(a.clicks) FROM dw.news_behavior_aggregate a WHERE a.tenant_id = tenant_id) AS clicks"),
        "WITH approved AS (" + RANKING_SQL + ") SELECT * FROM approved",
        RANKING_SQL.replace("SUM(clicks) AS clicks", "pg_read_file('/etc/passwd') AS clicks"),
        RANKING_SQL.replace("SUM(clicks) AS clicks", "SUM(clicks + pg_sleep(1)) AS clicks"),
        RANKING_SQL.replace("SUM(clicks)", '"SUM"(clicks)'),
        RANKING_SQL.replace("SUM(clicks)", '"sum"(clicks)'),
        RANKING_SQL.replace("SUM(clicks) AS clicks", "SUM(clicks) OVER () AS clicks"),
        RANKING_SQL.replace("SUM(clicks) AS clicks", "COUNT(clicks) AS clicks"),
        RANKING_SQL.replace("SUM(clicks) AS clicks", "clicks AS clicks"),
        RANKING_SQL.replace("SUM(clicks) AS clicks", "SUM(clicks) AS impressions"),
        RANKING_SQL.replace(" * 0.4", " * 400000"),
        RANKING_SQL.replace("title,", "secret_field,"),
        RANKING_SQL.replace("SELECT news_id,", "SELECT *,"),
        RANKING_SQL.replace("SELECT news_id,", "SELECT DISTINCT news_id,"),
        RANKING_SQL.replace("tenant_id = :tenant_id", "tenant_id = :window_start"),
        RANKING_SQL.replace("tenant_id = :tenant_id", "tenant_id = COALESCE(:tenant_id, tenant_id)"),
        RANKING_SQL.replace("tenant_id = :tenant_id", "tenant_id = ':tenant_id'"),
        RANKING_SQL.replace("tenant_id = :tenant_id", "tenant_id IN (:tenant_id)"),
        RANKING_SQL.replace("tenant_id = :tenant_id", ":tenant_id = :tenant_id"),
        RANKING_SQL.replace("event_time < :window_end", "event_time >= :window_end"),
        RANKING_SQL.replace(" AND event_time < :window_end", ""),
        RANKING_SQL.replace(_SCOPE, f"tenant_id = :tenant_id AND EXISTS (SELECT 1 WHERE {_SCOPE})"),
        RANKING_SQL.replace(_SCOPE, _SCOPE + " AND 1 = 1"),
        RANKING_SQL.replace(_SCOPE, _SCOPE + " AND tenant_id = :tenant_id"),
        RANKING_SQL.replace(_SCOPE, _SCOPE + " AND category = '科技'"),
        RANKING_SQL.replace("GROUP BY " + _DIMENSIONS, "GROUP BY " + _DIMENSIONS + " WITH ROLLUP"),
        RANKING_SQL.replace("GROUP BY " + _DIMENSIONS, "GROUP BY news_id"),
        RANKING_SQL.replace("ORDER BY ctr DESC, news_id ASC", "ORDER BY random()"),
        RANKING_SQL.replace("news_id ASC LIMIT", "news_id DESC LIMIT"),
        RANKING_SQL.replace("ORDER BY ctr DESC, news_id ASC", "ORDER BY ctr DESC"),
        TREND_SQL.replace("event_time ASC LIMIT", "event_time DESC LIMIT"),
        RANKING_SQL.replace("LIMIT :row_limit", "LIMIT 10"),
        RANKING_SQL.replace("LIMIT :row_limit", "LIMIT :tenant_id"),
        RANKING_SQL + " OFFSET 100",
        RANKING_SQL + " FOR UPDATE",
        RANKING_SQL.replace("GROUP BY", "HAVING SUM(clicks) > 0 GROUP BY"),
    ],
)
def test_rejects_sql_outside_frozen_execution_contract(sql: str, params: dict) -> None:
    with pytest.raises(Text2SqlGuardError):
        SqlAssistantGuard().validate(sql, params)


@pytest.mark.parametrize(
    "overrides",
    [
        {"tenant_id": ""},
        {"tenant_id": " "},
        {"tenant_id": 12},
        {"row_limit": 0},
        {"row_limit": -1},
        {"row_limit": 101},
        {"row_limit": True},
        {"row_limit": "10"},
        {"window_start": datetime(2026, 10, 3)},
        {"window_end": "not-a-time"},
        {"window_end": datetime(2026, 10, 3, tzinfo=timezone.utc)},
        {"window_end": datetime(2026, 10, 11, tzinfo=timezone.utc)},
        {"window_start": datetime(2026, 10, 3, 0, 30, tzinfo=timezone.utc)},
        {"unknown": 1},
    ],
)
def test_rejects_invalid_bound_values(overrides: dict, params: dict) -> None:
    params.update(overrides)
    with pytest.raises(Text2SqlGuardError):
        SqlAssistantGuard().validate(RANKING_SQL, params)


def test_rejects_missing_binding_and_extra_binding(params: dict) -> None:
    params.pop("window_end")
    with pytest.raises(Text2SqlGuardError):
        SqlAssistantGuard().validate(RANKING_SQL, params)


@pytest.mark.parametrize("field,value", [("content_type", "audio"), ("category", "用户画像"), ("category", {})])
def test_rejects_filter_values_outside_contract(field: str, value: object, params: dict) -> None:
    sql = RANKING_SQL.replace(_SCOPE, _SCOPE + f" AND {field} = :{field}")
    params[field] = value
    with pytest.raises(Text2SqlGuardError):
        SqlAssistantGuard().validate(sql, params)


def test_configured_maximum_applies_to_bound_limit(params: dict) -> None:
    params["row_limit"] = 6
    with pytest.raises(Text2SqlGuardError):
        SqlAssistantGuard(max_rows=5).validate(RANKING_SQL, params)


@pytest.mark.parametrize("max_rows", [0, -1, True, "10"])
def test_rejects_invalid_guard_configuration(max_rows: object) -> None:
    with pytest.raises(ValueError):
        SqlAssistantGuard(max_rows=max_rows)  # type: ignore[arg-type]
