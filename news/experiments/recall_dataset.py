"""Prepare an explicit, auditable corpus for offline news retrieval evaluation.

Snapshot corpora contain previously returned excerpts, not complete articles. They
are useful for a local smoke experiment but inherit the old retriever's candidate
selection bias. Question text and reference answers are never corpus inputs.
All parsing, identity checks and slicing use the Python standard library.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from statistics import median
from typing import Iterable
from urllib.parse import urlsplit


_NEWS_PATH = re.compile(r"^/rain/a/([A-Za-z0-9_-]+)/?$")
_SOURCE_LINE = re.compile(r"^来源地址\s*[:：]\s*(\S+)\s*$")
_HEADER_LINE = re.compile(r"^(?:作者|发布时间)\s*[:：]")
_PUBLISHED_LINE = re.compile(r"^发布时间\s*[:：]\s*(.*?)\s*$")
_TITLE_LINE = re.compile(r"^#\s+(.+)$")


def news_id_from_url(url: str) -> str:
    """Extract a news ID from an HTTPS Tencent News article URL.

    Only the crawler's ``https://news.qq.com/rain/a/<news_id>`` URL format
    is accepted. Query strings and fragments do not change article identity.
    File names, model outputs and caller-supplied news IDs cannot establish it.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("news URL must be a nonempty string")
    parsed = urlsplit(url.strip())
    match = _NEWS_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "news.qq.com" or not match:
        raise ValueError("expected https://news.qq.com/rain/a/<news_id> URL")
    return match.group(1)


def _source_url(news_id: str) -> str:
    return f"https://news.qq.com/rain/a/{news_id}"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON dataset {path}: {exc}") from exc


def _paths(paths: Iterable[str | Path]) -> list[Path]:
    out = [Path(path) for path in paths]
    if not out:
        raise ValueError("at least one explicit input path is required")
    resolved = [path.resolve() for path in out]
    if len(resolved) != len(set(resolved)):
        raise ValueError("input paths must not contain duplicate files")
    return out


def _text_with_title(title: str, body: str) -> str:
    return f"{title}\n{body}" if title else body


def _snapshot_body(text: str, source_name: str) -> tuple[str, str, str, str]:
    """Read the metadata preamble without deleting similarly named body lines."""
    lines = text.splitlines()
    title = source_name.strip()
    title_seen = False
    publish_time = ""
    source_urls: list[str] = []
    body_start = 0
    for position, line in enumerate(lines):
        stripped = line.strip()
        title_match = _TITLE_LINE.fullmatch(stripped)
        source_match = _SOURCE_LINE.fullmatch(stripped)
        if title_match and not title_seen and not source_urls:
            title = title_match.group(1).strip()
            title_seen = True
        elif source_match:
            source_urls.append(source_match.group(1))
        elif _PUBLISHED_LINE.fullmatch(stripped):
            publish_time = _PUBLISHED_LINE.fullmatch(stripped).group(1)
        elif stripped and not _HEADER_LINE.match(stripped):
            body_start = position
            break
        body_start = position + 1
    if not source_urls:
        raise ValueError("snapshot excerpt has no source URL in its preamble")
    identities = {news_id_from_url(url) for url in source_urls}
    if len(identities) != 1:
        raise ValueError("snapshot excerpt has conflicting source URLs")
    news_id = identities.pop()
    body = "\n".join(lines[body_start:]).strip()
    return news_id, title, body, publish_time


