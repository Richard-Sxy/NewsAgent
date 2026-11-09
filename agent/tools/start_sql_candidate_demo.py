#!/usr/bin/env python3
"""Start/status/stop the isolated SQL-candidate demo without loading dotenv.

Linux/WSL only. Uses the standard library and the fixed, repository-owned
Compose project. Stop retains containers and data volumes. This launcher never
switches production configuration or connects to an enterprise model.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[2]
PROJECT = "news-agent-sql-candidate-demo"
IMAGE = "news-agent/writing-agent-service:sql-candidate-demo"
API_URL = "http://127.0.0.1:12800"
WEB_URL = "http://127.0.0.1:12880"
COMPOSE_FILES = (
    ROOT / "deploy/docker-compose.native-e2e.yml",
    ROOT / "deploy/docker-compose.sql-candidate-demo.yml",
)
APP_SERVICES = ("api", "hot-news-worker", "writing-worker", "outbox-worker", "data-loop-worker", "web")
QUEUES = ("hot-news", "news-writing", "hot-news-data-loop")
POLLER_PROBE = r"""
import asyncio, json
from datetime import datetime, timezone, timedelta
from temporalio.client import Client
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.api.taskqueue.v1 import TaskQueue
async def main():
    client = await Client.connect("temporal:7233", namespace="default")
    counts = {}
    for queue in ("hot-news", "news-writing", "hot-news-data-loop"):
        counts[queue] = {}
        for label, kind in (("workflow", 1), ("activity", 2)):
            result = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(namespace="default", task_queue=TaskQueue(name=queue), task_queue_type=kind),
                timeout=timedelta(seconds=3))
            now = datetime.now(timezone.utc)
            counts[queue][label] = sum(-5 <= (now - item.last_access_time.ToDatetime(tzinfo=timezone.utc)).total_seconds() <= 90
                                       for item in result.pollers)
    print(json.dumps(counts))
