"""Merge versioned fixture worlds without summing their incompatible metrics.

Real article identities share content only when their verified bodies agree.
Synthetic titles are namespaced by scenario. Every observation retains its
dataset, scenario and metric-definition identity; gaps remain explicit.
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

from app.modules.behavior_tags.source import (
    MAX_FILE_BYTES, MAX_LINE_BYTES, MAX_TABLE_ROWS, TZ, WarehouseSource, WarehouseSourceError,
    _canonical, _digest, _hour, _integer, _json, _regular, _tenant, _text, _time,
    load_warehouse_source,
)

MERGE_VERSION = "behavior-source-merge-v1"
MAX_SOURCES = 10
MAX_MERGED_OBSERVATIONS = 1_000_000
PROFILE_NAMES = ("classic-v1", "enterprise-v1", "enterprise-v2", "public-headlines-v3",
                 "timeline-v4", "timeline-v5", "corpus-v6")
BASE_FIELDS = ("baseline_impressions", "baseline_clicks", "baseline_effective_consumptions", "baseline_interactions")
RICH_FIELDS = ("detail_page_views", "direct_page_views", "detail_visitors", "likes",
               "comments", "shares", "favorites", "negative_feedback")
CORE_COUNTS = ("impressions", "clicks", "unique_users", "effective_consumptions", "interactions")
_V7_COUNTS = (*CORE_COUNTS, "total_duration_seconds", "likes", "comments", "shares",
              "favorites", "follows", "negative_feedback", "video_starts", "video_completions")


def _plain(value):
    if isinstance(value, (datetime, UUID)):
        return value.isoformat() if isinstance(value, datetime) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _bounded_manifest(directory):
    path = directory / "manifest.json"
    _regular(path, 1_048_576)
    raw = path.read_bytes()
    if len(raw) > 1_048_576:
        raise WarehouseSourceError("manifest exceeds its budget")
    manifest = _json(raw)
    unsigned = dict(manifest)
    expected = _digest(unsigned.pop("dataset_sha256", None), "dataset")
    if sha256(_canonical(unsigned)).hexdigest() != expected:
        raise WarehouseSourceError("manifest dataset SHA-256 mismatch")
    return manifest, sha256(raw).hexdigest()


def _v7_rows(directory, manifest, table):
    spec = manifest.get("tables", {}).get(table, {})
    expected = _digest(spec.get("sha256"), table)
    count = _integer(spec.get("rows"), table + " row count", maximum=MAX_TABLE_ROWS)
    path = directory / (table + ".jsonl")
    _regular(path, MAX_FILE_BYTES)
    digest, seen, size = sha256(), 0, 0
    with path.open("rb") as stream:
        while raw := stream.readline(MAX_LINE_BYTES + 1):
            size += len(raw)
            seen += 1
            if len(raw) > MAX_LINE_BYTES or size > MAX_FILE_BYTES or seen > MAX_TABLE_ROWS:
                raise WarehouseSourceError("v7 source exceeds read budget")
            digest.update(raw)
            yield _json(raw)
    if seen != count or digest.hexdigest() != expected:
        raise WarehouseSourceError(table + " row count or SHA-256 mismatch")


def _keys(row, fields):
    if set(row) != set(fields.split()):
        raise WarehouseSourceError("v7 row has unexpected or missing fields")


def _load_v7(directory, *, tenant_ids=None, as_of=None):
    manifest, manifest_hash = _bounded_manifest(directory)
    if manifest.get("schema") != "news_sim_v7" or manifest.get("synthetic") is not True:
        raise WarehouseSourceError("only the frozen v7 synthetic contract is supported")
    version = _text(manifest.get("dataset_version"), "dataset_version")
    tenants = tuple(_tenant(value) for value in manifest.get("tenants", ()))
    selected = _select_tenants(tenants, tenant_ids)
    start = _hour(manifest.get("historical_window_start"), "historical start")
    end = _hour(manifest.get("historical_window_end"), "historical end")
    if not start < end or end - start > timedelta(days=31):
        raise WarehouseSourceError("invalid v7 coverage")
    articles = {}
    for row in _v7_rows(directory, manifest, "dim_news"):
        _keys(row, "category content_format content_source content_type content_version event_id lifecycle_revision news_id primary_topic publish_time publisher_id source status status_effective_at synthetic tenant_id title")
        key = _tenant(row["tenant_id"]), _text(row["news_id"], "news_id", 160)
        if key in articles or key[0] not in tenants or row["synthetic"] is not True:
            raise WarehouseSourceError("duplicate/unknown v7 article identity")
        if row["status"] not in {"published", "withdrawn", "restricted"} or row["content_type"] not in {"article", "video"}:
            raise WarehouseSourceError("invalid v7 content status/type")
        published = _time(row["publish_time"], "publish_time")
        if published > end or _time(row["status_effective_at"], "status_effective_at") < published:
            raise WarehouseSourceError("invalid v7 publication/lifecycle time")
        _integer(row["content_version"], "content_version", 1)
        _integer(row["lifecycle_revision"], "lifecycle_revision", 1)
        articles[key] = {**row, "content": "", "content_sha256": sha256(b"").hexdigest(),
            "source_content_version": row["content_version"], "source_url": "",
            "category_data_synthetic": True, "news_content_synthetic": True,
            "metric_data_synthetic": True, "content_scope": "synthetic_title_only"}
    snapshots, policy_versions = {}, set()
    for row in _v7_rows(directory, manifest, "window_snapshot"):
        _keys(row, "completeness dataset_version event_time late_added_clicks late_corrected_bins metric_policy_version quality_status snapshot_at snapshot_revision synthetic tenant_id watermark_at window_end")
        tenant, stamp = _tenant(row["tenant_id"]), _hour(row["event_time"], "event_time")
        revision = _integer(row["snapshot_revision"], "snapshot_revision", 1)
        key = tenant, stamp, revision
        watermark, observed = _time(row["watermark_at"], "watermark_at"), _time(row["snapshot_at"], "snapshot_at")
        if (tenant not in tenants or key in snapshots or not start <= stamp <= end
                or _time(row["window_end"], "window_end") != stamp + timedelta(hours=1)
                or not stamp <= watermark <= observed or row["synthetic"] is not True
                or row["dataset_version"] != version or row["completeness"] not in {"complete", "corrected", "incomplete"}
                or row["quality_status"] not in {"passed", "pending"}):
            raise WarehouseSourceError("invalid v7 snapshot identity/time/provenance")
        policy_versions.add(_text(row["metric_policy_version"], "metric_policy_version"))
        snapshots[key] = row
    if not snapshots or len(policy_versions) != 1:
        raise WarehouseSourceError("missing or mixed v7 policy")
    policy = next(iter(policy_versions))
    frozen_asof = max(_time(row["snapshot_at"], "snapshot_at") for row in snapshots.values())
    cutoff = frozen_asof if as_of is None else _time(as_of, "as_of")
    if not start < cutoff <= frozen_asof:
        raise WarehouseSourceError("as_of outside frozen v7 coverage")
    end = min(end, cutoff.replace(minute=0, second=0, microsecond=0))
    if end <= start:
        raise WarehouseSourceError("v7 cutoff has no closed hour")
    latest = {}
    for (tenant, stamp, revision), row in snapshots.items():
        key = tenant, stamp
        if _time(row["snapshot_at"], "snapshot_at") <= cutoff and (key not in latest or revision > latest[key]["snapshot_revision"]):
            latest[key] = row
    qualified = {key: row for key, row in latest.items() if key[0] in selected and start <= key[1] < end
        and row["completeness"] in {"complete", "corrected"} and row["quality_status"] == "passed"
        and _time(row["watermark_at"], "watermark_at") >= _time(row["window_end"], "window_end")}
    blocked = set()
    for row in _v7_rows(directory, manifest, "content_tombstone"):
        _keys(row, "blocked_through_version effective_at event_id lifecycle_revision news_id policy_version status tenant_id")
        key = _tenant(row["tenant_id"]), _text(row["news_id"], "news_id")
        if key not in articles or key in blocked or row["status"] not in {"withdrawn", "restricted"}:
            raise WarehouseSourceError("unknown/duplicate v7 tombstone")
        _integer(row["lifecycle_revision"], "lifecycle_revision", 1)
        _integer(row["blocked_through_version"], "blocked_through_version", 1)
        if _time(row["effective_at"], "tombstone effective_at") > frozen_asof:
            raise WarehouseSourceError("future v7 tombstone")
        blocked.add(key)
    observations, seen = {}, set()
    for row in _v7_rows(directory, manifest, "news_metric_hourly"):
        if set(row) != set(("tenant_id", "news_id", "event_time", "snapshot_revision", "uv_method", *_V7_COUNTS)):
            raise WarehouseSourceError("unexpected v7 hourly fields")
        key = _tenant(row["tenant_id"]), _text(row["news_id"], "news_id")
        stamp, revision = _hour(row["event_time"], "metric time"), _integer(row["snapshot_revision"], "revision", 1)
        identity = *key, stamp, revision
        if identity in seen or key not in articles or (*key[:1], stamp, revision) not in snapshots:
            raise WarehouseSourceError("duplicate or unbound v7 hourly observation")
        seen.add(identity)
        for field in _V7_COUNTS:
            _integer(row[field], field)
        if (row["uv_method"] != "synthetic_interval_union_v1" or row["clicks"] > row["impressions"]
                or row["unique_users"] > row["clicks"] or row["effective_consumptions"] > row["clicks"]
                or row["video_completions"] > row["video_starts"] or row["video_starts"] > row["clicks"]
                or row["interactions"] != sum(row[field] for field in ("likes", "comments", "shares", "favorites", "follows"))
                or stamp + timedelta(hours=1) <= _time(articles[key]["publish_time"], "publish_time")):
            raise WarehouseSourceError("invalid v7 metric relationship or UV method")
        window = qualified.get((key[0], stamp))
        if not window or revision != window["snapshot_revision"] or key in blocked or articles[key]["status"] != "published":
            continue
        observations[(*key, stamp)] = {**row, **{field: None for field in ("detail_page_views", "direct_page_views", "detail_visitors")},
            **{field: window[field] for field in ("watermark_at", "snapshot_at", "completeness", "quality_status")},
            "window_start": stamp.isoformat(), "window_end": window["window_end"], "policy_version": policy,
            "sample_count": None, "baseline_version": None, **dict.fromkeys(BASE_FIELDS),
            "content_sha256": articles[key]["content_sha256"], "source_content_version": articles[key]["content_version"]}
    seen_base = set()
    for row in _v7_rows(directory, manifest, "news_metric_baseline_hourly"):
        if set(row) != set(("tenant_id", "news_id", "event_time", "sample_count", "baseline_version", *BASE_FIELDS)):
            raise WarehouseSourceError("unexpected v7 baseline fields")
        key = _tenant(row["tenant_id"]), _text(row["news_id"], "news_id"), _hour(row["event_time"], "baseline time")
        if key in seen_base or key[:2] not in articles or not start <= key[2] <= _hour(manifest["historical_window_end"], "end"):
            raise WarehouseSourceError("duplicate or unbound v7 baseline")
        seen_base.add(key)
        samples = _integer(row["sample_count"], "sample_count", 0, 3)
        for field in BASE_FIELDS:
            if samples == 0 and row[field] is not None:
                raise WarehouseSourceError("zero-sample v7 baseline must be null")
            if samples:
                try:
                    number = Decimal(str(row[field]))
                except (InvalidOperation, ValueError, TypeError) as exc:
                    raise WarehouseSourceError("invalid v7 baseline") from exc
                if isinstance(row[field], bool) or not number.is_finite() or not 0 <= number <= 2**63 - 1:
                    raise WarehouseSourceError("invalid v7 baseline")
        if key in observations:
            observations[key].update({field: row[field] for field in ("sample_count", "baseline_version", *BASE_FIELDS)})
    eligible = tuple(row for key, row in sorted(articles.items()) if key[0] in selected
        and key not in blocked and row["status"] == "published" and _time(row["publish_time"], "publish_time") <= cutoff)
    identity = dict(source_schema="news_sim_v7", dataset_version=version, dataset_sha256=manifest["dataset_sha256"], manifest_sha256=manifest_hash,
        input_file_sha256={name + ".jsonl": manifest["tables"][name]["sha256"] for name in (
            "dim_news", "window_snapshot", "news_metric_hourly", "news_metric_baseline_hourly", "content_tombstone")},
        policy_version=policy, uv_method="synthetic_interval_union_v1", tenant_ids=list(selected),
        metric_data_synthetic=True, news_content_synthetic=True, scenario_id="tencent-news-v7",
        content_provenance="synthetic titles; no article bodies", sampled=False)
    return WarehouseSource(eligible, tuple(observations[key] for key in sorted(observations)), identity,
        dict(selected_articles=len(eligible), selected_observations=len(observations), latest_windows=len(latest),
             qualified_windows=len(qualified), not_historical_lifecycle_reconstruction=True), start, end, cutoff)


def _select_tenants(tenants, requested):
    if not tenants or len(tenants) != len(set(tenants)):
        raise WarehouseSourceError("invalid source tenants")
    selected = tenants if requested is None else tuple(_tenant(item) for item in requested)
    if not selected or len(selected) != len(set(selected)) or not set(selected) <= set(tenants):
        raise WarehouseSourceError("unknown/duplicate selected tenant")
    return selected


def _load_profile(profile, *, tenant_ids=None, as_of=None):
    # These imports are generator code only: no clients are instantiated and no
    # database, environment file, user-event source or network is read.
    from app.sql_assistant import (warehouse as w, scaled_profiles, timeline_profiles,
        timeline_rich_profiles, corpus_profiles, public_headlines, synthetic_profiles)
    from app.sql_assistant.scaled_profiles import iter_scaled_rows
    from app.sql_assistant.timeline_profiles import iter_timeline_rows
    from app.sql_assistant.timeline_rich_profiles import iter_timeline_v5_rows
    from app.sql_assistant.corpus_profiles import corpus_catalog, iter_corpus_rows
    from app.sql_assistant.public_headlines import public_catalog, iter_public_rows
    if profile not in PROFILE_NAMES:
        raise WarehouseSourceError("unknown generator profile")
    schema_digest = w.verify_schema_contract(profile)
    start, end = w.dataset_window(profile)
    cutoff = end if as_of is None else min(end, _time(as_of, "as_of"))
    if cutoff <= start:
        raise WarehouseSourceError("generator profile has no visible hour")
    sample_end = cutoff.replace(minute=0, second=0, microsecond=0)
    if sample_end <= start:
        raise WarehouseSourceError("generator cutoff has no closed hour")
    # Select the smallest supported news configuration, keeping its complete
    # visible time range so earlier scenarios remain usable for daily profiles.
    sample_start = start
    count = 120
    if profile in w.STREAMED_PROFILES:
        iterator = {"enterprise-v2": iter_scaled_rows, "timeline-v4": iter_timeline_rows,
            "timeline-v5": iter_timeline_v5_rows, "corpus-v6": iter_corpus_rows,
            "public-headlines-v3": iter_public_rows}[profile]
        manifest = w.streamed_manifest(profile, count)
        rows_for = lambda name: iterator(name, count)
    else:
        tables = w._fixture_rows(profile)
        rows_for = lambda name: iter(tables[("dim_news", "news_metric_hourly", "news_metric_baseline_hourly").index(name)])
        manifest = w.demo_dataset_info(profile, news_per_tenant=12)
    tenants = tuple(str(value) for value in (w.DEMO_TENANT_ID, w.ISOLATION_TENANT_ID))
    selected = _select_tenants(tenants, tenant_ids)
    full_articles = ({a["news_id"]: a for a in corpus_catalog()["articles"]} if profile == "corpus-v6" else {})
    public_articles = ({a["news_id"]: a for a in public_catalog()["articles"]} if profile == "public-headlines-v3" else {})
    articles = {}
    for raw in rows_for("dim_news"):
        row = _plain(raw)
        if row["tenant_id"] not in selected:
            continue
        original = full_articles.get(row["news_id"], {})
        headline = public_articles.get(row["news_id"], {})
        body = original.get("content", "")
        articles[(row["tenant_id"], row["news_id"])] = {**row, "content": body,
            "content_sha256": sha256(body.encode()).hexdigest(), "source_content_version": original.get("content_version", 1),
            "content_version": original.get("content_version", 1), "status": "published", "lifecycle_revision": 1,
            "category_data_synthetic": True, "news_content_synthetic": profile not in {"corpus-v6", "public-headlines-v3"},
            "metric_data_synthetic": True, "source_url": original.get("url", headline.get("url", "")),
            "content_scope": "verified_public_body" if original else "public_headline_only" if headline else "synthetic_title_only"}
    baseline = {}
    for raw in rows_for("news_metric_baseline_hourly"):
        stamp = _hour(raw["event_time"], "baseline time")
        if str(raw["tenant_id"]) in selected and sample_start <= stamp < sample_end:
            baseline[(str(raw["tenant_id"]), raw["news_id"], stamp)] = _plain(raw)
    observations, excluded = [], 0
    for raw in rows_for("news_metric_hourly"):
        row = _plain(raw)
        stamp = _hour(raw["event_time"], "event_time")
        key = row["tenant_id"], row["news_id"]
        if key not in articles or not sample_start <= stamp < sample_end:
            continue
        article = articles[key]
        # In the public-title v3 fixture publication dates and synthetic metric
        # dates differ. Pre-publication counts are not converted into fake visits.
        if stamp + timedelta(hours=1) <= _time(article["publish_time"], "publish_time"):
            excluded += 1
            continue
        base = baseline.get((*key, stamp), {})
        observations.append({**row, **{field: None for field in RICH_FIELDS},
            **{field: base.get(field) for field in BASE_FIELDS}, "sample_count": None,
            "baseline_version": profile + "-reference-baseline-v1", "snapshot_revision": 1,
            "window_start": stamp.isoformat(), "window_end": (stamp + timedelta(hours=1)).isoformat(),
            "watermark_at": sample_end.isoformat(), "snapshot_at": sample_end.isoformat(),
            "quality_status": "passed", "completeness": "complete",
            "policy_version": "legacy-hourly-aggregates-" + profile,
            "uv_method": "synthetic-per-news-hour-uv-no-cross-hour-union",
            "content_sha256": article["content_sha256"], "source_content_version": article["source_content_version"]})
    modules = [w, synthetic_profiles]
    if profile in w.STREAMED_PROFILES:
        modules.append(scaled_profiles)
    if profile in {"timeline-v4", "timeline-v5", "corpus-v6"}:
        modules.append(timeline_profiles)
    if profile == "timeline-v5":
        modules.append(timeline_rich_profiles)
    if profile == "corpus-v6":
        modules.append(corpus_profiles)
    if profile == "public-headlines-v3":
        modules.append(public_headlines)
    input_hashes = {}
    if profile == "corpus-v6":
        input_hashes["data/corpus-news-v6/catalog.json"] = sha256(corpus_profiles.CORPUS_CATALOG_PATH.read_bytes()).hexdigest()
    if profile == "public-headlines-v3":
        input_hashes["data/public-headlines/2026-10-07/catalog.json"] = sha256(public_headlines.CATALOG_PATH.read_bytes()).hexdigest()
    identity = dict(dataset_version=manifest.get("dataset_version", profile + "-frozen-generator-v1"),
        dataset_sha256=manifest.get("dataset_sha256", sha256(_canonical(manifest)).hexdigest()),
        generator_profile=profile, generator_news_per_tenant=count if profile in w.STREAMED_PROFILES else 12,
        generator_source_sha256=sha256(Path(w.__file__).read_bytes()).hexdigest(),
        generator_module_sha256={module.__name__: sha256(Path(module.__file__).read_bytes()).hexdigest() for module in modules},
        schema_sha256=sha256(schema_digest.encode()).hexdigest(), manifest_snapshot=_plain(manifest),
        input_file_sha256=input_hashes, policy_version="legacy-hourly-aggregates-" + profile,
        uv_method="synthetic-per-news-hour-uv-no-cross-hour-union", tenant_ids=list(selected),
        metric_data_synthetic=True, news_content_synthetic=profile not in {"corpus-v6", "public-headlines-v3"},
        scenario_id=profile, sampled=sample_start != start or sample_end != end,
        sampling_policy="complete visible range of bounded 120-news-per-tenant generator configuration (12 for v1)",
        excluded_prepublication_observations=excluded)
    return WarehouseSource(tuple(articles.values()), tuple(observations), identity,
        dict(selected_articles=len(articles), selected_observations=len(observations),
             sampled=identity["sampled"], excluded_prepublication_observations=excluded),
        sample_start, sample_end, cutoff)


def _load_source(value, *, tenant_ids=None, as_of=None):
    text = str(value)
    if text.startswith("profile:"):
        return _load_profile(text[8:], tenant_ids=tenant_ids, as_of=as_of)
    directory = Path(value)
    manifest, _ = _bounded_manifest(directory)
    if manifest.get("schema") == "news_sim_v7":
        return _load_v7(directory, tenant_ids=tenant_ids, as_of=as_of)
    if manifest.get("schema") == "news_sim_v8":
        return load_warehouse_source(directory, tenant_ids=tenant_ids, as_of=as_of)
    raise WarehouseSourceError("unknown frozen directory schema; use an explicit profile: name for generators")


def load_merged_source(directories: Sequence[str | Path], *, tenant_ids: tuple[str, ...] | None = None,
                       as_of: datetime | None = None) -> WarehouseSource:
    if isinstance(directories, (str, bytes, Path)) or not 1 <= len(directories) <= MAX_SOURCES:
        raise WarehouseSourceError("sources must be a bounded nonempty sequence")
    sources, by_version = [], {}
    for value in directories:
        source = _load_source(value, tenant_ids=tenant_ids, as_of=as_of)
        identity = source.source_identity
        version, digest = identity["dataset_version"], identity["dataset_sha256"]
        if version in by_version:
            if by_version[version] != digest:
                raise WarehouseSourceError("same dataset version has conflicting source hashes")
            continue
        by_version[version] = digest
        sources.append(source)
    blocked_real = {(row["tenant_id"], row["news_id"])
        for source in sources if source.source_identity.get("source_schema") == "news_sim_v8"
        for row in source.source_identity.get("blocked_news_identities", ())}
    articles, selected_priority, binding = {}, {}, {}
    source_ids, coverage = [], []
    for source in sources:
        identity = source.source_identity
        dataset = identity["dataset_version"]
        scenario = identity.get("scenario_id", "real-news-v8")
        policy = identity["policy_version"]
        definition = policy + "|" + identity["uv_method"]
        priority = 100 if identity.get("source_schema") == "news_sim_v8" else 80 if identity.get("generator_profile") == "corpus-v6" else 50
        source_ids.append(identity)
        for raw in source.articles:
            original_id = raw["news_id"]
            synthetic = raw.get("news_content_synthetic") is True
            if not synthetic and (raw["tenant_id"], original_id) in blocked_real:
                continue
            canonical_id = "synthetic:" + scenario + ":" + original_id if synthetic else original_id
            key = raw["tenant_id"], canonical_id
            article = {**raw, "news_id": canonical_id, "source_news_id": original_id,
                "scenario_id": scenario, "source_dataset_version": dataset,
                "content_scope": raw.get("content_scope", "verified_public_body")}
            ref = dict(source_dataset_version=dataset, scenario_id=scenario,
                dataset_sha256=identity["dataset_sha256"], source_news_id=original_id,
                content_sha256=raw["content_sha256"], content_version=raw.get("source_content_version", 1),
                content_scope=raw.get("content_scope", "verified_public_body"),
                news_content_synthetic=synthetic)
            current = articles.get(key)
            if current and current.get("content") and article.get("content") and current["content_sha256"] != article["content_sha256"]:
                raise WarehouseSourceError("same real news ID has conflicting verified content; explicit version namespace required")
            refs = current["source_refs"] + [ref] if current else [ref]
            if current is None or (article.get("content") and not current.get("content")) or priority > selected_priority[key]:
                articles[key] = {**article, "source_refs": refs}
                selected_priority[key] = priority
            else:
                current["source_refs"] = refs
            binding[(dataset, raw["tenant_id"], original_id)] = key
    observations = {}
    for source in sources:
        identity = source.source_identity
        dataset, scenario = identity["dataset_version"], identity.get("scenario_id", "real-news-v8")
        definition = identity["policy_version"] + "|" + identity["uv_method"]
        for raw in source.observations:
            if (raw["tenant_id"], raw["news_id"]) in blocked_real and not identity.get("news_content_synthetic"):
                continue
            canonical_key = binding.get((dataset, raw["tenant_id"], raw["news_id"]))
            if canonical_key is None:
                raise WarehouseSourceError("merged observation has no eligible article")
            article = articles[canonical_key]
            row = {**raw, "news_id": canonical_key[1], "source_news_id": raw["news_id"],
                "source_dataset_version": dataset, "scenario_id": scenario,
                "metric_policy_version": identity["policy_version"], "metric_definition_id": definition,
                "source_content_sha256": raw.get("content_sha256")}
            if row["content_sha256"] != article["content_sha256"]:
                if row["content_sha256"] != sha256(b"").hexdigest():
                    raise WarehouseSourceError("observation content differs from selected article")
                row["source_content_sha256"] = None
                row["content_sha256"] = article["content_sha256"]
                row["content_binding"] = "verified_canonical_news_id_body_enrichment; original source had no body"
            key = (*canonical_key, row["window_start"], row["window_end"], dataset, scenario, definition)
            if key in observations and observations[key] != row:
                raise WarehouseSourceError("conflicting duplicate observation within the same simulation world")
            observations[key] = row
            if len(observations) > MAX_MERGED_OBSERVATIONS:
                raise WarehouseSourceError("merged observations exceed the bounded budget")
    for article in articles.values():
        article["source_refs"].sort(key=lambda item: (item["source_dataset_version"], item["scenario_id"]))
    for source in sources:
        identity = source.source_identity
        dataset, scenario = identity["dataset_version"], identity.get("scenario_id", "real-news-v8")
        for tenant in identity["tenant_ids"]:
            hours = sorted({row["window_start"] for row in observations.values()
                if row["tenant_id"] == tenant and row["source_dataset_version"] == dataset})
            coverage.append(dict(tenant_id=tenant, scenario_id=scenario, source_dataset_version=dataset,
                window_start=source.window_start.isoformat(), window_end=source.window_end.isoformat(), hours=hours,
                metric_policy_version=identity["policy_version"],
                metric_definition_id=identity["policy_version"] + "|" + identity["uv_method"]))
    merged_identity = dict(dataset_version="news-behavior-multi-source-v1", merge_version=MERGE_VERSION,
        sources=source_ids, coverage_by_scenario=coverage, source_coverage=coverage,
        policy_version="per-observation metric_policy_version; never summed across worlds",
        uv_method="per-observation uv_method; never summed across worlds", metric_data_synthetic=True,
        news_content_synthetic=all(source.source_identity.get("news_content_synthetic") is True for source in sources),
        content_provenance="each article/source_ref records real public body, public headline or synthetic title",
        blocked_news_identities=[dict(tenant_id=tenant, news_id=news) for tenant, news in sorted(blocked_real)],
        tenant_ids=sorted({tenant for source in sources for tenant in source.source_identity["tenant_ids"]}))
    merged_identity["dataset_sha256"] = sha256(_canonical(merged_identity)).hexdigest()
    return WarehouseSource(tuple(articles[key] for key in sorted(articles)),
        tuple(observations[key] for key in sorted(observations)), merged_identity,
        dict(source_count=len(sources), selected_articles=len(articles), selected_observations=len(observations),
            sources=[source.stats for source in sources], uv_summed=False),
        min(source.window_start for source in sources), max(source.window_end for source in sources),
        max(source.as_of for source in sources))

