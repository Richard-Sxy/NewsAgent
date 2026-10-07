"""Security contracts for the isolated service and local fixed gateway template."""

from pathlib import Path
import re

import yaml


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def compose():
    return yaml.safe_load((DEPLOY / "docker-compose.analysis-service.yml").read_text())


def test_worker_service_remains_internal_only_without_published_ports():
    deployment = compose()
    service = deployment["services"]["analysis-service"]
    assert service["networks"] == ["analysis-private"]
    assert deployment["networks"]["analysis-private"]["internal"] is True
    assert not service.get("ports") and not service.get("network_mode")
    assert not service.get("volumes")
    assert not service.get("privileged")


def test_only_fixed_gateway_bridges_loopback_ingress_to_private_network():
    deployment = compose()
    gateway = deployment["services"]["analysis-gateway"]
    assert set(gateway["networks"]) == {"analysis-ingress", "analysis-private"}
    assert not deployment["networks"]["analysis-ingress"].get("internal", False)
    assert gateway["ports"] == ["127.0.0.1:28100:8080"]
    assert re.fullmatch(r"nginx:1\.28\.0-alpine@sha256:[0-9a-f]{64}", gateway["image"])
    assert "environment" not in gateway  # No token or model/database secrets.
    assert gateway["entrypoint"][0] == "nginx"
    assert gateway["volumes"] == ["./analysis-gateway.nginx.conf:/etc/nginx/analysis-gateway.conf:ro"]


def test_both_containers_have_no_socket_privilege_or_writable_root():
    for container in compose()["services"].values():
        assert container["read_only"] is True
        assert container["cap_drop"] == ["ALL"]
        assert container["security_opt"] == ["no-new-privileges:true"]
        assert container["user"] not in {"0", "0:0", "root"}
        assert container["pids_limit"] <= 32
        assert container["cpus"] <= 1
        assert container["mem_limit"] in {"384m", "64m"}
        assert container["logging"]["driver"] == "none"
        assert all("noexec,nosuid,size=8m" in mount for mount in container["tmpfs"])
        assert not any("docker.sock" in mount for mount in container.get("volumes", []))


def test_gateway_only_proxies_fixed_routes_with_bounded_streams_and_zero_retries():
    config = (DEPLOY / "analysis-gateway.nginx.conf").read_text()
    config = re.sub(r"#.*", "", config)
    assert re.findall(r"proxy_pass\s+([^;]+);", config) == [
        "http://fixed_analysis/health", "http://fixed_analysis/v1/analyze",
    ]
    assert "location = /health" in config and "location = /v1/analyze" in config
    assert "location / {" in config and "return 404" in config
    for directive in (
        "access_log off;", "client_max_body_size 256k;",
        "proxy_request_buffering off;", "proxy_buffering off;",
        "proxy_http_version 1.1;", "proxy_connect_timeout 1s;",
        "proxy_send_timeout 6s;", "proxy_read_timeout 6s;",
        "client_header_timeout 6s;", "client_body_timeout 6s;",
        "proxy_next_upstream off;", "proxy_ignore_client_abort off;",
        "server analysis-service:8100 resolve;",
        "resolver 127.0.0.11 valid=10s ipv6=off;",
    ):
        assert directive in config
    assert "$request_method != GET" in config and "$request_method != POST" in config
    assert '$request_uri ~ "[?]"' in config
    assert not re.search(r"proxy_pass\s+.*\$", config)
