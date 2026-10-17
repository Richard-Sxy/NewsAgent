"""Reproduce two real-content, synthetic-reader hot-news analyses without servers.

Run from agent/: python -m examples.reader_profile_hot_news_smoke --output /tmp/reader-smoke.json
The compact reader package is verified by the real local provider. Warehouse
dimension, metric and window files are streamed once and checked against their
manifest. No behavior package or raw synthetic-user event file is loaded.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from app.analytics.entities import ContentType
from app.analytics.hot_news_enrichment import HotNewsEnrichmentService
from app.analytics.metrics import NewsMetricSnapshot
from app.analytics.news_content import InMemoryNewsContentRepository, NewsContent
from app.analytics.ranking import HotNewsRanker
from app.model_runtime.core import StructuredInferenceService
from app.model_runtime.hot_news import NativeHotNewsAnalysisRunner, local_hot_news_prompts
from app.model_runtime.local_inference import LocalHotNewsInference
from app.reader_profiles.contracts import ReaderProfileSummary
from app.reader_profiles.local import LocalNewsReaderProfileClient
from app.reader_profiles.service import ReaderProfileService
from app.services.hot_news_analysis import HotNewsAnalysisInputBuilder, HotNewsAnalysisService, HotNewsAnalysisValidator
from app.services.hot_news_orchestration import (
    EmptyHotNewsBaselineProvider, HotNewsOrchestrationPolicy, HotNewsOrchestrationService, HotNewsRunRequest,
)


ROOT = Path(__file__).resolve().parents[2]
PROMPT = "native-e2e-prompt-v3-reader-profiles"


def _time(value):
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("smoke inputs require timezone-aware timestamps")
    return result


def _verified_rows(directory, manifest, table, *, news_ids=None):
    """Hash in the same streaming pass; parse only relevant article rows."""
    spec = manifest["tables"][table]
    digest, count, selected = sha256(), 0, []
    needles = tuple(json.dumps(news_id).encode() for news_id in news_ids) if news_ids else ()
    with (directory / (table + ".jsonl")).open("rb") as stream:
        for raw in stream:
            digest.update(raw)
            count += 1
            if not needles or any(needle in raw for needle in needles):
                selected.append(json.loads(raw))
    if digest.hexdigest() != spec["sha256"] or count != spec["rows"]:
        raise ValueError("warehouse file differs from its frozen manifest: " + table)
    return selected


def _candidates(client, tenant, start, end):
    uri = (client.directory / "reader_profiles.sqlite").as_uri() + "?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        rows = connection.execute(
            "SELECT payload_json FROM reader_profiles WHERE tenant_id=? AND window_start=? AND window_end=? ORDER BY news_id LIMIT 100",
            (tenant, start.isoformat(), end.isoformat()),
        )
        return {item.news_id: item for (raw,) in rows
            if (item := ReaderProfileSummary.model_validate_json(raw)).reader_count > 0}


def _source_inputs(directory, manifest, tenant, start, end, candidates):
    articles = {}
    for row in _verified_rows(directory, manifest, "dim_news", news_ids=candidates):
        news_id = row["news_id"]
        if row["tenant_id"] != tenant or news_id not in candidates or row["status"] != "published":
            continue
        body = row.get("content", "")
        digest = sha256(body.encode()).hexdigest()
        if not body or digest != row.get("content_sha256") or digest != candidates[news_id].content_sha256:
            continue
        articles[news_id] = NewsContent(news_id=news_id, title=row["title"], summary=body[:300], body=body,
            content_type=ContentType(row["content_type"]), publish_time=_time(row["publish_time"]), source_url=row["source_url"])
    if len(articles) < 2:
        raise ValueError("fewer than two visited news have exact warehouse body hashes")
    windows = [row for row in _verified_rows(directory, manifest, "window_snapshot")
        if row["tenant_id"] == tenant and _time(row["event_time"]) == start and _time(row["window_end"]) == end
        and row["completeness"] in {"complete", "corrected"} and row["quality_status"] == "passed"
        and _time(row["watermark_at"]) >= end]
    if not windows:
        raise ValueError("warehouse has no qualified complete snapshot for the reader window")
    revision = max(row["snapshot_revision"] for row in windows)
    metrics = {}
    for row in _verified_rows(directory, manifest, "news_metric_hourly", news_ids=articles):
        news_id = row["news_id"]
        if (row["tenant_id"] != tenant or news_id not in articles or _time(row["event_time"]) != start
                or row["snapshot_revision"] != revision):
            continue
        if news_id in metrics:
            raise ValueError("warehouse metric revision contains duplicate news")
        counts = {key: row[key] for key in ("impressions", "clicks", "unique_users", "total_duration_seconds",
            "effective_consumptions", "interactions")}
        if any(type(value) is not int or value < 0 for value in counts.values()):
            raise ValueError("warehouse metric contains invalid counts")
        metrics[news_id] = NewsMetricSnapshot(news_id=news_id, content_type=articles[news_id].content_type,
            window_start=start, window_end=end, **counts,
            ctr=Decimal(row["clicks"]) / row["impressions"] if row["impressions"] else Decimal(0))
    available = sorted(set(articles) & set(metrics))
    if len(available) < 2:
        raise ValueError("fewer than two visited news have matching warehouse content hashes and metrics")
    # Choose a deterministic pair with the clearest different long-term labels.
    pairs = [(first, second) for index, first in enumerate(available) for second in available[index + 1:]]
    def difference(pair):
        left, right = ({topic.topic_id for topic in candidates[news].long_term.topics} for news in pair)
        return (-len(left ^ right), *pair)
    chosen = min(pairs, key=difference)
    return [articles[news] for news in chosen], [metrics[news] for news in chosen]


class _EmptyRelatedEvidence:
    async def batch_search_related_news(self, queries, *, tenant_id):
        return {query.query_id: [] for query in queries}


class _FrozenMetrics:
    def __init__(self, metrics, tenant):
        self.metrics, self.tenant = metrics, tenant

    async def fetch_snapshots(self, query):
        if query.tenant_id != self.tenant or any((item.window_start, item.window_end) !=
                (query.window_start, query.window_end) for item in self.metrics):
            raise ValueError("smoke metrics crossed their frozen tenant/window")
        return self.metrics


class _RecordingLocalInference(LocalHotNewsInference):
    def __init__(self):
        self.payloads = []

    async def complete(self, request):
        self.payloads.append(json.loads(request.messages[1].content))
        return await super().complete(request)


async def smoke(*, reader_package, warehouse, tenant_id=None):
    client = LocalNewsReaderProfileClient(reader_package, environment="e2e")
    service = ReaderProfileService(client, catalog=client.catalog, environment="e2e", timeout_seconds=3)
    service.run_timeout_seconds = 5
    try:
        tenant = tenant_id or client.manifest["tenants"][0]
        end = _time(client.manifest["coverage_end"])
        start = end - timedelta(hours=client.manifest["window_hours"])
        candidates = _candidates(client, tenant, start, end)
        warehouse = Path(warehouse).resolve(strict=True)
        manifest = json.loads((warehouse / "manifest.json").read_text())
        contents, metrics = _source_inputs(warehouse, manifest, tenant, start, end, candidates)
        inference = _RecordingLocalInference()
        analysis = HotNewsAnalysisService(input_builder=HotNewsAnalysisInputBuilder(policy_version=PROMPT),
            runner=NativeHotNewsAnalysisRunner(StructuredInferenceService(inference=inference, prompts=local_hot_news_prompts()),
                tenant_id=tenant, prompt_version=PROMPT, model_route="native-local-model-v1"),
            validator=HotNewsAnalysisValidator())
        orchestration = HotNewsOrchestrationService(metric_source=_FrozenMetrics(metrics, tenant),
            baseline_provider=EmptyHotNewsBaselineProvider(), enrichment_service=HotNewsEnrichmentService(
                InMemoryNewsContentRepository(contents), _EmptyRelatedEvidence(), reader_profile_service=service,
                reader_profile_window=(start, end)), analysis_service=analysis,
            policy=HotNewsOrchestrationPolicy(production_bundle_version="local-reader-profile-smoke-v1", ranking_limit=2))
        request = HotNewsRunRequest(tenant, start, end, "local-reader-profile-smoke-v1",
            reader_profile_context_sha256=service.context_identity_for_window(tenant_id=tenant, window_start=start, window_end=end))
        result = await orchestration.run(request)
        unchanged = list(result.ranked_news) == HotNewsRanker().rank(metrics, {}, limit=2)
        inputs = {item["news_id"]: item["reader_profile_summary"] for item in inference.payloads}
        if not unchanged or len(inputs) != 2 or any(inputs[item.news_id] != item.reader_profile_summary.model_dump(mode="json")
                for item in result.analyzed_news):
            raise RuntimeError("reader profile smoke did not preserve model evidence or authoritative ranks")
        return {"functional_smoke_only": True, "enterprise_quality_validation": False,
            "source_mode": "synthetic", "tenant_id": tenant, "window_start": start.isoformat(), "window_end": end.isoformat(),
            "reader_package": str(client.directory), "reader_source_release_sha256": service.source_release_sha256,
            "source_warehouse": str(warehouse), "source_warehouse_dataset": manifest["dataset_version"],
            "verified_warehouse_files": {name: manifest["tables"][name]["sha256"] for name in
                ("dim_news", "news_metric_hourly", "window_snapshot")},
            "run_key": result.idempotency_key, "reader_context_sha256": request.reader_profile_context_sha256,
            "same_ranking_and_metrics": unchanged, "model_received_reader_profiles": len(inputs),
            "prompt_version": PROMPT,
            "limitations": ["全部读者是合成样例；不代表企业用户、真实触达或推送效果。",
                "行为事件与原仓小时指标独立模拟，reader_count不等同或反推原仓detail_visitors。",
                "仅选择最终小时两条有访问、正文哈希匹配的新闻；没有执行全榜检索或性能验收。",
                "关联新闻证据为空、历史热度基线为空；所有方向建议仅供人工核查，不发送推送。"],
            "items": [{"news_id": item.news_id, "title": item.analysis_input.title,
                "hot_score": item.analysis_input.hot_score, "metrics": item.analysis_input.metrics.model_dump(mode="json"),
                "reader_profile_summary": item.reader_profile_summary.model_dump(mode="json"),
                "reader_profile_trace": item.reader_profile_trace,
                "operation_suggestions": [row.model_dump(mode="json") for row in item.analysis.value.operation_suggestions],
                "limitations": item.analysis.value.limitations} for item in result.analyzed_news]}
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reader-package", type=Path, default=ROOT / "output/reader-profiles/local-readers-v1-20261017")
    parser.add_argument("--warehouse", type=Path, default=ROOT / "output/warehouse/real-news-v8-20261014")
    parser.add_argument("--tenant-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(smoke(reader_package=args.reader_package, warehouse=args.warehouse, tenant_id=args.tenant_id))
    serialized = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
        print(json.dumps({key: result[key] for key in ("functional_smoke_only", "source_mode",
            "window_start", "window_end", "same_ranking_and_metrics", "model_received_reader_profiles", "prompt_version")},
            ensure_ascii=False))
    else:
        print(serialized)


if __name__ == "__main__":
    main()
