"""Versioned inferred topics joined to an explicit immutable content release.

No runtime configuration, model calls, online index mutation or implicit latest
release selection occurs here. The caller supplies an existing content store.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json

from sqlalchemy import (
    Column, DateTime, ForeignKeyConstraint, Index, String, Table,
    and_, exists, func, insert, inspect, select, text,
)
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from app.modules.news_storage.store import (
    FrozenNewsStore, _batch_insert, _canonical, _digest, _identity, _json,
    items, metadata, versions,
)


tag_releases = Table(
    "news_content_tag_releases", metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("content_release_id", String(160), primary_key=True),
    Column("tag_release_id", String(160), primary_key=True),
    Column("content_identity_sha256", String(64), nullable=False),
    Column("identity_sha256", String(64), nullable=False),
    Column("schema_version", String(64), nullable=False),
    Column("derivation_version", String(128), nullable=False),
    Column("policy_version", String(128), nullable=False),
    Column("taxonomy_version", String(128), nullable=False),
    Column("tagger_sha256", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("manifest", _json, nullable=False),
    Column("policy", _json, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "content_release_id"],
        ["news_content_releases.tenant_id", "news_content_releases.release_id"],
    ),
)
tags = Table(
    "news_content_tags", metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("content_release_id", String(160), primary_key=True),
    Column("tag_release_id", String(160), primary_key=True),
    Column("news_id", String(160), primary_key=True),
    Column("record_sha256", String(64), nullable=False),
    Column("body_sha256", String(64)),
    Column("label_sha256", String(64), nullable=False),
    Column("label_status", String(32), nullable=False),
    Column("input_scope", String(24), nullable=False),
    Column("provenance", String(32), nullable=False),
    Column("topic_ids", _json, nullable=False),
    Column("label", _json, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "content_release_id", "tag_release_id"],
        ["news_content_tag_releases.tenant_id",
         "news_content_tag_releases.content_release_id",
         "news_content_tag_releases.tag_release_id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "content_release_id", "news_id"],
        ["news_content_release_items.tenant_id", "news_content_release_items.release_id",
         "news_content_release_items.news_id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "news_id", "record_sha256"],
        ["news_content_versions.tenant_id", "news_content_versions.news_id",
         "news_content_versions.record_sha256"],
    ),
)
topic_memberships = Table(
    "news_content_topic_memberships", metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("content_release_id", String(160), primary_key=True),
    Column("tag_release_id", String(160), primary_key=True),
    Column("news_id", String(160), primary_key=True),
    Column("topic_id", String(128), primary_key=True),
    ForeignKeyConstraint(
        ["tenant_id", "content_release_id", "tag_release_id", "news_id"],
        ["news_content_tags.tenant_id", "news_content_tags.content_release_id",
         "news_content_tags.tag_release_id", "news_content_tags.news_id"],
    ),
)
Index("ix_news_content_topic_lookup", topic_memberships.c.tenant_id,
      topic_memberships.c.content_release_id, topic_memberships.c.tag_release_id,
      topic_memberships.c.topic_id, topic_memberships.c.news_id)

_LABEL_COLUMNS = (
    "news_id", "record_sha256", "body_sha256", "label_sha256", "label_status",
    "input_scope", "provenance", "topic_ids",
)
_MANIFEST_COLUMNS = (
    "tenant_id", "content_release_id", "tag_release_id", "content_identity_sha256",
    "identity_sha256", "schema_version", "derivation_version", "policy_version",
    "taxonomy_version", "tagger_sha256",
)


def _scope(table, tenant_id: str, content_release_id: str, tag_release_id: str):
    return and_(table.c.tenant_id == tenant_id,
                table.c.content_release_id == content_release_id,
                table.c.tag_release_id == tag_release_id)


def _read_label(row) -> dict:
    label = row["label"]
    if not isinstance(label, dict) or any(label.get(key) != row[key] for key in _LABEL_COLUMNS):
        raise ValueError("stored tag columns differ from label")
    if _digest({key: value for key, value in label.items() if key != "label_sha256"}) != label.get("label_sha256"):
        raise ValueError("stored label/hash mismatch")
    if label.get("labels_are_inferred") is not True:
        raise ValueError("stored labels must remain inferred")
    return label


class FrozenNewsTagStore:
    def __init__(self, content_store: FrozenNewsStore):
        self.content_store = content_store
        self.engine = content_store.engine

    def initialize(self) -> None:
        """Create only absent tag tables/view; preserve all frozen content."""
        with self.engine.begin() as connection:
            metadata.create_all(connection, tables=[tag_releases, tags, topic_memberships], checkfirst=True)
            if "news_content_tagged_selected" not in inspect(connection).get_view_names():
                connection.execute(text("""
                    CREATE VIEW news_content_tagged_selected AS
                    SELECT c.tenant_id, c.release_id AS content_release_id,
                           c.identity_sha256 AS content_identity_sha256,
                           t.tag_release_id, r.identity_sha256 AS tag_identity_sha256,
                           r.policy_version, r.taxonomy_version, r.tagger_sha256,
                           c.news_id, c.record_sha256, c.body_sha256,
                           c.title, c.body, c.url, c.publish_time,
                           c.content_type, c.content_state, c.item_quality_flags,
                           t.topic_ids, t.label_status AS tag_status, t.input_scope,
                           t.provenance AS tag_provenance, t.label_sha256, t.label
                    FROM news_content_selected AS c
                    JOIN news_content_tags AS t
                      ON c.tenant_id=t.tenant_id AND c.release_id=t.content_release_id
                     AND c.news_id=t.news_id AND c.record_sha256=t.record_sha256
                    JOIN news_content_tag_releases AS r
                      ON t.tenant_id=r.tenant_id AND t.content_release_id=r.content_release_id
                     AND t.tag_release_id=r.tag_release_id
                    WHERE r.status='frozen'
                      AND c.identity_sha256=r.content_identity_sha256
                """))

    def _require_release(self, connection: Connection, tenant_id: str,
                         content_release_id: str, tag_release_id: str):
        _identity(tag_release_id, "tag_release_id", 160)
        content = self.content_store._require_release(connection, tenant_id, content_release_id)
        row = connection.execute(select(tag_releases).where(
            _scope(tag_releases, tenant_id, content_release_id, tag_release_id),
            tag_releases.c.status == "frozen",
        )).mappings().first()
        if row is None:
            raise ValueError("unknown frozen tag release for tenant/content release")
        if any(row[key] != row["manifest"].get(key) for key in _MANIFEST_COLUMNS):
            raise ValueError("stored tag release metadata differs from manifest")
        if row["content_identity_sha256"] != content["identity_sha256"]:
            raise ValueError("stored tag release content identity mismatch")
        if (row["policy"].get("policy_version") != row["policy_version"]
                or row["policy"].get("catalog", {}).get("taxonomy_version") != row["taxonomy_version"]):
            raise ValueError("stored tag policy metadata differs from manifest")
        return row

    def _read_package(self, connection: Connection, tenant_id: str,
                      content_release_id: str, tag_release_id: str) -> dict:
        release = self._require_release(connection, tenant_id, content_release_id, tag_release_id)
        labels = [_read_label(row) for row in connection.execute(select(tags).where(
            _scope(tags, tenant_id, content_release_id, tag_release_id),
        ).order_by(tags.c.news_id)).mappings()]
        memberships = [(row.news_id, row.topic_id) for row in connection.execute(
            select(topic_memberships.c.news_id, topic_memberships.c.topic_id).where(
                _scope(topic_memberships, tenant_id, content_release_id, tag_release_id),
            ).order_by(topic_memberships.c.news_id, topic_memberships.c.topic_id))]
        expected = sorted((row["news_id"], topic_id) for row in labels for topic_id in row["topic_ids"])
        if memberships != expected:
            raise ValueError("stored topic memberships differ from labels")
        return {"manifest": release["manifest"], "policy": release["policy"], "labels": labels}

    def import_release(self, package: dict) -> dict:
        from app.modules.news_storage.topic_labels import validate_tag_package

        package = json.loads(_canonical(package))
        validate_tag_package(package)
        manifest = package["manifest"]
        tenant = manifest["tenant_id"]
        content_release = manifest["content_release_id"]
        tag_release = manifest["tag_release_id"]
        created = False
        try:
            with self.engine.begin() as connection:
                content_package = self.content_store._read_package(connection, tenant, content_release)
                validate_tag_package(package, content_package)
                existing = connection.execute(select(tag_releases).where(
                    _scope(tag_releases, tenant, content_release, tag_release),
                )).mappings().first()
                if existing is not None:
                    if (existing["manifest"] != manifest or existing["policy"] != package["policy"]
                            or existing["status"] != "frozen"):
                        raise ValueError("tag release identity already has different contents")
                else:
                    connection.execute(insert(tag_releases), {
                        **{key: manifest[key] for key in _MANIFEST_COLUMNS},
                        "status": "frozen", "manifest": manifest,
                        "policy": package["policy"], "created_at": datetime.now(UTC),
                    })
                    scope = {"tenant_id": tenant, "content_release_id": content_release,
                             "tag_release_id": tag_release}
                    _batch_insert(connection, tags, [
                        {**scope, **{key: row[key] for key in _LABEL_COLUMNS}, "label": row}
                        for row in package["labels"]
                    ])
                    _batch_insert(connection, topic_memberships, [
                        {**scope, "news_id": row["news_id"], "topic_id": topic_id}
                        for row in package["labels"] for topic_id in row["topic_ids"]
                    ])
                    created = True
                # Verify persisted labels, membership projection and immutable
                # content binding before publishing the atomic transaction.
                validate_tag_package(self._read_package(connection, tenant, content_release, tag_release),
                                     content_package, verify_derivation=False)
        except IntegrityError as exc:
            raise ValueError("tag release conflicts with immutable storage") from exc
        result = self.summary(tenant, content_release, tag_release)
        result["import_status"] = "created" if created else "already_present"
        return result

    def _query(self, tenant_id: str, content_release_id: str, tag_release_id: str):
        return self.content_store._selected_query(tenant_id, content_release_id).join(tags, and_(
            tags.c.tenant_id == items.c.tenant_id,
            tags.c.content_release_id == items.c.release_id,
            tags.c.news_id == items.c.news_id,
            tags.c.record_sha256 == items.c.record_sha256,
            tags.c.tag_release_id == tag_release_id,
        )).add_columns(tags.c.label, *(
            tags.c[key].label("tag_" + key) for key in _LABEL_COLUMNS
        ))

    def _combine(self, connection, rows, release) -> list[dict]:
        from app.modules.behavior_tags.tagging import TopicPolicy
        from app.modules.news_storage.topic_labels import validate_label

        tenant, content_release, tag_release = (release[key] for key in (
            "tenant_id", "content_release_id", "tag_release_id"))
        if not rows:
            return []
        policy = TopicPolicy.model_validate(release["policy"])
        topic_names = {topic.topic_id: topic.name for topic in policy.catalog.topics}
        members = {}
        ids = [row["news_id"] for row in rows]
        for start in range(0, len(ids), 500):
            for member in connection.execute(select(topic_memberships).where(
                _scope(topic_memberships, tenant, content_release, tag_release),
                topic_memberships.c.news_id.in_(ids[start:start + 500]),
            )).mappings():
                members.setdefault(member["news_id"], []).append(member["topic_id"])
        result = self.content_store._with_evidence(connection, rows, tenant, content_release)
        for content, row in zip(result, rows, strict=True):
            label = _read_label({"label": row["label"], **{
                key: row["tag_" + key] for key in _LABEL_COLUMNS
            }})
            if any(label[key] != content[key] for key in ("news_id", "record_sha256", "body_sha256")):
                raise ValueError("stored label/content binding mismatch")
            validate_label(label, policy, content, {"quality_flags": content["selection_quality_flags"]})
            if sorted(members.get(content["news_id"], [])) != sorted(label["topic_ids"]):
                raise ValueError("stored topic memberships differ from labels")
            content["tags"] = {**label, "tag_release_id": tag_release,
                               "policy_version": release["policy_version"],
                               "taxonomy_version": release["taxonomy_version"],
                               "topic_names": [topic_names[topic] for topic in label["topic_ids"]]}
        return result

    def get_record(self, tenant_id: str, content_release_id: str,
                   tag_release_id: str, news_id: str) -> dict | None:
        _identity(news_id, "news_id", 160)
        with self.engine.connect() as connection:
            release = self._require_release(connection, tenant_id, content_release_id, tag_release_id)
            rows = connection.execute(self._query(tenant_id, content_release_id, tag_release_id).where(
                versions.c.news_id == news_id,
            )).mappings().all()
            if not rows:
                selected = connection.execute(select(items.c.news_id).where(
                    items.c.tenant_id == tenant_id, items.c.release_id == content_release_id,
                    items.c.news_id == news_id,
                )).first()
                if selected:
                    raise ValueError("stored label missing or bound to wrong selected record")
                return None
            return self._combine(connection, rows, release)[0]

    def list_records(self, tenant_id: str, content_release_id: str, tag_release_id: str,
                     after_news_id: str = "", limit: int = 100, body_only: bool = False,
                     topic_ids: tuple[str, ...] = ()) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if not isinstance(after_news_id, str) or len(after_news_id) > 160:
            raise ValueError("invalid after_news_id")
        if type(body_only) is not bool:
            raise ValueError("body_only must be a boolean")
        if not isinstance(topic_ids, (list, tuple)) or len(topic_ids) > 512:
            raise ValueError("topic_ids must be a bounded list or tuple")
        for topic_id in topic_ids:
            _identity(topic_id, "topic_id", 128)
        with self.engine.connect() as connection:
            release = self._require_release(connection, tenant_id, content_release_id, tag_release_id)
            query = self._query(tenant_id, content_release_id, tag_release_id).where(
                versions.c.news_id > after_news_id,
            )
            if body_only:
                query = query.where(versions.c.content_state == "body_available")
            if topic_ids:
                query = query.where(exists(select(1).select_from(topic_memberships).where(
                    _scope(topic_memberships, tenant_id, content_release_id, tag_release_id),
                    topic_memberships.c.news_id == versions.c.news_id,
                    topic_memberships.c.topic_id.in_(tuple(set(topic_ids))),
                )))
            rows = connection.execute(query.order_by(versions.c.news_id).limit(limit)).mappings().all()
            return self._combine(connection, rows, release)

    def summary(self, tenant_id: str, content_release_id: str, tag_release_id: str) -> dict:
        with self.engine.connect() as connection:
            release = self._require_release(connection, tenant_id, content_release_id, tag_release_id)
            counts = release["manifest"]["counts"]
            total = connection.execute(select(func.count()).select_from(tags).where(
                _scope(tags, tenant_id, content_release_id, tag_release_id),
            )).scalar_one()
            memberships = connection.execute(select(func.count()).select_from(topic_memberships).where(
                _scope(topic_memberships, tenant_id, content_release_id, tag_release_id),
            )).scalar_one()
            if total != counts["news_count"] or memberships != counts["topic_memberships_count"]:
                raise ValueError("stored tag release counts differ from manifest")
            return {**{key: release[key] for key in _MANIFEST_COLUMNS},
                    "status": release["status"], "counts": counts,
                    "legacy_source": release["manifest"]["legacy_source"]}

    def get_topics(self, tenant_id: str, content_release_id: str, tag_release_id: str) -> dict:
        """Return the exact catalogue and counts of this explicitly chosen release."""
        with self.engine.connect() as connection:
            release = self._require_release(connection, tenant_id, content_release_id, tag_release_id)
            counts = dict(connection.execute(select(
                topic_memberships.c.topic_id, func.count(),
            ).where(_scope(topic_memberships, tenant_id, content_release_id, tag_release_id)).group_by(
                topic_memberships.c.topic_id,
            )).all())
            catalog = release["policy"]["catalog"]
            return {"tenant_id": tenant_id, "content_release_id": content_release_id,
                    "tag_release_id": tag_release_id, "policy_version": release["policy_version"],
                    "catalog": catalog,
                    "topic_counts": {topic["topic_id"]: counts.get(topic["topic_id"], 0)
                                     for topic in catalog["topics"]}}
