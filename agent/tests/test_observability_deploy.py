"""Guard local-only signal routing, resource bounds and dashboard correlation contracts."""
import json
from pathlib import Path

import pytest
import yaml


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
CONFIG = DEPLOY / "observability"


def load(name):
    return yaml.safe_load((CONFIG / name).read_text())


def test_stack_is_isolated_pinned_bounded_and_read_only():
    compose = yaml.safe_load((DEPLOY / "docker-compose.observability.yml").read_text())
    assert compose["networks"]["observability"]["internal"] is True
    assert set(compose["services"]) == {"gateway", "otel-collector", "tempo", "loki", "prometheus", "grafana"}
    for name, service in compose["services"].items():
        if name != "gateway":
            assert service["networks"] == ["observability"]
            assert not service.get("ports")
    for service in compose["services"].values():
        assert ":latest" not in service["image"]
        assert "@sha256:" in service["image"]
        assert service["read_only"] is True
        assert service["mem_limit"].endswith("m")
        assert service["restart"] == "no"
        assert service["logging"]["options"] == {"max-size": "10m", "max-file": "2"}
        assert all(port.startswith("127.0.0.1:") for port in service.get("ports", []))
        for mount in service.get("volumes", []):
            source, _, mode = mount.rsplit(":", 2)
            assert mode == "ro"
            assert (DEPLOY / source).exists()
    for backend in ("tempo", "loki", "prometheus", "grafana"):
        assert all("size=" in mount for mount in compose["services"][backend]["tmpfs"])


@pytest.mark.parametrize("signal,exporter", [("traces", "otlp_http/tempo"), ("logs", "otlp_http/loki"), ("metrics", "prometheus")])
def test_collector_routes_three_signals_with_memory_and_batch_bounds(signal, exporter):
    config = load("otel-collector.yml")
    pipeline = config["service"]["pipelines"][signal]
    assert pipeline == {"receivers": ["otlp"], "processors": ["memory_limiter", "batch"], "exporters": [exporter]}
    assert config["processors"]["memory_limiter"]["limit_mib"] == 256
    assert config["processors"]["batch"]["send_batch_max_size"] == 512
    assert config["exporters"]["otlp_http/loki"]["endpoint"] == "http://loki:3100/otlp"
    assert config["exporters"]["prometheus"]["resource_constant_labels"]["included"] == [
        "service.name", "service.namespace", "deployment.environment.name"]


def test_backends_expire_data_and_do_not_index_ids():
    assert load("tempo.yml")["compactor"]["compaction"]["block_retention"] == "24h"
    loki = load("loki.yml")
    assert loki["limits_config"]["retention_period"] == "24h"
    assert loki["compactor"]["retention_enabled"] is True
    assert loki["limits_config"]["allow_structured_metadata"] is True
    labels = loki["limits_config"]["otlp_config"]["resource_attributes"]
    assert labels["ignore_defaults"] is True
    assert labels["attributes_config"][0]["attributes"] == [
        "service.name", "service.namespace", "deployment.environment.name"]


def test_dashboard_uses_three_provisioned_sources_and_structured_trace_links():
    datasources = {item["uid"]: item for item in load("grafana/provisioning/datasources/datasources.yml")["datasources"]}
    logs = datasources["newsagent-loki"]["jsonData"]["derivedFields"][0]
    assert logs["matcherType"] == "label"
    assert logs["matcherRegex"] == "trace_id"
    assert logs["datasourceUid"] == "newsagent-tempo"
    assert logs["url"] == "$${__value.raw}"
    trace = datasources["newsagent-tempo"]["jsonData"]["tracesToLogsV2"]
    assert trace["datasourceUid"] == "newsagent-loki"
    assert trace["query"] == '{$$__tags} | trace_id = "$${__span.traceId}"'
    dashboard = json.loads((CONFIG / "grafana/dashboards/newsagent.json").read_text())
    assert dashboard["uid"] == "newsagent-observability"
    used = {panel["datasource"]["uid"] for panel in dashboard["panels"] if "datasource" in panel}
    assert used == set(datasources)
    expressions = [target.get("expr", "") for panel in dashboard["panels"] for target in panel.get("targets", [])]
    assert any("newsagent_model_calls_total" in expr for expr in expressions)
    assert any('newsagent_operation=~".*validat.*"' in expr for expr in expressions)
