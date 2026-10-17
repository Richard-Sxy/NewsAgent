"""Discover public article-ID hints from an audited Tencent sitemap window.

No article bodies, enterprise settings or dotenv are loaded. Sitemap filenames
and article IDs are discovery hints only; a downstream collector must validate
the article HTML publication metadata before accepting a complete body.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

INDEX_URL = "https://news.qq.com/sitemap/index.xml"
MAX_BYTES = 2 * 1024 * 1024
TZ = ZoneInfo("Asia/Shanghai")
SITEMAP_PATH = re.compile(r"/sitemap/(?:index|sitemap_[0-9]+)\.xml")
FILENAME = re.compile(r"sitemap_[0-9]([0-9]{2})([0-9]{2})([0-9]{2})[0-9]+\.xml")
ARTICLE_PATH = re.compile(r"/rain/a/([0-9]{8}A[A-Za-z0-9]+)")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def encoded(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded(value))
    temporary.replace(path)


def validate_sitemap_url(url: str) -> str:
    if not isinstance(url, str):
        raise ValueError("sitemap URL must be a string")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid sitemap port") from exc
    if (parts.scheme != "https" or parts.hostname != "news.qq.com" or port not in (None, 443)
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or not SITEMAP_PATH.fullmatch(parts.path)):
        raise ValueError("only fixed HTTPS news.qq.com sitemap paths are allowed")
    return "https://news.qq.com" + parts.path


class SitemapRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = validate_sitemap_url(newurl)
        if target != validate_sitemap_url(req.full_url):
            raise ValueError("redirect changed the requested public sitemap")
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_xml(url: str, timeout: float = 15) -> bytes:
    url = validate_sitemap_url(url)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 NewsAgent-Public-Sitemap-Discovery/1.0",
                                    "Accept": "application/xml,text/xml", "Accept-Encoding": "identity"})
    with build_opener(SitemapRedirect()).open(request, timeout=timeout) as response:
        if validate_sitemap_url(response.geturl()) != url:
            raise ValueError("response changed the requested public sitemap")
        raw = response.read(MAX_BYTES + 1)
        if not raw.strip() or len(raw) > MAX_BYTES:
            raise ValueError("sitemap response is empty or exceeds the 2 MiB limit")
        return raw


def fetch_with_retry(url: str, timeout: float) -> bytes:
    for attempt in range(2):
        try:
            return fetch_xml(url, timeout)
        except HTTPError as exc:
            if (exc.code != 429 and exc.code < 500) or attempt:
                raise
        except (URLError, TimeoutError):
            if attempt:
                raise
        time.sleep(0.5)
    raise RuntimeError("bounded sitemap retry exhausted")


def xml_locations(raw: bytes, expected_root: str) -> list[str]:
    if not isinstance(raw, bytes) or not raw.strip() or len(raw) > MAX_BYTES:
        raise ValueError("XML input is empty or exceeds the size limit")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("XML document types and entities are not allowed")
    root = ElementTree.fromstring(raw)
    if root.tag.rsplit("}", 1)[-1] != expected_root:
        raise ValueError(f"expected {expected_root} XML; challenge or HTML responses are rejected")
    return [(node.text or "").strip() for node in root.findall(".//{*}loc") if (node.text or "").strip()]


def cache_relative(url: str) -> str:
    return "raw/" + urlsplit(validate_sitemap_url(url)).path.rsplit("/", 1)[-1]


def check_cache(output: Path) -> dict:
    source_path = output / "source_manifest.json"
    if source_path.exists():
        source = json.loads(source_path.read_bytes())
        if source.get("schema_version") != 1:
            raise ValueError("invalid frozen discovery source manifest")
        for field, expected_path in (("discovery_manifest", "discovery_manifest.json"),
                                     ("cache_manifest", "cache_manifest.json")):
            if source.get(field) != expected_path:
                raise ValueError("frozen discovery source reference changed")
            if digest((output / expected_path).read_bytes()) != source.get(field + "_sha256"):
                raise ValueError(f"frozen discovery {field} SHA-256 differs from its source manifest")
        if digest((output / "candidate_urls.txt").read_bytes()) != source.get("candidate_urls_sha256"):
            raise ValueError("frozen discovery candidate URL SHA-256 differs from its source manifest")
    path = output / "cache_manifest.json"
    if not path.exists():
        return {"schema_version": 1, "records": {}}
    cache = json.loads(path.read_bytes())
    if cache.get("schema_version") != 1 or not isinstance(cache.get("records"), dict):
        raise ValueError("invalid sitemap cache manifest")
    for url, row in cache["records"].items():
        relative = cache_relative(url)
        if row.get("url") != url or row.get("relative_path") != relative:
            raise ValueError("sitemap cache identity differs from its audit")
        raw_path = output / relative
        if raw_path.stat().st_size > MAX_BYTES:
            raise ValueError("cached sitemap exceeds the size limit")
        raw = raw_path.read_bytes()
        if digest(raw) != row.get("sha256") or len(raw) != row.get("bytes"):
            raise ValueError(f"sitemap cache SHA-256 differs from its audit: {url}")
    return cache


def save_cache(output: Path, cache: dict, url: str, raw: bytes) -> dict:
    relative = cache_relative(url)
    row = {"url": url, "relative_path": relative, "sha256": digest(raw), "bytes": len(raw),
           "fetched_at": datetime.now(timezone.utc).isoformat()}
    path = output / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(raw)
    temporary.replace(path)
    cache["records"][url] = row
    write_json(output / "cache_manifest.json", cache)
    return row


def sitemap_date_hints(urls: list[str], reference: date) -> list[dict]:
    """Unroll descending DD/HH/MM filename groups against the index fetch date.

    The public index observed on 2026-10-15 contains 115HHMM..., then
    114HHMM..., down through 101HHMM..., then 130HHMM... . The filename's
    leading namespace digit does not encode a trustworthy month; index order
    and the fetch-date reference disambiguate month rollover. These are only
    scheduling hints, never article publication dates.
    """
    result = []
    cursor = reference
    for url in urls:
        match = FILENAME.fullmatch(url.rsplit("/", 1)[-1])
        if not match:
            result.append({"url": url, "sitemap_date_hint": None, "minute": 0})
            continue
        day, hour, minute = map(int, match.groups())
        if not 1 <= day <= 31 or not 0 <= hour <= 23 or minute not in (0, 30):
            result.append({"url": url, "sitemap_date_hint": None, "minute": 0})
            continue
        candidate = cursor
        for _ in range(32):
            if candidate.day == day:
                break
            candidate -= timedelta(days=1)
        else:
            result.append({"url": url, "sitemap_date_hint": None, "minute": 0})
            continue
        cursor = candidate
        result.append({"url": url, "sitemap_date_hint": cursor.isoformat(), "minute": hour * 60 + minute})
    return result


def article_candidates(raw: bytes, minimum: date, end: date) -> tuple[list[dict], dict]:
    found = {}
    rejected = Counter()
    for url in xml_locations(raw, "urlset"):
        parts = urlsplit(url)
        match = ARTICLE_PATH.fullmatch(parts.path)
        try:
            valid_port = parts.port in (None, 443)
        except ValueError:
            valid_port = False
        if (parts.scheme != "https" or parts.hostname != "news.qq.com" or not valid_port
                or parts.username is not None or parts.password is not None or parts.query or parts.fragment or not match):
            rejected["not_allowlisted_A_article"] += 1
            continue
        news_id = match.group(1)
        try:
            hint = datetime.strptime(news_id[:8], "%Y%m%d").date()
        except ValueError:
            rejected["invalid_id_date_hint"] += 1
            continue
        if not minimum <= hint <= end:
            rejected["id_hint_outside_candidate_window"] += 1
            continue
        normalized = "https://news.qq.com" + parts.path
        found[normalized] = {"news_id": news_id, "url": normalized, "category": "public",
                             "channels": ["public"], "candidate_date": hint.isoformat(),
                             "date_hint_source": "news_id", "feed_publish_time": ""}
    return list(found.values()), dict(rejected)


def discover(output: Path, start: date, end: date, *, max_sitemaps: int = 150,
             workers: int = 4, target_per_day: int = 2000, timeout: float = 15) -> dict:
    if start > end or (end - start).days > 30:
        raise ValueError("date window must contain 1 through 31 days")
    if type(max_sitemaps) is not int or not 1 <= max_sitemaps <= 1000:
        raise ValueError("max_sitemaps must be an integer from 1 through 1000")
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("workers must be an integer from 1 through 8")
    if type(target_per_day) is not int or not 1 <= target_per_day <= 10000 or timeout <= 0:
        raise ValueError("target_per_day must be 1..10000 and timeout must be positive")
    output.mkdir(parents=True, exist_ok=True)
    cache = check_cache(output)
    if INDEX_URL in cache["records"]:
        index_raw = (output / cache_relative(INDEX_URL)).read_bytes()
    else:
        index_raw = fetch_with_retry(INDEX_URL, timeout)
        xml_locations(index_raw, "sitemapindex")
        save_cache(output, cache, INDEX_URL, index_raw)
    locations = list(dict.fromkeys(validate_sitemap_url(url) for url in xml_locations(index_raw, "sitemapindex")))
    if len(locations) > 1000 or INDEX_URL in locations:
        raise ValueError("sitemap index exceeds the 1000 child budget or recursively references itself")
    fetched_date = datetime.fromisoformat(cache["records"][INDEX_URL]["fetched_at"]).astimezone(TZ).date()
    located = sitemap_date_hints(locations, fetched_date)
    requested_days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    minimum = start - timedelta(days=1)
    groups = defaultdict(list)
    for row in located:
        groups[row["sitemap_date_hint"]].append(row)
    # Daytime shards usually contain more standalone text than overnight
    # shards. Round-robin dates prevent a newest-first scan starving history.
    for group in groups.values():
        group.sort(key=lambda row: (abs(row["minute"] - 15 * 60), -row["minute"]))
    candidates, audits, failures, seen_sitemaps = {}, [], [], set()
    duplicate_urls = 0
    counts = Counter()

    def load(row):
        url = row["url"]
        try:
            cached = url in cache["records"]
            raw = ((output / cache_relative(url)).read_bytes() if cached else fetch_with_retry(url, timeout))
            found, rejected = article_candidates(raw, minimum, end)
            return {"row": row, "raw": raw, "found": found, "rejected": rejected, "cached": cached}
        except Exception as exc:
            return {"row": row, "reason": f"{type(exc).__name__}: {exc}"}

    def next_row():
        deficient = sorted((day for day in requested_days if counts[day] < target_per_day),
                           key=lambda day: (counts[day], day))
        if not deficient:
            return None
        for day in deficient:
            if groups[day]:
                return groups[day].pop(0)
        # Filename conventions are only a prioritization hint. If target-day
        # groups exhaust, consume every remaining official child, preferring
        # adjacent/history shards before current-date shards.
        remaining = [key for key, rows in groups.items() if rows]
        remaining.sort(key=lambda key: (key is None, abs((date.fromisoformat(key) - start).days) if key else 999))
        return groups[remaining[0]].pop(0) if remaining else None

    with ThreadPoolExecutor(max_workers=workers) as executor:
        while len(seen_sitemaps) < max_sitemaps:
            batch = []
            while len(batch) < workers and len(seen_sitemaps) + len(batch) < max_sitemaps:
                row = next_row()
                if row is None:
                    break
                batch.append(row)
            if not batch:
                break
            for result in executor.map(load, batch):
                row = result["row"]
                seen_sitemaps.add(row["url"])
                if "reason" in result:
                    failure = {"url": row["url"], "sitemap_date_hint": row["sitemap_date_hint"], "reason": result["reason"]}
                    failures.append(failure)
                    print(json.dumps({"phase": "sitemap", "status": "failed", **failure}), flush=True)
                    continue
                raw = result.pop("raw")
                source = (cache["records"][row["url"]] if result["cached"]
                          else save_cache(output, cache, row["url"], raw))
                added = Counter()
                for article in result["found"]:
                    url = article["url"]
                    if url in candidates:
                        duplicate_urls += 1
                        continue
                    candidates[url] = {**article, "source_sitemap": row["url"]}
                    counts[article["candidate_date"]] += 1
                    added[article["candidate_date"]] += 1
                audits.append({**source, "sitemap_date_hint": row["sitemap_date_hint"], "cached": result["cached"],
                               "eligible_unique_within_sitemap": len(result["found"]),
                               "added_counts_by_id_date_hint": dict(added), "rejected_counts": result["rejected"]})
                print(json.dumps({"phase": "sitemap", "read": len(seen_sitemaps), "cached": result["cached"],
                                  "sitemap_date_hint": row["sitemap_date_hint"], "added": dict(added),
                                  "candidate_counts_by_id_date_hint": {day: counts[day] for day in requested_days}},
                                 ensure_ascii=False), flush=True)
    candidate_rows = sorted(candidates.values(), key=lambda row: (row["candidate_date"], row["news_id"]))
    manifest = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
                "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "Asia/Shanghai",
                "candidate_min_date": minimum.isoformat(), "candidates": candidate_rows,
                "counts_by_date": {day: counts[day] for day in requested_days},
                "all_hint_counts_by_date": dict(sorted(counts.items())),
                "target_met": all(counts[day] >= target_per_day for day in requested_days),
                "deficient_dates": [day for day in requested_days if counts[day] < target_per_day],
                "index_source": cache["records"][INDEX_URL], "raw_sitemaps": audits,
                "duplicate_candidate_occurrences": duplicate_urls, "failures": failures,
                "index_sitemap_count": len(locations), "attempted_sitemaps": len(seen_sitemaps),
                "index_counts_by_filename_date_hint": dict(Counter(row["sitemap_date_hint"] for row in located)),
                "settings": {"max_sitemaps": max_sitemaps, "workers": workers, "target_per_day": target_per_day,
                             "timeout_seconds": timeout, "max_response_bytes": MAX_BYTES, "max_retries": 1},
                "limitations": ["Article IDs and sitemap filenames provide discovery hints, not verified publication dates.",
                                "Every accepted body must independently pass the downstream article HTML publication-time check.",
                                "A IDs exclude obvious V video IDs but do not guarantee readable article paragraphs.",
                                "This is a finite public-sitemap sample, not an exhaustive daily archive."]}
    path = output / "discovery_manifest.json"
    write_json(path, manifest)
    (output / "candidate_urls.txt").write_text("".join(row["url"] + "\n" for row in candidate_rows), encoding="utf-8")
    write_json(output / "source_manifest.json", {
        "schema_version": 1, "discovery_manifest": "discovery_manifest.json", "discovery_manifest_sha256": digest(path.read_bytes()),
        "cache_manifest": "cache_manifest.json", "cache_manifest_sha256": digest((output / "cache_manifest.json").read_bytes()),
        "index_source_sha256": cache["records"][INDEX_URL]["sha256"],
        "candidate_urls_sha256": digest((output / "candidate_urls.txt").read_bytes())})
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 10, 7))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 10, 14))
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[2] / "var/news/data/incremental/2026-10-07_2026-10-14-v2/sitemap-discovery")
    parser.add_argument("--max-sitemaps", type=int, default=150)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--target-per-day", type=int, default=2000)
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    result = discover(args.output_dir, args.start_date, args.end_date, max_sitemaps=args.max_sitemaps,
                      workers=args.workers, target_per_day=args.target_per_day, timeout=args.timeout)
    print(json.dumps({"phase": "complete", "candidates": len(result["candidates"]),
                      "counts_by_date": result["counts_by_date"], "target_met": result["target_met"],
                      "attempted_sitemaps": result["attempted_sitemaps"], "failures": len(result["failures"]),
                      "manifest": str(args.output_dir / "discovery_manifest.json")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