def load_snapshot_chunks(paths: Iterable[str | Path]) -> tuple[list[dict], dict]:
    """Extract deduplicated excerpts only from ``items[].retrieved[].text``.

    Blank excerpts and excerpts with absent/invalid source metadata are excluded
    with visible counts. Invalid JSON structures and conflicting source URLs
    fail explicitly. Duplicate identity is news ID plus SHA256 of the body.
    ``text`` is the embedding input: the title followed by the cleaned excerpt.
    """
    files = _paths(paths)
    chunks: list[dict] = []
    seen: set[tuple[str, str]] = set()
    excluded: Counter[str] = Counter()
    body_lengths: list[int] = []
    records = 0
    for path in files:
        snapshot = _read_json(path)
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("items"), list):
            raise ValueError(f"{path}: expected snapshot object with items list")
        for item_index, item in enumerate(snapshot["items"]):
            if not isinstance(item, dict) or not isinstance(item.get("retrieved"), list):
                raise ValueError(f"{path}: item {item_index} requires retrieved list")
            for retrieved_index, record in enumerate(item["retrieved"]):
                records += 1
                location = f"{path}: items[{item_index}].retrieved[{retrieved_index}]"
                if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                    raise ValueError(f"{location}: text must be a string")
                text = record["text"].strip()
                if not text:
                    excluded["empty_text"] += 1
                    continue
                source_name = record.get("source_name", "")
                if source_name is None:
                    source_name = ""
                if not isinstance(source_name, str):
                    raise ValueError(f"{location}: source_name must be a string")
                try:
                    news_id, title, body, publish_time = _snapshot_body(text, source_name)
                except ValueError as exc:
                    if "conflicting" in str(exc):
                        raise ValueError(f"{location}: {exc}") from exc
                    excluded["missing_or_invalid_source_url"] += 1
                    continue
                declared_id = record.get("news_id")
                if declared_id is not None and str(declared_id) != news_id:
                    raise ValueError(f"{location}: news_id conflicts with source URL")
                if not body:
                    excluded["empty_body"] += 1
                    continue
                digest = _sha256(body)
                identity = (news_id, digest)
                if identity in seen:
                    excluded["duplicate_news_id_and_body"] += 1
                    continue
                seen.add(identity)
                body_lengths.append(len(body))
                chunk = {
                    "chunk_id": f"{news_id}:{digest}",
                    "news_id": news_id,
                    "title": title,
                    "text": _text_with_title(title, body),
                    "source_url": _source_url(news_id),
                    "source_snapshot": str(path.resolve()),
                    "source_text_hash": digest,
                }
                if publish_time:
                    chunk["publish_time"] = publish_time
                for field in ("content_version", "publish_time"):
                    if record.get(field) is not None:
                        chunk[field] = record[field]
                chunks.append(chunk)
    provenance = {
        "source_kind": "historical_retrieval_excerpts",
        "source_files": [str(path.resolve()) for path in files],
        "retrieved_records": records,
        "chunk_count": len(chunks),
        "news_count": len({chunk["news_id"] for chunk in chunks}),
        "excluded_records": dict(excluded),
        "body_hash_algorithm": "sha256_utf8_cleaned_body",
        "excerpt_body_chars": {
            "min": min(body_lengths, default=0),
            "median": median(body_lengths) if body_lengths else 0,
            "max": max(body_lengths, default=0),
        },
        "rechunked": False,
        "limitations": [
            "Contains historical retrieved excerpts, not complete article bodies.",
            "The corpus inherits candidate-pool selection bias from the historical retriever.",
            "Question and reference-answer fields are excluded from the corpus.",
        ],
    }
    return chunks, provenance


