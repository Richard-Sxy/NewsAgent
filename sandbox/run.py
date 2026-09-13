"""NewsAgent 本地沙盒入口。

用法（在仓库根目录执行）：
    python3 sandbox/run.py                     # 交互式 REPL
    python3 sandbox/run.py demo.py             # 运行 snippets/ 下的片段
    python3 sandbox/run.py -c "print(1+1)"     # 直接执行一行代码
    python3 sandbox/run.py --venv-pip list     # 查看沙盒 venv 已装包

片段文件直接写纯 Python，run.py 会用沙盒 venv 的解释器执行它。
安装依赖：sandbox/.venv/bin/pip install <pkg>
"""

import code
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"
SNIPPETS = ROOT / "snippets"


def ensure_venv() -> None:
    if not VENV_PYTHON.exists():
        sys.exit(
            "未找到 sandbox/.venv，请先创建：\n"
            "  /opt/homebrew/bin/python3.11 -m venv sandbox/.venv"
        )


def resolve_snippet(name: str) -> Path:
    direct = Path(name)
    if direct.exists():
        return direct
    return SNIPPETS / name


def run_file(path: Path) -> None:
    if not path.exists():
        sys.exit(f"片段文件不存在：{path}")
    subprocess.run([str(VENV_PYTHON), str(path)], check=False)


def run_code(code_text: str) -> None:
    namespace: dict = {}
    exec(code_text, namespace)
    code.interact(
        banner=f"NewsAgent 沙盒（{VENV_PYTHON}）\n已执行：{code_text!r}\n",
        local=namespace,
    )


def repl() -> None:
    banner = (
        "NewsAgent 本地沙盒 REPL\n"
        f"解释器：{VENV_PYTHON}\n"
        "退出：exit() 或 Ctrl+D"
    )
    code.interact(banner=banner, local={})


def main(argv: list[str]) -> int:
    ensure_venv()
    if argv[:1] == ["--venv-pip"]:
        subprocess.run([str(VENV_PYTHON), "-m", "pip", *argv[1:]], check=False)
        return 0
    if argv[:1] == ["-c"]:
        if len(argv) < 2:
            sys.exit("用法：run.py -c \"<代码>\"")
        run_code(argv[1])
        return 0
    if argv[:1] == ["--help"]:
        print(__doc__)
        return 0
    if argv:
        run_file(resolve_snippet(argv[0]))
        return 0
    repl()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
