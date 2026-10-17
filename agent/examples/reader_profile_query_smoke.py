"""Read the real local aggregate package through its Port, cache and HTTP router.

This is a bounded functional latency probe, not an enterprise-scale load test.
No socket, model service, personal-event read or running deployment is needed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
from time import perf_counter

from fastapi import FastAPI
import httpx

from app.api.dependencies import get_data_loop_gateway_token
from app.api.reader_profiles import router
from app.modules.reader_profiles.contracts import ReaderProfileQuery
from app.modules.reader_profiles.local import LocalNewsReaderProfileClient
from app.modules.reader_profiles.service import ReaderProfileService


def timings(samples):
    ordered = sorted(samples)
    return {"samples": len(samples), "p50_ms": round(ordered[math.ceil(len(ordered) * .5) - 1], 3),
            "p95_ms": round(ordered[math.ceil(len(ordered) * .95) - 1], 3)}


async def run(package: Path, repeats: int):
    began = perf_counter()
    client = LocalNewsReaderProfileClient(package, environment="test")
    startup_ms = (perf_counter() - began) * 1000
    service = ReaderProfileService(client, catalog=client.catalog, environment="test", timeout_seconds=3)
    try:
        with sqlite3.connect((package / "reader_profiles.sqlite").as_uri() + "?mode=ro", uri=True) as db:
            tenant, start, end = db.execute("SELECT tenant_id,window_start,window_end FROM coverage ORDER BY window_start DESC,tenant_id LIMIT 1").fetchone()
            ids = tuple(row[0] for row in db.execute(
                "SELECT news_id FROM reader_profiles WHERE tenant_id=? AND window_start=? AND window_end=? ORDER BY news_id LIMIT 20",
                (tenant, start, end)))
            placeholders = ",".join("?" for _ in ids)
            plan = db.execute("EXPLAIN QUERY PLAN SELECT news_id,payload_json FROM reader_profiles WHERE tenant_id=? AND window_start=? AND window_end=? AND news_id IN (" + placeholders + ")",
                (tenant, start, end, *ids)).fetchall()
        assert ids and all("SEARCH" in row[3] and "SCAN" not in row[3] for row in plan), plan
        query = ReaderProfileQuery(tenant_id=tenant, news_ids=ids,
            window_start=datetime.fromisoformat(start), window_end=datetime.fromisoformat(end),
            source_release_sha256=client.source_release_sha256)
        uncached = []
        for _ in range(repeats):
            began = perf_counter()
            await client.fetch(query)
            uncached.append((perf_counter() - began) * 1000)
        first = await service.fetch(query)
        cached = []
        for _ in range(repeats):
            began = perf_counter()
            assert await service.fetch(query) == first
            cached.append((perf_counter() - began) * 1000)
        app = FastAPI()
        app.include_router(router)
        app.state.reader_profile_service = service
        app.dependency_overrides[get_data_loop_gateway_token] = lambda: "isolated-reader-smoke-token"
        headers = {"Authorization": "Bearer isolated-reader-smoke-token", "X-Tenant-ID": tenant,
            "X-User-ID": "22222222-2222-4222-8222-222222222222", "X-Hot-News-Roles": "hot-news:read"}
        body = {"news_ids": list(ids), "window_start": start, "window_end": end}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated.test") as http:
            response = await http.post("/api/v1/reader-profiles/query", headers=headers, json=body)
            assert response.status_code == 200, response.status_code
            assert response.headers["cache-control"] == "no-store"
            value = response.json()
            assert value["snapshot"]["tenant_id"] == tenant and len(value["snapshot"]["items"]) == len(ids)
            assert value["dispatch_enabled"] is False and value["manual_review_required"] is True
            assert all(key not in response.text for key in ('"synthetic_user_id"', '"event_id"', '"topic_weights"'))
            denied = await http.post("/api/v1/reader-profiles/query", headers={**headers, "X-Hot-News-Roles": "hot-news:decide"}, json=body)
            assert denied.status_code == 403
        return {"probe_version": "local-reader-aggregate-functional-v1", "source_mode": "synthetic",
            "source_release_sha256": client.source_release_sha256,
            "scope": {"tenant_id": tenant, "window_start": start, "window_end": end, "news_count": len(ids)},
            "package_counts": client.manifest["counts"],
            "source_event_rows": client.manifest["source_artifacts"]["synthetic_events.jsonl"]["rows"],
            "startup_hash_verification_ms": round(startup_ms, 3),
            "uncached_local_aggregate_port": timings(uncached), "validated_cache_hits": timings(cached),
            "sqlite_query_plan": [row[3] for row in plan], "api_status": response.status_code,
            "permission_denied_status": denied.status_code,
            "reader_counts": {item.news_id: item.reader_count for item in first.items},
            "limitations": ["Local synthetic aggregate reads only; OS page cache is uncontrolled.",
                "Startup includes full aggregate artifact hash verification; timings exclude startup.",
                "Does not measure enterprise network, production concurrency, model latency or million-reader workloads."]}
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=30)
    args = parser.parse_args()
    if not 5 <= args.repeats <= 100:
        parser.error("repeats must be between 5 and 100")
    report = asyncio.run(run(args.package.resolve(strict=True), args.repeats))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("scope", "api_status", "uncached_local_aggregate_port", "validated_cache_hits")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
