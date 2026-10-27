"""Discover bounded article links from explicitly selected annual topic pages.

Links and topic membership are discovery hints, never relevance judgments.
Article publication dates and bodies are verified by collect_annual_event_news.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from hashlib import sha256
import argparse
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

from bs4 import BeautifulSoup

HOSTS = ("news.cn", "xinhuanet.com", "olympic.cn")
ARTICLE = re.compile(r"(?:/20\d{6}/[^/]+/(?:c|content)\.html?|/20\d{2}/\d{4}/\d+\.html?)$")


def safe_url(value):
    p = urlsplit(value)
    host = (p.hostname or "").lower()
    if p.scheme not in ("http", "https") or p.username or p.password or p.port not in (None, 80, 443):
        raise ValueError("unsafe topic URL")
    if not any(host == item or host.endswith("." + item) for item in HOSTS):
        raise ValueError("topic host is not reviewed")
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


class Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, safe_url(newurl))


def article_links(html, page_url, *, start, end, limit):
    found = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        try:
            url = safe_url(urljoin(page_url, a["href"]))
        except ValueError:
            continue
        if not ARTICLE.search(urlsplit(url).path):
            continue
        title = a.get_text(" ", strip=True) or a.get("title", "").strip()
        if len(title) < 4 or title in ("查看详情", "更多报道", "点击进入"):
            continue
        match = re.search(r"/(20\d{6})(?:/|$)", urlsplit(url).path)
        hint = None
        if match:
            raw = match.group(1)
            try:
                hint = date(int(raw[:4]), int(raw[4:6]), int(raw[6:8])).isoformat()
            except ValueError:
                continue
            if not start <= hint <= end:
                continue
        # This remains a URL hint and must never become verified publication time.
        found.setdefault(url, {"url": url, "title": title, "published_at": None,
            "date_hint_from_url": hint, "source": urlsplit(url).hostname,
            "discovery_kind": "topic_page_link", "discovered_from": page_url,
            "role_hint": "unknown"})
    return list(found.values())[:limit]


def discover(seed, *, cache, start, end, limit, timeout):
    url = safe_url(seed["url"])
    key = sha256(url.encode()).hexdigest()
    path = cache / (key + ".html")
    if path.exists():
        raw = path.read_bytes()
    else:
        req = Request(url, headers={"User-Agent": "NewsAgent-public-research/1.0"})
        with build_opener(Redirects()).open(req, timeout=timeout) as response:
            raw = response.read(3_000_001)
            if len(raw) > 3_000_000:
                raise ValueError("topic page exceeds byte budget")
        path.write_bytes(raw)
    seed_limit = seed.get("limit", limit)
    if type(seed_limit) is not int or not 1 <= seed_limit <= 300:
        raise ValueError("invalid seed article limit")
    articles = article_links(raw.decode("utf-8", errors="replace"), url,
        start=start, end=end, limit=min(seed_limit, limit))
    return {"event_id": seed["event_id"], "category": seed["category"], "title": seed["title"],
        "event_date": None, "keywords": seed.get("keywords", []), "articles": articles,
        "discovery_group_type": "topic_collection", "source_page": url,
        "source_page_sha256": sha256(raw).hexdigest(),
        "notes": "Topic-page association is unreviewed discovery provenance, not a fact group or human relevance label."}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seeds", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--cache", type=Path, required=True)
    p.add_argument("--start-date", default="2025-10-27")
    p.add_argument("--end-date", default="2026-10-27")
    p.add_argument("--limit-per-topic", type=int, default=100)
    p.add_argument("--timeout", type=float, default=15)
    args = p.parse_args()
    if not 1 <= args.limit_per_topic <= 300 or not 0 < args.timeout <= 60:
        p.error("invalid discovery budget")
    start, end = date.fromisoformat(args.start_date).isoformat(), date.fromisoformat(args.end_date).isoformat()
    if start > end:
        p.error("invalid date range")
    seeds = json.loads(args.seeds.read_text(encoding="utf-8"))["topic_pages"]
    if not 1 <= len(seeds) <= 100:
        p.error("topic seed count exceeds budget")
    args.cache.mkdir(parents=True, exist_ok=True)
    def one(seed):
        try:
            return discover(seed, cache=args.cache, start=start, end=end,
                limit=args.limit_per_topic, timeout=args.timeout), None
        except Exception as exc:
            return None, {"event_id": seed["event_id"], "url": seed["url"], "error_type": type(exc).__name__}
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, seeds))
    events = [event for event, error in results if event]
    errors = [error for event, error in results if error]
    value = {"schema_version": 1, "scope": {"start_date": start, "end_date": end,
        "categories": sorted({e["category"] for e in events})},
        "generated_at": datetime.now(timezone.utc).isoformat(), "events": events, "errors": errors,
        "limitations": ["Topic collections are not counts of verified independent event threads.",
            "Discovered links are not downloaded bodies; URL date hints are not verified publication dates.",
            "All relevance labels remain pending human judgment."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"topic_collections": len(events), "unique_urls": len({a["url"] for e in events for a in e["articles"]}),
        "errors": errors, "output": str(args.output)}, ensure_ascii=False))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
