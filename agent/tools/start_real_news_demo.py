#!/usr/bin/env python3
"""Start a separate real-content demo with synthetic metrics and local model Ports."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sys

TOOLS_DIR = str(Path(__file__).resolve().parent)
if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)

from deploy_bge_demo import (  # noqa: E402
    APP_SERVICES, GIB, INFRA_SERVICES, BgeDemoDeployment, DeploymentError, DeploymentOptions,
)


PROJECT = "news-agent-real-news-demo"
APP_IMAGE = "news-agent/conversation-hooks-bge-demo:local"
WEB_IMAGE = "news-agent/frontend:real-news-demo"
WEB_RUNTIME_IMAGE = "news-agent/frontend:conversation-hooks-demo"
OVERLAYS = (
    "docker-compose.native-e2e.yml", "docker-compose.bge-snapshot.yml",
    "docker-compose.bge-demo.yml", "docker-compose.conversation-hooks-bge-demo.yml",
    "docker-compose.real-news-demo.yml",
)
INCREMENTAL_OVERLAY = "docker-compose.incremental-news-demo.yml"
INCREMENTAL_CONFIG = "knowledge-search.incremental-20261014-v1.yml"
INCREMENTAL_PACKAGE = "../var/agent/data/news-bge-incremental-20261014-v1"
INCREMENTAL_INDEX_VERSIONS = {
    "v1": (INCREMENTAL_OVERLAY, INCREMENTAL_CONFIG, INCREMENTAL_PACKAGE),
    "v2": ("docker-compose.bulk-news-demo.yml", "knowledge-search.incremental-20261014-v2.yml",
           "../var/agent/data/news-bge-incremental-20261014-v2"),
}
CATALOG = "data/corpus-news-v6/catalog.json"
SCHEMA = "docs/sql-warehouse/sql-warehouse-schema-v6.md"
RUNTIME_PROBE = """
import importlib.metadata as m
expected = {'numpy':'2.4.6', 'hnswlib':'0.8.0', 'sentence-transformers':'6.0.1', 'torch':'2.8.0+cpu'}
assert all(m.version(k) == v for k, v in expected.items()), 'BGE dependency contract mismatch'
import torch
assert torch.version.cuda is None, 'CPU-only PyTorch required'
from app.sql_assistant.corpus_profiles import corpus_catalog
from app.sql_assistant.warehouse import verify_schema_contract
catalog = corpus_catalog()
assert len(catalog['articles']) == 120, 'real-content catalog count mismatch'
verify_schema_contract('corpus-v6')
print('local dependency, 120-article catalog and v6 schema contracts verified')
"""


def _constant(path: Path, name: str) -> str:
    """Read trusted version constants without importing host-side dependencies."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                value = ast.literal_eval(node.value)
                if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                    return value
    except (OSError, SyntaxError, ValueError):
        pass
    raise DeploymentError(f"无法读取冻结版本常量 {path.name}:{name}。")


