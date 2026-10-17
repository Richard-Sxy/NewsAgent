"""Offline contracts for the repository's enterprise Kubernetes templates.

Only ``kubectl kustomize`` is invoked: these tests do not contact a cluster,
read a dotenv file, build images, or call enterprise dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml

from app.config import Settings
from app.knowledge.search_config import load_knowledge_search_config
from app.model_runtime.config_file import (
    load_model_runtime_config,
    validate_runtime_environment,
)


ROOT = Path(__file__).resolve().parents[2]
K8S = ROOT / "k8s"


@pytest.fixture(scope="module")
def rendered():
    kubectl = shutil.which("kubectl")
    if kubectl is None:
        pytest.skip("offline deployment contract tests require kubectl kustomize")
    result = {}
    for name, directory in (("base", K8S), ("tke", K8S / "tke"), ("migrate", K8S / "migrate")):
        process = subprocess.run(
            [kubectl, "kustomize", str(directory)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        result[name] = [item for item in yaml.safe_load_all(process.stdout) if item]
    return result


def resource(objects, kind, name):
    matches = [item for item in objects if item["kind"] == kind and item["metadata"]["name"] == name]
    assert len(matches) == 1, (kind, name)
    return matches[0]


def generated_config(objects, prefix):
    matches = [item for item in objects if item["kind"] == "ConfigMap"
               and item["metadata"]["name"].startswith(prefix + "-")]
    assert len(matches) == 1, prefix
    return matches[0]


@pytest.mark.parametrize("name", ["base", "tke", "migrate"])
def test_each_manifest_set_renders_locally_in_one_namespace(rendered, name):
    assert rendered[name]
    for item in rendered[name]:
        if item["kind"] == "Namespace":
            assert item["metadata"]["name"] == "news-agent"
        else:
            assert item["metadata"]["namespace"] == "news-agent"


def test_services_select_their_deployments_and_named_ports(rendered):
    base = rendered["base"]
    for name, service_port, container_port in (("newsagent-api", 8000, 8000), ("newsagent-frontend", 80, 8080)):
        deployment = resource(base, "Deployment", name)
        pod = deployment["spec"]["template"]
        selector = deployment["spec"]["selector"]["matchLabels"]
        assert all(pod["metadata"]["labels"].get(key) == value for key, value in selector.items())
        service = resource(base, "Service", name)
        assert service["spec"]["type"] == "ClusterIP"
        assert service["spec"]["selector"] == selector
        assert service["spec"]["ports"] == [{"name": "http", "port": service_port, "targetPort": "http"}]
        assert pod["spec"]["containers"][0]["ports"] == [{"name": "http", "containerPort": container_port}]
        pdb = resource(base, "PodDisruptionBudget", name)
        assert pdb["spec"]["selector"]["matchLabels"] == selector


def test_generated_config_references_and_read_only_mounts_are_resolved(rendered):
    base = rendered["base"]
    settings = generated_config(base, "newsagent-settings")
    runtime = generated_config(base, "newsagent-runtime")
    frontend = generated_config(base, "newsagent-frontend")
    runtime_names = {"newsagent-api", "newsagent-writing-worker", "newsagent-hot-news-worker", "newsagent-data-loop-worker"}
    for item in base:
        if item["kind"] != "Deployment":
            continue
        name = item["metadata"]["name"]
        pod = item["spec"]["template"]["spec"]
        container = pod["containers"][0]
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
        if name == "newsagent-frontend":
            assert volumes["config"]["configMap"]["name"] == frontend["metadata"]["name"]
            assert mounts["config"] == {"name": "config", "mountPath": "/usr/share/nginx/html/config.json", "subPath": "config.json", "readOnly": True}
            continue
        assert container["envFrom"] == [
            {"configMapRef": {"name": settings["metadata"]["name"]}},
            {"secretRef": {"name": "newsagent-secrets"}},
        ]
        if name in runtime_names:
            assert volumes["runtime"]["configMap"]["name"] == runtime["metadata"]["name"]
            assert mounts["runtime"] == {"name": "runtime", "mountPath": "/etc/newsagent/runtime", "readOnly": True}
            for key in ("MODEL_RUNTIME_CONFIG_PATH", "KNOWLEDGE_SEARCH_CONFIG_PATH"):
                path = Path(settings["data"][key])
                assert str(path.parent) == mounts["runtime"]["mountPath"]
                assert path.name in runtime["data"]


def test_templates_validate_with_the_actual_runtime_loaders(rendered):
    runtime = generated_config(rendered["base"], "newsagent-runtime")
    model = load_model_runtime_config(K8S / "config/model-runtime.yml")
    validate_runtime_environment(model, environment="production")
    assert yaml.safe_load(runtime["data"]["model-runtime.yml"]) == yaml.safe_load((K8S / "config/model-runtime.yml").read_text())
    for endpoint in (model.inference, model.embedding):
        assert endpoint.provider == "openai_compatible"
        assert endpoint.url.scheme == "https"
        assert endpoint.api_key_env
    for scene in ("qa_generation", "conversation", "conversation_memory", "text2sql_assistant", "research", "outline", "section", "revise", "assemble", "review", "hot_news_error_attribution"):
        binding = model.agent_scene(scene)
        model.prompt_registry().resolve(scene=binding.scene, version=binding.prompt_version)
        assert binding.model_route in model.inference.model_routes
    model.prompt_registry().resolve(scene="hot_news_analysis", version="approved-hot-news-prompt-v1")
    knowledge = load_knowledge_search_config(K8S / "config/knowledge-search.yml")
    assert knowledge.backend == "postgres"
    assert yaml.safe_load(runtime["data"]["knowledge-search.yml"]) == knowledge.model_dump(exclude_none=True)


def test_settings_validate_using_only_template_values_without_dotenv(rendered, monkeypatch):
    # Clear actual Settings environment sources; the test must never inherit a
    # developer's enterprise credentials or invoke the default .env reader.
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)
        monkeypatch.delenv(field.lower(), raising=False)
    config = generated_config(rendered["base"], "newsagent-settings")["data"]
    secret = yaml.safe_load((K8S / "secret.example.yml").read_text())["stringData"]
    values = {key.lower(): value for key, value in (config | secret).items()}
    settings = Settings(_env_file=None, **values)
    assert settings.environment == "production"
    assert settings.model_runtime_backend == "native"
    assert str(settings.database_url).startswith("postgresql+psycopg://")
    assert str(settings.redis_url).startswith("rediss://")
    assert settings.hot_news_dependencies_factory is None
    assert json.loads(settings.hot_news_runtime_manifest_json) == []
    assert json.loads(settings.hot_news_schedule_definitions_json) == []
    assert settings.cms_publish_url is None
    assert settings.artifact_sse_algorithm == "AES256"
    for field in ("conversation_enabled", "conversation_knowledge_enabled", "conversation_hot_news_query_enabled", "conversation_async_admission_enabled", "conversation_data_analysis_enabled", "data_loop_data_analysis_enabled"):
        assert getattr(settings, field) is False


def test_production_entrypoints_do_not_wire_demo_factories(rendered):
    expected = {
        "newsagent-writing-worker": "app.worker",
        "newsagent-outbox-worker": "app.outbox_worker",
        "newsagent-hot-news-worker": "app.hot_news_worker",
        "newsagent-data-loop-worker": "app.data_loop_worker",
    }
    for name, module in expected.items():
        deployment = resource(rendered["base"], "Deployment", name)
        assert deployment["spec"]["template"]["spec"]["containers"][0]["command"] == ["python", "-m", module]
    api = resource(rendered["base"], "Deployment", "newsagent-api")
    command = api["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[:2] == ["uvicorn", "app.main:app"]
    assert not any("examples." in json.dumps(item) or "local_simulation" in json.dumps(item) for item in rendered["base"])
    for name in ("newsagent-hot-news-worker", "newsagent-data-loop-worker"):
        assert resource(rendered["base"], "Deployment", name)["spec"]["replicas"] == 0


def test_secret_templates_are_external_and_never_browser_configuration(rendered):
    assert not any(item["kind"] == "Secret" for objects in rendered.values() for item in objects)
    secret = yaml.safe_load((K8S / "secret.example.yml").read_text())
    assert secret["kind"] == "Secret" and secret["metadata"]["name"] == "newsagent-secrets"
    secret_keys = set(secret["stringData"])
    assert {"DATABASE_URL", "REDIS_URL", "ENTERPRISE_MODEL_API_KEY", "ENTERPRISE_EMBEDDING_API_KEY", "DATA_LOOP_GATEWAY_TOKEN", "KNOWLEDGE_INGEST_TOKEN"} <= secret_keys
    for item in rendered["base"]:
        if item["kind"] == "ConfigMap":
            assert not secret_keys.intersection(item["data"])
    browser = json.loads(generated_config(rendered["base"], "newsagent-frontend")["data"]["config.json"])
    assert browser == json.loads((K8S / "config/frontend.json").read_text())
    assert browser["apiBaseUrl"] == "" and browser["tenantId"] == ""
    assert browser["environment"] == "production" and browser["enableHotNews"] is False
    assert not any(word in key.lower() for key in browser for word in ("token", "secret", "password", "authorization", "apikey"))


def test_migrations_are_a_separate_one_shot_job_with_a_dedicated_secret(rendered):
    assert not any(item["kind"] in {"Job", "CronJob"} for item in rendered["base"])
    for item in rendered["base"]:
        if item["kind"] == "Deployment":
            assert not item["spec"]["template"]["spec"].get("initContainers")
    assert len(rendered["migrate"]) == 1
    job = resource(rendered["migrate"], "Job", "newsagent-migrate")
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never" and job["spec"]["backoffLimit"] == 0
    container = pod["containers"][0]
    assert container["workingDir"] == "/app"
    assert container["command"] == ["alembic", "upgrade", "head"]
    assert not container.get("envFrom")
    assert container["env"] == [{"name": "DATABASE_URL", "valueFrom": {"secretKeyRef": {"name": "newsagent-migration-secrets", "key": "DATABASE_URL"}}}]
    secret = yaml.safe_load((K8S / "migration-secret.example.yml").read_text())
    assert secret["metadata"]["name"] == "newsagent-migration-secrets"
    assert set(secret["stringData"]) == {"DATABASE_URL"}


def test_tke_ingress_addition_routes_api_exclusively_through_the_gateway(rendered):
    assert not any(item["kind"] == "Ingress" for item in rendered["base"])
    assert not any(item["kind"] == "Deployment" for item in rendered["tke"])
    ingress = resource(rendered["tke"], "Ingress", "newsagent")
    paths = ingress["spec"]["rules"][0]["http"]["paths"]
    assert {path["path"]: path["backend"]["service"]["name"] for path in paths} == {
        "/api": "newsagent-gateway", "/": "newsagent-frontend-ingress",
    }
    assert all(path["pathType"] == "Prefix" for path in paths)
    assert not any(path["backend"]["service"]["name"] == "newsagent-api" for path in paths)
    for name in ("newsagent-gateway", "newsagent-frontend-ingress"):
        assert resource(rendered["tke"], "Service", name)["spec"]["type"] == "NodePort"
    frontend = resource(rendered["tke"], "Service", "newsagent-frontend-ingress")
    assert frontend["spec"]["selector"] == resource(rendered["base"], "Service", "newsagent-frontend")["spec"]["selector"]
    assert frontend["spec"]["ports"] == [{"name": "http", "port": 80, "targetPort": "http"}]
    gateway = resource(rendered["tke"], "Service", "newsagent-gateway")
    policy = resource(rendered["base"], "NetworkPolicy", "newsagent-api-from-gateway")
    peer = policy["spec"]["ingress"][0]["from"][0]
    assert peer == {"podSelector": {"matchLabels": gateway["spec"]["selector"]}}
    assert policy["spec"]["ingress"][0]["ports"] == [{"port": 8000, "protocol": "TCP"}]
