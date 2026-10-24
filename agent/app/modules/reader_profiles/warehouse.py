"""Frozen synthetic reader aggregates projected into explicit PostgreSQL tables.

No application Settings, enterprise personal events, model calls, or implicit latest.
The importer streams existing anonymous aggregates, then verifies every stored row.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.modules.reader_profiles.build import _hash_file, _iso, _json, _loads, _time
from app.modules.reader_profiles.contracts import (
    IDENTITY_POLICY_VERSION, READER_DEFINITION, ReaderProfileSummary,
)
from app.modules.reader_profiles.local import LocalNewsReaderProfileClient

SCHEMA = "news_audience"
VERSION = "news-reader-audience-v1"
COLUMNS = {
    "content_binding": "tenant_id source_release_sha256 news_id content_sha256 content_topic_ids binding".split(),
    "reader_snapshot": ("tenant_id snapshot_id window_start window_end profile_cutoff knowledge_as_of "
        "knowledge_as_of_basis history_start source_revision source_release_sha256 source_mode taxonomy_version "
        "profile_policy_version identity_policy_version reader_definition count_method generated_at watermark_at "
        "completeness quality_status quality_basis expires_at min_support").split(),
    "topic_dictionary": "taxonomy_version topic_id topic_name parent_topic_id topic_level".split(),
    "news_reader_population": ("tenant_id snapshot_id news_id lookback_hours content_sha256 source_has_body_hash "
        "reader_count profiled_reader_count unknown_reader_count cold_start_reader_count baseline_profiled_count "
        "status trigger_article_excluded limitations source_payload_sha256 source_topic_limit topics_are_top_k "
        "source_topic_count").split(),
    "news_reader_topic": ("tenant_id snapshot_id news_id lookback_hours topic_id topic_reader_count "
        "baseline_topic_count taxonomy_version source_rank").split(),
}
JSON_COLUMNS = {"binding", "content_topic_ids"}


def snapshot_id(release, tenant, start, end, revision):
    """Identical to the immutable local reader client's snapshot identity."""
    return sha256(_json(dict(release=release, tenant=str(tenant), start=_iso(start),
                            end=_iso(end), revision=revision)).encode()).hexdigest()


def _plain(value):
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _digest_row(digest, row, columns):
    digest.update((_json([_plain(row[col]) for col in columns]) + "\n").encode())


