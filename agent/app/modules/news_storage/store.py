"""Immutable public-news releases on an explicitly supplied SQLAlchemy engine.

This module never discovers credentials or loads application Settings. The
caller supplies an isolated database; initialize() only creates missing tables.
It does not replace the online knowledge lifecycle or build vector indexes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from hashlib import sha256
import json
from typing import Any

from sqlalchemy import (
    JSON, Column, DateTime, ForeignKeyConstraint, Index, MetaData, String, Table,
    Text, and_, event, func, insert, inspect, select, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from app.analytics.entities import ContentType
from app.analytics.news_content import NewsContent


metadata = MetaData()
_json = JSON().with_variant(JSONB(), "postgresql")
versions = Table(
    "news_content_versions", metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("news_id", String(160), primary_key=True),
    Column("record_sha256", String(64), primary_key=True),
    Column("title", Text),
    Column("body", Text),
    Column("url", Text),
    Column("publish_time", DateTime(timezone=True)),
    Column("content_type", String(16), nullable=False),
    Column("content_state", String(24), nullable=False),
    Column("body_sha256", String(64)),
    # Only extra fields (fragments and quality flags), never duplicate body.
    Column("record", _json, nullable=False),
)
releases = Table(
    "news_content_releases", metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("release_id", String(160), primary_key=True),
    Column("identity_sha256", String(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("selection_policy", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("manifest", _json, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
provenance = Table(
    "news_content_provenance", metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("release_id", String(160), primary_key=True),
    Column("news_id", String(160), primary_key=True),
    Column("record_sha256", String(64), primary_key=True),
    Column("source_sha256", String(64), primary_key=True),
    Column("source", _json, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "release_id"],
        ["news_content_releases.tenant_id", "news_content_releases.release_id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "news_id", "record_sha256"],
        ["news_content_versions.tenant_id", "news_content_versions.news_id",
         "news_content_versions.record_sha256"],
    ),
)
items = Table(
    "news_content_release_items", metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("release_id", String(160), primary_key=True),
    Column("news_id", String(160), primary_key=True),
    Column("record_sha256", String(64), nullable=False),
    Column("quality_flags", _json, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "release_id"],
        ["news_content_releases.tenant_id", "news_content_releases.release_id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "news_id", "record_sha256"],
        ["news_content_versions.tenant_id", "news_content_versions.news_id",
         "news_content_versions.record_sha256"],
    ),
)
Index("ix_news_content_versions_state", versions.c.tenant_id, versions.c.content_state)
_RECORD_COLUMNS = (
    "news_id", "record_sha256", "title", "body", "url", "publish_time",
    "content_type", "content_state", "body_sha256",
)


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _identity(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {name}")


def _utc_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("publish_time requires a timezone")
    return stamp.astimezone(UTC)


def _record_values(record: dict, tenant_id: str) -> dict:
    row = {key: record[key] for key in _RECORD_COLUMNS}
    row["tenant_id"] = tenant_id
    row["publish_time"] = _utc_time(row["publish_time"])
    row["record"] = {key: value for key, value in record.items()
                     if key not in _RECORD_COLUMNS}
    return row


def _read_record(row: Any) -> dict:
    record = {key: row[key] for key in _RECORD_COLUMNS}
    stamp = record["publish_time"]
    if stamp is not None:
        record["publish_time"] = stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC).isoformat()
    record.update(row["record"])
    from app.modules.news_storage.model import normalize_record

    if normalize_record(record) != record:
        raise ValueError("stored content record/hash mismatch")
    return record


def _sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _batch_insert(connection: Connection, table: Table, rows: list[dict]) -> None:
    for start in range(0, len(rows), 500):
        connection.execute(insert(table), rows[start:start + 500])


class FrozenNewsStore:
    def __init__(self, engine: Engine):
        self.engine = engine
        if engine.dialect.name == "sqlite":
            if not event.contains(engine, "connect", _sqlite_foreign_keys):
                event.listen(engine, "connect", _sqlite_foreign_keys)
            # Also enable constraints on a connection already in the pool.
            with engine.connect() as connection:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    def initialize(self) -> None:
        """Create absent tables only; never clear or migrate existing data."""
        with self.engine.begin() as connection:
            metadata.create_all(connection, checkfirst=True)
            if "news_content_selected" not in inspect(connection).get_view_names():
                connection.execute(text("""
                    CREATE VIEW news_content_selected AS
                    SELECT r.tenant_id, r.release_id, r.identity_sha256,
                           v.news_id, v.record_sha256, v.body_sha256,
                           v.title, v.body, v.url, v.publish_time,
                           v.content_type, v.content_state,
                           i.quality_flags AS item_quality_flags, v.record
                    FROM news_content_releases AS r
                    JOIN news_content_release_items AS i
                      ON r.tenant_id=i.tenant_id AND r.release_id=i.release_id
                    JOIN news_content_versions AS v
                      ON i.tenant_id=v.tenant_id AND i.news_id=v.news_id
                     AND i.record_sha256=v.record_sha256
                    WHERE r.status='frozen'
                """))

    @staticmethod
    def _require_release(connection: Connection, tenant_id: str, release_id: str):
        _identity(tenant_id, "tenant_id", 128)
        _identity(release_id, "release_id", 160)
        row = connection.execute(select(releases).where(
            releases.c.tenant_id == tenant_id,
            releases.c.release_id == release_id,
            releases.c.status == "frozen",
        )).mappings().first()
        if row is None:
            raise ValueError("unknown frozen release for tenant")
        manifest = row["manifest"]
        if any(row[key] != manifest.get(key) for key in (
            "tenant_id", "release_id", "identity_sha256", "schema_version", "selection_policy",
        )):
            raise ValueError("stored release metadata differs from manifest")
        return row

    @staticmethod
    def _read_package(connection: Connection, tenant_id: str, release_id: str) -> dict:
        release = FrozenNewsStore._require_release(connection, tenant_id, release_id)
        version_ids = select(provenance.c.news_id, provenance.c.record_sha256).where(
            provenance.c.tenant_id == tenant_id,
            provenance.c.release_id == release_id,
        ).distinct().subquery()
        records = [_read_record(row) for row in connection.execute(
            select(versions).join(version_ids, and_(
                versions.c.news_id == version_ids.c.news_id,
                versions.c.record_sha256 == version_ids.c.record_sha256,
            )).where(versions.c.tenant_id == tenant_id).order_by(
                versions.c.news_id, versions.c.record_sha256,
            )
        ).mappings()]
        sources = [
            {"news_id": row["news_id"], "record_sha256": row["record_sha256"],
             "source": row["source"]}
            for row in connection.execute(select(provenance).where(
                provenance.c.tenant_id == tenant_id,
                provenance.c.release_id == release_id,
            ).order_by(provenance.c.news_id, provenance.c.record_sha256,
                       provenance.c.source_sha256)).mappings()
        ]
        selected = [
            {"news_id": row["news_id"], "record_sha256": row["record_sha256"],
             "quality_flags": row["quality_flags"]}
            for row in connection.execute(select(items).where(
                items.c.tenant_id == tenant_id, items.c.release_id == release_id,
            ).order_by(items.c.news_id)).mappings()
        ]
        return {"manifest": release["manifest"], "versions": records,
                "provenance": sources, "items": selected}

    def import_release(self, package: dict) -> dict:
        """Validate, write and verify one complete release in a transaction."""
        from app.modules.news_storage.model import validate_package

        # Own immutable inputs for the duration of the transaction.
        package = json.loads(_canonical(package))
        validate_package(package)
        manifest = package["manifest"]
        tenant_id, release_id = manifest["tenant_id"], manifest["release_id"]
        expected_versions = {(row["news_id"], row["record_sha256"])
                             for row in package["versions"]}
        referenced_versions = {(row["news_id"], row["record_sha256"])
                               for row in package["provenance"]}
        if expected_versions != referenced_versions:
            raise ValueError("every release version must have source provenance")
        created = False
        try:
            with self.engine.begin() as connection:
                existing = connection.execute(select(releases).where(
                    releases.c.tenant_id == tenant_id,
                    releases.c.release_id == release_id,
                )).mappings().first()
                if existing is not None:
                    if existing["manifest"] != manifest or existing["status"] != "frozen":
                        raise ValueError("release identity already has a different manifest")
                    validate_package(self._read_package(connection, tenant_id, release_id))
                else:
                    connection.execute(insert(releases), {
                        "tenant_id": tenant_id, "release_id": release_id,
                        "identity_sha256": manifest["identity_sha256"],
                        "schema_version": manifest["schema_version"],
                        "selection_policy": manifest["selection_policy"],
                        "status": "frozen", "manifest": manifest,
                        "created_at": datetime.now(UTC),
                    })
                    existing_versions: dict[tuple[str, str], dict] = {}
                    news_ids = sorted({row["news_id"] for row in package["versions"]})
                    for start in range(0, len(news_ids), 500):
                        for row in connection.execute(select(versions).where(
                            versions.c.tenant_id == tenant_id,
                            versions.c.news_id.in_(news_ids[start:start + 500]),
                        )).mappings():
                            existing_versions[(row["news_id"], row["record_sha256"])] = _read_record(row)
                    new_rows = []
                    for record in package["versions"]:
                        key = (record["news_id"], record["record_sha256"])
                        old = existing_versions.get(key)
                        if old is not None and old != record:
                            raise ValueError("stored content version differs from its hash")
                        if old is None:
                            new_rows.append(_record_values(record, tenant_id))
                    _batch_insert(connection, versions, new_rows)
                    _batch_insert(connection, provenance, [
                        dict(tenant_id=tenant_id, release_id=release_id,
                             news_id=row["news_id"], record_sha256=row["record_sha256"],
                             source_sha256=_digest(row["source"]), source=row["source"])
                        for row in package["provenance"]
                    ])
                    _batch_insert(connection, items, [
                        dict(tenant_id=tenant_id, release_id=release_id, **row)
                        for row in package["items"]
                    ])
                    # Reconstruct from SQL and rerun content hashes, references,
                    # counts and manifest identity before committing publication.
                    validate_package(self._read_package(connection, tenant_id, release_id))
                    created = True
        except IntegrityError as exc:
            raise ValueError("release conflicts with existing immutable storage") from exc
        result = self.summary(tenant_id, release_id)
        result["import_status"] = "created" if created else "already_present"
        return result

    @staticmethod
    def _selected_query(tenant_id: str, release_id: str):
        return select(versions, items.c.quality_flags.label("selection_quality_flags")).join(
            items, and_(versions.c.tenant_id == items.c.tenant_id,
                        versions.c.news_id == items.c.news_id,
                        versions.c.record_sha256 == items.c.record_sha256),
        ).where(items.c.tenant_id == tenant_id, items.c.release_id == release_id)

    @staticmethod
    def _with_evidence(connection: Connection, rows: list, tenant_id: str,
                       release_id: str) -> list[dict]:
        if not rows:
            return []
        sources: dict[tuple[str, str], list[dict]] = {}
        news_ids = [row["news_id"] for row in rows]
        for start in range(0, len(news_ids), 500):
            for source in connection.execute(select(provenance).where(
                provenance.c.tenant_id == tenant_id,
                provenance.c.release_id == release_id,
                provenance.c.news_id.in_(news_ids[start:start + 500]),
            ).order_by(provenance.c.source_sha256)).mappings():
                sources.setdefault((source["news_id"], source["record_sha256"]), []).append(source["source"])
        results = []
        for row in rows:
            result = _read_record(row)
            result.update(tenant_id=tenant_id, release_id=release_id,
                          selection_quality_flags=row["selection_quality_flags"],
                          provenance=sources.get((row["news_id"], row["record_sha256"]), []))
            results.append(result)
        return results

    def get_record(self, tenant_id: str, release_id: str, news_id: str) -> dict | None:
        _identity(news_id, "news_id", 160)
        with self.engine.connect() as connection:
            self._require_release(connection, tenant_id, release_id)
            row = connection.execute(self._selected_query(tenant_id, release_id).where(
                versions.c.news_id == news_id,
            )).mappings().first()
            return None if row is None else self._with_evidence(connection, [row], tenant_id, release_id)[0]

    def get_records(self, tenant_id: str, release_id: str, news_ids: tuple[str, ...]) -> list[dict]:
        """Bounded bulk read used by the asynchronous content Port adapter."""
        identities = tuple(dict.fromkeys(news_ids))
        for news_id in identities:
            _identity(news_id, "news_id", 160)
        with self.engine.connect() as connection:
            self._require_release(connection, tenant_id, release_id)
            records = []
            for start in range(0, len(identities), 500):
                rows = connection.execute(self._selected_query(tenant_id, release_id).where(
                    versions.c.news_id.in_(identities[start:start + 500]),
                ).order_by(versions.c.news_id)).mappings().all()
                records.extend(self._with_evidence(connection, rows, tenant_id, release_id))
            return records

    def list_records(self, tenant_id: str, release_id: str, after_news_id: str = "",
                     limit: int = 100, body_only: bool = False) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(after_news_id, str) or len(after_news_id) > 160:
            raise ValueError("invalid after_news_id")
        if type(body_only) is not bool:
            raise ValueError("body_only must be a boolean")
        with self.engine.connect() as connection:
            self._require_release(connection, tenant_id, release_id)
            query = self._selected_query(tenant_id, release_id).where(
                versions.c.news_id > after_news_id,
            ).order_by(versions.c.news_id).limit(limit)
            if body_only:
                query = query.where(versions.c.content_state == "body_available")
            rows = connection.execute(query).mappings().all()
            return self._with_evidence(connection, rows, tenant_id, release_id)

    def summary(self, tenant_id: str, release_id: str) -> dict:
        with self.engine.connect() as connection:
            release = self._require_release(connection, tenant_id, release_id)
            manifest = release["manifest"]
            count = connection.execute(select(func.count()).select_from(items).where(
                items.c.tenant_id == tenant_id, items.c.release_id == release_id,
            )).scalar_one()
            if count != manifest["counts"]["news_count"]:
                raise ValueError("stored release item count differs from manifest")
            return {"tenant_id": tenant_id, "release_id": release_id,
                    "status": release["status"], "identity_sha256": release["identity_sha256"],
                    "schema_version": release["schema_version"],
                    "selection_policy": release["selection_policy"],
                    "counts": manifest["counts"]}


class ReleaseNewsContentRepository:
    """Explicit frozen release adapter; never advertises vector lifecycle IDs."""

    def __init__(self, store: FrozenNewsStore, tenant_id: str, release_id: str):
        _identity(tenant_id, "tenant_id", 128)
        _identity(release_id, "release_id", 160)
        self.store = store
        self.tenant_id = tenant_id
        self.release_id = release_id

    def _batch(self, tenant_id: str, news_ids: tuple[str, ...]) -> dict[str, NewsContent]:
        if tenant_id != self.tenant_id:
            raise ValueError("repository is bound to a different tenant")
        result = {}
        for row in self.store.get_records(self.tenant_id, self.release_id, news_ids):
            news_id = row["news_id"]
            flags = set(row["quality_flags"]) | set(row["selection_quality_flags"])
            if (row["content_state"] != "body_available" or not row["body"]
                    or not row["title"] or not row["url"] or not row["publish_time"]
                    or "metadata_review_required" in flags):
                continue
            content = NewsContent(
                news_id=news_id, title=row["title"], body=row["body"],
                summary=row["body"][:300], content_type=ContentType(row["content_type"]),
                publish_time=_utc_time(row["publish_time"]), source_url=row["url"],
                collection_id=None,
            )
            content.validate()
            result[news_id] = content
        return result

    async def batch_get_by_news_ids(self, *, tenant_id: str,
                                    news_ids: tuple[str, ...]) -> dict[str, NewsContent]:
        return await asyncio.to_thread(self._batch, tenant_id, news_ids)

    async def get_record(self, *, tenant_id: str, news_id: str) -> dict | None:
        if tenant_id != self.tenant_id:
            raise ValueError("repository is bound to a different tenant")
        return await asyncio.to_thread(self.store.get_record, tenant_id, self.release_id, news_id)
