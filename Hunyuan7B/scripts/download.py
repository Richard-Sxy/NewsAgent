"""Download a pinned official checkpoint and verify all repository objects."""
import hashlib
import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

from huggingface_hub import HfApi, snapshot_download
from ranged_download import download

REPO_ID = "tencent/Hunyuan-7B-Instruct"
REVISION = "6fd6ecb05e76589bc43b79f49e3619445c6b4593"
ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "small_model/models/Hunyuan-7B-Instruct"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=["huggingface", "modelscope"], default="modelscope")
    parser.add_argument("--direct", action="store_true", help="Disable curl proxy use for large files")
    parser.add_argument("--proxy", help="Optional curl proxy URL (do not include credentials)")
    args = parser.parse_args()
    info = HfApi().model_info(REPO_ID, revision=REVISION, files_metadata=True, token=False)
    small = [entry.rfilename for entry in info.siblings if not entry.lfs]
    snapshot_download(REPO_ID, revision=REVISION, local_dir=MODEL_DIR, token=False, max_workers=3, allow_patterns=small)
    def fetch_large(entry):
        if args.source == "modelscope":
            url = ("https://www.modelscope.cn/api/v1/models/Tencent-Hunyuan/Hunyuan-7B-Instruct/repo"
                   f"?Revision=master&FilePath={entry.rfilename}")
        else:
            url = f"https://huggingface.co/{REPO_ID}/resolve/{REVISION}/{entry.rfilename}"
        download(url, MODEL_DIR / entry.rfilename, entry.size, entry.lfs.sha256,
                 workers=8, direct=args.direct, proxy=args.proxy)
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch_large, [entry for entry in info.siblings if entry.lfs]))
    files = []
    for entry in info.siblings:
        path = MODEL_DIR / entry.rfilename
        size = path.stat().st_size
        sha256 = hashlib.sha256()
        git_sha1 = hashlib.sha1(f"blob {size}\0".encode())
        with path.open("rb") as source:
            for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                sha256.update(block)
                git_sha1.update(block)
        if size != entry.size:
            raise RuntimeError(f"Size mismatch: {entry.rfilename}")
        if entry.lfs:
            valid = sha256.hexdigest() == entry.lfs.sha256
        else:
            valid = git_sha1.hexdigest() == entry.blob_id
        if not valid:
            raise RuntimeError(f"Hash mismatch: {entry.rfilename}")
        files.append({"path": entry.rfilename, "bytes": size, "sha256": sha256.hexdigest()})
        print(f"Verified {entry.rfilename}", flush=True)
    manifest = {"repo_id": REPO_ID, "revision": REVISION, "large_file_source": args.source, "files": files}
    temporary = MODEL_DIR / "download-manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(MODEL_DIR / "download-manifest.json")
    print(f"Ready: {MODEL_DIR}", flush=True)


if __name__ == "__main__":
    main()