class ReaderWarehouseSource:
    def __init__(self, package_path):
        self.client = LocalNewsReaderProfileClient(package_path, environment="local")
        self.manifest, self.policy = self.client.manifest, self.client.policy
        self.release = self.client.source_release_sha256
        self.catalog = self.client.catalog
        self.topic_limit = self.manifest["top_k"]
        if (type(self.topic_limit) is not int or not 1 <= self.topic_limit <= 50
                or self.policy.get("top_k") != self.topic_limit
                or self.policy.get("minimum_lift_sample") != 5):
            raise ValueError("unsupported source TopK or support contract")
        self.bindings, self.snapshots = {}, {}
        with self.client._connect() as conn:
            for tenant, news, payload in conn.execute("SELECT * FROM contents ORDER BY tenant_id,news_id"):
                if tenant not in self.manifest["tenants"] or not news:
                    raise ValueError("invalid source content identity")
                binding = _loads(payload)
                if not isinstance(binding, dict) or set(binding) != {"content_sha256", "topic_ids"}:
                    raise ValueError("source content binding contains unsupported fields")
                self.catalog.validate_ids(binding["topic_ids"])
                body_hash = binding.get("content_sha256")
                if body_hash is not None and (len(body_hash) != 64 or any(c not in "0123456789abcdef" for c in body_hash)):
                    raise ValueError("invalid source content hash")
                self.bindings[tenant, news] = binding
            for tenant, start, end, payload in conn.execute("SELECT * FROM coverage ORDER BY tenant_id,window_start,window_end"):
                value = _loads(payload)
                start, end = _iso(start), _iso(end)
                if (tenant not in self.manifest["tenants"] or value.get("complete") is not True
                        or value.get("source_mode") != "synthetic"
                        or value.get("window_start") != start or value.get("window_end") != end
                        or _time(end) - _time(start) != timedelta(hours=self.manifest["window_hours"])
                        or _time(start) < _time(self.manifest["coverage_start"])
                        or _time(end) > _time(self.manifest["coverage_end"])
                        or any(x.minute or x.second or x.microsecond for x in (_time(start), _time(end)))):
                    raise ValueError("invalid source coverage")
                self.snapshots[tenant, start, end] = self._snapshot(tenant, start, end)
        if (len(self.bindings) != self.manifest["counts"]["content_bindings"]
                or len(self.snapshots) != self.manifest["counts"]["covered_windows"]):
            raise ValueError("source binding/coverage count mismatch")

    def _snapshot(self, tenant, start, end):
        m = self.manifest
        return dict(tenant_id=tenant, snapshot_id=snapshot_id(self.release, tenant, start, end, m["snapshot_revision"]),
            window_start=start, window_end=end, profile_cutoff=start,
            knowledge_as_of=_iso(m["generated_at"]), knowledge_as_of_basis="synthetic_package_frozen_at",
            history_start=_iso(m["coverage_start"]), source_revision=m["snapshot_revision"],
            source_release_sha256=self.release, source_mode="synthetic", taxonomy_version=m["taxonomy_version"],
            profile_policy_version=m["profile_policy_version"], identity_policy_version=IDENTITY_POLICY_VERSION,
            reader_definition=READER_DEFINITION, count_method="exact_synthetic_distinct",
            generated_at=_iso(m["generated_at"]), watermark_at=_iso(m["source_watermark"]),
            completeness="complete", quality_status="passed", quality_basis="local_import_contract_validation",
            expires_at=None, min_support=5)

    def dictionary_rows(self):
        topics = {t.topic_id: t for t in self.catalog.topics}
        def level(topic):
            return 1 if topic.parent_topic_id is None else 1 + level(topics[topic.parent_topic_id])
        return sorted((dict(taxonomy_version=self.catalog.taxonomy_version, topic_id=t.topic_id,
            topic_name=t.name, parent_topic_id=t.parent_topic_id, topic_level=level(t))
            for t in topics.values()), key=lambda r: (r["topic_level"], r["topic_id"]))

    def binding_rows(self):
        for (tenant, news), binding in sorted(self.bindings.items()):
            yield dict(tenant_id=tenant, source_release_sha256=self.release, news_id=news,
                content_sha256=binding.get("content_sha256"), content_topic_ids=binding["topic_ids"], binding=binding)

    def records(self):
        count = 0
        with self.client._connect() as conn:
            for tenant, news, start, end, raw in conn.execute(
                    "SELECT * FROM reader_profiles ORDER BY tenant_id,news_id,window_start,window_end"):
                count += 1
                start, end = _iso(start), _iso(end)
                snapshot = self.snapshots.get((tenant, start, end))
                binding = self.bindings.get((tenant, news))
                item = ReaderProfileSummary.model_validate(_loads(raw))
                if (snapshot is None or binding is None or item.news_id != news
                        or item.content_sha256 != binding.get("content_sha256")
                        or list(item.content_topic_ids) != binding["topic_ids"]
                        or item.profile_cutoff != _time(start) or not item.trigger_article_excluded
                        or item.source_mode != "synthetic" or item.count_method != "exact_synthetic_distinct"
                        or item.identity_policy_version != IDENTITY_POLICY_VERSION
                        or item.reader_definition != READER_DEFINITION
                        or item.taxonomy_version != self.catalog.taxonomy_version
                        or item.profile_policy_version != self.manifest["profile_policy_version"]
                        or item.long_term.lookback_hours != 720 or item.short_term.lookback_hours != 24):
                    raise ValueError("source reader identity/content/causality mismatch")
                for dimension in (item.long_term, item.short_term):
                    self.catalog.validate_ids(tuple(t.topic_id for t in dimension.topics))
                    if len(dimension.topics) > self.topic_limit:
                        raise ValueError("source topic count exceeds frozen limit")
                    ordering = [(-t.reader_count, t.topic_id) for t in dimension.topics]
                    if ordering != sorted(ordering):
                        raise ValueError("source topics do not follow the frozen TopK order")
                yield tenant, news, snapshot, item, sha256(raw.encode()).hexdigest()
        if count != self.manifest["counts"]["reader_profiles"]:
            raise ValueError("source reader count mismatch")

    def population_rows(self):
        for tenant, news, snap, item, payload_sha in self.records():
            for dimension in (item.long_term, item.short_term):
                limits = list(item.limitations)
                if item.reader_count and item.profile_cutoff - timedelta(hours=dimension.lookback_hours) < _time(self.manifest["coverage_start"]):
                    limits.append("history is truncated at package coverage start; lookback is a maximum")
                limits.append(f"source retains up to {self.topic_limit} topics; omitted topics are not zero")
                yield dict(tenant_id=tenant, snapshot_id=snap["snapshot_id"], news_id=news,
                    lookback_hours=dimension.lookback_hours, content_sha256=item.content_sha256,
                    source_has_body_hash=item.content_sha256 is not None, reader_count=item.reader_count,
                    profiled_reader_count=dimension.profiled_reader_count,
                    unknown_reader_count=dimension.unknown_reader_count,
                    cold_start_reader_count=dimension.cold_start_reader_count,
                    baseline_profiled_count=dimension.baseline_profiled_count, status=item.status,
                    trigger_article_excluded=item.trigger_article_excluded, limitations=limits,
                    source_payload_sha256=payload_sha, source_topic_limit=self.topic_limit,
                    topics_are_top_k=True, source_topic_count=len(dimension.topics))

    def topic_rows(self):
        for tenant, news, snap, item, _ in self.records():
            for dimension in (item.long_term, item.short_term):
                for rank, topic in enumerate(dimension.topics, 1):
                    yield dict(tenant_id=tenant, snapshot_id=snap["snapshot_id"], news_id=news,
                        lookback_hours=dimension.lookback_hours, topic_id=topic.topic_id,
                        topic_reader_count=topic.reader_count, baseline_topic_count=topic.baseline_reader_count,
                        taxonomy_version=item.taxonomy_version, source_rank=rank)

    def verify_unchanged(self):
        if _hash_file(self.client.directory / "manifest.json") != self.release:
            raise ValueError("source manifest changed during import")
        for name, spec in self.manifest["artifacts"].items():
            if _hash_file(self.client.directory / name) != spec["sha256"]:
                raise ValueError("source artifact changed during import")


