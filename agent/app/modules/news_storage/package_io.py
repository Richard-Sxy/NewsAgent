"""Small, checksum-verified disk format for immutable public-content releases."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile

from app.modules.news_storage.model import canonical_json, validate_package


FILES = {"versions.jsonl": "versions", "provenance.jsonl": "provenance",
         "release_items.jsonl": "items"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"invalid JSON constant: {value}")


def _json(value):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def save_package(package: dict, output: Path, *, inventory: dict | None = None) -> dict:
    """Reserve a new directory exclusively and publish its manifest last."""
    validate_package(package)
    output = Path(output).absolute()
    if output.exists():
        raise FileExistsError("release output already exists; choose a new directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    reserved, published, moved = False, False, []
    try:
        files = {}
        for name, key in FILES.items():
            checksum, size = sha256(), 0
            with (staging / name).open("xb") as handle:
                for record in package[key]:
                    line = (canonical_json(record) + "\n").encode("utf-8")
                    handle.write(line)
                    checksum.update(line)
                    size += len(line)
            files[name] = {"sha256": checksum.hexdigest(), "bytes": size, "rows": len(package[key])}
        wrapper = {"release": package["manifest"], "files": files, "source_inventory": inventory}
        (staging / "manifest.json").write_text(
            json.dumps(wrapper, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        # Check the serialized representation too, before making it discoverable.
        load_package(staging)
        # mkdir is an exclusive reservation even if another builder appeared
        # after the initial exists() check. POSIX rename of a directory alone
        # could silently replace a concurrently created empty destination.
        output.mkdir()
        reserved = True
        for name in (*FILES, "manifest.json"):
            (staging / name).rename(output / name)
            moved.append(name)
        published = True
        return wrapper
    finally:
        if reserved and not published:
            for name in moved:
                (output / name).unlink(missing_ok=True)
            try:
                output.rmdir()
            except OSError:
                pass  # Never remove files created by another process.
        if staging.exists():
            shutil.rmtree(staging)


def load_package(directory: Path) -> dict:
    directory = Path(directory).resolve(strict=True)
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 2_000_000:
        raise ValueError("invalid release manifest file")
    wrapper = _json(manifest_path.read_text(encoding="utf-8"))
    if set(wrapper) != {"release", "files", "source_inventory"} or set(wrapper["files"]) != set(FILES):
        raise ValueError("invalid release files")
    package = {"manifest": wrapper["release"]}
    for name, key in FILES.items():
        path = directory / name
        expected = wrapper["files"][name]
        if path.is_symlink() or path.stat().st_size != expected["bytes"]:
            raise ValueError(f"file size/path mismatch: {name}")
        checksum, records = sha256(), []
        with path.open("rb") as handle:
            for line in handle:
                checksum.update(line)
                records.append(_json(line))
        if checksum.hexdigest() != expected["sha256"] or len(records) != expected["rows"]:
            raise ValueError(f"file checksum/count mismatch: {name}")
        package[key] = records
    validate_package(package)
    return package
