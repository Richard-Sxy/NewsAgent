#!/usr/bin/env python3
"""Administrator-only aggregate contract check; synthetic data is the default.

No .env discovery, endpoint discovery, raw-key configuration, or automatic
adapter import occurs. --factory (or an explicitly selected YAML source_factory)
is an administrator-authorized Python plugin, never model-controlled input.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import importlib
import json
from pathlib import Path
import re
import sys

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.analytics.entities import ContentType
from app.analytics.metrics import NewsMetricSnapshot
from app.data_analysis.acceptance import AcceptanceRequest, run_aggregate_acceptance


_CONFIG_KEYS = frozenset({
    "schema_version", "tenant_id", "window_start", "window_end", "content_types",
    "max_rows", "timeout_seconds", "source_factory",
})
_FACTORY_PATTERN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", re.ASCII)


class AcceptanceConfigError(ValueError):
    pass


class _UniqueSafeLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    value = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if type(key) is not str or key in value:
            raise AcceptanceConfigError("configuration_invalid")
        value[key] = loader.construct_object(value_node, deep=deep)
    return value


_UniqueSafeLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _parse_datetime(value: object) -> datetime:
    if type(value) is not str or len(value) > 64:
        raise AcceptanceConfigError("configuration_invalid")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_config(path: str | None) -> tuple[AcceptanceRequest, str | None]:
    if path is None:
        start = datetime(2026, 10, 3, 2, tzinfo=timezone.utc)
        return AcceptanceRequest(
            tenant_id="11111111-1111-4111-8111-111111111111",
            window_start=start, window_end=start + timedelta(hours=1),
            content_types=frozenset(ContentType),
        ), None
    source = Path(path).resolve(strict=True)
    if source.suffix.lower() not in {".yml", ".yaml"} or not source.is_file() or source.stat().st_size > 65_536:
        raise AcceptanceConfigError("configuration_invalid")
    config = yaml.load(source.read_text(encoding="utf-8"), Loader=_UniqueSafeLoader)
    if type(config) is not dict or frozenset(config) != _CONFIG_KEYS or config["schema_version"] != "1.0":
        raise AcceptanceConfigError("configuration_invalid")
    types = config["content_types"]
    if (
        type(types) is not list or not 1 <= len(types) <= 2
        or any(type(item) is not str or item not in {"article", "video"} for item in types)
        or len(set(types)) != len(types)
    ):
        raise AcceptanceConfigError("configuration_invalid")
    factory = config["source_factory"]
    if factory is not None and (type(factory) is not str or _FACTORY_PATTERN.fullmatch(factory) is None):
        raise AcceptanceConfigError("configuration_invalid")
    request = AcceptanceRequest(
        tenant_id=config["tenant_id"], window_start=_parse_datetime(config["window_start"]),
        window_end=_parse_datetime(config["window_end"]),
        content_types=frozenset(ContentType(item) for item in types),
        max_rows=config["max_rows"], timeout_seconds=config["timeout_seconds"],
        mode="adapter_contract" if factory else "synthetic_fixture",
    )
    request.validate()
    return request, factory


class SyntheticAggregateSource:
    """Deterministic aggregate-only fixture; no database, network or credentials."""

    async def fetch_snapshots(self, query):
        return [NewsMetricSnapshot(
            news_id=f"synthetic-{content_type.value}", content_type=content_type,
            window_start=query.window_start, window_end=query.window_end,
            impressions=100, clicks=10, unique_users=7, total_duration_seconds=140,
            effective_consumptions=7, interactions=3, ctr=Decimal("0.1"),
        ) for content_type in sorted(query.content_types or frozenset(ContentType), key=lambda item: item.value)][:query.ranking_limit]


def load_source_factory(factory: str):
    if type(factory) is not str or _FACTORY_PATTERN.fullmatch(factory) is None:
        raise AcceptanceConfigError("factory_invalid")
    module_name, callable_name = factory.split(":", 1)
    creator = getattr(importlib.import_module(module_name), callable_name)
    if not callable(creator):
        raise AcceptanceConfigError("factory_invalid")
    source = creator()
    if not callable(getattr(source, "fetch_snapshots", None)):
        raise AcceptanceConfigError("factory_invalid")
    return source


def _failure(code: str) -> dict:
    return {
        "schema_version": "1.0", "status": "contract_failed",
        "checks": [{"name": "administrator_configuration", "status": "failed", "code": code}],
        "limitations": ["No production or tenant-isolation acceptance is established."],
    }


async def run_cli(args: argparse.Namespace) -> dict:
    try:
        request, config_factory = load_config(args.config)
        factory = args.factory if args.factory is not None else config_factory
        source = load_source_factory(factory) if factory else SyntheticAggregateSource()
        return await run_aggregate_acceptance(
            source, request.to_query(), max_rows=request.max_rows,
            timeout_seconds=request.timeout_seconds,
            mode="adapter_contract" if factory else "synthetic_fixture",
        )
    except Exception:
        # Do not print file contents, plugin exceptions, endpoints or credentials.
        return _failure("configuration_or_factory_invalid")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Explicit public YAML configuration; no automatic discovery")
    parser.add_argument("--factory", help="Explicit trusted Python factory module:callable returning NewsMetricSource")
    args = parser.parse_args(argv)
    report = asyncio.run(run_cli(args))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
    return 0 if report["status"] in {"local_contract_passed", "contract_passed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