def _initialize(conn):
    exists = conn.execute("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=%s) AS present", (SCHEMA,)).fetchone()["present"]
    if not exists:
        conn.execute(Path(__file__).with_suffix(".sql").read_text(), prepare=False)
    else:
        comment = conn.execute("SELECT obj_description(oid,'pg_namespace') AS value FROM pg_namespace WHERE nspname=%s", (SCHEMA,)).fetchone()["value"]
        if not comment or not comment.startswith(VERSION + ":"):
            raise ValueError("refuse to reuse an unknown audience schema")
        expected_columns = dict(COLUMNS,
            package_release="source_release_sha256 manifest policy schema_version row_counts projection_digests imported_at".split(),
            unified_news_binding="tenant_id source_release_sha256 news_id content_tenant_id content_release_id record_sha256 body_sha256 binding_method bound_at".split())
        for table, columns in expected_columns.items():
            actual = {r["column_name"] for r in conn.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (SCHEMA, table))}
            if actual != set(columns):
                raise ValueError(f"audience schema column mismatch: {table}")
        actual_types = {(r["table_name"], r["column_name"]): r["udt_name"] for r in conn.execute(
            "SELECT table_name,column_name,udt_name FROM information_schema.columns WHERE table_schema=%s", (SCHEMA,))}
        for table, field, expected_type in [
            ("reader_snapshot", "tenant_id", "uuid"), ("reader_snapshot", "window_start", "timestamptz"),
            ("reader_snapshot", "window_end", "timestamptz"), ("reader_snapshot", "source_revision", "int8"),
            ("news_reader_population", "reader_count", "int8"), ("news_reader_population", "lookback_hours", "int4"),
            ("news_reader_topic", "topic_reader_count", "int8"), ("package_release", "manifest", "jsonb"),
            ("package_release", "projection_digests", "jsonb")]:
            if actual_types.get((table, field)) != expected_type:
                raise ValueError(f"audience schema type mismatch: {table}.{field}")
        views = {r["viewname"] for r in conn.execute("SELECT viewname FROM pg_views WHERE schemaname=%s", (SCHEMA,))}
        if not {"v_news_reader_topic_metrics", "v_news_reader_population_status"}.issubset(views):
            raise ValueError("audience query views missing")
        if conn.execute("SELECT 1 FROM news_audience.package_release WHERE schema_version<>%s LIMIT 1", (VERSION,)).fetchone():
            raise ValueError("audience schema version mismatch")


def _copy(conn, table, rows):
    columns = COLUMNS[table]
    digest, count = sha256(), 0
    statement = sql.SQL("COPY {}.{} ({}) FROM STDIN").format(sql.Identifier(SCHEMA), sql.Identifier(table),
        sql.SQL(",").join(map(sql.Identifier, columns)))
    with conn.cursor().copy(statement) as writer:
        for row in rows:
            _digest_row(digest, row, columns)
            writer.write_row([Jsonb(row[c]) if c in JSON_COLUMNS else row[c] for c in columns])
            count += 1
    return count, digest.hexdigest()


