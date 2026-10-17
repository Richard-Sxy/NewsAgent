"""Activate the reviewed query-only model on the existing local BGE API.

No dotenv files are loaded. The existing key is read from the process environment
or a hidden interactive prompt and stays in memory until Compose returns.
"""

from pathlib import Path
import sys

from deploy_bge_demo import BgeDemoDeployment, DeploymentError, DeploymentOptions


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    options = DeploymentOptions(agent_root=root, model="compatible", skip_build=True)
    deployment = BgeDemoDeployment(options)
    try:
        deployment._ensure_model_key()
        command = deployment.compose_command(runtime=True)
        command.extend(("-f", str(root / "deploy/docker-compose.structured-query-demo.yml")))
        deployment._run([*command, "config", "--quiet"], runtime=True, timeout=15)
        deployment._run([*command, "up", "-d", "--no-deps", "--no-build", "--pull", "never",
                         "--force-recreate", "api"], runtime=True, timeout=120, capture=False)
        deployment._wait_until_ready()
    except DeploymentError as exc:
        print(deployment._redact(str(exc)), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已停止激活；已有数据保留。", file=sys.stderr)
        return 130
    print("结构化查询模型已就绪：http://127.0.0.1:5178/chat", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
