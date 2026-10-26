"""Import/verify the already pinned Windows assets, without remote model code."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "small_model/models/Hunyuan-7B-Instruct"
REVISION = "6fd6ecb05e76589bc43b79f49e3619445c6b4593"
MANIFEST_SHA256 = "6a1c1caacb82d6b861ad8e4c960582b6c2bb90567cf271f0610c1c80ec02e982"


def checksum(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def manifest(directory):
    path = directory / "download-manifest.json"
    if path.is_symlink() or checksum(path) != MANIFEST_SHA256:
        raise ValueError("Asset manifest does not match the approved local snapshot")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["repo_id"] != "tencent/Hunyuan-7B-Instruct" or value["revision"] != REVISION:
        raise ValueError("Unexpected model identity")
    names = [r["path"] for r in value["files"]]
    if len(names) != len(set(names)) or any(Path(n).name != n or n in (".", "..") for n in names):
        raise ValueError("Invalid asset paths")
    return value


def verify(directory=MODEL_DIR):
    value = manifest(directory)
    for entry in value["files"]:
        path = directory / entry["path"]
        if (path.is_symlink() or path.stat().st_size != entry["bytes"]
                or checksum(path) != entry["sha256"]):
            raise ValueError("Asset checksum/size mismatch: " + entry["path"])
    return value


def import_assets(source):
    source = Path(source).resolve(strict=True)
    if source == MODEL_DIR.resolve():
        raise ValueError("Source and destination must be different")
    value = manifest(source)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for entry in value["files"]:
        original, target = source / entry["path"], MODEL_DIR / entry["path"]
        if original.is_symlink() or original.stat().st_size != entry["bytes"]:
            raise ValueError("Source path/size mismatch: " + entry["path"])
        if target.exists():
            if target.is_symlink() or checksum(target) != entry["sha256"]:
                raise FileExistsError("Refusing to replace a different asset: " + str(target))
            print("verified existing", target.name, flush=True)
            continue
        part = target.with_name("." + target.name + ".importing")
        hasher = hashlib.sha256()
        with original.open("rb") as reader, part.open("xb") as writer:
            for block in iter(lambda: reader.read(8 * 1024 * 1024), b""):
                writer.write(block)
                hasher.update(block)
        if part.stat().st_size != entry["bytes"] or hasher.hexdigest() != entry["sha256"]:
            raise ValueError("Imported bytes differ from the pinned manifest: " + target.name)
        part.rename(target)
        print("copied and verified", target.name, entry["bytes"], flush=True)
    target_manifest = MODEL_DIR / "download-manifest.json"
    if target_manifest.exists():
        manifest(MODEL_DIR)
    else:
        with target_manifest.open("xb") as writer:
            writer.write((source / "download-manifest.json").read_bytes())
    verify()
    print(json.dumps({"status": "verified", "model_dir": str(MODEL_DIR),
                      "revision": REVISION, "files": len(value["files"])}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--import-from", type=Path)
    args = parser.parse_args()
    if args.import_from:
        import_assets(args.import_from)
    else:
        result = verify()
        print(json.dumps({"status": "verified", "revision": REVISION, "files": len(result["files"])}))
