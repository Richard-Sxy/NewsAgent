"""Freeze public event articles for manual retrieval review (Python 3.8+).

Only audited public HTTP(S) GETs are performed. No enterprise settings,
database writes, models, relevance judgments or embeddings are involved.
Use: PYTHONPATH=news python -m experiments.collect_annual_event_news
     --manifest events.json --output-dir news/data/annual-v1
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import socket
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from itertools import islice
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ALLOWED_DOMAINS = frozenset((
    "news.cn", "xinhuanet.com", "chinanews.com.cn", "chinanews.com",
    "people.com.cn", "cctv.com", "gov.cn", "guowuyuan.gov.cn",
    "anthropic.com", "apple.com", "nvidia.com", "samsung.com", "sony.com",
    "nintendo.com", "olympics.com", "olympic.cn", "fifa.com", "uefa.com",
    "thegameawards.com", "ces.tech", "federalreserve.gov", "pbc.gov.cn",
    "stats.gov.cn", "mod.gov.cn",
    "ithome.com", "nbd.com.cn", "thepaper.cn", "bjnews.com.cn",
    "apple.com.cn", "tencent.com", "hesaitech.com", "gamersky.com",
    "17173.com", "pconline.com.cn", "sznews.com", "yna.co.kr", "korea.net",
    "cctv.cn", "cnfin.com", "sse.com.cn", "cs.com.cn", "stcn.com",
    "yicai.com", "nvidia.cn", "gov.hk", "rthk.hk",
))
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_WORKERS = 8
PARSER_VERSION = 1
NOISE_SELECTORS = (
    "script,style,noscript,nav,aside,footer,header,form,button,svg,template,iframe,"
    "[class*='recommend'],[id*='recommend'],[class*='related'],[id*='related'],"
    "[class*='share'],[class*='copyright'],[class*='breadcrumb'],"
    "[class*='advert'],[class*='sidebar'],[class*='cookie'],[data-no-read]"
)
BODY_SELECTORS = (
    "#article-content", ".article-content", ".content-article", ".article-body",
    ".article_body", ".articleBody", "[itemprop='articleBody']", ".main-aticle",
    ".detail-content", ".content-detail", "#text_area", ".text_area",
    "#content_area", ".content_area", ".cnt_bd", ".left_zw", "#rwb_zw",
    ".rm_txt_con", ".show_text", ".showtext", ".box_con", "#ozoom",
    ".nr-article", ".pagebody-copy", ".rich_media_content", ".content_desc",
    "#cont_1_1_2", "#detail", "#content", "#zoom", "article", "main",
)
PUBLISH_KEYS = frozenset((
    "article:published_time", "og:published_time", "pubdate", "publishdate",
    "publish_date", "publish_time", "pubtime", "apub:time", "datepublished",
    "dcterms.created", "dc.date.issued", "sailthru.date", "date", "creationdate",
))
DATE_PATTERN = re.compile(
    r"(?P<y>20\d{2})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})日?"
    r"(?:[T\s]+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2})(?:\.\d+)?)?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?)?"
)


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError("linked cache files are forbidden")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def normalize_url(raw):
    """Validate a public source and discard unknown query values/credentials."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise ValueError("invalid URL")
    parts = urlsplit(raw.strip())
    host = (parts.hostname or "").lower().rstrip(".")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("invalid URL port")
    if parts.scheme.lower() not in ("http", "https") or parts.username is not None or parts.password is not None:
        raise ValueError("only HTTP(S) URLs without user information are accepted")
    if port not in (None, 80 if parts.scheme.lower() == "http" else 443):
        raise ValueError("nonstandard port is forbidden")
    if not host or "\\" in raw or any(ord(c) < 32 for c in raw):
        raise ValueError("invalid URL hostname or control character")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are forbidden")
    if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS):
        raise ValueError("hostname is outside the audited source allowlist")
    safe_query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=False):
        if key.lower() in ("id", "cid", "newsid", "articleid", "article_id", "contentid") and re.fullmatch(r"[0-9]{1,24}", value):
            safe_query.append((key.lower(), value))
    return urlunsplit((parts.scheme.lower(), host, parts.path or "/", urlencode(sorted(safe_query)), ""))