def _read_projection(conn, table, release, taxonomy):
    cols = COLUMNS[table]
    if table == "topic_dictionary":
        tail, params = 'WHERE t.taxonomy_version=%s ORDER BY t.topic_level,t.topic_id COLLATE "C"', (taxonomy,)
    elif table in {"content_binding", "reader_snapshot"}:
        ordering = 't.tenant_id,t.news_id COLLATE "C"' if table == "content_binding" else "t.tenant_id,t.window_start,t.window_end"
        tail, params = f"WHERE t.source_release_sha256=%s ORDER BY {ordering}", (release,)
    else:
        ordering = 't.tenant_id,t.news_id COLLATE "C",s.window_start,s.window_end,t.lookback_hours DESC'
        if table == "news_reader_topic":
            ordering += ",t.source_rank"
        tail = "JOIN news_audience.reader_snapshot s ON s.tenant_id=t.tenant_id AND s.snapshot_id=t.snapshot_id WHERE s.source_release_sha256=%s ORDER BY " + ordering
        params = (release,)
    query = "SELECT " + ",".join("t." + c for c in cols) + f" FROM news_audience.{table} t " + tail
    digest, count = sha256(), 0
    with conn.cursor(name="verify_" + table) as reader:
        reader.itersize = 5000
        reader.execute(query, params)
        for row in reader:
            _digest_row(digest, row, cols)
            count += 1
    return count, digest.hexdigest()


def verify_projection(conn, source):
    release = conn.execute("SELECT * FROM news_audience.package_release WHERE source_release_sha256=%s", (source.release,)).fetchone()
    if not release or release["manifest"] != source.manifest or release["policy"] != source.policy or release["schema_version"] != VERSION:
        raise ValueError("stored package identity mismatch")
    for table in COLUMNS:
        count, digest = _read_projection(conn, table, source.release, source.catalog.taxonomy_version)
        if count != release["row_counts"].get(table) or digest != release["projection_digests"].get(table):
            raise ValueError(f"stored projection changed: {table}")
    return release["row_counts"]


def import_package(dsn, package_path, progress=lambda message: None):
    source = ReaderWarehouseSource(package_path)
    progress("source hashes verified")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
        # Serialize schema initialization and immutable publication; no unrelated tables are locked.
        conn.execute("SELECT pg_advisory_xact_lock(237461987)")
        _initialize(conn)
        old = conn.execute("SELECT 1 FROM news_audience.package_release WHERE source_release_sha256=%s", (source.release,)).fetchone()
        if old:
            counts = verify_projection(conn, source)
            source.verify_unchanged()
            return dict(status="already_imported_verified", source_release_sha256=source.release, row_counts=counts)
        conn.execute("INSERT INTO news_audience.package_release(source_release_sha256,manifest,policy,schema_version) VALUES(%s,%s,%s,%s)",
                     (source.release, Jsonb(source.manifest), Jsonb(source.policy), VERSION))
        rows = source.dictionary_rows()
        existing = conn.execute('SELECT * FROM news_audience.topic_dictionary WHERE taxonomy_version=%s ORDER BY topic_level,topic_id COLLATE "C"', (source.catalog.taxonomy_version,)).fetchall()
        if existing and existing != rows:
            raise ValueError("immutable topic dictionary conflicts with existing version")
        counts, digests = {}, {}
        if existing:
            counts["topic_dictionary"], digests["topic_dictionary"] = _read_projection(conn, "topic_dictionary", source.release, source.catalog.taxonomy_version)
        else:
            counts["topic_dictionary"], digests["topic_dictionary"] = _copy(conn, "topic_dictionary", rows)
        for table, iterator in [("content_binding", source.binding_rows()),
                                ("reader_snapshot", (source.snapshots[k] for k in sorted(source.snapshots))),
                                ("news_reader_population", source.population_rows()),
                                ("news_reader_topic", source.topic_rows())]:
            counts[table], digests[table] = _copy(conn, table, iterator)
            progress(f"{table}: {counts[table]} rows copied")
        if counts["news_reader_population"] != 2 * source.manifest["counts"]["reader_profiles"]:
            raise ValueError("population count mismatch")
        conn.execute("UPDATE news_audience.package_release SET row_counts=%s,projection_digests=%s WHERE source_release_sha256=%s",
                     (Jsonb(counts), Jsonb(digests), source.release))
        verify_projection(conn, source)
        source.verify_unchanged()
        progress("all stored projection rows verified; committing")
    return dict(status="imported_verified", source_release_sha256=source.release, row_counts=counts,
                projection_digests=digests, source_mode="synthetic")


