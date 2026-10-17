"""Freeze an audited, bounded public-news increment without enterprise config.

Run from the repository root with PYTHONPATH=news. Discovery is Tencent's
public PC feed; complete bodies and their publication metadata come only from
allowlisted news.qq.com pages. A feed timestamp never substitutes for a missing
article timestamp. Existing valid article files remain frozen on continuation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

from experiments.collect_recall_articles import (
    MAX_HTML_BYTES, fetch_html, news_id_of, normalize_article_url,
    parse_article, valid_cached_article,
)

CHANNELS = ("tech", "sports", "finance", "ent", "auto", "edu")
ARTICLE_CATEGORIES = (*CHANNELS, "public")
MAX_PAGES_PER_CHANNEL = 1000
MAX_TARGET_PER_DAY = 10000
MAX_WORKERS = 8
FEED_URL = "https://i.news.qq.com/web_feed/getPCList"
MAX_FEED_BYTES = 4 * 1024 * 1024
TZ = ZoneInfo("Asia/Shanghai")
ARTICLE_ID = re.compile(r"^[0-9]{8}[A-Za-z0-9]{4,32}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json_bytes(value))
    temporary.replace(path)


def publication_time(raw: str) -> datetime:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("publication time is missing")
    value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.astimezone(TZ)


def days_between(start: date, end: date) -> list[str]:
    if start > end:
        raise ValueError("start_date must not be after end_date")
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def frozen_article(output_dir: Path, row: dict, start: date, end: date) -> dict:
    url = normalize_article_url(row["url"])
    news_id = news_id_of(url)
    relative = f"articles/{news_id}.json"
    if row.get("news_id") != news_id or row.get("relative_path") != relative:
        raise ValueError("article identity or relative path differs from its frozen manifest")
    article_path = output_dir / relative
    if article_path.is_symlink() or article_path.stat().st_size > MAX_HTML_BYTES:
        raise ValueError("frozen article is linked or exceeds the size limit")
    raw = article_path.read_bytes()
    if sha256(raw) != row.get("sha256"):
        raise ValueError("article file SHA-256 differs from its frozen manifest")
    article = validate_article(json.loads(raw), url, start, end)
    document_hash = sha256((article["title"] + "\x1f" + article["content"]).encode("utf-8"))
    if document_hash != row.get("document_sha256") or article.get("document_sha256") != document_hash:
        raise ValueError("article document SHA-256 differs from its frozen manifest")
    source_hash = article.get("source_html_sha256", "")
    if not SHA256.fullmatch(source_hash) or source_hash != row.get("source_html_sha256"):
        raise ValueError("article source HTML SHA-256 differs from its frozen manifest")
    if article["publish_time"] != row.get("publish_time") or article.get("category") != row.get("category"):
        raise ValueError("article publication metadata differs from its frozen manifest")
    return article


def verify_frozen_articles(output_dir: Path, *, verify_discovery_reference: bool = False,
                           allowed_pending_paths: set[str] | None = None) -> None:
    """Fail before continuation can re-sign altered files from an existing version."""
    path = output_dir / "manifest.json"
    if not path.exists():
        return
    manifest = json.loads(path.read_bytes())
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("articles"), list):
        raise ValueError("frozen article manifest has an invalid schema")
    start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
    expected_paths = set()
    for row in manifest["articles"]:
        try:
            frozen_article(output_dir, row, start, end)
            relative = row["relative_path"]
            if relative in expected_paths:
                raise ValueError("article identity or relative path differs from its frozen manifest")
            expected_paths.add(relative)
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise ValueError(f"frozen article integrity check failed: {row.get('news_id')}: {exc}") from exc
    actual_paths = {path.relative_to(output_dir).as_posix()
                    for path in (output_dir / "articles").glob("*.json")}
    if actual_paths != expected_paths | (allowed_pending_paths or set()):
        raise ValueError("article cache contains files outside its frozen manifest")
    if verify_discovery_reference:
        source_path = manifest.get("source_manifest")
        if (not isinstance(source_path, str) or Path(source_path).is_absolute()
                or ".." in Path(source_path).parts or not source_path.endswith(".json")):
            raise ValueError("frozen manifest has an unsupported discovery reference")
        referenced = output_dir / source_path
        if referenced.is_symlink() or not referenced.resolve().is_relative_to(output_dir.resolve()):
            raise ValueError("frozen discovery reference escapes output directory")
        if sha256(referenced.read_bytes()) != manifest.get("source_manifest_sha256"):
            raise ValueError("discovery manifest SHA-256 differs from the frozen article manifest")


def verify_discovery_cache(output_dir: Path, previous: dict, requested_page_size: int) -> None:
    if previous.get("schema_version") != 1 or not isinstance(previous.get("feed_requests"), list):
        raise ValueError("frozen discovery manifest has an invalid schema")
    if previous.get("settings", {}).get("page_size") != requested_page_size:
        raise ValueError("cached feed request does not match the requested page settings; use a new output version")
    for row in previous["feed_requests"]:
        if row.get("status") != "success":
            continue
        try:
            channel, page = row["channel"], row["page"]
            request = feed_request(channel, page, requested_page_size)
            relative = f"discovery/{channel}/page-{page:03d}.json"
            if (row.get("endpoint") != FEED_URL or row.get("relative_path") != relative
                    or row.get("request") != request or row.get("request_sha256") != sha256(json_bytes(request))):
                raise ValueError("feed request parameters or SHA-256 differ from the frozen discovery manifest")
            path = output_dir / relative
            if path.stat().st_size > MAX_FEED_BYTES:
                raise ValueError("frozen feed exceeds the size limit")
            if sha256(path.read_bytes()) != row.get("response_sha256"):
                raise ValueError("feed response SHA-256 differs from the frozen discovery manifest")
            request_path = path.with_suffix(".request.json")
            if request_path.exists() and json.loads(request_path.read_bytes()) != request:
                raise ValueError("cached feed request parameters differ from the frozen discovery manifest")
        except (KeyError, OSError, ValueError, TypeError) as exc:
            raise ValueError(f"frozen feed integrity check failed: {row.get('channel')}/{row.get('page')}: {exc}") from exc


class RejectFeedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("public feed redirects are not allowed")


def feed_request(channel: str, page: int, page_size: int) -> dict:
    if channel not in CHANNELS:
        raise ValueError("unsupported public news channel")
    if type(page) is not int or not 0 <= page < MAX_PAGES_PER_CHANNEL:
        raise ValueError(f"feed page must be an integer from 0 through {MAX_PAGES_PER_CHANNEL - 1}")
    if type(page_size) is not int or not 1 <= page_size <= 50:
        raise ValueError("page_size must be an integer from 1 through 50")
    return {
        "qimei36": "0_news_agent_crawler", "device_id": "0_news_agent_crawler",
        "forward": "2" if page == 0 else "1", "base_req": {"from": "pc"},
        "flush_num": page, "channel_id": f"news_news_{channel}",
        "item_count": page_size, "is_local_chlid": "0",
    }


def fetch_feed(channel: str, page: int, page_size: int, timeout: float) -> bytes:
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    body = json.dumps(feed_request(channel, page, page_size), separators=(",", ":")).encode()
    request = Request(FEED_URL, data=body, method="POST", headers={
        "User-Agent": "Mozilla/5.0 NewsAgent-Public-Increment/1.0",
        "Content-Type": "application/json", "Accept": "application/json",
        "Accept-Encoding": "identity",
    })
    with build_opener(RejectFeedRedirect()).open(request, timeout=timeout) as response:
        if response.geturl() != FEED_URL:
            raise ValueError("feed response left its fixed public endpoint")
        raw = response.read(MAX_FEED_BYTES + 1)
        if not raw.strip() or len(raw) > MAX_FEED_BYTES:
            raise ValueError("feed response is empty or exceeds the 4 MiB limit")
        return raw


def retry(operation, retries: int = 2):
    for attempt in range(retries + 1):
        try:
            return operation()
        except HTTPError as exc:
            if exc.code != 429 and exc.code < 500:
                raise
            if attempt == retries:
                raise
        except (URLError, TimeoutError):
            if attempt == retries:
                raise
        time.sleep(0.5 * 2 ** attempt)
    raise RuntimeError("bounded retry exhausted")


def extract_candidates(payload: dict, channel: str, start: date, end: date):
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        raise ValueError("public feed did not return a successful object")
    data = payload.get("data")
    if not isinstance(data, list):
        raise ValueError("public feed data must be a list")
    candidates, rejected = [], []
    for parent in data:
        if not isinstance(parent, dict):
            rejected.append({"channel": channel, "reason": "feed item is not an object"})
            continue
        children = parent.get("sub_item") or [parent]
        if not isinstance(children, list):
            rejected.append({"channel": channel, "reason": "sub_item is not a list"})
            continue
        for item in children:
            if not isinstance(item, dict):
                continue
            news_id = item.get("id")
            # Only standalone text articles; aggregation topics and video are
            # not complete article candidates, even if they have summaries.
            if str(item.get("articletype")) != "0":
                rejected.append({"channel": channel, "news_id": news_id,
                                 "reason": "non-text or aggregation item"})
                continue
            try:
                if not isinstance(news_id, str) or not ARTICLE_ID.fullmatch(news_id):
                    raise ValueError("invalid standalone article id")
                url = normalize_article_url(f"https://news.qq.com/rain/a/{news_id}")
                published = publication_time(item.get("publish_time"))
                if not start <= published.date() <= end:
                    rejected.append({"channel": channel, "news_id": news_id, "url": url,
                                     "publish_time": published.isoformat(), "reason": "feed date outside window"})
                    continue
                candidates.append({"news_id": news_id, "url": url, "category": channel,
                                   "feed_publish_time": published.isoformat(),
                                   "feed_title": str(item.get("title") or "")})
            except (ValueError, TypeError) as exc:
                rejected.append({"channel": channel, "news_id": news_id, "reason": str(exc)})
    return candidates, rejected


def discover(output_dir: Path, start: date, end: date, *, pages_per_channel: int = 200,
             page_size: int = 50, timeout: float = 15, interval: float = 0.25) -> dict:
    if type(pages_per_channel) is not int or not 1 <= pages_per_channel <= MAX_PAGES_PER_CHANNEL:
        raise ValueError(f"pages_per_channel must be an integer from 1 through {MAX_PAGES_PER_CHANNEL}")
    if interval < 0:
        raise ValueError("interval cannot be negative")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    days = days_between(start, end)
    pin_discovery_reference(output_dir)
    prior_audits = {}
    previous_manifest = output_dir / "discovery_manifest.json"
    if previous_manifest.exists():
        previous = json.loads(previous_manifest.read_bytes())
        verify_discovery_cache(output_dir, previous, page_size)
        prior_audits = {(row["channel"], row["page"]): row
                        for row in previous.get("feed_requests", []) if row.get("status") == "success"}
    candidates, rejected, audits, failures, duplicates = {}, [], [], [], []

    def checkpoint():
        rows = sorted(candidates.values(), key=lambda row: (row["category"], row["feed_publish_time"], row["url"]))
        counts = Counter(row["feed_publish_time"][:10] for row in rows)
        result = {"schema_version": 1, "generated_at": now_utc(), "start_date": start.isoformat(),
                  "end_date": end.isoformat(), "timezone": "Asia/Shanghai", "candidates": rows,
                  "counts_by_date": {day: counts[day] for day in days}, "feed_requests": audits,
                  "failures": failures, "rejected": rejected, "duplicates": duplicates,
                  "settings": {"pages_per_channel": pages_per_channel, "page_size": page_size}}
        write_json(output_dir / "discovery_manifest.json", result)
        return result
    for channel in CHANNELS:
        for page in range(pages_per_channel):
            request = feed_request(channel, page, page_size)
            path = output_dir / "discovery" / channel / f"page-{page:03d}.json"
            request_path = path.with_suffix(".request.json")
            audit = {"channel": channel, "page": page, "endpoint": FEED_URL,
                     "request": request, "request_sha256": sha256(json_bytes(request)),
                     "request_hash_encoding": "SHA-256 of indented UTF-8 request JSON with final newline",
                     "relative_path": path.relative_to(output_dir).as_posix()}
            try:
                cached = path.exists()
                if cached:
                    if path.stat().st_size > MAX_FEED_BYTES:
                        raise ValueError("cached feed exceeds size limit")
                    recorded_request = (json.loads(request_path.read_bytes()) if request_path.exists()
                                        else prior_audits.get((channel, page), {}).get("request"))
                    if recorded_request != request:
                        raise ValueError("cached feed request does not match the requested page settings")
                    raw = path.read_bytes()
                else:
                    raw = retry(lambda: fetch_feed(channel, page, page_size, timeout))
                payload = json.loads(raw)
                found, invalid = extract_candidates(payload, channel, start, end)
                if not cached:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(raw)
                if not request_path.exists():
                    write_json(request_path, request)
                audit.update({"status": "success", "cached": cached,
                              "response_sha256": sha256(raw), "response_bytes": len(raw),
                              "candidate_count": len(found), "rejected_count": len(invalid)})
                rejected.extend({**row, "page": page} for row in invalid)
                for row in found:
                    url = row["url"]
                    if url in candidates:
                        duplicates.append({"url": url, "channel": channel, "page": page})
                        existing = candidates[url]
                        if channel not in existing["channels"]:
                            existing["channels"].append(channel)
                    else:
                        candidates[url] = {**row, "channels": [channel]}
                counts = Counter(row["feed_publish_time"][:10] for row in found)
                print(json.dumps({"phase": "discovery", "channel": channel, "page": page,
                                  "cached": cached, "candidates_by_date": dict(counts),
                                  "unique_candidates_so_far": len(candidates)}, ensure_ascii=False), flush=True)
            except Exception as exc:
                audit.update({"status": "failed", "reason": f"{type(exc).__name__}: {exc}"})
                failures.append(audit.copy())
                print(json.dumps({"phase": "discovery", "channel": channel, "page": page,
                                  "error": audit["reason"]}, ensure_ascii=False), flush=True)
            audits.append(audit)
            checkpoint()
            if not audit.get("cached"):
                time.sleep(interval)
    result = checkpoint()
    rows = result["candidates"]
    (output_dir / "candidate_urls.txt").write_text("".join(row["url"] + "\n" for row in rows), encoding="utf-8")
    return result


def validate_article(article: dict, url: str, start: date, end: date) -> dict:
    if not valid_cached_article(article, url):
        raise ValueError("article does not satisfy complete-body cache contract")
    title, content = article["title"], article["content"]
    if len(title) > 500 or "\n" in title or "\r" in title:
        raise ValueError("article title is outside the knowledge API contract")
    if len(content) > 200000:
        raise ValueError("article body exceeds the knowledge API contract; original text is not truncated")
    published = publication_time(article["publish_time"])
    if not start <= published.date() <= end:
        raise ValueError(f"article date outside window: {published.isoformat()}")
    return {**article, "publish_time": published.isoformat()}


def pin_discovery_reference(output_dir: Path) -> None:
    """Preserve a verified old discovery revision before appending discovery."""
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        return
    verify_frozen_articles(output_dir, verify_discovery_reference=True)
    manifest = json.loads(manifest_path.read_bytes())
    if manifest["source_manifest"] != "discovery_manifest.json":
        return
    digest = manifest["source_manifest_sha256"]
    relative = f"source-discovery/discovery-{digest}.json"
    destination = output_dir / relative
    raw = (output_dir / "discovery_manifest.json").read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != raw:
        raise ValueError("immutable discovery revision already exists with different bytes")
    if not destination.exists():
        destination.write_bytes(raw)
    manifest["source_manifest"] = relative
    write_json(manifest_path, manifest)


def load_external_discovery(path: Path, output_dir: Path, start: date, end: date) -> dict:
    """Copy a public candidate manifest by hash; dates remain discovery hints."""
    if path.is_symlink() or path.stat().st_size > 512 * 1024 * 1024:
        raise ValueError("external discovery manifest is linked or exceeds 512 MiB")
    raw = path.read_bytes()
    value = json.loads(raw)
    if value.get("schema_version") != 1 or not isinstance(value.get("candidates"), list):
        raise ValueError("external discovery manifest has an invalid schema")
    for key, expected in (("start_date", start.isoformat()), ("end_date", end.isoformat())):
        if value.get(key, expected) != expected:
            raise ValueError("external discovery window differs from the requested publication window")
    candidates, seen = [], set()
    for item in value["candidates"]:
        url = normalize_article_url(item["url"])
        identity = news_id_of(url)
        if not ARTICLE_ID.fullmatch(identity) or item.get("news_id") != identity:
            raise ValueError("external candidate URL and news_id differ")
        if url in seen:
            continue
        category = item.get("category", "public")
        if category not in ARTICLE_CATEGORIES:
            raise ValueError("external candidate category is unsupported")
        seen.add(url)
        candidates.append({**item, "url": url, "category": category,
                           "channels": item.get("channels") or [category]})
    digest = sha256(raw)
    relative = f"source-discovery/discovery-{digest}.json"
    destination = output_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes() != raw:
        raise ValueError("external discovery revision SHA-256 collision or corruption")
    if not destination.exists():
        destination.write_bytes(raw)
    return {**value, "candidates": candidates, "settings": value.get("settings", {}),
            "_source_manifest": relative, "_source_manifest_sha256": digest}


def refresh_counts(manifest: dict) -> dict:
    days = days_between(date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"]))
    articles = sorted(manifest["articles"], key=lambda row: (row["publish_time"], row["news_id"]))
    counts = Counter(row["publish_time"][:10] for row in articles)
    channel_counts = Counter((row["category"], row["publish_time"][:10]) for row in articles)
    target = manifest.get("target_per_day", 1000)
    manifest.update({"articles": articles, "success": len(articles),
                     "cached": sum(bool(row.get("cached")) for row in articles),
                     "counts_by_date": {day: counts[day] for day in days},
                     "counts_by_channel_date": {channel: {day: channel_counts[(channel, day)] for day in days}
                                                for channel in ARTICLE_CATEGORIES},
                     "target_per_day": target, "targets_by_date": {day: target for day in days},
                     "deficits_by_date": {day: max(0, target - counts[day]) for day in days},
                     "quota_complete": all(counts[day] >= target for day in days),
                     "missing_dates": [day for day in days if not counts[day]],
                     "corpus_sha256": sha256(json_bytes([{k: v for k, v in row.items() if k != "cached"}
                                                         for row in articles])),
                     "checkpoint_at": now_utc()})
    return manifest


def recover_pending_articles(output_dir: Path) -> None:
    """Recover only journal-bound bytes, never re-sign an altered frozen file."""
    pending_path, manifest_path = output_dir / "pending_articles.json", output_dir / "manifest.json"
    if not pending_path.exists():
        return
    pending = json.loads(pending_path.read_bytes())
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    if pending.get("schema_version") != 1 or not isinstance(pending.get("articles"), list):
        raise ValueError("pending article journal has an invalid schema")
    existing = {row["relative_path"]: row for row in manifest["articles"]}
    ready = [row for row in pending["articles"] if (output_dir / row["relative_path"]).exists()]
    extras = {row["relative_path"] for row in ready} - set(existing)
    if sha256(manifest_raw) != pending.get("base_manifest_sha256"):
        # The manifest may already have committed this batch before a crash.
        if extras or any(existing.get(row["relative_path"], {}).get("sha256") != row["sha256"] for row in ready):
            raise ValueError("pending article journal does not belong to the frozen manifest")
        verify_frozen_articles(output_dir, verify_discovery_reference=True)
        pending_path.unlink()
        return
    verify_frozen_articles(output_dir, verify_discovery_reference=True, allowed_pending_paths=extras)
    start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
    for row in ready:
        frozen_article(output_dir, row, start, end)
        if row["relative_path"] not in existing:
            manifest["articles"].append(row)
    progress = pending.get("manifest_progress", {})
    for key in ("failures", "out_of_window", "duplicate_content", "skipped", "attempted_articles"):
        if key in progress:
            manifest[key] = progress[key]
    refresh_counts(manifest)
    manifest["collection_status"] = "checkpoint_recovered"
    write_json(manifest_path, manifest)
    pending_path.unlink()


def commit_article_batch(output_dir: Path, manifest: dict, prepared: list[tuple[dict, bytes]]) -> None:
    """Journal expected bytes before exposing new article files to continuation."""
    manifest_path = output_dir / "manifest.json"
    pending_path = output_dir / "pending_articles.json"
    if pending_path.exists():
        raise ValueError("an uncommitted article batch must be recovered before collection")
    write_json(pending_path, {"schema_version": 1, "base_manifest_sha256": sha256(manifest_path.read_bytes()),
                              "articles": [row for row, _ in prepared],
                              "manifest_progress": {key: manifest[key] for key in (
                                  "failures", "out_of_window", "duplicate_content", "skipped", "attempted_articles")
                                  if key in manifest}})
    for row, raw in prepared:
        if sha256(raw) != row["sha256"]:
            raise ValueError("new article bytes differ from the batch journal")
        path = output_dir / row["relative_path"]
        if path.exists():
            raise ValueError("a new article would replace an existing frozen file")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
        manifest["articles"].append(row)
    write_json(manifest_path, refresh_counts(manifest))
    pending_path.unlink()


def seed_from_manifest(output_dir: Path, source_manifest: Path, start: date, end: date, *,
                       target_per_day: int = 1000, expected_sha256: str | None = None) -> int:
    if source_manifest.name != "manifest.json" or source_manifest.is_symlink():
        raise ValueError("seed must be a regular frozen manifest.json")
    if output_dir.resolve() == source_manifest.parent.resolve():
        raise ValueError("seed source must belong to another frozen output version")
    recover_pending_articles(output_dir)
    verify_frozen_articles(source_manifest.parent, verify_discovery_reference=True)
    raw = source_manifest.read_bytes()
    digest = sha256(raw)
    if expected_sha256 is not None and digest != expected_sha256:
        raise ValueError("seed manifest SHA-256 differs from the expected value")
    source = json.loads(raw)
    if (source.get("start_date"), source.get("end_date")) != (start.isoformat(), end.isoformat()):
        raise ValueError("seed manifest belongs to a different publication window")
    provenance = {"path": str(source_manifest.resolve()), "sha256": digest}
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        verify_frozen_articles(output_dir, verify_discovery_reference=True)
        manifest = json.loads(manifest_path.read_bytes())
        if manifest.get("seed_manifest") != provenance:
            raise ValueError("output version is already bound to another seed manifest")
        if manifest.get("target_per_day") != target_per_day:
            raise ValueError("collection contract target differs from the existing seed version")
    else:
        if list((output_dir / "articles").glob("*.json")):
            raise ValueError("cannot seed over article files without a frozen manifest")
        source_raw = (source_manifest.parent / source["source_manifest"]).read_bytes()
        relative = f"source-discovery/discovery-{sha256(source_raw)}.json"
        (output_dir / relative).parent.mkdir(parents=True, exist_ok=True)
        (output_dir / relative).write_bytes(source_raw)
        manifest = {**source, "articles": [], "target_per_day": target_per_day,
                    "seed_manifest": provenance, "source_manifest": relative,
                    "source_manifest_sha256": sha256(source_raw), "collection_status": "seeding"}
        # The source version's collection contract remains auditable through
        # seed_manifest.sha256; this new version binds its own discovery later.
        manifest.pop("collection_contract", None)
        write_json(manifest_path, refresh_counts(manifest))
    present = {row["news_id"] for row in manifest["articles"]}
    remaining = [row for row in source["articles"] if row["news_id"] not in present]
    if not remaining:
        return len(source["articles"])
    for offset in range(0, len(remaining), 64):
        prepared = [({**row, "cached": True}, (source_manifest.parent / row["relative_path"]).read_bytes())
                    for row in remaining[offset:offset + 64]]
        commit_article_batch(output_dir, manifest, prepared)
    manifest["collection_status"] = "seeded"
    manifest["target_per_day"] = target_per_day
    write_json(manifest_path, refresh_counts(manifest))
    return len(source["articles"])


class RequestLimiter:
    def __init__(self, interval: float):
        if interval < 0:
            raise ValueError("request interval cannot be negative")
        self.interval, self.next_start = interval, 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            delay = self.next_start - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.next_start = time.monotonic() + self.interval


def collection_contract(previous: dict, *, start: date, end: date, target_per_day: int,
                        per_channel_day: int | None, source_sha256: str) -> dict:
    requested = {"start_date": start.isoformat(), "end_date": end.isoformat(),
                 "timezone": "Asia/Shanghai", "target_per_day": target_per_day,
                 "per_channel_day": per_channel_day, "source_manifest_sha256": source_sha256}
    prior = previous.get("collection_contract")
    if prior is None and previous:
        # A seed has not selected its discovery source or collection cap yet.
        # Its target was explicitly chosen while copying the previous version.
        seeded_only = (previous.get("collection_status") in {"seeded", "seeding"}
                       and previous.get("seed_manifest") is not None)
        if seeded_only:
            if previous.get("target_per_day") != target_per_day:
                raise ValueError("collection contract target differs from the frozen seed target")
        else:
            # Compatibility for the already-running v2 collector: infer only
            # from persisted fields, never from this invocation's new values.
            fields = ("start_date", "end_date", "timezone", "target_per_day", "source_manifest_sha256")
            if any(key not in previous for key in fields) or "per_channel_day" not in previous.get("settings", {}):
                raise ValueError("historical collection contract is incomplete; use a new output version")
            prior = {key: previous[key] for key in fields}
            prior["per_channel_day"] = previous["settings"]["per_channel_day"]
    if prior is not None and prior != requested:
        raise ValueError("collection contract changed (window, target, category cap or discovery SHA); use a new output version")
    return requested


def collect(output_dir: Path, discovery: dict, start: date, end: date, *,
            workers: int = 4, timeout: float = 15, per_channel_day: int | None = None,
            target_per_day: int = 1000, request_interval: float = 0.15) -> dict:
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError(f"workers must be an integer from 1 through {MAX_WORKERS}")
    if type(target_per_day) is not int or not 1 <= target_per_day <= MAX_TARGET_PER_DAY:
        raise ValueError(f"target_per_day must be an integer from 1 through {MAX_TARGET_PER_DAY}")
    if per_channel_day is not None and (type(per_channel_day) is not int or not 1 <= per_channel_day <= MAX_TARGET_PER_DAY):
        raise ValueError(f"per_channel_day must be absent or an integer from 1 through {MAX_TARGET_PER_DAY}")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    days = days_between(start, end)
    limiter = RequestLimiter(request_interval)
    recover_pending_articles(output_dir)
    verify_frozen_articles(output_dir)
    manifest_path = output_dir / "manifest.json"
    previous = json.loads(manifest_path.read_bytes()) if manifest_path.exists() else {}
    if not previous and list((output_dir / "articles").glob("*.json")):
        raise ValueError("article cache contains files outside a frozen manifest or pending journal")
    if previous and (previous.get("start_date"), previous.get("end_date")) != (start.isoformat(), end.isoformat()):
        raise ValueError("output version belongs to another publication window")
    source_relative = discovery.get("_source_manifest", "discovery_manifest.json")
    source_raw = (output_dir / source_relative).read_bytes()
    source_digest = sha256(source_raw)
    if source_digest != discovery.get("_source_manifest_sha256", source_digest):
        raise ValueError("external discovery manifest changed before collection")
    contract = collection_contract(previous, start=start, end=end, target_per_day=target_per_day,
                                     per_channel_day=per_channel_day, source_sha256=source_digest)
    candidates = discovery["candidates"]
    articles = [{**row, "cached": True} for row in previous.get("articles", [])]
    manifest = {
        **previous, "schema_version": 1, "generated_at": now_utc(), "start_date": start.isoformat(),
        "end_date": end.isoformat(), "timezone": "Asia/Shanghai", "articles": articles,
        "requested_candidates": len(candidates), "attempted_articles": 0,
        "target_per_day": target_per_day, "collection_status": "collecting",
        "collection_contract": contract,
        "failures": list(previous.get("failures", [])),
        "out_of_window": list(previous.get("out_of_window", [])),
        "duplicate_content": list(previous.get("duplicate_content", [])), "skipped": [],
        "source_manifest": source_relative, "source_manifest_sha256": source_digest,
        "settings": {**discovery.get("settings", {}), "workers": workers, "timeout_seconds": timeout,
                     "target_per_day": target_per_day, "per_channel_day": per_channel_day,
                     "request_interval_seconds": request_interval, "channels": list(ARTICLE_CATEGORIES),
                     "max_article_response_bytes": MAX_HTML_BYTES, "max_feed_response_bytes": MAX_FEED_BYTES,
                     "max_retries": 2},
        "sources": {"feed_endpoint": FEED_URL, "article_host": "news.qq.com",
                    "publication_source": "article HTML metadata; naive timestamps interpreted in Asia/Shanghai",
                    "candidate_dates_are_hints_only": True,
                    "html_hash_encoding": "sha256 of fetched HTML decoded by the existing parser, encoded as UTF-8"},
        "limitations": ["Public candidate discovery is bounded and does not establish exhaustive daily coverage.",
                         "Only accessible standalone text articles with recognized paragraphs and publication metadata are included.",
                         "Frozen public content does not validate enterprise permissions or model quality."],
    }
    quotas = Counter((row["category"], row["publish_time"][:10]) for row in articles)
    counts = Counter(row["publish_time"][:10] for row in articles)
    content_seen = {row["document_sha256"]: row["url"] for row in articles}
    seen_ids = {row["news_id"] for row in articles}
    write_json(manifest_path, refresh_counts(manifest))

    def fetch_one(row):
        try:
            url = normalize_article_url(row["url"])
            identity = news_id_of(url)
            if not ARTICLE_ID.fullmatch(identity) or row.get("news_id", identity) != identity:
                raise ValueError("candidate URL and news_id differ")
            if row.get("category") not in ARTICLE_CATEGORIES:
                raise ValueError("candidate has unsupported category")

            def download():
                limiter.wait()
                return fetch_html(url, timeout)

            html = retry(download)
            article = parse_article(url, html)
            article["publish_time_original"] = article["publish_time"]
            article = validate_article(article, url, start, end)
            article.update({"category": row["category"], "discovery_channels": row.get("channels") or [row["category"]],
                            "feed_publish_time": row.get("feed_publish_time", ""),
                            "candidate_date_hint": row.get("candidate_date", ""),
                            "source_html_sha256": sha256(html.encode("utf-8")),
                            "document_sha256": sha256((article["title"] + "\x1f" + article["content"]).encode("utf-8"))})
            return {"row": row, "article": article}
        except Exception as exc:
            return {"row": row, "reason": f"{type(exc).__name__}: {exc}"}

    # Hints affect ordering only. Every individual quota decision below uses
    # the verified HTML metadata, including mismatched or absent feed dates.
    ordered = sorted(candidates, key=lambda row: (row.get("feed_publish_time") or row.get("candidate_date") or "", row["url"]), reverse=True)
    buckets = {}
    for row in ordered:
        hint = (row.get("feed_publish_time") or row.get("candidate_date") or "")[:10]
        buckets.setdefault(hint, deque()).append(row)

    def next_candidate():
        # Filled hint groups are deferred, never discarded. Once all deficit
        # groups are exhausted, their remaining candidates can still supply a
        # different true HTML publication date, including the previous ID day.
        eligible = [day for day in days if counts[day] < target_per_day and buckets.get(day)]
        if eligible:
            hint = max(eligible, key=lambda day: (target_per_day - counts[day], day))
        else:
            remaining = [hint for hint, queue in buckets.items() if queue]
            if not remaining:
                return None
            hint = max(remaining)
        return buckets[hint].popleft()

    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            while not all(counts[day] >= target_per_day for day in days):
                batch = []
                while len(batch) < workers:
                    row = next_candidate()
                    if row is None:
                        break
                    identity = news_id_of(row["url"])
                    if identity in seen_ids:
                        continue
                    seen_ids.add(identity)
                    batch.append(row)
                if not batch:
                    break
                prepared = []
                for result in executor.map(fetch_one, batch):
                    manifest["attempted_articles"] += 1
                    row = result["row"]
                    if "reason" in result:
                        failure = {"url": row["url"], "category": row.get("category"), "reason": result["reason"]}
                        target = "out_of_window" if "article date outside window" in result["reason"] else "failures"
                        manifest[target].append(failure)
                        continue
                    article = result["article"]
                    day, category = article["publish_time"][:10], article["category"]
                    if counts[day] >= target_per_day:
                        manifest["skipped"].append({"url": article["url"], "reason": "actual publication day quota reached"})
                        continue
                    if per_channel_day is not None and quotas[(category, day)] >= per_channel_day:
                        manifest["skipped"].append({"url": article["url"], "reason": "actual publication channel/day quota reached"})
                        continue
                    document_hash = article["document_sha256"]
                    if document_hash in content_seen:
                        manifest["duplicate_content"].append({"url": article["url"], "same_as": content_seen[document_hash],
                                                              "document_sha256": document_hash})
                        continue
                    content_seen[document_hash] = article["url"]
                    counts[day] += 1
                    quotas[(category, day)] += 1
                    raw = json_bytes(article)
                    entry = {"news_id": article["news_id"], "url": article["url"],
                             "relative_path": f"articles/{article['news_id']}.json", "sha256": sha256(raw),
                             "document_sha256": document_hash, "source_html_sha256": article["source_html_sha256"],
                             "publish_time": article["publish_time"], "category": category,
                             "content_characters": len(article["content"]), "cached": False}
                    prepared.append((entry, raw))
                if (output_dir / source_relative).read_bytes() != source_raw:
                    raise ValueError("discovery source changed during collection")
                if prepared:
                    commit_article_batch(output_dir, manifest, prepared)
                else:
                    write_json(manifest_path, refresh_counts(manifest))
                if batch:
                    print(json.dumps({"phase": "articles", "seen_candidates": len(seen_ids),
                                      "attempted_articles": manifest["attempted_articles"], "success": len(manifest["articles"]),
                                      "failures": len(manifest["failures"]),
                                      "counts_by_date": manifest["counts_by_date"],
                                      "deficits_by_date": manifest["deficits_by_date"]}, ensure_ascii=False), flush=True)
    except BaseException:
        # A journal is authoritative if interruption happened during a commit;
        # never replace its base manifest with partially processed memory state.
        if not (output_dir / "pending_articles.json").exists():
            manifest["collection_status"] = "interrupted"
            write_json(manifest_path, refresh_counts(manifest))
        raise
    refresh_counts(manifest)
    manifest["unattempted_candidates_remaining"] = sum(len(queue) for queue in buckets.values())
    manifest["deferred_candidates_remaining"] = sum(len(queue) for hint, queue in buckets.items()
                                                     if hint in days and counts[hint] >= target_per_day)
    manifest["collection_status"] = "target_met" if manifest["quota_complete"] else "candidate_budget_exhausted"
    write_json(manifest_path, manifest)
    (output_dir / "successful_urls.txt").write_text("".join(row["url"] + "\n" for row in manifest["articles"]), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 10, 7))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 10, 14))
    parser.add_argument("--output-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "data/incremental/2026-10-07_2026-10-14-v2")
    parser.add_argument("--pages-per-channel", type=int, default=200)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--per-channel-day", type=int, default=None,
                        help="Optional compatibility cap per category/day; default has no category cap")
    parser.add_argument("--target-per-day", type=int, default=1000,
                        help="Verified, deduplicated full-body target for every actual publication date")
    parser.add_argument("--request-interval", type=float, default=0.15,
                        help="Minimum seconds between article request starts across all workers")
    parser.add_argument("--seed-manifest", type=Path,
                        help="Verified frozen v1 manifest copied byte-for-byte into this new version")
    parser.add_argument("--seed-manifest-sha256")
    parser.add_argument("--discovery-manifest", type=Path,
                        help="Frozen external candidate manifest; skip getPCList discovery")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--discover-only", action="store_true")
    args = parser.parse_args()
    days_between(args.start_date, args.end_date)
    if (not 1 <= args.workers <= MAX_WORKERS or not 1 <= args.target_per_day <= MAX_TARGET_PER_DAY
            or (args.per_channel_day is not None and not 1 <= args.per_channel_day <= MAX_TARGET_PER_DAY)
            or not 1 <= args.pages_per_channel <= MAX_PAGES_PER_CHANNEL or args.request_interval < 0):
        parser.error("workers must be 1..8, daily targets/caps 1..10000, pages 1..1000, request interval >=0")
    recover_pending_articles(args.output_dir)
    if args.seed_manifest:
        seed_from_manifest(args.output_dir, args.seed_manifest, args.start_date, args.end_date,
                           target_per_day=args.target_per_day, expected_sha256=args.seed_manifest_sha256)
    previous = args.output_dir / "manifest.json"
    if previous.exists():
        old = json.loads(previous.read_bytes())
        if (old.get("start_date"), old.get("end_date"), old.get("timezone")) != (
                args.start_date.isoformat(), args.end_date.isoformat(), "Asia/Shanghai"):
            parser.error("output version belongs to a different publication window")
    verify_frozen_articles(args.output_dir, verify_discovery_reference=True)
    if args.discovery_manifest:
        discovered = load_external_discovery(args.discovery_manifest, args.output_dir, args.start_date, args.end_date)
    else:
        discovered = discover(args.output_dir, args.start_date, args.end_date,
                              pages_per_channel=args.pages_per_channel, page_size=args.page_size, timeout=args.timeout)
    if args.discover_only:
        print(json.dumps({"phase": "discovery_complete", "counts_by_date": discovered["counts_by_date"],
                          "candidates": len(discovered["candidates"]), "feed_failures": len(discovered["failures"])},
                         ensure_ascii=False), flush=True)
        return
    manifest = collect(args.output_dir, discovered, args.start_date, args.end_date,
                       workers=args.workers, timeout=args.timeout, per_channel_day=args.per_channel_day,
                       target_per_day=args.target_per_day, request_interval=args.request_interval)
    print(json.dumps({"phase": "complete", "manifest": str(args.output_dir / "manifest.json"),
                      "success": manifest["success"], "counts_by_date": manifest["counts_by_date"],
                      "missing_dates": manifest["missing_dates"], "deficits_by_date": manifest["deficits_by_date"],
                      "quota_complete": manifest["quota_complete"], "target_per_day": manifest["target_per_day"],
                      "failures": len(manifest["failures"])},
                     ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
