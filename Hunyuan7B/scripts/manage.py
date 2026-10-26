"""Start/status/stop only this WSL model process, with PID-reuse protection."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.request

from assets import ROOT, REVISION

STATE = ROOT / "small_model/runtime/hunyuan"
RECEIPT = STATE / "wsl-service.json"
PYTHON = ROOT / "small_model/environments/hunyuan/bin/python"
SERVER = Path(__file__).resolve().parent / "serve.py"


def process_identity(pid):
    try:
        proc = Path("/proc") / str(pid)
        stat = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        argv = (proc / "cmdline").read_bytes().split(b"\0")
        return {"start_ticks": stat[19], "uid": proc.stat().st_uid,
                "argv": [x.decode() for x in argv if x], "state": stat[0]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def owned(receipt):
    current = process_identity(receipt["pid"])
    return bool(current and current["state"] != "Z"
                and current["uid"] == os.getuid()
                and current["start_ticks"] == receipt["start_ticks"]
                and current["argv"] == receipt["argv"]
                and receipt["argv"][:3] == [str(PYTHON), "-u", str(SERVER)])


def health(port):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            value = json.load(response)
            return value if (value.get("status") == "ok"
                             and value.get("model") == f"hunyuan-7b-instruct-{REVISION[:7]}-nf4") else None
    except (OSError, ValueError):
        return None


@contextmanager
def exclusive():
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / "service-control.lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def start(port, wait_seconds):
    with exclusive():
        if RECEIPT.exists():
            receipt = json.loads(RECEIPT.read_text())
            if owned(receipt):
                if receipt["port"] != port:
                    raise RuntimeError("This service already runs on another port")
                return receipt, health(port)
        # Detect a conflicting owner before spawning or writing a new receipt.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))
        if not PYTHON.exists():
            raise FileNotFoundError("Run bash small_model/scripts/setup.sh first")
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = STATE / f"wsl-server-{timestamp}.log"
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   TOKENIZERS_PARALLELISM="false")
        command = [str(PYTHON), "-u", str(SERVER), "--port", str(port)]
        with log_path.open("xb") as log:
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        identity = process_identity(child.pid)
        if identity is None:
            raise RuntimeError("Server exited before its process identity was recorded")
        receipt = {"pid": child.pid, "start_ticks": identity["start_ticks"],
                   "argv": command, "port": port, "log_path": str(log_path),
                   "started_at": timestamp, "workspace": str(ROOT)}
        RECEIPT.write_text(json.dumps(receipt, indent=2) + "\n")
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not owned(receipt):
            raise RuntimeError("Server exited; inspect " + receipt["log_path"])
        ready = health(port)
        if ready:
            return receipt, ready
        time.sleep(1)
    return receipt, None


def stop():
    with exclusive():
        if not RECEIPT.exists():
            return {"status": "not_running"}
        receipt = json.loads(RECEIPT.read_text())
        if not owned(receipt):
            return {"status": "not_running", "receipt": receipt}
        os.kill(receipt["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and owned(receipt):
            time.sleep(.2)
        if owned(receipt):
            return {"status": "stopping", "pid": receipt["pid"]}
        return {"status": "stopped", "pid": receipt["pid"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("start", "status", "stop"))
    parser.add_argument("--port", type=int, default=8008)
    parser.add_argument("--wait-seconds", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 0 <= args.wait_seconds <= 600:
        parser.error("invalid port or wait duration")
    if args.command == "start":
        receipt, ready = start(args.port, args.wait_seconds)
        result = {"status": "ready" if ready else "starting", "process": receipt, "health": ready}
    elif args.command == "stop":
        result = stop()
    elif RECEIPT.exists():
        receipt = json.loads(RECEIPT.read_text())
        running = owned(receipt)
        ready = health(receipt["port"]) if running else None
        result = {"status": "ready" if ready else "starting" if running else "not_running",
                  "process": receipt, "health": ready}
    else:
        result = {"status": "not_running"}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