def load_articles_chunks(
    articles_dir: str | Path, chunk_chars: int = 800, overlap: int = 80,
) -> tuple[list[dict], dict]:
    """Slice crawler JSON article bodies and repeat the title within the cap.

    ``chunk_chars`` is a character cap for title + newline + body, not a model
    token limit. ``overlap`` applies to the body; an overly long title is rejected
    instead of silently truncating the title or changing the requested overlap.
    """
    if isinstance(chunk_chars, bool) or not isinstance(chunk_chars, int) or chunk_chars <= 0:
        raise ValueError("chunk_chars must be a positive integer")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or not 0 <= overlap < chunk_chars:
        raise ValueError("overlap must be an integer in [0, chunk_chars)")
    directory = Path(articles_dir)
    if not directory.is_dir():
        raise ValueError(f"article directory does not exist: {directory}")
    files = sorted(directory.glob("*.json"))
    collection_manifest = None
    manifest_path = directory / "collection_manifest.json"
    if manifest_path.exists():
        collection_manifest = _read_json(manifest_path)
        if not isinstance(collection_manifest, dict):
            raise ValueError("collection manifest must be an object")
        selected = collection_manifest.get("successful_news_ids")
        if not isinstance(selected, list) or not selected:
            raise ValueError("collection manifest requires successful_news_ids")
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in selected):
            raise ValueError("collection manifest has an invalid news ID")
        if len(selected) != len(set(selected)):
            raise ValueError("collection manifest requires unique successful_news_ids")
        files = [directory / f"{news_id}.json" for news_id in selected]
        canonical = [_read_json(path) for path in files]
        for expected_id, article in zip(selected, canonical):
            if not isinstance(article, dict) or news_id_from_url(article.get("url")) != expected_id:
                raise ValueError("collection manifest news ID differs from article source URL")
        manifest_hash = hashlib.sha256(json.dumps(
            canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        if manifest_hash != collection_manifest.get("hash"):
            raise ValueError("frozen article corpus hash differs from collection manifest")
    if not files:
        raise ValueError(f"article directory has no JSON files: {directory}")
    chunks: list[dict] = []
    seen_articles: dict[str, tuple[str, str, str]] = {}
    excluded: Counter[str] = Counter()
    for path in files:
        article = _read_json(path)
        if not isinstance(article, dict):
            raise ValueError(f"{path}: expected article object")
        body = article.get("content", "")
        title = article.get("title", "")
        if body is None:
            body = ""
        if title is None:
            title = ""
        if not isinstance(body, str) or not isinstance(title, str):
            raise ValueError(f"{path}: content and title must be strings")
        body, title = body.strip(), title.strip()
        if not body:
            excluded["empty_body"] += 1
            continue
        try:
            news_id = news_id_from_url(article.get("url"))
        except ValueError as exc:
            raise ValueError(f"{path}: {exc}") from exc
        declared_id = article.get("news_id")
        if declared_id is not None and str(declared_id) != news_id:
            raise ValueError(f"{path}: news_id conflicts with source URL")
        identity = (
            title, _sha256(body),
            json.dumps(article.get("content_version"), sort_keys=True, ensure_ascii=False),
        )
        if news_id in seen_articles:
            if seen_articles[news_id] != identity:
                raise ValueError(f"{path}: conflicting article versions for news_id {news_id}")
            excluded["duplicate_article"] += 1
            continue
        seen_articles[news_id] = identity
        body_chars = chunk_chars - (len(title) + 1 if title else 0)
        if body_chars <= overlap:
            raise ValueError(f"{path}: title leaves insufficient body capacity for overlap")
        for start in range(0, len(body), body_chars - overlap):
            piece = body[start:start + body_chars].strip()
            if piece:
                digest = _sha256(piece)
                chunk = {
                    "chunk_id": f"{news_id}:{start}:{digest}",
                    "news_id": news_id,
                    "title": title,
                    "text": _text_with_title(title, piece),
                    "source_url": _source_url(news_id),
                    "source_article": str(path.resolve()),
                    "source_text_hash": digest,
                    "body_start": start,
                }
                for field in ("content_version", "publish_time"):
                    if article.get(field) is not None:
                        chunk[field] = article[field]
                chunks.append(chunk)
            if start + body_chars >= len(body):
                break
    return chunks, {
        "source_kind": "crawler_article_json",
        "source_directory": str(directory.resolve()),
        "collection_manifest": collection_manifest,
        "article_files": len(files),
        "news_count": len(seen_articles),
        "chunk_count": len(chunks),
        "excluded_records": dict(excluded),
        "chunk_chars": chunk_chars,
        "body_overlap_chars": overlap,
        "title_repeated_per_chunk": True,
        "limitations": [
            "Character caps do not guarantee fit within the embedding model token limit.",
            "Conflicting article versions must be resolved before creating the corpus.",
        ],
    }


def load_questions(paths: Iterable[str | Path]) -> list[dict]:
    """Load evaluation queries with filename namespaces and URL-derived labels.

    Reused row IDs across differently named datasets are allowed. Duplicate IDs
    within a dataset and ambiguous filename namespaces raise an error. Expected
    URLs are the only relevance labels; reference answers are never returned.
    """
    files = _paths(paths)
    namespaces: set[str] = set()
    query_ids: set[str] = set()
    queries: list[dict] = []
    for path in files:
        namespace = path.stem
        if namespace in namespaces:
            raise ValueError(f"duplicate question dataset namespace: {namespace}")
        namespaces.add(namespace)
        rows = _read_json(path)
        if not isinstance(rows, list):
            raise ValueError(f"{path}: expected question list")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise ValueError(f"{path}: question {index} must be an object")
            raw_id = row.get("id")
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)) or not str(raw_id).strip():
                raise ValueError(f"{path}: question {index} requires nonempty id")
            query_id = f"{namespace}:{raw_id}"
            if query_id in query_ids:
                raise ValueError(f"duplicate query ID: {query_id}")
            question = row.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError(f"{path}: question {index} requires nonempty question")
            urls = row.get("expected_urls")
            if not isinstance(urls, list) or not urls:
                raise ValueError(f"{path}: question {index} requires expected_urls labels")
            relevance: dict[str, int] = {}
            for url in urls:
                try:
                    relevance[news_id_from_url(url)] = 1
                except ValueError as exc:
                    raise ValueError(f"{path}: question {index}: {exc}") from exc
            topic = row.get("topic", "")
            if topic is None:
                topic = ""
            if not isinstance(topic, str):
                raise ValueError(f"{path}: question {index}: topic must be a string")
            query_ids.add(query_id)
            queries.append({
                "query_id": query_id,
                "question": question.strip(),
                "relevance": relevance,
                "topic": topic,
            })
    return queries