class RealNewsDemoDeployment(BgeDemoDeployment):
    """Reuse bounded/local Docker helpers while fixing the project and assets."""

    def __init__(self, options: DeploymentOptions, environ: dict | None = None, *, incremental_news_index: bool = False,
                 incremental_news_version: str = "v1"):
        super().__init__(options, environ)
        self.incremental_news_index = incremental_news_index
        if incremental_news_version not in INCREMENTAL_INDEX_VERSIONS:
            raise DeploymentError("未知增量新闻索引版本。")
        self.index_overlay, self.index_config, self.index_package = INCREMENTAL_INDEX_VERSIONS[incremental_news_version]
        self.environ["NEWSAGENT_REAL_API_PORT"] = str(options.api_port)
        self.environ["NEWSAGENT_REAL_WEB_PORT"] = str(options.web_port)

    def compose_command(self, *args: str, runtime: bool = False) -> list[str]:
        command = ["docker", "compose", "--env-file", os.devnull, "-p", PROJECT]
        overlays = (*OVERLAYS, self.index_overlay) if self.incremental_news_index else OVERLAYS
        for name in overlays:
            command.extend(("-f", str(self.root.parent / "deploy" / name)))
        return [*command, *args]

    def _validate_files(self) -> None:
        o = self.options
        if o.model != "local":
            raise DeploymentError("真实新闻演示固定使用本地模型，不接受企业端点或密钥。")
        if any(not 1024 <= p <= 65535 for p in (o.api_port, o.web_port)) or o.api_port == o.web_port:
            raise DeploymentError("API/前端端口必须不同，并位于 1024–65535。")
        if not 2 <= o.min_free_gib <= 1024:
            raise DeploymentError("最小磁盘余量必须位于 2–1024 GiB。")
        required = [*(f"../deploy/{name}" for name in OVERLAYS), CATALOG, SCHEMA,
            "app/sql_assistant/corpus_profiles.py", "app/sql_assistant/warehouse.py",
            "../deploy/text2sql-scenes.corpus-v6.yml", "../deploy/conversation-hooks.model-runtime.local.yml",
            "../deploy/knowledge-search.bge-snapshot.yml", "../deploy/bge-demo.nginx.conf",
            "../deploy/bge-demo.frontend.json", "frontend/Dockerfile.conversation-hooks.cached"]
        if not o.skip_build:
            required.append("frontend/dist/index.html")
        if self.incremental_news_index:
            required.extend((f"../deploy/{self.index_overlay}", f"../deploy/{self.index_config}",
                             f"{self.index_package}/SHA256SUMS", f"{self.index_package}/build_report.json"))
        missing = [p for p in required if not (self.root / p).is_file()]
        package = self.root.parent / "var/knowledge-snapshots/rag-recall-demo"
        missing.extend(f"../var/knowledge-snapshots/rag-recall-demo/{p}" for p in (
            "SHA256SUMS", "data/experiments/offline_recall_fg/chunks.jsonl",
            "data/experiments/offline_recall_fg/embeddings.npy", "data/experiments/offline_recall_fg/index.bin",
            "data/models/bge-large-zh-v1.5/modules.json") if not (package / p).is_file())
        if missing:
            raise DeploymentError("缺少部署文件：" + "、".join(missing) + "。前端请先执行 npm run build。")
        for relative, module, constant in (
            (CATALOG, "app/sql_assistant/corpus_profiles.py", "CORPUS_CATALOG_SHA256"),
            (SCHEMA, "app/sql_assistant/warehouse.py", "SCHEMA_SHA256_V6"),
        ):
            if hashlib.sha256((self.root / relative).read_bytes()).hexdigest() != _constant(self.root / module, constant):
                raise DeploymentError(f"冻结文件 {relative} 的 SHA-256 不匹配；请使用新版本，不能绕过校验。")
        try:
            catalog = json.loads((self.root / CATALOG).read_text(encoding="utf-8"))
            if not isinstance(catalog, dict) or len(catalog.get("articles", [])) != 120:
                raise ValueError("invalid count")
        except (OSError, ValueError, TypeError) as exc:
            raise DeploymentError("corpus-v6 必须包含完整的 120 篇冻结新闻。") from exc

    def _check_ports(self) -> None:
        rows = self._project_status()
        for port, service in ((self.options.api_port, "api"), (self.options.web_port, "web")):
            owned = any(row.get("Project") == PROJECT and row.get("Service") == service
                and row.get("State") == "running" and any(p.get("PublishedPort") == port
                    and p.get("URL") == "127.0.0.1" for p in row.get("Publishers") or []) for row in rows)
            if owned:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as exc:
                    raise DeploymentError(f"端口 {port} 被其他实例占用；请选择空闲端口。") from exc

    def preflight(self) -> None:
        self._set_stage("1/6 只读预检：真实新闻目录与独立项目")
        self._validate_files()
        free = shutil.disk_usage(self.root).free / GIB
        if free < self.options.min_free_gib:
            raise DeploymentError(f"磁盘余量 {free:.1f} GiB 低于预算；脚本不会删除任何数据。")
        if shutil.which("docker", path=self.environ.get("PATH")) is None:
            raise DeploymentError("未找到 Docker，请先打开本机 Docker Desktop。")
        self._validate_local_docker()
        self._run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
        version = self._run(["docker", "compose", "version", "--short"], timeout=10)
        found = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if found is None or tuple(map(int, found.groups())) < (2, 24, 4):
            raise DeploymentError("需要 Docker Compose 2.24.4 或更新版本。")
        try:
            if int(self._run(["docker", "info", "--format", "{{.MemTotal}}"], timeout=10)) < 6 * GIB:
                raise DeploymentError("Docker 内存不足 6 GiB，无法可靠加载 API/Worker 的本地 BGE。")
        except ValueError as exc:
            raise DeploymentError("无法确认 Docker 内存配置。") from exc
        self._check_ports()
        self._run(self.compose_command("config", "--quiet"), timeout=15)
        for image in (APP_IMAGE, WEB_IMAGE if self.options.skip_build else WEB_RUNTIME_IMAGE):
            self._run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], timeout=10)
        print(f"项目：{PROJECT}；120 篇真实缓存新闻；行为指标/基线与模型均为本地模拟。", flush=True)

    def _verify_runtime_contract(self) -> None:
        command = ["docker", "run", "--rm", "--network", "none", "--read-only", "--pull", "never",
            "--entrypoint", "python"]
        for directory in ("app", "docs", "data"):
            command.extend(("--mount", f"type=bind,src={self.root / directory},dst=/app/{directory},readonly"))
        self._run([*command, APP_IMAGE, "-c", RUNTIME_PROBE], timeout=60)

    def deploy(self) -> None:
        self.preflight()
        self._set_stage("2/6 校验本地依赖镜像并离线组装前端")
        self._verify_runtime_contract()
        if not self.options.skip_build:
            self._run(self.compose_command("--progress", "plain", "build", "web"), timeout=300, capture=False)
        self._set_stage("3/6 启动新项目基础服务")
        self._run(self.compose_command("up", "-d", "--no-build", "--pull", "never", *INFRA_SERVICES),
            timeout=600, capture=False)
        self._wait_for_infrastructure()
        self._set_stage("4/6 迁移新项目数据库")
        self._run(self.compose_command("run", "--rm", "--no-deps", "--pull", "never", "migrate"),
            timeout=300, capture=False)
        self._set_stage("5/6 启动 API、三个 Worker、Outbox 与前端")
        self._run(self.compose_command("up", "-d", "--no-deps", "--no-build", "--pull", "never", *APP_SERVICES),
            timeout=600, capture=False)
        self._set_stage("6/6 检查 API、前端、模型与真实 Worker poller")
        self._wait_until_ready()
        print(f"部署就绪：http://127.0.0.1:{self.options.web_port}/chat\n"
            f"API：http://127.0.0.1:{self.options.api_port}/ready\n"
            "示例：查询点击量最高的前5条图文新闻并分析原因。\n"
            "新闻标题/正文/链接来自冻结公开缓存；30 天指标窗口为合成，不能解释为真实流量。\n"
            "旧 BGE 演示与历史保留；写作、记忆晋升及配置发布仍遵守人工审批。", flush=True)

    def stop(self) -> None:
        self._set_stage("停止真实新闻独立演示，保留数据卷与历史")
        self._validate_local_docker()
        self._run(self.compose_command("stop"), timeout=120, capture=False)