def article_id_of(url):
    return sha256(normalize_url(url).encode("utf-8"))[:24]


def require_public_dns(url):
    host = urlsplit(normalize_url(url)).hostname
    addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("source DNS must resolve only to public addresses")


class SafeRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = normalize_url(urljoin(req.full_url, newurl))
        require_public_dns(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch_html(url, timeout=18):
    """Bounded GET with at most one retry; parsing never changes raw bytes."""
    url = normalize_url(url)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    require_public_dns(url)
    for attempt in range(2):
        try:
            request = Request(url, method="GET", headers={
                "User-Agent": "Mozilla/5.0 NewsAgent-Public-Event-Corpus/1.0",
                "Accept": "text/html,application/xhtml+xml", "Accept-Encoding": "identity",
            })
            with build_opener(ProxyHandler({}), SafeRedirectHandler()).open(request, timeout=timeout) as response:
                final_url = normalize_url(response.geturl())
                if response.headers.get_content_type() not in ("text/html", "application/xhtml+xml", "text/plain"):
                    raise ValueError("source did not return HTML")
                raw = response.read(MAX_HTML_BYTES + 1)
                if not raw.strip() or len(raw) > MAX_HTML_BYTES:
                    raise ValueError("source body is empty or exceeds 4 MiB")
                return {"raw": raw, "final_url": final_url,
                        "charset": response.headers.get_content_charset(), "fetched_at": now_utc()}
        except HTTPError as exc:
            if attempt or (exc.code != 429 and exc.code < 500):
                raise
        except (URLError, TimeoutError, socket.timeout):
            if attempt:
                raise
        time.sleep(0.35)
    raise RuntimeError("bounded fetch retries exhausted")


def normalize_space(value):
    return re.sub(r"\s+", " ", value).strip()


def decode_html(raw, charset=None):
    head = raw[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", head, re.I)
    for candidate in (charset, match.group(1) if match else None, "utf-8", "gb18030"):
        if candidate:
            try:
                return raw.decode(candidate)
            except (LookupError, UnicodeDecodeError):
                pass
    return raw.decode("utf-8", errors="replace")


def parse_date(value):
    """Do not invent midnight, source timezone, or a date from an article URL."""
    if not isinstance(value, str):
        return None
    match = DATE_PATTERN.search(value.strip())
    if not match:
        return None
    item = match.groupdict()
    try:
        day = date(int(item["y"]), int(item["m"]), int(item["d"]))
        if item["h"] is None:
            return {"published_at": day.isoformat(), "publication_date": day.isoformat(),
                    "date_precision": "day", "source_timezone": None}
        stamp = "%sT%02d:%02d:%02d" % (day.isoformat(), int(item["h"]), int(item["mi"]), int(item["s"] or 0))
        offset = item["tz"] or ""
        if offset == "Z":
            offset = "+00:00"
        elif offset and ":" not in offset:
            offset = offset[:3] + ":" + offset[3:]
        datetime.fromisoformat(stamp + offset)
        return {"published_at": stamp + offset, "publication_date": day.isoformat(),
                "date_precision": "second" if item["s"] else "minute", "source_timezone": offset or None}
    except ValueError:
        return None


def jsonld_objects(soup):
    for node in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            stack = [json.loads(node.string or node.get_text())]
        except (ValueError, TypeError):
            continue
        while stack:
            current = stack.pop()
            if isinstance(current, list):
                stack.extend(current)
            elif isinstance(current, dict):
                yield current
                stack.extend(v for v in current.values() if isinstance(v, (dict, list)))


def page_publication(soup, objects):
    for node in soup.find_all("meta"):
        key = (node.get("property") or node.get("name") or node.get("itemprop") or "").lower()
        if key in PUBLISH_KEYS:
            parsed = parse_date(node.get("content", ""))
            if parsed:
                return dict(parsed, date_basis="meta:" + key, date_evidence=node.get("content", "")[:250])
    for obj in objects:
        parsed = parse_date(obj.get("datePublished"))
        if parsed:
            return dict(parsed, date_basis="jsonld:datePublished", date_evidence=str(obj["datePublished"])[:250])
    title = soup.find("h1")
    neighborhood = set()
    if title is not None:
        neighborhood = {id(node) for node in islice(title.next_elements, 100)}
        neighborhood.update(id(node) for node in islice(title.previous_elements, 35))
    for node in soup.find_all(True):
        signature = str(node.get("id", "")) + " " + " ".join(node.get("class", []))
        explicit = str(node.get("itemprop", "")).lower() == "datepublished"
        nearby = id(node) in neighborhood
        candidate = explicit or (nearby and (
            re.search(r"(?:time|date|info|source)", signature, re.I) or node.name == "time"))
        if not candidate:
            continue
        text = normalize_space(node.get_text(" ", strip=True))
        visible = nearby and len(text) < 300
        if not (explicit or visible) or re.search(r"(?:更新|修改|modified|updated)", signature + " " + text, re.I):
            continue
        parsed = parse_date(node.get("datetime", "") if explicit or node.name == "time" else text)
        if parsed:
            return dict(parsed, date_basis="page:datePublished" if explicit else "page:near_title", date_evidence=text[:250])
    return {"published_at": None, "publication_date": None, "date_precision": None,
            "source_timezone": None, "date_basis": "missing", "date_evidence": None}


def parse_article(url, html, fetched_at=None):
    from bs4 import BeautifulSoup
    url = normalize_url(url)
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    for node in (soup.find("h1"), soup.find("meta", attrs={"property": "og:title"}), soup.title):
        if node:
            title = normalize_space(node.get("content", "") if node.name == "meta" else node.get_text(" ", strip=True))
            if title:
                break
    objects = list(jsonld_objects(soup))
    publication = page_publication(soup, objects)
    for node in soup.select(NOISE_SELECTORS):
        if node.parent is not None:
            node.decompose()
    body, basis = "", "missing"
    for selector in BODY_SELECTORS:
        candidates = []
        for container in soup.select(selector):
            paragraphs = []
            for node in container.find_all(["p", "h2", "h3", "li"]):
                if node.find(["p", "li"]) is not None:
                    continue
                text = normalize_space(node.get_text(" ", strip=True))
                if len(text) < 8 or re.match(r"^(?:责任编辑|编辑|校对|审核|图片来源|摄影)[：:\s]", text):
                    continue
                if not paragraphs or text != paragraphs[-1]:
                    paragraphs.append(text)
            candidate = "\n".join(paragraphs)
            if candidate:
                candidates.append(candidate)
        if candidates:
            candidate = max(candidates, key=len)
            if len(candidate) >= 200:
                body, basis = candidate, "dom:" + selector
                break
            if len(candidate) > len(body):
                body, basis = candidate, "dom:" + selector
    if len(body) < 200:
        for obj in objects:
            if isinstance(obj.get("articleBody"), str) and len(obj["articleBody"]) > len(body):
                body, basis = obj["articleBody"].strip(), "jsonld:articleBody"
    quality = []
    if len(title) < 4:
        quality.append("title_missing_or_short")
    if len(body) < 200:
        quality.append("body_too_short")
    if body and normalize_space(body) == normalize_space(title):
        quality.append("title_only")
    if body and len(re.sub(r"[^\w\u3400-\u9fff]", "", body)) / max(len(body), 1) < 0.35:
        quality.append("body_low_text_density")
    return dict({"schema_version": 1, "parser_version": PARSER_VERSION,
                 "url": url, "article_id": article_id_of(url), "title": title, "body": body,
                 "body_basis": basis, "body_sha256": sha256(body.encode("utf-8")),
                 "fetched_at": fetched_at or now_utc(), "quality_flags": quality,
                 "relevance_label": None, "relevance_review_status": "pending_human"}, **publication)


def merge_manifests(paths):
    merged, rejected, sources = {}, [], []
    for path in paths:
        raw = Path(path).read_bytes()
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ValueError("manifest must be an object containing events")
        sources.append({"path": str(path), "sha256": sha256(raw), "scope": data.get("scope", {}), "events": len(data["events"])})
        for event in data["events"]:
            if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
                raise ValueError("every event needs an event_id")
            event_id, category = event["event_id"], str(event.get("category", "unknown"))
            for article in event.get("articles", []):
                if not isinstance(article, dict):
                    raise ValueError("event articles must be objects")
                try:
                    url = normalize_url(article.get("url"))
                except (ValueError, TypeError):
                    rejected.append({"event_id": event_id, "category": category,
                                     "reason": "unsafe_or_unsupported_url",
                                     "url_sha256": sha256(str(article.get("url", "")).encode("utf-8"))})
                    continue
                row = merged.setdefault(url, {"url": url, "event_ids": [], "categories": [], "discovery_hints": []})
                if event_id not in row["event_ids"]:
                    row["event_ids"].append(event_id)
                if category not in row["categories"]:
                    row["categories"].append(category)
                hint = {key: article.get(key) for key in ("title", "published_at", "source", "discovery_kind", "role_hint")}
                hint.update({"event_id": event_id, "category": category, "verified": False, "role_hint_is_gold": False})
                if hint not in row["discovery_hints"]:
                    row["discovery_hints"].append(hint)
    return list(merged.values()), rejected, sources


def article_status(article, start, end):
    if not article.get("publication_date"):
        return "date_missing"
    day = date.fromisoformat(article["publication_date"])
    if not start <= day <= end:
        return "date_rejected"
    return "metadata_only" if article.get("quality_flags") else "body_ready"


def load_cached(output_dir, url):
    identity = article_id_of(url)
    path = output_dir / "articles" / (identity + ".json")
    raw_path = output_dir / "raw" / (identity + ".html")
    if not path.exists():
        return None
    if path.is_symlink() or raw_path.is_symlink():
        raise ValueError("linked cache files are forbidden")
    article = json.loads(path.read_text(encoding="utf-8"))
    if article.get("url") != url or article.get("article_id") != identity:
        raise ValueError("cached article identity mismatch")
    if not raw_path.exists() or raw_path.stat().st_size > MAX_HTML_BYTES or sha256(raw_path.read_bytes()) != article.get("raw_sha256"):
        raise ValueError("raw HTML SHA-256 mismatch")
    if sha256(article.get("body", "").encode("utf-8")) != article.get("body_sha256"):
        raise ValueError("parsed body SHA-256 mismatch")
    if article.get("parser_version") != PARSER_VERSION:
        raise ValueError("cached parser version differs; use a new output directory")
    verified = parse_article(url, decode_html(raw_path.read_bytes(), article.get("source_charset")))
    for field in ("title", "body", "published_at", "publication_date", "date_precision",
                  "source_timezone", "date_basis", "date_evidence", "quality_flags"):
        if article.get(field) != verified.get(field):
            raise ValueError("cached parsed metadata differs from frozen raw HTML")
    return article


def safe_error(exc):
    if isinstance(exc, HTTPError):
        return "HTTP_%d" % exc.code
    if isinstance(exc, (URLError, TimeoutError, socket.timeout)):
        return "network_or_timeout:" + type(exc).__name__
    known = ("cached article identity mismatch", "raw HTML SHA-256 mismatch",
             "parsed body SHA-256 mismatch", "linked cache files are forbidden")
    return str(exc) if str(exc) in known else type(exc).__name__


def collect_one(row, output_dir, start, end, timeout):
    url = row["url"]
    identity = article_id_of(url)
    try:
        article = load_cached(output_dir, url)
        cached = article is not None
        if article is None:
            fetched = fetch_html(url, timeout)
            article = parse_article(url, decode_html(fetched["raw"], fetched.get("charset")), fetched["fetched_at"])
            raw_path = output_dir / "raw" / (identity + ".html")
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            if raw_path.is_symlink():
                raise ValueError("linked cache files are forbidden")
            raw_path.write_bytes(fetched["raw"])
            article.update({"raw_sha256": sha256(fetched["raw"]), "raw_bytes": len(fetched["raw"]),
                            "raw_path": "raw/" + identity + ".html", "final_url": fetched["final_url"],
                            "source_charset": fetched.get("charset")})
        article.update(row)
        article["status"] = article_status(article, start, end)
        article["relevance_label"] = None
        article["relevance_review_status"] = "pending_human"
        parsed_path = output_dir / "articles" / (identity + ".json")
        write_json(parsed_path, article)
        return {"url": url, "article_id": identity, "status": article["status"], "cached": cached,
                "event_ids": row["event_ids"], "categories": row["categories"], "raw_bytes": article["raw_bytes"],
                "body_chars": len(article["body"]), "published_at": article["published_at"],
                "date_precision": article["date_precision"], "date_basis": article["date_basis"],
                "relative_path": "articles/" + identity + ".json", "parsed_sha256": sha256(parsed_path.read_bytes()),
                "raw_sha256": article["raw_sha256"],
                "body_sha256": article["body_sha256"], "quality_flags": article["quality_flags"]}
    except Exception as exc:
        return {"url": url, "article_id": identity, "status": "failed", "error": safe_error(exc),
                "event_ids": row["event_ids"], "categories": row["categories"]}


def collect(manifests, output_dir, start, end, workers=4, timeout=18, max_articles=1000, discover_links=False):
    if type(workers) is not int or not 1 <= workers <= MAX_WORKERS:
        raise ValueError("workers must be from 1 to 8")
    if type(max_articles) is not int or not 1 <= max_articles <= 10000:
        raise ValueError("max_articles must be from 1 to 10000")
    if timeout <= 0 or timeout > 120 or start > end:
        raise ValueError("invalid timeout or date window")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    for directory in (output_dir, output_dir / "raw", output_dir / "articles"):
        if directory.is_symlink():
            raise ValueError("linked cache directories are forbidden")
    rows, rejected, sources = merge_manifests(manifests)
    selected = rows[:max_articles]
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(lambda row: collect_one(row, output_dir, start, end, timeout), selected):
            results.append(result)
            write_json(output_dir / "progress.json", {"schema_version": 1, "updated_at": now_utc(),
                       "completed": len(results), "selected": len(selected), "articles": results})
    counts = Counter(row["status"] for row in results)
    categories, events = {}, {}
    for result in results:
        for field, table in (("categories", categories), ("event_ids", events)):
            for key in result[field]:
                table.setdefault(key, Counter())[result["status"]] += 1
    report = {"schema_version": 1, "parser_version": PARSER_VERSION, "created_at": now_utc(),
              "scope": {"start_date": start.isoformat(), "end_date": end.isoformat()}, "sources": sources,
              "settings": {"workers": workers, "timeout": timeout, "max_articles": max_articles, "discover_links": discover_links},
              "discovery_note": "Only supplied audited URLs were fetched; recursive discovery is not implemented.",
              "unique_candidates": len(rows), "attempted": len(results), "body_ready": counts["body_ready"],
              "metadata_only": counts["metadata_only"], "date_missing": counts["date_missing"],
              "date_rejected": counts["date_rejected"], "failed": counts["failed"],
              "cached": sum(bool(row.get("cached")) for row in results),
              "raw_bytes": sum(row.get("raw_bytes", 0) for row in results), "category_coverage": categories,
              "event_coverage": events, "rejected_urls": rejected, "articles": results,
              "relevance_review_status": "pending_human", "accuracy": None}
    write_json(output_dir / "report.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=18)
    parser.add_argument("--max-articles", type=int, default=1000)
    parser.add_argument("--discover-links", action="store_true", help="Reserved; only manifest URLs are collected in v1")
    parser.add_argument("--start-date", type=date.fromisoformat, default=date(2025, 10, 27))
    parser.add_argument("--end-date", type=date.fromisoformat, default=date(2026, 10, 27))
    args = parser.parse_args(argv)
    report = collect(args.manifest, args.output_dir, args.start_date, args.end_date,
                     args.workers, args.timeout, args.max_articles, args.discover_links)
    print(json.dumps({key: report[key] for key in ("attempted", "body_ready", "metadata_only",
                                                 "date_missing", "date_rejected", "failed", "cached")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

