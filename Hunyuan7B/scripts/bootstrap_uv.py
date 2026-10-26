"""Install a checksum-verified uv binary inside this experiment directory."""
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "small_model/runtime/hunyuan/bootstrap"
VERSION = "0.9.2"


def fetch(url, target):
    subprocess.run(["curl", "-4", "--fail", "--location", "--silent", "--show-error",
                    "--retry", "2", "--connect-timeout", "15", "--max-time", "180",
                    "--output", str(target), url], check=True)


def main():
    BOOTSTRAP.mkdir(parents=True, exist_ok=True)
    metadata = BOOTSTRAP / "uv-metadata.json"
    fetch("https://pypi.org/pypi/uv/" + VERSION + "/json", metadata)
    assets = json.loads(metadata.read_text())["urls"]
    asset = next(r for r in assets if r["filename"].endswith(
        "manylinux_2_17_x86_64.manylinux2014_x86_64.whl"))
    wheel = BOOTSTRAP / "uv.whl"
    if not wheel.exists() or hashlib.sha256(wheel.read_bytes()).hexdigest() != asset["digests"]["sha256"]:
        fetch(asset["url"], wheel)
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != asset["digests"]["sha256"]:
        raise ValueError("uv wheel checksum mismatch")
    with zipfile.ZipFile(wheel) as archive:
        member = next(n for n in archive.namelist() if n.endswith("/scripts/uv"))
        payload = archive.read(member)
    binary = BOOTSTRAP / "uv"
    if binary.exists() and binary.read_bytes() != payload:
        raise FileExistsError("Existing uv differs from pinned wheel")
    if not binary.exists():
        with binary.open("xb") as handle:
            handle.write(payload)
    binary.chmod(0o755)
    (BOOTSTRAP / "source.json").write_text(json.dumps(
        {"name": "uv", "version": VERSION, "url": asset["url"],
         "sha256": asset["digests"]["sha256"]}, indent=2) + "\n")
    print(binary)


if __name__ == "__main__":
    main()
