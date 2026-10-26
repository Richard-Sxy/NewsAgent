"""Bounded, resumable HTTP range download for proxies that stall on large GETs."""
import concurrent.futures
import hashlib
from pathlib import Path
import re
import shutil
import subprocess

from filelock import FileLock


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def download(url, target, size, sha256, workers=4, direct=False, proxy=None):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target) + ".range.lock", timeout=1):
        if target.exists() and target.stat().st_size == size and digest(target) == sha256:
            return
        parts = target.parent / ("." + target.name + ".parts")
        parts.mkdir(exist_ok=True)
        chunk_size = 64 * 1024 * 1024
        count = (size + chunk_size - 1) // chunk_size

        def fetch(index):
            begin = index * chunk_size
            end = min(size, begin + chunk_size) - 1
            part = parts / f"{index:05d}"
            if part.exists() and part.stat().st_size == end - begin + 1:
                return
            temporary = part.with_suffix(".tmp")
            headers = part.with_suffix(".headers")
            network = part.with_suffix(".network")
            curl = shutil.which("curl.exe") or shutil.which("curl")
            if not curl:
                raise RuntimeError("curl is required for large downloads")
            completed = temporary.stat().st_size if temporary.exists() else 0
            if completed > end - begin + 1:
                raise RuntimeError("Invalid partial file size")
            # Resume only whole 8MiB blocks. Older interrupted 64MiB transfers
            # can leave arbitrary offsets that some CDN routes serve poorly.
            if completed != end - begin + 1 and completed % (8 * 1024 * 1024):
                completed -= completed % (8 * 1024 * 1024)
                with temporary.open("r+b") as partial:
                    partial.truncate(completed)
            with temporary.open("ab") as output:
                for request_begin in range(begin + completed, end + 1, 8 * 1024 * 1024):
                    request_end = min(end, request_begin + 8 * 1024 * 1024 - 1)
                    for attempt in range(4):
                        args = [curl, "--fail", "--location", "--silent", "--show-error",
                                "--connect-timeout", "15", "--max-time", "180",
                                "--range", f"{request_begin}-{request_end}", "--output", str(network),
                                "--dump-header", str(headers)]
                        if direct:
                            args += ["--noproxy", "*"]
                        elif proxy:
                            args += ["--proxy", proxy]
                        try:
                            subprocess.run([*args, url], check=True, capture_output=True, timeout=190)
                            ranges = re.findall(r"(?im)^content-range:\s*(.*?)\s*$", headers.read_text(encoding="iso-8859-1"))
                            if not ranges or ranges[-1] != f"bytes {request_begin}-{request_end}/{size}":
                                raise RuntimeError("Server did not honor the exact byte range")
                            if network.stat().st_size != request_end - request_begin + 1:
                                raise RuntimeError("Incomplete HTTP range")
                            output.write(network.read_bytes())
                            output.flush()
                            network.unlink()
                            headers.unlink()
                            break
                        except Exception as exc:
                            code = getattr(exc, "returncode", "n/a")
                            print(f"{target.name}: part {index + 1} attempt {attempt + 1} failed ({type(exc).__name__}, exit={code})", flush=True)
                            if attempt == 3:
                                raise RuntimeError(f"Download failed for {target.name}, part {index + 1}") from None
            temporary.replace(part)
            print(f"{target.name}: part {index + 1}/{count}", flush=True)

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
        futures = [pool.submit(fetch, index) for index in range(count)]
        try:
            for future in concurrent.futures.as_completed(futures):
                future.result()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
        temporary = target.with_suffix(target.suffix + ".assembling")
        with temporary.open("wb") as output:
            for index in range(count):
                with (parts / f"{index:05d}").open("rb") as source:
                    for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                        output.write(block)
        if digest(temporary) != sha256:
            raise RuntimeError(f"SHA-256 mismatch: {target.name}; preserve parts for diagnosis")
        temporary.replace(target)
        # Delete only our exact validated part files after successful assembly.
        for index in range(count):
            part = parts / f"{index:05d}"
            part.unlink()
            for suffix in (".tmp", ".headers", ".network"):
                part.with_suffix(suffix).unlink(missing_ok=True)
        parts.rmdir()
        print(f"Verified download: {target.name}", flush=True)
