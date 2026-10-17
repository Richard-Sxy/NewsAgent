#!/usr/bin/env python3
"""Bounded HTTP latency benchmark for the read-only lifecycle search endpoint.

The load model is closed-loop: each of ``concurrency`` workers starts its next
request after its previous request completes. It does not simulate a fixed QPS
arrival process. Query order is deterministic round-robin, not a relevance test.
Only an explicitly named environment variable supplies authentication; .env files
are never loaded. Results contain allowlisted timings, never queries or articles.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx


SEARCH_PATH = "/api/v1/knowledge/search"
MAX_REQUESTS = 100_000
MAX_CONCURRENCY = 256
MAX_WARMUP = 10_000
MAX_QUERY_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
TIERS = ("hot", "warm", "cold")
TIER_METRICS = ("vector_search_ms", "state_gate_ms", "filter_ms", "search_and_gate_ms")
TRACE_TIMINGS = ("queue_ms", "embedding_ms", "final_gate_ms", "fusion_ms", "total_ms")
TRACE_STATUSES = {"running", "complete", "failed", "timeout", "cancelled", "interrupted"}
TRACE_STAGES = {"queue", "embedding", "tiers", "final_gate", "fusion", "complete"}
PERCENTILE_METHOD = "R-7 linear interpolation: sorted values at index (n - 1) * p; empty = null"


@dataclass(frozen=True)
class Query:
    identifier: str = field(repr=False)
    text: str = field(repr=False)
    limit: int


@dataclass(frozen=True)
class BenchmarkConfig:
    url: str
    tenant_id: str
    token_env: str
    query_file: Path
    requests: int
    concurrency: int
    warmup: int
    timeout: float
    output_dir: Path

    def validated(self) -> BenchmarkConfig:
        url = search_url(self.url)
        try:
            tenant = str(UUID(self.tenant_id))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("tenant_id must be a UUID") from None
        if not isinstance(self.token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.token_env):
            raise ValueError("token_env must name an environment variable")
        for name, value, lower, upper in (
            ("requests", self.requests, 1, MAX_REQUESTS),
            ("concurrency", self.concurrency, 1, MAX_CONCURRENCY),
            ("warmup", self.warmup, 0, MAX_WARMUP),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"{name} must be an integer in [{lower}, {upper}]")
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= 300):
            raise ValueError("timeout must be finite and in (0, 300] seconds")
        return BenchmarkConfig(url, tenant, self.token_env, Path(self.query_file),
            self.requests, self.concurrency, self.warmup, float(self.timeout), Path(self.output_dir))


def search_url(value: str) -> str:
    """Accept an origin or the exact search URL; never forward URL credentials."""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or "?" in value or "#" in value
                or parsed.path not in {"", "/", SEARCH_PATH}
                or any(ord(char) < 33 for char in value)):
            raise ValueError
        _ = parsed.port
        if parsed.scheme == "http":
            host = parsed.hostname.lower()
            local = host == "localhost" or host.endswith(".localhost")
            try:
                local = local or ipaddress.ip_address(host).is_private
            except ValueError:
                pass
            if not local:
                raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise ValueError("url must be an HTTPS origin/search endpoint, or HTTP localhost/private IP; "
                         "credentials, query parameters and fragments are forbidden") from None
    return urlunsplit((parsed.scheme, parsed.netloc, SEARCH_PATH, "", ""))


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def load_queries(path: Path) -> tuple[tuple[Query, ...], str]:
    with path.open("rb") as source:
        data = source.read(MAX_QUERY_BYTES + 1)
    if len(data) > MAX_QUERY_BYTES:
        raise ValueError("query_file exceeds the 16 MiB limit")
    try:
        lines = data.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        raise ValueError("query_file must contain UTF-8 JSONL") from None
    queries, ids = [], set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line, object_pairs_hook=_unique_json_object)
            if not isinstance(value, dict) or set(value) != {"id", "query", "limit"}:
                raise ValueError
            identifier, text, limit = value["id"], value["query"], value["limit"]
            if (not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 160
                    or identifier in ids or not isinstance(text, str) or not text.strip()
                    or len(text) > 2000 or type(limit) is not int or not 1 <= limit <= 1000):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError(f"invalid query_file record at line {number}; "
                             "require unique string id, nonempty query <= 2000 chars, limit 1..1000") from None
        queries.append(Query(identifier, text, limit))
        ids.add(identifier)
        if len(queries) > 10_000:
            raise ValueError("query_file exceeds 10000 records")
    if not queries:
        raise ValueError("query_file must contain at least one query")
    return tuple(queries), hashlib.sha256(data).hexdigest()


def _number(value):
    try:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return value
    except OverflowError:
        pass
    return None


def sanitize_trace(value) -> dict:
    """Allowlist numbers and fixed enums, even if the server echoes input text."""
    if not isinstance(value, dict):
        return {}
    result = {name: _number(value.get(name)) for name in TRACE_TIMINGS}
    for name in ("trace_schema_version", "returned_news"):
        if type(value.get(name)) is int and value[name] >= 0:
            result[name] = value[name]
    for name, allowed in (("status", TRACE_STATUSES), ("stage", TRACE_STAGES)):
        if isinstance(value.get(name), str) and value[name] in allowed:
            result[name] = value[name]
    source_tiers = value.get("tiers")
    result["tiers"] = {}
    for tier in TIERS:
        source = source_tiers.get(tier) if isinstance(source_tiers, dict) else None
        if not isinstance(source, dict):
            continue
        target = {name: _number(source.get(name)) for name in TIER_METRICS}
        for name in ("hits", "refill_rounds"):
            if type(source.get(name)) is int and source[name] >= 0:
                target[name] = source[name]
        if type(source.get("components_available")) is bool:
            target["components_available"] = source["components_available"]
        if isinstance(source.get("status"), str) and source["status"] in TRACE_STATUSES:
            target["status"] = source["status"]
        rounds = source.get("rounds")
        if isinstance(rounds, list):
            target["rounds"] = [{name: _number(item.get(name)) for name in
                ("requested", "retrieved", "accepted", "vector_search_ms", "state_gate_ms", "filter_ms")}
                for item in rounds[:64] if isinstance(item, dict)]
            target["rounds_truncated"] = len(rounds) > 64
        result["tiers"][tier] = target
    return result


def percentiles(values: list[float]) -> dict:
    ordered = sorted(values)
    result = {"count": len(ordered)}
    for label, probability in (("p50", .50), ("p95", .95), ("p99", .99)):
        if not ordered:
            result[label] = None
            continue
        position = (len(ordered) - 1) * probability
        lower, upper = math.floor(position), math.ceil(position)
        result[label] = ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)
    return result


class _Accumulator:
    def __init__(self):
        self.total = self.succeeded = 0
        self.errors, self.statuses = Counter(), Counter()
        self.metrics = {name: [] for name in ("client_wall_ms", "server_total_ms", *TRACE_TIMINGS[:-1])}
        self.tier_metrics = {tier: {name: [] for name in TIER_METRICS} for tier in TIERS}

    def add(self, sample):
        self.total += 1
        self.statuses[str(sample["http_status"]) if sample["http_status"] is not None else "no_response"] += 1
        if not sample["success"]:
            self.errors[sample["error_type"]] += 1
            return
        self.succeeded += 1
        self.metrics["client_wall_ms"].append(sample["client_wall_ms"])
        trace = sample["trace"]
        for name in self.metrics:
            if name == "client_wall_ms":
                continue
            value = _number(trace.get("total_ms" if name == "server_total_ms" else name))
            if value is not None:
                self.metrics[name].append(value)
        for tier, metrics in self.tier_metrics.items():
            for name in metrics:
                value = _number(trace.get("tiers", {}).get(tier, {}).get(name))
                if value is not None:
                    metrics[name].append(value)

    def summary(self):
        return {"total": self.total, "succeeded": self.succeeded, "failed": self.total-self.succeeded,
            "error_rate": (self.total-self.succeeded)/self.total if self.total else None,
            "http_status_counts": dict(self.statuses), "error_type_counts": dict(self.errors)}


class _ResponseTooLarge(Exception):
    pass


async def _sample(client, config, token, query, *, query_index, request_index, phase):
    sample = {"phase": phase, "request_index": request_index, "query_index": query_index,
        "http_status": None, "success": False, "error_type": None, "result_count": None, "trace": {}}
    start = perf_counter()
    try:
        async with asyncio.timeout(config.timeout):
            async with client.stream("POST", config.url,
                headers={"Authorization": f"Bearer {token}", "X-Tenant-ID": config.tenant_id},
                json={"query": query.text, "limit": query.limit}, timeout=config.timeout,
                follow_redirects=False) as response:
                sample["http_status"] = response.status_code
                chunks, length = [], 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > MAX_RESPONSE_BYTES:
                        raise _ResponseTooLarge
                    chunks.append(chunk)
                try:
                    payload = json.loads(b"".join(chunks))
                except (ValueError, UnicodeDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    detail = payload.get("detail")
                    trace = payload.get("trace", detail.get("trace") if isinstance(detail, dict) else None)
                    sample["trace"] = sanitize_trace(trace)
                if not 200 <= response.status_code < 300:
                    sample["error_type"] = "HTTPStatusError"
                elif not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                    sample["error_type"] = "InvalidResponse"
                else:
                    sample.update(success=True, result_count=len(payload["items"]))
    except TimeoutError:
        sample["error_type"] = "ClientTimeout"
    except httpx.HTTPError as exc:
        # Never serialize exception messages, request URLs or response content.
        sample["error_type"] = type(exc).__name__
    except _ResponseTooLarge:
        sample["error_type"] = "ResponseTooLarge"
    except Exception:
        # Malformed or unexpectedly shaped remote input cannot abort other work.
        sample["error_type"] = "UnexpectedClientError"
    finally:
        sample["client_wall_ms"] = (perf_counter()-start)*1000
    return sample


async def run_benchmark(config: BenchmarkConfig, *, client: httpx.AsyncClient | None = None,
                        environ=None) -> dict:
    """Run with one shared client; injected clients remain owned by the caller.

    An injected client must use a transport without automatic retries. Production
    clients are constructed here with retries=0, no redirects and no proxy env.
    Samples are written as requests finish, with indices for deterministic joins.
    """
    config = config.validated()
    environment = os.environ if environ is None else environ
    token = environment.get(config.token_env)
    if (not isinstance(token, str) or not token or not token.isascii()
            or any(char.isspace() or ord(char) < 33 or ord(char) == 127 for char in token)):
        raise ValueError("token_env must contain a nonempty ASCII bearer token without whitespace")
    queries, digest = load_queries(config.query_file)
    if config.output_dir.exists() and (not config.output_dir.is_dir() or any(config.output_dir.iterdir())):
        raise ValueError("output_dir must be new or empty; existing evidence is never overwritten")
    config.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"schema_version": 1, "started_at_utc": datetime.now(UTC).isoformat(),
        "url": config.url, "method": "POST", "tenant_id": config.tenant_id,
        "token_env": config.token_env, "query_file_sha256": digest, "query_count": len(queries),
        "requests": config.requests, "concurrency": config.concurrency, "warmup": config.warmup,
        "timeout_seconds": config.timeout, "load_model": "closed_loop_fixed_concurrency",
        "query_order": "round_robin_reset_after_warmup", "retries": 0, "follow_redirects": False,
        "percentile_method": PERCENTILE_METHOD,
        "sample_order": "completion_order; request_index is zero-based within each phase",
        "trace_fields": "allowlisted numeric measurements and fixed enums only",
        "measurement_scope": "HTTP client wall time and server trace; not Milvus server-only time"}
    with (config.output_dir / "manifest.json").open("x", encoding="utf-8") as target:
        json.dump(manifest, target, ensure_ascii=False, indent=2, allow_nan=False)
    own_client = client is None
    if client is None:
        limits = httpx.Limits(max_connections=config.concurrency, max_keepalive_connections=config.concurrency)
        client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(retries=0, limits=limits, trust_env=False),
            timeout=config.timeout, follow_redirects=False, trust_env=False)
    try:
        with (config.output_dir / "samples.jsonl").open("x", encoding="utf-8") as samples:
            async def phase(name, count):
                accumulator = _Accumulator()
                sequence = iter(range(count))

                async def worker():
                    for index in sequence:
                        query_index = index % len(queries)
                        sample = await _sample(client, config, token, queries[query_index],
                            query_index=query_index, request_index=index, phase=name)
                        samples.write(json.dumps(sample, ensure_ascii=False, allow_nan=False)+"\n")
                        accumulator.add(sample)

                start = perf_counter()
                await asyncio.gather(*(worker() for _ in range(min(count, config.concurrency))))
                return accumulator, (perf_counter()-start)*1000

            warmup, warmup_ms = await phase("warmup", config.warmup)
            measured, measurement_ms = await phase("measurement", config.requests)
    finally:
        if own_client:
            await client.aclose()
    report = {"schema_version": 1,
        "status": "failed" if measured.errors or warmup.errors else "passed",
        "measurement": {**measured.summary(), "elapsed_ms": measurement_ms,
            "completed_requests_per_second": measured.total/(measurement_ms/1000) if measurement_ms > 0 else None},
        "warmup": {**warmup.summary(), "elapsed_ms": warmup_ms, "included_in_percentiles": False},
        "percentile_method": PERCENTILE_METHOD, "latency_population": "successful measurement requests only",
        "latency_ms": {name: percentiles(values) for name, values in measured.metrics.items()},
        "tiers": {tier: {name: percentiles(values) for name, values in metrics.items()}
                  for tier, metrics in measured.tier_metrics.items()},
        "missing_metrics": "null percentiles and count=0; partial coverage uses only observed values",
        "scope": "Closed-loop HTTP benchmark; no automatic retry; no relevance labels or fixed-QPS model"}
    with (config.output_dir / "report.json").open("x", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="service origin or exact /api/v1/knowledge/search URL")
    parser.add_argument("--tenant-id", required=True, help="tenant UUID")
    parser.add_argument("--token-env", required=True, help="environment variable holding the bearer token")
    parser.add_argument("--query-file", required=True, type=Path, help="UTF-8 JSONL: id, query, limit")
    parser.add_argument("--requests", required=True, type=int, help=f"measured requests, 1..{MAX_REQUESTS}")
    parser.add_argument("--concurrency", required=True, type=int, help=f"shared-pool workers, 1..{MAX_CONCURRENCY}")
    parser.add_argument("--warmup", required=True, type=int, help=f"warmup requests, 0..{MAX_WARMUP}")
    parser.add_argument("--timeout", required=True, type=float, help="whole request deadline in seconds, (0, 300]")
    parser.add_argument("--output-dir", required=True, type=Path, help="new or empty evidence directory")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_benchmark(BenchmarkConfig(**vars(args))))
    except ValueError as exc:
        print(f"benchmark configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print("benchmark filesystem error: check input/output paths and permissions", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("benchmark interrupted; partial samples may exist, report is incomplete", file=sys.stderr)
        return 130
    print(json.dumps({"status": report["status"], "measurement": report["measurement"],
                      "warmup": report["warmup"]}, ensure_ascii=False, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
