"""独立计算进程入口。从标准输入读取 JSON，调用 engine.analyze()，再通过标准输出返回 JSON，在 Linux 下设置 CPU、内存等资源限制。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import runpy
import sys


MAX_IO_BYTES = 1_048_576


def _error(code: str, status: int = 2) -> int:
    # Static codes deliberately exclude input values, paths and exception details.
    sys.stdout.write(json.dumps({"error_code": code, "retryable": False}))
    return status


def _reject_constant(_value: str) -> None:
    raise ValueError("non-finite JSON value")


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _apply_linux_limits(memory_mb: int, cpu_seconds: int) -> None:
    if not sys.platform.startswith("linux"):
        return
    import resource

    limits = (
        (resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1),
        (resource.RLIMIT_AS, memory_mb * 1024 * 1024, memory_mb * 1024 * 1024),
        (resource.RLIMIT_NOFILE, 32, 32),
        (resource.RLIMIT_FSIZE, 0, 0),
        (resource.RLIMIT_CORE, 0, 0),
    )
    for kind, soft, hard in limits:
        resource.setrlimit(kind, (soft, hard))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--memory-mb", type=int, default=128)
    parser.add_argument("--cpu-seconds", type=int, default=3)
    args = parser.parse_args()
    if not 32 <= args.memory_mb <= 4096 or not 1 <= args.cpu_seconds <= 60:
        return _error("resource_limit")
    try:
        _apply_linux_limits(args.memory_mb, args.cpu_seconds)
    except Exception:
        return _error("resource_limit", 3)
    try:
        raw = sys.stdin.buffer.read(MAX_IO_BYTES + 1)
        if len(raw) > MAX_IO_BYTES:
            return _error("input_limit")
        request = json.loads(
            raw.decode("utf-8"),
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except Exception:
        return _error("invalid_request")
    try:
        # The only loaded file is shipped beside this trusted worker. Neither its
        # location nor any module name is taken from the request.
        engine = runpy.run_path(str(Path(__file__).resolve().with_name("engine.py")))
        try:
            result = engine["analyze"](request)
        except engine["AnalysisInputError"]:
            return _error("invalid_request")
        payload = json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > MAX_IO_BYTES:
            return _error("output_limit", 3)
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return 0
    except Exception:
        return _error("worker_failed", 3)


if __name__ == "__main__":
    raise SystemExit(main())