def argument_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="deploy", choices=("deploy", "check", "status", "stop"))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="只读检查，既不构建也不启动应用")
    modes.add_argument("--status", action="store_true", help="只读查看本项目服务状态")
    modes.add_argument("--stop", action="store_true", help="停止本项目服务并保留数据卷")
    parser.add_argument("--api-port", type=int, default=28070)
    parser.add_argument("--web-port", type=int, default=5179)
    parser.add_argument("--min-free-gib", type=float, default=10)
    parser.add_argument("--skip-build", action="store_true", help="使用已存在的 real-news-demo 前端镜像")
    parser.add_argument("--incremental-news-index", action="store_true",
                        help="加载10月7—14日增量新闻BGE快照，保留原有v6模拟指标")
    parser.add_argument("--incremental-news-version", choices=tuple(INCREMENTAL_INDEX_VERSIONS), default="v1",
                        help="v1为原541篇增量；v2为每日千篇扩容，选择v2会启用增量索引")
    return parser


def main(argv=None) -> int:
    args = argument_parser().parse_args(argv)
    options = DeploymentOptions(agent_root=Path(__file__).resolve().parents[1], model="local",
        api_port=args.api_port, web_port=args.web_port, min_free_gib=args.min_free_gib, skip_build=args.skip_build)
    deployment = RealNewsDemoDeployment(options,
        incremental_news_index=args.incremental_news_index or args.incremental_news_version != "v1",
        incremental_news_version=args.incremental_news_version)
    action = "check" if args.check else "status" if args.status else "stop" if args.stop else args.action
    try:
        getattr(deployment, action)()
    except DeploymentError as exc:
        print(f"部署停止：{deployment._redact(str(exc))}\n"
            "检查服务：python3 agent/tools/start_real_news_demo.py --status", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("已取消；已经启动的本项目服务和历史保留，可用 --status/--stop 管理。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
