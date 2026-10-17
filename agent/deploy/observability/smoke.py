#!/usr/bin/env python3
"""Send synthetic OTLP JSON and verify all three local backends (no app secrets)."""
from __future__ import annotations

import argparse
import json
import secrets
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import ProxyHandler, Request, build_opener


_LOCAL_HTTP = build_opener(ProxyHandler({}))


def local_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise argparse.ArgumentTypeError("smoke endpoints must be local HTTP loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise argparse.ArgumentTypeError("use a base URL without credentials, query, or path")
    return value.rstrip("/")


def request(url: str, data: dict | None = None) -> dict:
    payload = None if data is None else json.dumps(data).encode()
    req = Request(url, payload, headers={"Content-Type": "application/json", "Accept": "application/json"})
    # Never send loopback synthetic telemetry through an inherited HTTP proxy.
    with _LOCAL_HTTP.open(req, timeout=5) as response:
        body = response.read(2_000_000)
    result = json.loads(body) if body else {}
    if result.get("partialSuccess"):
        raise RuntimeError("Collector partially rejected the synthetic telemetry")
    return result


def send_when_ready(url: str, data: dict) -> None:
    deadline = time.monotonic() + 15
    while True:
        try:
            request(url, data)
            return
        except HTTPError as error:
            if error.code not in {502, 503, 504}:
                raise
        except (URLError, TimeoutError):
            pass
        if time.monotonic() >= deadline:
            raise SystemExit("local Collector is not ready after 15 seconds")
        time.sleep(1)


def attrs(values: dict) -> list:
    return [{"key": key, "value": {"stringValue": str(value)}} for key, value in values.items()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otlp-endpoint", type=local_endpoint, default="http://127.0.0.1:24318")
    parser.add_argument("--tempo", type=local_endpoint, default="http://127.0.0.1:23200")
    parser.add_argument("--loki", type=local_endpoint, default="http://127.0.0.1:23100")
    parser.add_argument("--prometheus", type=local_endpoint, default="http://127.0.0.1:29090")
    args = parser.parse_args()
    trace_id, span_id, parent_id = secrets.token_hex(16), secrets.token_hex(8), secrets.token_hex(8)
    now = time.time_ns()
    service = "newsagent-observability-smoke"
    resource = {"attributes": attrs({"service.name": service, "service.namespace": "newsagent",
                                    "deployment.environment.name": "local"})}
    labels = attrs({"model.scene": "smoke", "model.route": "local", "gen_ai.operation.name": "chat",
                    "newsagent.outcome": "error", "newsagent.stage": "response_decode", "error.type": "invalid_json"})
    scope = {"name": "newsagent.observability.smoke", "version": "1"}
    spans = [
        {"traceId": trace_id, "spanId": parent_id, "name": "observability.smoke", "kind": 1,
         "startTimeUnixNano": str(now - 500_000_000), "endTimeUnixNano": str(now), "status": {"code": 2}},
        {"traceId": trace_id, "spanId": span_id, "parentSpanId": parent_id, "name": "model.response_decode",
         "kind": 1, "startTimeUnixNano": str(now - 250_000_000), "endTimeUnixNano": str(now),
         "attributes": labels, "status": {"code": 2}},
    ]
    send_when_ready(args.otlp_endpoint + "/v1/traces", {"resourceSpans": [{"resource": resource,
            "scopeSpans": [{"scope": scope, "spans": spans}]}]})
    send_when_ready(args.otlp_endpoint + "/v1/logs", {"resourceLogs": [{"resource": resource,
            "scopeLogs": [{"scope": scope, "logRecords": [{"timeUnixNano": str(now),
                "observedTimeUnixNano": str(now), "severityNumber": 17, "severityText": "ERROR",
                "body": {"stringValue": "model.failure"}, "traceId": trace_id, "spanId": span_id,
                "attributes": labels}]}]}]})
    point = {"attributes": labels, "startTimeUnixNano": str(now - 1_000_000_000), "timeUnixNano": str(now)}
    metrics = [
        {"name": "newsagent.model.calls", "unit": "{call}", "sum": {"aggregationTemporality": 2,
            "isMonotonic": True, "dataPoints": [{**point, "asInt": "1"}]}},
        {"name": "newsagent.model.duration", "unit": "s", "histogram": {"aggregationTemporality": 2,
            "dataPoints": [{**point, "count": "1", "sum": 0.25, "bucketCounts": ["0", "1", "0"],
                            "explicitBounds": [0.1, 1.0]}]}},
    ]
    send_when_ready(args.otlp_endpoint + "/v1/metrics", {"resourceMetrics": [{"resource": resource,
            "scopeMetrics": [{"scope": scope, "metrics": metrics}]}]})
    found = {"trace": False, "log": False, "metric": False}
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline and not all(found.values()):
        for signal in list(found):
            if found[signal]:
                continue
            try:
                if signal == "trace":
                    result = request(args.tempo + "/api/traces/" + trace_id)
                    found[signal] = bool(result.get("batches") or result.get("resourceSpans"))
                elif signal == "log":
                    query = '{service_name="' + service + '"} | trace_id = "' + trace_id + '"'
                    result = request(args.loki + "/loki/api/v1/query_range?" + urlencode({"query": query, "limit": 5}))
                    found[signal] = bool(result.get("data", {}).get("result"))
                else:
                    query = 'newsagent_model_calls_total{service_name="' + service + '",newsagent_outcome="error"}'
                    result = request(args.prometheus + "/api/v1/query?" + urlencode({"query": query}))
                    found[signal] = bool(result.get("data", {}).get("result"))
            except (HTTPError, URLError, TimeoutError):
                pass
        if not all(found.values()):
            time.sleep(1)
    print(json.dumps({"trace_id": trace_id, "signals": found, "synthetic_only": True}, ensure_ascii=False))
    if not all(found.values()):
        raise SystemExit("synthetic OTLP smoke failed; inspect the isolated Collector/backend logs")


if __name__ == "__main__":
    main()
