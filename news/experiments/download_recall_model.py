"""Download public BGE weights at a resolved commit, without implicit credentials."""

from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
import urllib.parse
import urllib.request


FILES = (
    "config.json", "modules.json", "sentence_bert_config.json",
    "tokenizer.json", "tokenizer_config.json", "special_tokens_map.json",
    "vocab.txt", "1_Pooling/config.json", "model.safetensors",
)


def download(repo: str, revision: str, output_dir: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repo must be a public Hugging Face owner/model identifier")
    metadata_url = f"https://huggingface.co/api/models/{repo}/revision/{urllib.parse.quote(revision, safe='')}"
    with urllib.request.urlopen(metadata_url, timeout=30) as response:
        metadata = json.load(response)
    commit = metadata["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Hub did not return a concrete model commit")
    available = {row["rfilename"] for row in metadata["siblings"]}
    required = {"config.json", "modules.json", "vocab.txt", "1_Pooling/config.json", "model.safetensors"}
    if not required <= available:
        raise ValueError("model repository does not match the supported BGE safetensors layout")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest_file = out / "download_manifest.json"
    if manifest_file.exists():
        previous = json.loads(manifest_file.read_text())
        if previous["repo"] != repo or previous["resolved_commit"] != commit:
            raise ValueError("existing model directory belongs to another model revision")
    else:
        previous = {"files": {}}
    files = {}
    for name in FILES:
        if name not in available:
            continue
        destination = out / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing = previous.get("files", {}).get(name)
        if destination.is_file() and existing:
            digest = sha256(destination.read_bytes()).hexdigest()
            if digest == existing["sha256"]:
                files[name] = existing
                print(f"cached: {name}", flush=True)
                continue
        temporary = destination.with_suffix(destination.suffix + ".download")
        url = f"https://huggingface.co/{repo}/resolve/{commit}/{name}"
        digest = sha256()
        size = 0
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("wb") as target:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > 250 * 1024 * 1024:
                    raise ValueError("model asset exceeds this small-model experiment's size limit")
                target.write(block)
                digest.update(block)
        temporary.replace(destination)
        files[name] = {"bytes": size, "sha256": digest.hexdigest()}
        print(f"downloaded: {name}, {size} bytes", flush=True)
    manifest = {"repo": repo, "requested_revision": revision, "resolved_commit": commit, "files": files}
    manifest_file.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"resolved model: {repo}@{commit}", flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    download(args.repo, args.revision, args.output_dir)


if __name__ == "__main__":
    main()
