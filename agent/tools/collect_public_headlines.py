"""Collect public Tencent News title metadata; no enterprise config or article body is saved."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.etree import ElementTree

TZ = timezone(timedelta(hours=8))
INDEX_URL = "https://news.qq.com/sitemap/index.xml"
MAX_BYTES = 3 * 1024 * 1024
ARTICLE_PATH = re.compile(r"/rain/a/(\d{8}[A-Z0-9]+)")
CATEGORY_MAP = {
    "tech": "科技", "technology": "科技", "science": "科技", "digital": "科技",
    "finance": "财经", "fin": "财经", "stock": "财经", "economy": "财经", "money": "财经",
    "sports": "体育", "sport": "体育", "nba": "体育", "football": "体育", "soccer": "体育",
}


def allowed_url(url: str) -> str:
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname != "news.qq.com"
            or parts.username or parts.password or parts.port not in (None, 443)
            or parts.query or parts.fragment):
        raise ValueError("only fixed public news.qq.com HTTPS URLs are allowed")
    if not (ARTICLE_PATH.fullmatch(parts.path) or re.fullmatch(r"/sitemap/(?:index|sitemap_\d+)\.xml", parts.path)):
        raise ValueError("URL is outside the public article/sitemap allowlist")
    return url


class AllowedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed_url(newurl)
        if urlsplit(newurl).path != urlsplit(req.full_url).path:
            raise ValueError("redirect changed the requested public object")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str) -> str:
    allowed_url(url)
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 NewsAgent-Public-Headline-Sample/1.0",
                                    "Accept-Encoding": "identity"})
    with build_opener(AllowedRedirect()).open(request, timeout=12) as response:
        allowed_url(response.geturl())
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("public response exceeds bounded size")
        return raw.decode("utf-8", errors="replace")


class HeadlineParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.title_parts = []
        self.in_h1 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            key = attrs.get("property") or attrs.get("name")
            if key and attrs.get("content"):
                self.meta[key] = attrs["content"]
        if tag == "h1":
            self.in_h1 = True

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_h1 = False

    def handle_data(self, data):
        if self.in_h1:
            self.title_parts.append(data)


def parse_headline(url: str, html: str, target_date: date) -> dict:
    allowed_url(url)
    match = ARTICLE_PATH.fullmatch(urlsplit(url).path)
    if not match:
        raise ValueError("headline must have a public article URL")
    parser = HeadlineParser()
    parser.feed(html)
    title = "".join(parser.title_parts).strip() or parser.meta.get("og:title", "").strip()
    title = re.sub(r"_腾讯新闻$", "", title).strip()
    if not 4 <= len(title) <= 180 or any(ord(c) < 32 for c in title):
        raise ValueError("headline is missing or outside the text budget")
    published = parser.meta.get("article:published_time", "")
    if not published:
        raise ValueError("publication metadata is required; URL dates alone are insufficient")
    timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=TZ)
    timestamp = timestamp.astimezone(TZ)
    if timestamp.date() != target_date:
        raise ValueError("publication time is outside the explicitly requested date")
    news_id = match.group(1)
    source = parser.meta.get("article:author", "").strip() or "腾讯新闻"
    if len(source) > 128 or any(ord(c) < 32 for c in source):
        raise ValueError("source is outside the text budget")
    return {"news_id": news_id, "title": title, "source": source, "url": url,
            "published_at": timestamp.isoformat(),
            "category": CATEGORY_MAP.get(parser.meta.get("category", "").lower(), "社会"),
            "content_type": "video" if news_id[8:9] == "V" else "article"}


def collect(args) -> dict:
    target_date = date.fromisoformat(args.date)
    if args.count not in (120, 1200) or not 1 <= args.workers <= 8 or not 1 <= args.sitemaps <= 20:
        raise ValueError("count, workers or sitemap budget is outside the approved bounds")
    destination = Path(args.output)
    if destination.exists():
        raise ValueError("catalog already exists; choose a new version instead of overwriting")
    index = ElementTree.fromstring(fetch(INDEX_URL))
    sitemap_urls = [node.text for node in index.findall(".//{*}loc") if node.text][:args.sitemaps]
    candidates = []
    sitemap_failures = 0
    for url in sitemap_urls:
        try:
            root = ElementTree.fromstring(fetch(url))
            candidates.extend(node.text for node in root.findall(".//{*}loc")
                              if node.text and f"/rain/a/{target_date:%Y%m%d}" in node.text)
        except (ValueError, OSError, ElementTree.ParseError):
            sitemap_failures += 1
    candidates = list(dict.fromkeys(candidates))
    successes, failures, seen_titles = [], 0, set()

    def load(url):
        try:
            return parse_headline(url, fetch(url), target_date)
        except (ValueError, OSError):
            return None

    # Batches bound outstanding work; each article is fetched once, without retries.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for offset in range(0, len(candidates), 64):
            for row in pool.map(load, candidates[offset:offset + 64]):
                if row is None or row["title"] in seen_titles:
                    failures += 1
                elif len(successes) < args.count:
                    seen_titles.add(row["title"])
                    successes.append(row)
            print(json.dumps({"fetched": min(offset + 64, len(candidates)),
                              "accepted_unique": len(successes), "rejected": failures}), flush=True)
            if len(successes) == args.count:
                break
    if len(successes) != args.count:
        raise ValueError(f"insufficient distinct verified headlines: {len(successes)}/{args.count}")
    catalog = {"catalog_version": "qq-public-headlines-2026-10-07-v1",
               "collected_at": datetime.now(TZ).isoformat(),
               "articles": sorted(successes, key=lambda row: row["news_id"])}
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"output": str(destination), "accepted_unique": len(successes),
            "public_date": args.date, "candidate_count": len(candidates),
            "sitemap_failures": sitemap_failures, "rejected": failures}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--count", type=int, default=1200)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--sitemaps", type=int, default=12)
    print(json.dumps(collect(parser.parse_args()), ensure_ascii=False))


if __name__ == "__main__":
    main()
