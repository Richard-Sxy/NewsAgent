"""Build a new, auditable BGE package without changing the frozen source package.

The input manifest binds every article's file bytes and document hash. Failed
builds remain in a private sibling ``.<output>.building`` directory; ``--resume``
only accepts the exact same inputs and reuses individually verified batches.
No application settings, .env file, enterprise model, or running deployment is
read or changed. Publication is one directory rename after complete validation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge.native_indexing import split_text
from app.retrieval.bge_snapshot import (
    BGE_DIMENSIONS,
    _CORPUS_PATH,
    _MODEL_PATH,
    _read_chunks,
    _validate_model_config,
    _verify_package,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NEWS_ID = re.compile(r"\d{8}[A-Za-z0-9_-]{1,152}")
SHA256 = re.compile(r"[0-9a-f]{64}")
CHUNK_CHARS, CHUNK_OVERLAP = 800, 80
EMBEDDING_PLAN_VERSION = "length-sorted-batches-v1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name + ".", delete=False) as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f"JSON input missing, linked, or exceeds 64 MiB: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return value


def _published(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("article publish_time is required")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("article publish_time must include a timezone")
    return timestamp.astimezone(SHANGHAI)


def load_articles(articles_dir: Path, manifest_path: Path, *, expected_manifest_sha256: str | None = None):
    """Verify the complete manifest/file set before loading any model."""
    articles_dir, manifest_path = articles_dir.resolve(), manifest_path.absolute()
    manifest = _read_json(manifest_path)
    manifest_sha = file_sha256(manifest_path)
    if expected_manifest_sha256 is not None and manifest_sha != expected_manifest_sha256:
        raise ValueError("input manifest SHA-256 differs from the explicitly expected value")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1 or manifest.get("timezone") != "Asia/Shanghai":
        raise ValueError("input manifest must use schema_version 1 and Asia/Shanghai")
    start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
    if start > end:
        raise ValueError("input manifest date range is reversed")
    entries = manifest.get("articles")
    if not isinstance(entries, list) or not entries:
        raise ValueError("input manifest must contain at least one successful article")
    articles, audited, paths, seen = [], [], set(), set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("input manifest article entries must be objects")
        news_id = entry.get("news_id")
        if not isinstance(news_id, str) or NEWS_ID.fullmatch(news_id) is None or news_id in seen:
            raise ValueError("input manifest contains an invalid or duplicate news_id")
        seen.add(news_id)
        relative = Path(entry.get("relative_path", ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("input article path escapes manifest directory")
        path = manifest_path.parent / relative
        if (path.is_symlink() or path.resolve().parent != articles_dir
                or path.name != news_id + ".json"):
            raise ValueError("manifest article path does not match its news_id and articles directory")
        article = _read_json(path)
        digest = entry.get("sha256")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None or file_sha256(path) != digest:
            raise ValueError(f"article file SHA-256 mismatch: {news_id}")
        title, content = article.get("title"), article.get("content")
        if (not isinstance(title, str) or not title.strip() or len(title) > 500
                or "\n" in title or "\r" in title
                or not isinstance(content, str) or not content.strip()):
            raise ValueError(f"article requires a title and nonempty original body: {news_id}")
        version = article.get("content_version")
        if type(version) is not int or version < 1 or article.get("news_id") != news_id:
            raise ValueError(f"article news_id or content_version is invalid: {news_id}")
        url = article.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if (parsed is None or parsed.scheme != "https" or parsed.hostname != "news.qq.com"
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path != f"/rain/a/{news_id}" or any(char.isspace() for char in url)):
            raise ValueError(f"article URL does not establish its news_id: {news_id}")
        timestamp = _published(article.get("publish_time"))
        if not start <= timestamp.date() <= end or timestamp != _published(entry.get("publish_time")):
            raise ValueError(f"article publication metadata is outside the manifest window or differs: {news_id}")
        document_sha = hashlib.sha256(f"{title}\x1f{content}".encode("utf-8")).hexdigest()
        if entry.get("document_sha256") != document_sha:
            raise ValueError(f"article document SHA-256 mismatch: {news_id}")
        if "document_sha256" in article and article["document_sha256"] != document_sha:
            raise ValueError(f"article's own document SHA-256 mismatch: {news_id}")
        paths.add(path.resolve())
        articles.append({**article, "publish_time": timestamp.isoformat(), "document_sha256": document_sha})
        audited.append({"news_id": news_id, "relative_path": str(relative), "sha256": digest,
                        "document_sha256": document_sha, "publish_time": timestamp.isoformat(),
                        "content_version": version})
    actual = {path.resolve() for path in articles_dir.glob("*.json")}
    if paths != actual:
        raise ValueError("input manifest does not exactly cover the articles directory")
    articles.sort(key=lambda item: item["news_id"])
    audited.sort(key=lambda item: item["news_id"])
    counts = dict(sorted(Counter(article["publish_time"][:10] for article in articles).items()))
    missing = [(start + timedelta(days=offset)).isoformat()
               for offset in range((end - start).days + 1)
               if (start + timedelta(days=offset)).isoformat() not in counts]
    return articles, {"manifest_sha256": manifest_sha, "manifest_path": str(manifest_path.resolve()),
                      "start_date": start.isoformat(), "end_date": end.isoformat(),
                      "timezone": "Asia/Shanghai", "articles": audited,
                      "counts_by_date": counts, "missing_dates": missing,
                      "collection_failure_count": len(manifest.get("failures", []))}


def article_chunks(articles: list[dict], existing_news_ids: set[str]):
    rows, skipped = [], []
    for article in articles:
        if article["news_id"] in existing_news_ids:
            skipped.append(article["news_id"])
            continue
        # Repeat the trustworthy metadata header so every retrieved fragment has
        # its title; the original body follows deterministic 800/80 slicing.
        header = (f"# {article['title'].strip()}\n来源地址: {article['url']}\n"
                  f"发布时间: {article['publish_time']}\n\n")
        for index, body in enumerate(split_text(article["content"], max_chars=CHUNK_CHARS,
                                                overlap=CHUNK_OVERLAP)):
            rows.append({"news_id": article["news_id"], "url": article["url"],
                         "publish_time": article["publish_time"], "chunk_index": index,
                         "content_version": article["content_version"],
                         "document_sha256": article["document_sha256"], "text": header + body})
    return rows, skipped


def _validate_vectors(vectors, *, count: int) -> None:
    import numpy as np

    if vectors.shape != (count, BGE_DIMENSIONS) or vectors.dtype != np.float32:
        raise ValueError("BGE vectors must be float32 with matching rows and 1024 dimensions")
    for offset in range(0, count, 2048):
        block = vectors[offset:offset + 2048]
        if not np.isfinite(block).all() or not np.allclose(np.sum(block * block, axis=1), 1.0, atol=0.001):
            raise ValueError("BGE vectors must contain finite, normalized coordinates")


def _copy_model(base: Path, stage: Path, *, mode: str) -> dict[str, int]:
    counts = {"hardlinked": 0, "copied": 0}
    for source in sorted((base / _MODEL_PATH).rglob("*")):
        if not source.is_file():
            continue
        if source.is_symlink():
            raise ValueError("frozen model files must be regular files, not symlinks")
        destination = stage / source.relative_to(base)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_symlink() or file_sha256(destination) != file_sha256(source):
                raise ValueError("resumed model file differs from the immutable source")
            same_inode = ((source.stat().st_dev, source.stat().st_ino)
                          == (destination.stat().st_dev, destination.stat().st_ino))
            counts["hardlinked" if same_inode else "copied"] += 1
            continue
        if mode == "hardlink":
            try:
                os.link(source, destination)
                counts["hardlinked"] += 1
                continue
            except OSError:
                pass  # Another filesystem may require an independent byte copy.
        shutil.copy2(source, destination)
        counts["copied"] += 1
    return counts


@contextmanager
def _build_lock(stage: Path):
    import fcntl

    with (stage / ".build.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("another process is building this output package") from exc
        yield


def _checkpoint_vectors(stage: Path, state: dict, new_rows: list[dict], *, model_dir: Path,
                        batch_size: int, device: str, precision: str = "float32"):
    import numpy as np

    destination = stage / "new_embeddings.npy"
    checkpoint = state.get("new_embeddings_sha256")
    if checkpoint is not None:
        if not destination.is_file() or file_sha256(destination) != checkpoint:
            raise ValueError("embedding checkpoint SHA-256 differs; choose a new output package")
        vectors = np.load(destination, mmap_mode="r", allow_pickle=False)
        _validate_vectors(vectors, count=len(new_rows))
        return vectors
    # Length grouping retains the original encode() padding optimization while
    # allowing each inference batch to commit separately. The final vector file
    # is scattered back to the original JSONL positions, never this work order.
    order = sorted(range(len(new_rows)), key=lambda index: (-len(new_rows[index]["text"]), index))
    plan = []
    for offset in range(0, len(order), batch_size):
        indices = order[offset:offset + batch_size]
        inputs = {"row_indices": indices, "texts": [new_rows[index]["text"] for index in indices]}
        input_sha = hashlib.sha256(json.dumps(inputs, ensure_ascii=False,
                                              separators=(",", ":")).encode("utf-8")).hexdigest()
        plan.append((indices, input_sha))
    batches_dir = stage / "embedding_batches"
    batches_dir.mkdir(exist_ok=True)
    for abandoned in batches_dir.glob(".embedding-batch.*"):
        if abandoned.is_file():
            abandoned.unlink()
    records = state.setdefault("embedding_batches", [])
    if not isinstance(records, list) or len(records) > len(plan):
        raise ValueError("embedding batch checkpoint list differs from the build plan")
    # Verify all committed work before loading the model or issuing inference.
    for batch_index, record in enumerate(records):
        indices, input_sha = plan[batch_index]
        path = batches_dir / f"batch-{batch_index:06d}.npy"
        if (not isinstance(record, dict) or record.get("batch_index") != batch_index
                or record.get("rows") != len(indices) or record.get("input_sha256") != input_sha
                or not isinstance(record.get("sha256"), str) or SHA256.fullmatch(record["sha256"]) is None
                or not path.is_file() or file_sha256(path) != record["sha256"]):
            raise ValueError("embedding batch checkpoint SHA-256 or input identity differs; choose a new output package")
        _validate_vectors(np.load(path, mmap_mode="r", allow_pickle=False), count=len(indices))
    if len(records) < len(plan):
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(str(model_dir), device=device,
                                    local_files_only=True, trust_remote_code=False)
        model.max_seq_length = 512
        if model.get_sentence_embedding_dimension() != BGE_DIMENSIONS:
            raise ValueError("local BGE model must produce 1024 dimensions")
        if precision == "float16":
            model.half()  # Only the in-memory model changes; source weights stay frozen.
        resumed_batches = len(records)
        for batch_index in range(len(records), len(plan)):
            batch_started = time.monotonic()
            indices, input_sha = plan[batch_index]
            vectors = np.asarray(model.encode(
                [new_rows[index]["text"] for index in indices], batch_size=batch_size,
                normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False,
            ), dtype=np.float32)
            if precision == "float16":
                # FP16 inference can round the model's L2 normalization. Restore
                # unit length in the published float32 space, rejecting invalid
                # coordinates and grossly unnormalized outputs first.
                if vectors.shape != (len(indices), BGE_DIMENSIONS) or not np.isfinite(vectors).all():
                    raise ValueError("BGE vectors must have matching rows and finite coordinates")
                norms = np.linalg.norm(vectors, axis=1, keepdims=True)
                if not np.allclose(norms, 1.0, atol=0.01):
                    raise ValueError("BGE vectors must contain normalized coordinates")
                vectors /= norms
            _validate_vectors(vectors, count=len(indices))
            with tempfile.NamedTemporaryFile(mode="wb", dir=batches_dir, prefix=".embedding-batch.", delete=False) as handle:
                np.save(handle, vectors, allow_pickle=False)
                temporary = Path(handle.name)
            path = batches_dir / f"batch-{batch_index:06d}.npy"
            temporary.replace(path)
            records.append({"batch_index": batch_index, "rows": len(indices),
                            "input_sha256": input_sha, "sha256": file_sha256(path)})
            _write_json(stage / "build_state.json", state)
            print(json.dumps({"phase": "embedding_batches", "completed_batches": len(records),
                              "total_batches": len(plan), "completed_chunks": min(len(records) * batch_size, len(new_rows)),
                              "total_chunks": len(new_rows), "precision": precision,
                              "resumed_batches": resumed_batches,
                              "batch_seconds": round(time.monotonic() - batch_started, 3)}), flush=True)
        del model
    # Only the small batch result is resident. Assemble a memory-mapped final
    # checkpoint and publish it atomically after all batch hashes validate.
    with tempfile.NamedTemporaryFile(dir=stage, prefix=".new_embeddings.", delete=False) as handle:
        temporary = Path(handle.name)
    assembled = np.lib.format.open_memmap(temporary, mode="w+", dtype=np.float32,
                                         shape=(len(new_rows), BGE_DIMENSIONS))
    for batch_index, (indices, _) in enumerate(plan):
        path = batches_dir / f"batch-{batch_index:06d}.npy"
        if file_sha256(path) != records[batch_index]["sha256"]:
            raise ValueError("embedding batch checkpoint SHA-256 changed before assembly")
        batch = np.load(path, mmap_mode="r", allow_pickle=False)
        _validate_vectors(batch, count=len(indices))
        assembled[indices] = batch
    assembled.flush()
    _validate_vectors(assembled, count=len(new_rows))
    del assembled
    temporary.replace(destination)
    state["new_embeddings_sha256"] = file_sha256(destination)
    _write_json(stage / "build_state.json", state)
    return np.load(destination, mmap_mode="r", allow_pickle=False)


def build_package(*, articles_dir: Path, base_package: Path, output_package: Path,
                  snapshot_version: str, input_manifest: Path | None = None,
                  expected_manifest_sha256: str | None = None, resume: bool = False,
                  model_copy_mode: str = "hardlink", batch_size: int = 8,
                  device: str = "cpu", threads: int = 2, precision: str = "float32") -> dict:
    import numpy as np

    if (not snapshot_version.strip() or len(snapshot_version) > 128
            or not 1 <= batch_size <= 64 or not 1 <= threads <= 16
            or model_copy_mode not in {"hardlink", "copy"} or device not in {"cpu", "cuda", "mps"}
            or precision not in {"float32", "float16"}):
        raise ValueError("invalid snapshot version or BGE build settings")
    base, output = base_package.resolve(), output_package.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output package already exists; choose a new version (never overwrite)")
    output = output.resolve()
    if (output.is_relative_to(base) or base.is_relative_to(output)
            or articles_dir.resolve().is_relative_to(output)):
        raise ValueError("output package must be independent of source packages and articles")
    articles, audit = load_articles(articles_dir, input_manifest or articles_dir.parent / "manifest.json",
                                     expected_manifest_sha256=expected_manifest_sha256)
    base_digest = _verify_package(base)
    _validate_model_config(base / _MODEL_PATH)
    old_chunks = _read_chunks(base / _CORPUS_PATH / "chunks.jsonl", tenant_id="incremental-build")
    old_ids = {row.news_id for row in old_chunks}
    old_vectors = np.load(base / _CORPUS_PATH / "embeddings.npy", mmap_mode="r", allow_pickle=False)
    _validate_vectors(old_vectors, count=len(old_chunks))
    new_rows, skipped = article_chunks(articles, old_ids)
    identity = {"base_package": str(base), "base_manifest_sha256": base_digest,
                "input_manifest_sha256": audit["manifest_sha256"], "articles": audit["articles"],
                "snapshot_version": snapshot_version, "chunk_chars": CHUNK_CHARS,
                "chunk_overlap": CHUNK_OVERLAP, "batch_size": batch_size, "device": device,
                "model_copy_mode": model_copy_mode, "threads": threads, "precision": precision,
                "embedding_plan_version": EMBEDDING_PLAN_VERSION}
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_name("." + output.name + ".building")
    try:
        stage.mkdir()
        state = {"schema_version": 1, "identity": identity, "status": "building"}
        _write_json(stage / "build_state.json", state)
    except FileExistsError:
        if not resume or stage.is_symlink():
            raise ValueError("private build directory exists; use --resume with identical inputs or a new output version")
        state = _read_json(stage / "build_state.json")
        if state.get("schema_version") != 1 or state.get("identity") != identity:
            raise ValueError("resume inputs differ from the failed build; choose a new output version")
    if any(path.is_symlink() for path in stage.rglob("*")):
        raise ValueError("private build directory contains symlinks; choose a new output version")
    with _build_lock(stage):
        try:
            state["status"] = "building"
            state.pop("failure_type", None)
            state.pop("recovery", None)
            _write_json(stage / "build_state.json", state)
            # Discard only abandoned files created by this tool, never model
            # hardlinks or the validated embedding checkpoint.
            for directory, prefixes in ((stage, (".new_embeddings.",)),
                                        (stage / _CORPUS_PATH, (".chunks.", ".embeddings.", ".index."))):
                if directory.is_dir():
                    for abandoned in directory.iterdir():
                        if abandoned.is_file() and abandoned.name.startswith(prefixes):
                            abandoned.unlink()
            corpus = stage / _CORPUS_PATH
            corpus.mkdir(parents=True, exist_ok=True)
            copy_counts = _copy_model(base, stage, mode=model_copy_mode)
            # Set the CPU budget before importing the inference runtime.
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"
            os.environ["USE_TF"] = "0"
            os.environ["USE_FLAX"] = "0"
            os.environ["OMP_NUM_THREADS"] = str(threads)
            os.environ["MKL_NUM_THREADS"] = str(threads)
            new_vectors = _checkpoint_vectors(stage, state, new_rows, model_dir=stage / _MODEL_PATH,
                                              batch_size=batch_size, device=device, precision=precision)
            chunks_path = corpus / "chunks.jsonl"
            # Copy bytes unchanged: old rows with absent title/date metadata stay
            # byte-for-byte identical, preserving their original HNSW positions.
            with tempfile.NamedTemporaryFile(mode="wb", dir=corpus, prefix=".chunks.", delete=False) as destination, (base / _CORPUS_PATH / "chunks.jsonl").open("rb") as source:
                temporary_chunks = Path(destination.name)
                shutil.copyfileobj(source, destination)
                if source.tell():
                    source.seek(-1, os.SEEK_END)
                    if source.read(1) != b"\n" and new_rows:
                        destination.write(b"\n")
                for row in new_rows:
                    destination.write((json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
            temporary_chunks.replace(chunks_path)
            total_rows = len(old_chunks) + len(new_rows)
            with tempfile.NamedTemporaryFile(dir=corpus, prefix=".embeddings.", delete=False) as handle:
                temporary_vectors = Path(handle.name)
            combined = np.lib.format.open_memmap(temporary_vectors, mode="w+",
                                                 dtype=np.float32, shape=(total_rows, BGE_DIMENSIONS))
            combined[:len(old_chunks)] = old_vectors
            combined[len(old_chunks):] = new_vectors
            combined.flush()
            _validate_vectors(combined, count=total_rows)
            import hnswlib

            index = hnswlib.Index(space="ip", dim=BGE_DIMENSIONS)
            index.init_index(max_elements=total_rows, ef_construction=200, M=32, random_seed=100)
            index.set_num_threads(threads)
            index.add_items(combined, np.arange(total_rows), num_threads=threads)
            index.set_ef(256)
            if index.get_current_count() != total_rows or set(index.get_ids_list()) != set(range(total_rows)):
                raise ValueError("rebuilt HNSW labels differ from JSONL row positions")
            with tempfile.NamedTemporaryFile(dir=corpus, prefix=".index.", delete=False) as handle:
                temporary_index = Path(handle.name)
            index.save_index(str(temporary_index))
            temporary_index.replace(corpus / "index.bin")
            del combined
            temporary_vectors.replace(corpus / "embeddings.npy")
            model_files = sorted(path for path in (stage / _MODEL_PATH).rglob("*") if path.is_file())
            package_files = model_files + [corpus / name for name in ("chunks.jsonl", "embeddings.npy", "index.bin")]
            (stage / "SHA256SUMS").write_text("".join(
                f"{file_sha256(path)}  {path.relative_to(stage).as_posix()}\n"
                for path in sorted(package_files)), encoding="utf-8")
            package_digest = _verify_package(stage)
            parsed = _read_chunks(chunks_path, tenant_id="incremental-build")
            if len(parsed) != total_rows:
                raise ValueError("published chunk count differs from the vector row count")
            # Recheck sources before publication, including model hardlinks.
            if _verify_package(base) != base_digest:
                raise ValueError("frozen base package changed during this build")
            _, final_audit = load_articles(articles_dir, Path(audit["manifest_path"]),
                                           expected_manifest_sha256=audit["manifest_sha256"])
            if final_audit["articles"] != audit["articles"]:
                raise ValueError("input articles changed during this build")
            report = {"schema_version": 1, "status": "completed", "snapshot_version": snapshot_version,
                      "output_package": str(output), "built_at": datetime.now(timezone.utc).isoformat(),
                      "base_package": str(base), "base_manifest_sha256": base_digest,
                      "package_manifest_sha256": package_digest,
                      "embedding_version": "bge-large-zh-v1.5:" + package_digest[:16],
                      "input": audit, "input_articles": len(articles),
                      "base_articles": len(old_ids), "base_chunks": len(old_chunks),
                      "added_articles": len(articles) - len(skipped), "added_chunks": len(new_rows),
                      "skipped_existing_news_ids": skipped, "total_articles": len(old_ids) + len(articles) - len(skipped),
                      "total_chunks": total_rows, "dimensions": BGE_DIMENSIONS,
                      "model_files": copy_counts,
                      "embedding_inference": {"device": device, "precision": precision,
                                              "stored_vector_dtype": "float32", "max_sequence_length": 512,
                                              "batch_size": batch_size, "plan_version": EMBEDDING_PLAN_VERSION,
                                              "checkpoint_batches": len(state.get("embedding_batches", [])),
                                              "normalization": "l2-float32" if precision == "float16" else "model-l2-float32"},
                      "chunking": {"body_chars": CHUNK_CHARS, "overlap_chars": CHUNK_OVERLAP,
                                   "metadata_header_repeated": True},
                      "hnsw": {"space": "ip", "M": 32, "ef_construction": 200, "ef": 256,
                               "random_seed": 100, "threads": threads},
                      "limitations": ["Public source collection is bounded; counts do not establish full news coverage.",
                                       "Existing news IDs are preserved and skipped; corrections require a separate versioned workflow.",
                                       "This tool builds files only; API and Worker must explicitly select the new package."]}
            _write_json(stage / "build_report.json", report)
            state["status"] = "completed"
            _write_json(stage / "build_state.json", state)
            if output.exists() or output.is_symlink():
                raise ValueError("output appeared during build; publication refused")
            stage.rename(output)
            return report
        except BaseException as exc:
            state["status"] = "failed"
            state["failure_type"] = type(exc).__name__
            # Exception bodies may come from third-party libraries. Persist a
            # type and recovery instruction, avoiding uncontrolled text/logs.
            state["recovery"] = "Rerun the identical command with --resume; choose a new output version if inputs changed."
            _write_json(stage / "build_state.json", state)
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--articles-dir", required=True, type=Path)
    parser.add_argument("--base-package", required=True, type=Path)
    parser.add_argument("--output-package", required=True, type=Path)
    parser.add_argument("--snapshot-version", required=True)
    parser.add_argument("--input-manifest", type=Path)
    parser.add_argument("--expected-manifest-sha256")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--model-copy-mode", choices=("hardlink", "copy"), default="hardlink")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--device", choices=("cpu", "cuda", "mps"), default="cpu")
    parser.add_argument("--precision", choices=("float32", "float16"), default="float32",
                        help="Inference dtype only; source model stays unchanged and stored vectors remain float32")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    try:
        report = build_package(**vars(args))
    except Exception as exc:
        parser.exit(1, f"BGE build failed ({type(exc).__name__}): {exc}\n")
    print(json.dumps({key: report[key] for key in (
        "status", "output_package", "snapshot_version", "input_articles", "added_articles", "added_chunks",
        "total_articles", "total_chunks", "embedding_version", "package_manifest_sha256")},
        ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