def bind_unified_content(dsn, release, content_tenant, content_release):
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(237461987)")
        if not conn.execute("SELECT 1 FROM news_audience.package_release WHERE source_release_sha256=%s", (release,)).fetchone():
            raise ValueError("unknown reader release")
        if not conn.execute("SELECT 1 FROM news_content_releases WHERE tenant_id=%s AND release_id=%s AND status='frozen'", (content_tenant, content_release)).fetchone():
            raise ValueError("unknown frozen news release")
        expected = conn.execute("""SELECT b.tenant_id,b.source_release_sha256,b.news_id,
            c.tenant_id AS content_tenant_id,c.release_id AS content_release_id,c.record_sha256,c.body_sha256,
            'news_id_and_body_sha256' AS binding_method
            FROM news_audience.content_binding b JOIN news_content_selected c
              ON c.news_id=b.news_id AND c.body_sha256=b.content_sha256
            WHERE b.source_release_sha256=%s AND c.tenant_id=%s AND c.release_id=%s
              AND b.content_sha256 IS NOT NULL ORDER BY b.tenant_id,b.news_id""", (release, content_tenant, content_release)).fetchall()
        cols = ["tenant_id", "source_release_sha256", "news_id", "content_tenant_id", "content_release_id", "record_sha256", "body_sha256", "binding_method"]
        old = conn.execute("SELECT " + ",".join(cols) + " FROM news_audience.unified_news_binding WHERE source_release_sha256=%s AND content_tenant_id=%s AND content_release_id=%s ORDER BY tenant_id,news_id", (release, content_tenant, content_release)).fetchall()
        if old:
            if old != expected:
                raise ValueError("existing unified content binding differs")
        elif expected:
            statement = "INSERT INTO news_audience.unified_news_binding(" + ",".join(cols) + ") VALUES(" + ",".join(["%s"] * len(cols)) + ")"
            with conn.cursor() as cursor:
                cursor.executemany(statement, [[row[c] for c in cols] for row in expected])
        return dict(status="already_bound_verified" if old else "bound", matched_business_news=len(expected),
                    distinct_news=len({r["news_id"] for r in expected}), source_release_sha256=release,
                    content_tenant_id=content_tenant, content_release_id=content_release)


def query_audience(dsn, *, release, tenant, start, end, news_ids, lookback_hours=720, top_k=5):
    tenant = str(UUID(str(tenant)))
    start, end = _time(start), _time(end)
    if lookback_hours not in (24, 720) or type(top_k) is not int or not 1 <= top_k <= 50:
        raise ValueError("invalid lookback or TopK")
    if not 1 <= len(news_ids) <= 20 or len(set(news_ids)) != len(news_ids):
        raise ValueError("request requires 1..20 distinct news IDs")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        snap = conn.execute("SELECT * FROM news_audience.reader_snapshot WHERE tenant_id=%s AND source_release_sha256=%s AND window_start=%s AND window_end=%s", (tenant, release, start, end)).fetchone()
        if not snap or snap["completeness"] not in ("complete", "corrected") or snap["quality_status"] != "passed" or snap["watermark_at"] < end:
            raise ValueError("unavailable reader window")
        if snap["source_mode"] != "synthetic":
            raise ValueError("this local query tool only supports frozen synthetic releases")
        results = []
        for news in news_ids:
            binding = conn.execute("SELECT * FROM news_audience.content_binding WHERE tenant_id=%s AND source_release_sha256=%s AND news_id=%s", (tenant, release, news)).fetchone()
            if not binding:
                results.append(dict(news_id=news, status="unavailable", reason="news_not_bound_to_source", reader_count=None, topics=[]))
                continue
            pop = conn.execute("SELECT * FROM news_audience.v_news_reader_population_status WHERE tenant_id=%s AND snapshot_id=%s AND news_id=%s AND lookback_hours=%s", (tenant, snap["snapshot_id"], news, lookback_hours)).fetchone()
            if not pop:
                results.append(dict(news_id=news, status="empty", reader_count=0, lookback_hours=lookback_hours,
                    topics=[], reason="known_source_news_in_complete_window_without_visits",
                    limitations=["empty reader cohort; baseline not computed"]))
                continue
            topics = conn.execute("SELECT topic_id,topic_name,topic_level,topic_reader_count,share_all,share_profiled,baseline_topic_count,baseline_share,lift,low_sample,source_rank FROM news_audience.v_news_reader_topic_metrics WHERE tenant_id=%s AND snapshot_id=%s AND news_id=%s AND lookback_hours=%s ORDER BY source_rank LIMIT %s", (tenant, snap["snapshot_id"], news, lookback_hours, top_k)).fetchall()
            results.append(dict(pop, topics=topics))
        return dict(snapshot=snap, items=results)
