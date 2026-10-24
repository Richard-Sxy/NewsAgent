"""Business metric boundaries: bad SQL must stop before the warehouse Port."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
import sqlglot

from app.clients.enterprise.sql_warehouse import InMemorySqlWarehouseClient
from app.domain.errors import HotNewsDataQualityError, Text2SqlGuardError
from app.modules.hot_news.scoring.text2sql_metric_source import (
    HotNewsMetricSqlTemplate, Text2SqlNewsMetricSource,
)
from tests.test_text2sql_metric_source import (
    METRIC_COLUMNS, START, TABLE, generated_plan, query, row, schema, template,
)


def approved_sql() -> str:
    return template().render(query(), max_rows=1000)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [
    lambda sql: sql.replace("clicks AS clicks", "clicks * 1000 AS clicks"),
    lambda sql: sql.replace("clicks AS clicks", "999999 AS clicks"),
    lambda sql: sql.replace("clicks AS clicks", "SUM(DISTINCT clicks) AS clicks"),
    lambda sql: sql.replace("news_id AS news_id", "'fake-news' AS news_id"),
    lambda sql: sql.replace("content_type AS content_type", "'video' AS content_type"),
    lambda sql: sql.replace(" ORDER BY", " AND clicks > 10 ORDER BY"),
    lambda sql: sql.replace(" ORDER BY", " GROUP BY news_id, content_type ORDER BY"),
    lambda sql: sql.replace(" ORDER BY", " HAVING SUM(clicks) > 10 ORDER BY"),
    lambda sql: sql.replace("SELECT ", "SELECT DISTINCT "),
    lambda sql: sql.replace("ORDER BY news_id, content_type", "ORDER BY clicks DESC"),
    lambda sql: sql.replace("LIMIT :row_limit", "LIMIT 1"),
    lambda sql: sql.replace(" FROM", ", clicks AS clicks FROM"),
    lambda sql: sql.replace(" FROM", ", 99 AS ctr FROM"),
    lambda sql: sql.replace("clicks AS clicks, ", ""),
])
async def test_changed_metric_or_candidate_scope_never_reaches_warehouse(mutate):
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse, schema=schema(), metric_contract=template(),
        generator=generated_plan(mutate(approved_sql())),
    )
    with pytest.raises(Text2SqlGuardError):
        await source.fetch_batch(query())
    assert warehouse.queries == []


@pytest.mark.asyncio
async def test_formatting_projection_order_and_redundant_aliases_are_allowed():
    statement = sqlglot.parse_one(approved_sql(), read="postgres")
    statement.set("expressions", list(reversed(statement.expressions)))
    candidate = statement.sql(pretty=True).replace("news_id AS news_id", "news_id")
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse, schema=schema(), metric_contract=template(),
        generator=generated_plan(candidate),
    )
    batch = await source.fetch_batch(query())
    assert len(warehouse.queries) == 1
    assert batch.snapshots[0].clicks == 40
    assert batch.metric_contract_version == template().version
    assert len(batch.metric_contract_sha256) == 64


@pytest.mark.asyncio
async def test_no_approved_contract_stops_before_generation():
    generator = generated_plan(approved_sql())
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse, schema=schema(), generator=generator,
    )
    with pytest.raises(HotNewsDataQualityError, match="approved metric contract"):
        await source.fetch_batch(query())
    generator.run.assert_not_awaited()
    assert warehouse.queries == []


@pytest.mark.asyncio
async def test_unsupported_metric_is_not_generated_or_silently_discarded():
    generator = generated_plan(approved_sql())
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse, schema=schema(), template=template(), generator=generator,
    )
    with pytest.raises(HotNewsDataQualityError, match="unsupported snapshot metrics"):
        await source.fetch_batch(query(requested_metrics=frozenset({"growth"})))
    generator.run.assert_not_awaited()
    assert warehouse.queries == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"window_end": START + timedelta(hours=2)},
    {"window_start": START + timedelta(minutes=1), "window_end": START + timedelta(hours=1, minutes=1)},
    {"window_end": START + timedelta(minutes=30)},
    # This looks like one wall-clock hour but spans two elapsed UTC hours.
    {"window_start": datetime(2026, 11, 1, 1, tzinfo=ZoneInfo("America/New_York")),
     "window_end": datetime(2026, 11, 1, 2, tzinfo=ZoneInfo("America/New_York"))},
])
async def test_non_bucket_window_cannot_claim_hourly_uv(changes):
    generator = generated_plan(approved_sql())
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse, schema=schema(), metric_contract=template(), generator=generator,
    )
    with pytest.raises(HotNewsDataQualityError, match="one aligned"):
        await source.fetch_batch(query(**changes))
    generator.run.assert_not_awaited()
    assert warehouse.queries == []


@pytest.mark.asyncio
async def test_duplicate_news_rows_do_not_become_multiple_ranked_items():
    warehouse = InMemorySqlWarehouseClient((row(), row()))
    source = Text2SqlNewsMetricSource(warehouse=warehouse, schema=schema(), template=template())
    with pytest.raises(HotNewsDataQualityError, match="duplicate"):
        await source.fetch_batch(query())


def test_contract_expression_mapping_is_a_frozen_copy():
    expressions = {name: name for name in METRIC_COLUMNS}
    contract = HotNewsMetricSqlTemplate(
        table=TABLE, metric_expressions=expressions, group_by_dimensions=False,
    )
    before = contract.sha256
    expressions["clicks"] = "999999"
    assert contract.metric_expressions["clicks"] == "clicks"
    assert contract.sha256 == before
    with pytest.raises(TypeError):
        contract.metric_expressions["clicks"] = "999999"
    assert replace(contract, version="new-approved-version").sha256 != before


@pytest.mark.asyncio
async def test_template_path_cannot_bypass_a_separate_approved_contract():
    bad_template = replace(template(), metric_expressions={
        name: "999999" if name == "clicks" else name for name in METRIC_COLUMNS
    })
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(
        warehouse=warehouse, schema=schema(), template=bad_template,
        metric_contract=template(),
    )
    with pytest.raises(Text2SqlGuardError, match="approved metric contract"):
        await source.fetch_batch(query())
    assert warehouse.queries == []


@pytest.mark.asyncio
async def test_ctr_request_uses_python_calculation_without_model_projection():
    warehouse = InMemorySqlWarehouseClient((row(),))
    source = Text2SqlNewsMetricSource(warehouse=warehouse, schema=schema(), template=template())
    batch = await source.fetch_batch(query(requested_metrics=frozenset({"ctr"})))
    assert batch.snapshots[0].ctr == Decimal("0.4")
    assert " AS ctr" not in warehouse.queries[0].sql
