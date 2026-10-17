#!/usr/bin/env python3
"""Start the isolated local NewsAgent/BGE stack using only Python's stdlib."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener


PROJECT = "news-agent-bge-demo"
APP_IMAGE = "news-agent/writing-agent-service:bge-demo"
WEB_IMAGE = "news-agent/frontend:bge-demo"
MODEL_KEY = "NEWSAGENT_MODEL_API_KEY"
APP_SERVICES = ("api", "hot-news-worker", "writing-worker", "outbox-worker", "data-loop-worker", "web")
INFRA_SERVICES = ("postgres", "temporal-postgres", "redis", "temporal", "minio", "minio-init")
QUEUES = ("hot-news", "news-writing", "hot-news-data-loop")
GIB = 1024 ** 3

# Probe the actual SDK contract instead of depending on a particular CLI JSON
# format. Only counts of recent pollers leave the container, never its config.
POLLER_PROBE = """
import asyncio, json
from datetime import datetime, timezone, timedelta
from temporalio.client import Client
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.api.taskqueue.v1 import TaskQueue
async def main():
    client = await Client.connect("temporal:7233", namespace="default")
    result = {}
    for queue in ("hot-news", "news-writing", "hot-news-data-loop"):
        result[queue] = {}
        for label, kind in (("workflow", 1), ("activity", 2)):
            response = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(namespace="default",
                    task_queue=TaskQueue(name=queue), task_queue_type=kind),
                timeout=timedelta(seconds=3))
            now = datetime.now(timezone.utc)
            result[queue][label] = sum(
                -5 <= (now - p.last_access_time.ToDatetime(tzinfo=timezone.utc)).total_seconds() <= 90
                for p in response.pollers)
    print(json.dumps(result))
