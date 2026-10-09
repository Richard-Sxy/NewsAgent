"""把 SQL 查询绑定到一次热点任务。固定查询 ID、租户、用户、时间窗口、配置范围，让任务重试是继续使用同一份查询，避免串用。"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.session import Database


class HotNewsSqlBindingError(RuntimeError):
    """Missing/conflicting authorization is terminal, not a transport retry."""

    retryable = False
    status_code = 409


class HotNewsSqlBindingPersistenceError(RuntimeError):
    """Temporary binding-store failure; the same immutable request may retry."""

    retryable = True
    status_code = 503


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise HotNewsSqlBindingError("SQL run windows must be timezone-aware")
    return value.astimezone(timezone.utc)


def _identifier(value: Any, *, name: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise HotNewsSqlBindingError(f"SQL run {name} is invalid")
    return value


def _uuid(value: Any, *, name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise HotNewsSqlBindingError(f"SQL run {name} must be a UUID") from exc


def _sha256(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise HotNewsSqlBindingError(f"SQL run {name} must be a SHA-256 digest")
    return value


def build_hot_news_sql_scope(
    *,
    question: str,
    scenario_id: str,
    window_start: datetime,
    window_end: datetime,
    schema_sha256: str,
    scenario: dict,
    production_bundle_version: str,
    model_scene: str,
    user_id: str | None = None,
    warehouse_schema_version: str = "news-warehouse-v1",
    dataset_identity: dict | None = None,
) -> str:
    """Hash the approved scope independently of randomized preview IDs.

    The outer HotNewsRunRequest identity adds tenant and Workflow identity.
    ``user_id`` separates otherwise identical private preview scopes belonging
    to different operators when the caller opts into owner-specific run keys.
    """

    start, end = _utc(window_start), _utc(window_end)
    if not timedelta(0) < end - start <= timedelta(days=7):
        raise HotNewsSqlBindingError("SQL run window must be positive and at most seven days")
    if not isinstance(scenario, dict) or not scenario:
        raise HotNewsSqlBindingError("SQL run scenario snapshot is required")
    payload = {
        "namespace": "newsagent.hot-news.sql-scope.v1",
        "warehouse_schema_version": _identifier(warehouse_schema_version, name="schema version", max_length=64),
        "dataset_version": "sample-v1",
        "schema_sha256": _sha256(schema_sha256, name="schema_sha256"),
        "question": _identifier(question, name="question", max_length=1000).strip(),
        "scenario_id": _identifier(scenario_id, name="scenario_id", max_length=64),
        "scenario": scenario,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "production_bundle_version": _identifier(production_bundle_version, name="bundle", max_length=128),
        "model_scene": _identifier(model_scene, name="model_scene", max_length=128),
    }
    if dataset_identity is not None:
        if not isinstance(dataset_identity, dict) or set(dataset_identity) != {
            "dataset_profile", "dataset_version", "dataset_sha256", "news_per_tenant",
        }:
            raise HotNewsSqlBindingError("Scaled SQL run requires its complete dataset identity")
        size = dataset_identity["news_per_tenant"]
        profiles = {"timeline-v4": ("news-warehouse-v4", (120, 1200, 12000)),
                    "enterprise-v2": ("news-warehouse-v2", (120, 1200, 12000)),
                    "public-headlines-v3": ("news-warehouse-v3", (120, 1200))}
        profile = dataset_identity["dataset_profile"]
        approved = profiles.get(profile) if type(profile) is str else None
        if (approved is None or warehouse_schema_version != approved[0]
                or type(size) is not int or size not in approved[1]):
            raise HotNewsSqlBindingError("Scaled SQL run dataset profile or size is invalid")
        payload["dataset_version"] = _identifier(dataset_identity["dataset_version"], name="dataset version", max_length=128)
        payload["dataset_identity"] = {
            **dataset_identity,
            "dataset_sha256": _sha256(dataset_identity["dataset_sha256"], name="dataset_sha256"),
        }
    elif warehouse_schema_version != "news-warehouse-v1":
        raise HotNewsSqlBindingError("Scaled SQL run requires its dataset identity")
    if user_id is not None:
        payload["user_id"] = _uuid(user_id, name="user_id")
    try:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise HotNewsSqlBindingError("SQL run scenario snapshot must be finite JSON") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_FIELDS = (
    "run_key", "query_id", "tenant_id", "user_id", "production_bundle_version",
    "scope_sha256", "window_start", "window_end",
)
_FIELD_SQL = ", ".join(_FIELDS)


class HotNewsSqlBindingStore:
    """Use unique run/query identities and INSERT conflicts for atomic claims."""

    def __init__(self, database: Database) -> None:
        self.database = database

    async def initialize(self, environment: str) -> None:
        if environment != "e2e":
            raise HotNewsSqlBindingError("SQL run binding initialization is only allowed in e2e")
        try:
            async with self.database.session() as session:
                await session.execute(text("SELECT pg_advisory_xact_lock(726031004)"))
                await session.execute(text("""
                    CREATE TABLE IF NOT EXISTS public.hot_news_sql_bindings (
                        run_key text PRIMARY KEY,
                        query_id uuid NOT NULL UNIQUE,
                        tenant_id uuid NOT NULL,
                        user_id uuid NOT NULL,
                        production_bundle_version text NOT NULL,
                        scope_sha256 text NOT NULL CHECK (scope_sha256 ~ '^[0-9a-f]{64}$'),
                        window_start timestamptz NOT NULL,
                        window_end timestamptz NOT NULL,
                        created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CHECK (window_start < window_end),
                        FOREIGN KEY (query_id) REFERENCES public.sql_assistant_query_audit(query_id)
                    )
                """))
        except SQLAlchemyError as exc:
            raise HotNewsSqlBindingPersistenceError("SQL run binding store initialization failed") from exc

    async def get_or_claim(
        self, *, run_key: str, query_id: str, tenant_id: str, user_id: str,
        production_bundle_version: str, scope_sha256: str,
        window_start: datetime, window_end: datetime,
    ) -> dict[str, Any]:
        expected = self._expected(
            run_key=run_key, query_id=query_id, tenant_id=tenant_id, user_id=user_id,
            production_bundle_version=production_bundle_version, scope_sha256=scope_sha256,
            window_start=window_start, window_end=window_end,
        )
        try:
            async with self.database.session() as session:
                # Checking preview ownership in the same INSERT avoids a gap
                # between authorization and claim. ON CONFLICT covers both the
                # run primary key and query uniqueness without aborting the tx.
                result = await session.execute(text(f"""
                    INSERT INTO public.hot_news_sql_bindings ({_FIELD_SQL})
                    SELECT :run_key, CAST(:query_id AS uuid), CAST(:tenant_id AS uuid),
                        CAST(:user_id AS uuid), :production_bundle_version,
                        :scope_sha256, :window_start, :window_end
                    WHERE EXISTS (
                        SELECT 1 FROM public.sql_assistant_query_audit
                        WHERE query_id=CAST(:query_id AS uuid)
                          AND tenant_id=CAST(:tenant_id AS uuid)
                          AND user_id=CAST(:user_id AS uuid)
                          AND expires_at > CURRENT_TIMESTAMP
                    )
                    ON CONFLICT DO NOTHING
                    RETURNING {_FIELD_SQL}
                """), expected)
                row = result.mappings().one_or_none()
                if row is not None:
                    return self._validate_binding(dict(row), expected, canonical_query=False)
                result = await session.execute(text("""
                    SELECT run_key FROM public.hot_news_sql_bindings
                    WHERE query_id=CAST(:query_id AS uuid)
                """), {"query_id": expected["query_id"]})
                query_binding = result.mappings().one_or_none()
                if query_binding is not None and query_binding["run_key"] != expected["run_key"]:
                    raise HotNewsSqlBindingError("SQL preview is already bound to a different run")
                result = await session.execute(text(f"""
                    SELECT {_FIELD_SQL} FROM public.hot_news_sql_bindings
                    WHERE run_key=:run_key
                """), {"run_key": expected["run_key"]})
                row = result.mappings().one_or_none()
                if row is None:
                    raise HotNewsSqlBindingError("SQL preview is unavailable or already bound to a different run")
                # A concurrent authorized preview may have a different UUID;
                # only the first one is canonical for this frozen run scope.
                return self._validate_binding(dict(row), expected, canonical_query=True)
        except SQLAlchemyError as exc:
            raise HotNewsSqlBindingPersistenceError("SQL run binding store is temporarily unavailable") from exc

    async def require(
        self, *, run_key: str, query_id: str, tenant_id: str, user_id: str,
        production_bundle_version: str, scope_sha256: str,
        window_start: datetime, window_end: datetime,
    ) -> dict[str, Any]:
        expected = self._expected(
            run_key=run_key, query_id=query_id, tenant_id=tenant_id, user_id=user_id,
            production_bundle_version=production_bundle_version, scope_sha256=scope_sha256,
            window_start=window_start, window_end=window_end,
        )
        try:
            async with self.database.session() as session:
                result = await session.execute(text(f"""
                    SELECT {_FIELD_SQL} FROM public.hot_news_sql_bindings
                    WHERE run_key=:run_key
                      AND tenant_id=CAST(:tenant_id AS uuid)
                      AND user_id=CAST(:user_id AS uuid)
                """), {key: expected[key] for key in ("run_key", "tenant_id", "user_id")})
                row = result.mappings().one_or_none()
                if row is None:
                    raise HotNewsSqlBindingError("SQL run has no matching approved query binding")
                return self._validate_binding(dict(row), expected, canonical_query=False)
        except SQLAlchemyError as exc:
            raise HotNewsSqlBindingPersistenceError("SQL run binding store is temporarily unavailable") from exc

    @staticmethod
    def _expected(**values: Any) -> dict[str, Any]:
        expected = {
            "run_key": _identifier(values["run_key"], name="run_key", max_length=160),
            "query_id": _uuid(values["query_id"], name="query_id"),
            "tenant_id": _uuid(values["tenant_id"], name="tenant_id"),
            "user_id": _uuid(values["user_id"], name="user_id"),
            "production_bundle_version": _identifier(values["production_bundle_version"], name="bundle", max_length=128),
            "scope_sha256": _sha256(values["scope_sha256"], name="scope_sha256"),
            "window_start": _utc(values["window_start"]),
            "window_end": _utc(values["window_end"]),
        }
        if not timedelta(0) < expected["window_end"] - expected["window_start"] <= timedelta(days=7):
            raise HotNewsSqlBindingError("SQL run window must be positive and at most seven days")
        return expected

    @classmethod
    def _validate_binding(
        cls, row: dict[str, Any], expected: dict[str, Any], *, canonical_query: bool,
    ) -> dict[str, Any]:
        try:
            actual = cls._expected(**row)
        except (KeyError, TypeError, ValueError) as exc:
            raise HotNewsSqlBindingError("SQL run binding is malformed") from exc
        compared = tuple(field for field in _FIELDS if not canonical_query or field != "query_id")
        if any(actual[field] != expected[field] for field in compared):
            raise HotNewsSqlBindingError("SQL run binding differs from frozen authorization")
        return actual