asyncio.run(main())
"""


class DemoError(RuntimeError):
    pass


def environment() -> dict[str, str]:
    env = dict(os.environ)
    # CLI-owned files/project win over inherited Compose settings. Node preload
    # hooks and Vite overrides cannot silently load dotenv or select a remote API.
    for key in list(env):
        if key.startswith(("COMPOSE_", "VITE_", "DOTENV_")) or key == "NODE_OPTIONS":
            env.pop(key)
    env["COMPOSE_DISABLE_ENV_FILE"] = "1"
    env["NEWSAGENT_SKIP_DOTENV"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def compose_command(*args: str) -> list[str]:
    command = ["docker", "compose", "--env-file", "/dev/null", "--project-name", PROJECT]
    for file in COMPOSE_FILES:
        command.extend(("--file", str(file)))
    return [*command, *args]


def run(command: list[str], *, env: dict[str, str], timeout: float = 30,
        capture: bool = False, cwd: Path = ROOT) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True, check=True,
                                timeout=timeout, stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    except subprocess.TimeoutExpired:
        raise DemoError("命令超时；现有容器和卷均保留，请运行 status 检查。") from None
    except subprocess.CalledProcessError:
        # Do not print captured configuration or arbitrary subprocess diagnostics.
        raise DemoError("命令执行失败；检查上方构建输出，或运行 status / logs 定位。") from None
    return result.stdout.strip() if capture else ""


def preflight(env: dict[str, str]) -> None:
    if os.name != "posix" or not Path("/dev/null").exists():
        raise DemoError("请在 Linux/WSL 中运行此脚本。")
    if not shutil.which("docker", path=env.get("PATH")):
        raise DemoError("未找到 docker；请先开启 Docker Desktop 的 WSL 集成或本地 Docker 服务。")
    for file in COMPOSE_FILES:
        if not file.is_file():
            raise DemoError(f"缺少 Compose 文件：{file}")
        # Refuse service env_file declarations before Compose could resolve them.
        if re.search(r"(?<![A-Za-z0-9_])[\"\']?env_file[\"\']?\s*:", file.read_text(encoding="utf-8-sig")):
            raise DemoError("隔离 Compose 不允许服务级 env_file。")
    # A local demo must not accidentally deploy to an SSH/TCP remote context.
    context = env.get("DOCKER_CONTEXT") or run(["docker", "context", "show"], env=env, capture=True)
    host = env.get("DOCKER_HOST") if not env.get("DOCKER_CONTEXT") else None
    if not host:
        raw = run(["docker", "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"], env=env, capture=True)
        host = json.loads(raw)
    if not isinstance(host, str) or not (host.startswith("unix://") or re.fullmatch(r"tcp://(?:127\.0\.0\.1|localhost):\d+", host)):
        raise DemoError("该入口只允许本地 Docker context；不会向远程 Docker 部署。")
    version = run(["docker", "compose", "version", "--short"], env=env, capture=True)
    found = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    if not found or tuple(map(int, found.groups())) < (2, 24, 4):
        raise DemoError("需要 Docker Compose 2.24.4 或更新版本以支持隔离端口覆盖。")
    run(["docker", "info", "--format", "{{.ServerVersion}}"], env=env, capture=True)
    definition = json.loads(run(compose_command("config", "--format", "json"), env=env, capture=True))
    for service in definition.get("services", {}).values():
        if service.get("env_file"):
            raise DemoError("隔离 Compose 不允许服务级 env_file。")
        for volume in service.get("volumes", []):
            source = str(volume.get("source", "")) if isinstance(volume, dict) else str(volume)
            if Path(source).name.startswith(".env"):
                raise DemoError("隔离 Compose 不允许挂载 dotenv 文件。")
    for name in APP_SERVICES:
        if name not in definition.get("services", {}):
            raise DemoError(f"隔离 Compose 缺少服务：{name}")


def node_binary(explicit: str | None, env: dict[str, str]) -> Path:
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        found = shutil.which("node", path=env.get("PATH"))
        candidates = ([Path(found)] if found else [])
        candidates += sorted((Path.home() / ".nvm/versions/node").glob("*/bin/node"), reverse=True)
    for node in candidates:
        if not node.is_file() or not os.access(node, os.X_OK):
            continue
        try:
            version = run([str(node), "--version"], env=env, timeout=5, capture=True)
        except (DemoError, OSError):
            continue
        found = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
        if found and tuple(map(int, found.groups())) >= (20, 19, 0):
            return node.resolve()
    raise DemoError("需要 Linux Node >=20.19；可用 --node /home/shi/.nvm/versions/node/v22.22.0/bin/node 指定。")


def build_assets(args, env: dict[str, str]) -> None:
    frontend = ROOT / "agent/frontend"
    if args.skip_build:
        if not (frontend / "dist/index.html").is_file():
            raise DemoError("--skip-build 需要已有 agent/frontend/dist/index.html。")
        run(["docker", "image", "inspect", IMAGE, "--format", "{{.Id}}"], env=env, capture=True)
        print("复用已有前端 dist 与 SQL candidate 镜像；未验证它们一定对应当前源码。", flush=True)
        return
    node = node_binary(args.node, env)
    build_env = dict(env)
    build_env["PATH"] = str(node.parent) + os.pathsep + build_env.get("PATH", "")
    npm = node.parent / "npm"
    if not npm.is_file():
        raise DemoError("所选 Node 同目录缺少 npm。")
    if not (frontend / "node_modules/vite/package.json").is_file():
        print("安装前端锁定依赖（npm ci；仅缺少依赖时执行）……", flush=True)
        run([str(npm), "ci"], env=build_env, cwd=frontend, timeout=1200)
    print("构建前端：类型检查 + 不读 dotenv 的隔离 Vite 入口……", flush=True)
    run([str(npm), "run", "build:isolated"], env=build_env, cwd=frontend, timeout=600)
    print("构建独立 Python 演示镜像……", flush=True)
    run(compose_command("build", "migrate"), env=env, timeout=1800)


def fetch(url: str) -> tuple[int, str]:
    # Loopback probes must not be redirected through inherited HTTP proxies.
    with build_opener(ProxyHandler({})).open(Request(url, headers={"Accept": "application/json"}), timeout=3) as response:
        return response.status, response.read(65536).decode("utf-8")


def poller_counts(env: dict[str, str]) -> dict:
    return json.loads(run(compose_command("exec", "-T", "api", "python", "-c", POLLER_PROBE),
                          env=env, timeout=20, capture=True))


def wait_ready(env: dict[str, str], timeout: int) -> None:
    deadline, next_notice = time.monotonic() + timeout, 0.0
    while time.monotonic() < deadline:
        try:
            api_status, ready = fetch(API_URL + "/ready")
            web_status, _ = fetch(WEB_URL + "/healthz")
            if api_status == 200 and json.loads(ready).get("status") == "ready" and web_status == 200:
                counts = poller_counts(env)
                if all(counts.get(queue, {}).get(kind, 0) > 0 for queue in QUEUES for kind in ("workflow", "activity")):
                    return
        except (OSError, URLError, ValueError, DemoError):
            pass
        if time.monotonic() >= next_notice:
            print("等待 API、前端以及 hot-news/news-writing/hot-news-data-loop Workflow 与 Activity poller 就绪……", flush=True)
            next_notice = time.monotonic() + 15
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise DemoError(f"{timeout} 秒内未全部就绪；容器和数据保留，请运行 status / logs。")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="启动 SQL candidate 隔离演示；模型与行为数据均为本地模拟，不读取 .env。")
    result.add_argument("action", nargs="?", choices=("start", "status", "stop", "logs"), default="start")
    result.add_argument("--skip-build", action="store_true", help="复用已有演示镜像及前端 dist，不构建")
    result.add_argument("--node", help="用于前端构建的 Linux Node 绝对路径")
    result.add_argument("--timeout", type=int, default=300, help="启动后等待就绪的秒数，默认300（范围30–1800）")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if not 30 <= args.timeout <= 1800:
        parser().error("--timeout 必须位于30–1800秒")
    env = environment()
    try:
        preflight(env)
        if args.action == "status":
            run(compose_command("ps", "--all"), env=env)
            for label, url in (("API", API_URL + "/ready"), ("前端", WEB_URL + "/healthz")):
                try:
                    code, _ = fetch(url)
                    print(f"{label}: HTTP {code} ({url})")
                except (OSError, URLError):
                    print(f"{label}: 尚未就绪 ({url})")
            return 0
        if args.action == "logs":
            run(compose_command("logs", "--tail", "80", *APP_SERVICES), env=env)
            return 0
        if args.action == "stop":
            run(compose_command("stop"), env=env, timeout=120)
            print("已停止本项目容器；数据卷保留，未影响其他 Compose 项目。")
            return 0
        print(f"隔离项目：{PROJECT}；合成行为数据与确定性模型替身，不代表企业模型质量。", flush=True)
        build_assets(args, env)
        run(compose_command("up", "--detach", "--no-build", *APP_SERVICES), env=env, timeout=600)
        wait_ready(env, args.timeout)
        print(f"演示入口：{WEB_URL}/chat\n热点结果：{WEB_URL}/hot-news\nAPI：{API_URL}/ready\n"
              "就绪探针仅证明服务和Worker可用；请发送示例问题验证完整SQL与热点分析运行。")
        return 0
    except (DemoError, OSError, ValueError) as exc:
        print(f"启动工具：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("操作已中断；不会删除容器或数据卷。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