asyncio.run(main())
"""


class DeploymentError(RuntimeError):
    """A bounded, actionable deployment failure."""


@dataclass(frozen=True)
class DeploymentOptions:
    agent_root: Path
    model: str = "compatible"
    api_port: int = 28060
    web_port: int = 5178
    min_free_gib: float = 10
    skip_build: bool = False


class BgeDemoDeployment:
    def __init__(self, options: DeploymentOptions, environ: dict | None = None):
        self.options = options
        self.root = Path(options.agent_root).resolve()
        self.environ = dict(os.environ if environ is None else environ)
        # Explicit files/project and stable dataset sizing must win over unrelated
        # shell/Compose defaults. Docker connection/auth settings stay intact.
        for key in list(self.environ):
            if key.startswith("COMPOSE_"):
                self.environ.pop(key)
        self.environ["NEWSAGENT_BGE_API_PORT"] = str(options.api_port)
        self.environ["NEWSAGENT_BGE_WEB_PORT"] = str(options.web_port)
        self.stage = "初始化"
        self._http = build_opener(ProxyHandler({}))
        self._docker_connection: list[str] = []

    def compose_command(self, *args: str, runtime: bool = False) -> list[str]:
        files = ["docker-compose.native-e2e.yml", "docker-compose.bge-snapshot.yml", "docker-compose.bge-demo.yml"]
        if runtime and self.options.model == "compatible":
            files.append("docker-compose.timeline-query-model.yml")
        command = ["docker", "compose", "--env-file", os.devnull, "-p", PROJECT]
        for file in files:
            command.extend(("-f", str(self.root.parent / "deploy" / file)))
        return [*command, *args]

    def _environment(self, runtime: bool) -> dict:
        result = dict(self.environ)
        # Connection selection is validated and pinned by explicit CLI flags.
        result.pop("DOCKER_HOST", None)
        result.pop("DOCKER_CONTEXT", None)
        result.pop("BUILDKIT_HOST", None)
        # The daemon/context's default builder is local. Never inherit a
        # selected remote/cloud builder when sending this checkout's context.
        result["BUILDX_BUILDER"] = "default"
        if not runtime or self.options.model == "local":
            result.pop(MODEL_KEY, None)
        return result

    def _redact(self, value: str) -> str:
        key = self.environ.get(MODEL_KEY)
        return value.replace(key, "[模型密钥已隐藏]") if key else value

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()

    def _run(self, command: list[str], *, runtime: bool = False,
             timeout: float = 30, capture: bool = True) -> str:
        """No shell, no automatic retries, no logging of environment values."""
        if command and command[0] == "docker" and self._docker_connection:
            command = ["docker", *self._docker_connection, *command[1:]]
        try:
            process = subprocess.Popen(
                command, cwd=self.root, env=self._environment(runtime), text=True,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise DeploymentError(f"{self.stage}：无法运行 {command[0]}。请检查安装和 PATH。") from exc
        try:
            if capture:
                output, _ = process.communicate(timeout=timeout)
            else:
                def forward():
                    assert process.stdout is not None
                    for line in process.stdout:
                        print(self._redact(line.rstrip()), flush=True)
                reader = threading.Thread(target=forward, daemon=True)
                reader.start()
                process.wait(timeout=timeout)
                reader.join(timeout=3)
                output = ""
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            raise DeploymentError(
                f"{self.stage}：命令在 {timeout:g} 秒内未完成。Docker 检查超时时，先在 Desktop 恢复引擎；"
                "本脚本不会重启 Docker 或清理数据。"
            ) from exc
        except KeyboardInterrupt:
            self._terminate(process)
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
        if process.returncode:
            detail = self._redact(output.strip())[-1800:] if capture else "具体错误见上方输出。"
            raise DeploymentError(f"{self.stage}：命令失败（exit={process.returncode}）。{detail}")
        return output

    def _read_json(self, command: list[str], *, runtime: bool = False, timeout: float = 30):
        raw = self._run(command, runtime=runtime, timeout=timeout).strip()
        if not raw:
            return []
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            try:
                return [json.loads(line) for line in raw.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise DeploymentError(f"{self.stage}：Docker 返回了无法识别的状态数据。") from exc

    def _set_stage(self, value: str) -> None:
        self.stage = value
        print(f"\n[{value}]", flush=True)

    def _validate_files(self) -> None:
        o = self.options
        if o.model not in {"compatible", "local"}:
            raise DeploymentError("模型模式只能是 compatible 或 local。")
        if any(not 1024 <= port <= 65535 for port in (o.api_port, o.web_port)) or o.api_port == o.web_port:
            raise DeploymentError("API/前端端口必须不同，并位于 1024–65535。")
        if not 2 <= o.min_free_gib <= 1024:
            raise DeploymentError("最小磁盘余量必须位于 2–1024 GiB。")
        required = [
            "Dockerfile.e2e", "Dockerfile.e2e.dockerignore",
            "../deploy/docker-compose.native-e2e.yml", "../deploy/docker-compose.bge-snapshot.yml",
            "../deploy/docker-compose.bge-demo.yml", "../deploy/knowledge-search.bge-snapshot.yml",
            "../deploy/bge-demo.nginx.conf", "../deploy/bge-demo.frontend.json",
            "../deploy/text2sql-scenes.timeline-v4.yml", "frontend/Dockerfile.bge-demo",
            "frontend/Dockerfile.bge-demo.dockerignore", "frontend/package-lock.json",
        ]
        if o.model == "compatible":
            required.extend(("../deploy/docker-compose.timeline-query-model.yml", "../deploy/model-runtime.compatible.yml"))
        missing = [name for name in required if not (self.root / name).is_file()]
        package = self.root.parent / "var/knowledge-snapshots/rag-recall-demo"
        package_files = ("SHA256SUMS", "data/experiments/offline_recall_fg/chunks.jsonl",
                         "data/experiments/offline_recall_fg/embeddings.npy",
                         "data/experiments/offline_recall_fg/index.bin",
                         "data/models/bge-large-zh-v1.5/modules.json")
        missing.extend(f"../var/knowledge-snapshots/rag-recall-demo/{name}" for name in package_files if not (package / name).is_file())
        if missing:
            raise DeploymentError("缺少部署文件：" + "、".join(missing) +
                                  "。企业模式沿用已有 model-runtime.compatible.yml；本脚本不读取或生成其中的配置。")

    def _ensure_model_key(self) -> None:
        if self.options.model != "compatible":
            return
        key = self.environ.get(MODEL_KEY, "").strip()
        if not key:
            if not sys.stdin.isatty():
                raise DeploymentError(
                    f"企业查询模型缺少 {MODEL_KEY}。请在交互终端运行，脚本会安全询问；"
                    "或事先在当前终端注入该变量。"
                )
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", getpass.GetPassWarning)
                    key = getpass.getpass("请输入现有企业模型 API Key（输入不显示）：").strip()
            except (EOFError, getpass.GetPassWarning) as exc:
                raise DeploymentError("未完成安全密钥输入。请在可隐藏输入的交互终端重试或预先注入模型密钥。") from exc
        if not key or any(ord(char) < 32 or ord(char) == 127 for char in key):
            raise DeploymentError("企业模型密钥为空或格式无效。")
        self.environ[MODEL_KEY] = key

    def _project_status(self) -> list[dict]:
        result = self._read_json(self.compose_command("ps", "-a", "--format", "json"), timeout=10)
        return result if isinstance(result, list) else [result]

    def _check_ports(self) -> None:
        existing = self._project_status()
        for port, service in ((self.options.api_port, "api"), (self.options.web_port, "web")):
            owned = any(row.get("Service") == service and row.get("Project") == PROJECT
                        and row.get("State") == "running"
                        and any(pub.get("PublishedPort") == port and pub.get("URL") == "127.0.0.1"
                                for pub in row.get("Publishers") or []) for row in existing)
            if owned:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                try:
                    probe.bind(("127.0.0.1", port))
                except OSError as exc:
                    raise DeploymentError(f"端口 {port} 被其他实例占用。使用 --api-port/--web-port 选择空闲端口。") from exc

    def _validate_local_docker(self) -> None:
        # Docker gives DOCKER_CONTEXT precedence over DOCKER_HOST. Resolve that
        # exact selection, then pin every command to it, including status/stop.
        context = self.environ.get("DOCKER_CONTEXT", "").strip()
        host = self.environ.get("DOCKER_HOST", "").strip()
        if context or not host:
            context = context or self._run(["docker", "context", "show"], timeout=10).strip()
            endpoint = self._read_json(
                ["docker", "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"],
                timeout=10,
            )
            connection = ["--context", context]
        else:
            endpoint, connection = host, ["--host", host]
        local = isinstance(endpoint, str) and endpoint.startswith(("unix://", "npipe://"))
        if isinstance(endpoint, str) and endpoint.startswith(("tcp://", "http://", "https://")):
            parsed = urlsplit(endpoint)
            if parsed.username or parsed.password:
                raise DeploymentError("Docker 本地连接地址不能包含凭证。")
            try:
                local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                local = parsed.hostname == "localhost"
        if not local:
            raise DeploymentError("本入口只部署到本地 Docker 引擎。请切回 Docker Desktop/本机 context。")
        self._docker_connection = connection

    def preflight(self) -> None:
        self._set_stage("1/6 只读预检")
        self._validate_files()
        free = shutil.disk_usage(self.root).free / GIB
        if free < self.options.min_free_gib:
            raise DeploymentError(
                f"磁盘只剩 {free:.1f} GiB，低于本次预算 {self.options.min_free_gib:g} GiB。"
                "先释放空间；脚本不会删除文件、镜像或数据卷。"
            )
        if shutil.which("docker", path=self.environ.get("PATH")) is None:
            raise DeploymentError("未找到 Docker。请先安装并打开 Docker Desktop。")
        self._validate_local_docker()
        self._run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=10)
        version = self._run(["docker", "compose", "version", "--short"], timeout=10).strip()
        found = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
        if found is None or tuple(map(int, found.groups())) < (2, 24, 4):
            raise DeploymentError("本部署配置需要 Docker Compose 2.24.4 或更新版本（支持 !override）。")
        memory = self._run(["docker", "info", "--format", "{{.MemTotal}}"], timeout=10).strip()
        try:
            if int(memory) < 6 * GIB:
                raise DeploymentError("Docker 内存少于 6 GiB。请在 Desktop 分配足够内存以加载 API/Worker 的 BGE。")
        except ValueError as exc:
            raise DeploymentError("无法确认 Docker 内存配置。") from exc
        self._check_ports()
        # Build validation never evaluates the secret-requiring overlay.
        self._run(self.compose_command("config", "--quiet"), timeout=15)
        self._ensure_model_key()
        self._run(self.compose_command("config", "--quiet", runtime=True), runtime=True, timeout=15)
        if self.options.skip_build:
            for image in (APP_IMAGE, WEB_IMAGE):
                self._run(["docker", "image", "inspect", image, "--format", "{{.Id}}"], timeout=10)
        print(f"项目：{PROJECT}；磁盘余量：{free:.1f} GiB；查询理解：{self.options.model}", flush=True)

    def check(self) -> None:
        self.preflight()
        print("预检通过；未构建镜像或启动应用。", flush=True)

    def _wait_for_infrastructure(self, timeout: float = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = {row.get("Service"): row for row in self._project_status()}
            for name in INFRA_SERVICES:
                row = rows.get(name, {})
                if row.get("State") in {"exited", "dead"} and (name != "minio-init" or row.get("ExitCode") != 0):
                    raise DeploymentError(f"{self.stage}：{name} 已退出，后续服务未启动。请执行 status 检查。")
            init = rows.get("minio-init", {})
            if all(rows.get(name, {}).get("Health") == "healthy" for name in INFRA_SERVICES if name != "minio-init") \
                    and init.get("State") == "exited" and init.get("ExitCode") == 0:
                return
            time.sleep(2)
        raise DeploymentError(f"{self.stage}：基础服务在 {timeout:g} 秒内未就绪。请执行 status 检查。")

    def _fetch_json(self, url: str):
        with self._http.open(Request(url, headers={"Accept": "application/json"}), timeout=3) as response:
            return json.load(response)

    def _verify_pollers(self) -> bool:
        data = self._read_json(
            self.compose_command("exec", "-T", "api", "python", "-c", POLLER_PROBE), timeout=25,
        )
        return isinstance(data, dict) and all(
            isinstance(data.get(queue), dict)
            and all(isinstance(data[queue].get(kind), int) and data[queue][kind] > 0
                    for kind in ("workflow", "activity")) for queue in QUEUES
        )

    def _wait_until_ready(self, timeout: float = 600) -> None:
        deadline, next_notice = time.monotonic() + timeout, 0.0
        expected = "openai_compatible" if self.options.model == "compatible" else "local"
        while time.monotonic() < deadline:
            rows = {row.get("Service"): row for row in self._project_status()}
            for name in APP_SERVICES:
                if rows.get(name, {}).get("State") in {"exited", "dead"}:
                    raise DeploymentError(f"{self.stage}：{name} 已退出。请执行 status，按文档检查该服务日志。")
            try:
                ready = self._fetch_json(f"http://127.0.0.1:{self.options.api_port}/ready")
                with self._http.open(f"http://127.0.0.1:{self.options.web_port}/healthz", timeout=3) as response:
                    web_ready = response.status == 200
                runtime = self._fetch_json(
                    f"http://127.0.0.1:{self.options.web_port}/api/v1/conversations/runtime",
                )
                provider = ((runtime.get("query_scope") or {}).get("understanding_model") or {}).get("provider")
                if runtime.get("query_enabled") and provider != expected:
                    raise DeploymentError(f"{self.stage}：查询理解模型未按 {self.options.model} 配置启动。")
                if ready.get("status") == "ready" and web_ready and runtime.get("query_enabled") \
                        and all(rows.get(name, {}).get("State") == "running" for name in APP_SERVICES) \
                        and self._verify_pollers():
                    return
            except (OSError, URLError, ValueError):
                pass
            if time.monotonic() >= next_notice:
                print("等待：API 初始化样本与 BGE、前端代理、三个 Worker 队列就绪……", flush=True)
                next_notice = time.monotonic() + 15
            time.sleep(2)
        raise DeploymentError(f"{self.stage}：服务在 {timeout:g} 秒内未全部就绪。保留当前实例，请执行 status 定位。")

    def deploy(self) -> None:
        self.preflight()
        self._set_stage("2/6 构建业务和前端镜像")
        if not self.options.skip_build:
            self._run(self.compose_command("--progress", "plain", "build", "migrate", "web"),
                      timeout=3600, capture=False)
        else:
            contract = (
                "import importlib.metadata as m;"
                "expected={'numpy':'2.4.6','hnswlib':'0.8.0','sentence-transformers':'6.0.1','torch':'2.8.0+cpu'};"
                "assert all(m.version(k)==v for k,v in expected.items()),'BGE dependency contract mismatch';"
                "import torch;assert torch.version.cuda is None,'BGE demo requires CPU-only PyTorch'"
            )
            self._run(["docker", "run", "--rm", "--network", "none", "--read-only",
                       "--entrypoint", "python", APP_IMAGE, "-c", contract], timeout=30)
            print("复用已验证的本项目镜像。", flush=True)
        self._set_stage("3/6 启动基础服务")
        self._run(self.compose_command("up", "-d", *INFRA_SERVICES), timeout=600, capture=False)
        self._wait_for_infrastructure()
        self._set_stage("4/6 执行本项目数据库迁移")
        self._run(self.compose_command("run", "--rm", "--no-deps", "migrate"), timeout=300, capture=False)
        self._set_stage("5/6 启动 API、Worker 和前端")
        self._run(self.compose_command("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                                     *APP_SERVICES, runtime=True), runtime=True, timeout=600, capture=False)
        self._set_stage("6/6 验证 API、前端代理和真实 Worker poller")
        self._wait_until_ready()
        model_note = ("企业模型用于查询理解，其他分析使用本地测试 Port。"
                      if self.options.model == "compatible" else "查询理解和分析使用本地测试 Port。")
        print(f"\n部署就绪： http://127.0.0.1:{self.options.web_port}/chat\n"
              f"API 就绪检查： http://127.0.0.1:{self.options.api_port}/ready\n"
              "示例：查询点击率最高的前5条视频新闻并分析原因 → 解释第一条新闻 → 检索第一条的相关新闻\n"
              f"样本为 timeline-v4 合成数据；{model_note}BGE 使用真实本地快照。\n"
              "部署就绪不代表企业接口效果验收；发送示例问题验证实际查询链路。",
              flush=True)

    def status(self) -> None:
        self._set_stage("本项目服务状态")
        self._validate_local_docker()
        for row in self._project_status():
            # Whitelist public status fields; never inspect or print Config.Env.
            print(json.dumps({key: row.get(key) for key in ("Service", "State", "Health", "ExitCode")},
                             ensure_ascii=False), flush=True)
        print(f"聊天入口：http://127.0.0.1:{self.options.web_port}/chat", flush=True)

    def stop(self) -> None:
        self._set_stage("停止独立 BGE 演示，保留数据卷")
        self._validate_local_docker()
        self._run(self.compose_command("stop"), timeout=120, capture=False)


def argument_parser(*, local_only: bool = False):
    description = ("启动完整本地模拟：前端、API、Worker、数据库和真实 BGE 快照；无需企业模型密钥。"
                   if local_only else __doc__)
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("action", nargs="?", default="deploy", choices=("deploy", "check", "status", "stop"))
    parser.add_argument("--check", action="store_true", help="仅执行只读预检")
    if local_only:
        parser.set_defaults(model="local")
    else:
        parser.add_argument("--model", choices=("compatible", "local"), default="compatible",
                            help="compatible 沿用已有企业查询理解；local 使用测试规则 Port")
    parser.add_argument("--api-port", type=int, default=28060)
    parser.add_argument("--web-port", type=int, default=5178)
    parser.add_argument("--min-free-gib", type=float, default=10, help="构建前的宿主磁盘余量预算，默认 10 GiB")
    parser.add_argument("--skip-build", action="store_true", help="复用本项目已构建且依赖验证通过的镜像")
    return parser


def parse_args(argv=None):
    return argument_parser().parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    options = DeploymentOptions(agent_root=Path(__file__).resolve().parents[1], model=args.model,
        api_port=args.api_port, web_port=args.web_port, min_free_gib=args.min_free_gib, skip_build=args.skip_build)
    deployment = BgeDemoDeployment(options)
    action = "check" if args.check else args.action
    try:
        getattr(deployment, action)()
    except DeploymentError as exc:
        entry = Path(sys.argv[0]).name
        if entry not in {"deploy_bge_demo.py", "start_local_demo.py"}:
            entry = "deploy_bge_demo.py"
        print(f"\n部署停止：{deployment._redact(str(exc))}\n"
              f"检查服务：python3 agent/tools/{entry} status（在仓库根目录）",
              file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\n已取消当前命令。已启动的本项目服务和数据保留，可用 status/stop 管理。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
