"""Freeze public Tencent News pages for a local retrieval benchmark.

Example (no enterprise configuration is read)::

    python -m experiments.collect_recall_articles \
        --questions tests/retrieval_questions.json tests/retrieval_questions_semantic.json \
        --url-file data/public_urls.txt --distractors 500 --seed 42 \
        --output-dir data/experiments/recall/articles

All labeled URLs are requested. Distractors are sampled without replacement
from the supplied URL file after excluding labeled URLs. Valid cached articles
are reused, so rerunning does not silently replace the frozen corpus.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_HTML_BYTES = 2 * 1024 * 1024
ARTICLE_PATH = re.compile(r"/rain/a/([A-Za-z0-9_-]+)/?")
NO_READ = re.compile(r"<!--\s*NO_READ_(?:BEGIN|START)\s*-->.*?<!--\s*NO_READ_END\s*-->",
                     re.IGNORECASE | re.DOTALL)
CREDIT = re.compile(
    r"^(?:作者|编辑|责编|责任编辑|记者|撰文|文|校对|审核|图源|图片来源|图片|摄影|摄图|供图)"
    r"[：:]\s*.{0,100}$"
)
IMAGE_CREDIT = re.compile(r"^(?:图源|图片来源|图片|摄影|摄图|供图)\s+\S.{0,100}$")


def normalize_article_url(url: str) -> str:
    """Validate the public article host/path and remove query/fragment."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("article URL must be a nonempty string")
    parts = urlsplit(url.strip())
    try:
        valid_port = parts.port in (None, 443)
    except ValueError as exc:
        raise ValueError("invalid article URL port") from exc
    if (parts.scheme != "https" or parts.hostname != "news.qq.com"
            or parts.username is not None or parts.password is not None or not valid_port):
        raise ValueError("only HTTPS news.qq.com article URLs are allowed")
    match = ARTICLE_PATH.fullmatch(parts.path)
    if not match:
        raise ValueError("article URL must have path /rain/a/<news_id>")
    return urlunsplit(("https", "news.qq.com", f"/rain/a/{match.group(1)}", "", ""))


def news_id_of(url: str) -> str:
    return normalize_article_url(url).rsplit("/", 1)[-1]


class PublicArticleRedirectHandler(HTTPRedirectHandler):
    """Reject redirects outside the public article allowlist before following."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed = normalize_article_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, allowed)


def fetch_html(url: str, timeout: float = 15) -> str:
    url = normalize_article_url(url)
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    request = Request(url, headers={
        "User-Agent": "Mozilla/5.0 NewsAgent-Public-Recall-Experiment/1.0",
        "Accept": "text/html",
        "Accept-Encoding": "identity",
    })
    opener = build_opener(PublicArticleRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        # Check again in case a custom opener or an unusual response bypassed
        # redirect handling; never read an external host's response body.
        final_url = normalize_article_url(response.geturl())
        if news_id_of(final_url) != news_id_of(url):
            raise ValueError("redirect changed the requested article news_id")
        raw = response.read(MAX_HTML_BYTES + 1)
        if len(raw) > MAX_HTML_BYTES:
            raise ValueError("article response exceeds the 2 MiB limit")
        if not raw.strip():
            raise ValueError("article response body is empty")
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, errors="replace")
        except LookupError as exc:
            raise ValueError("article response has an unsupported charset") from exc


def parse_article(url: str, html: str, fetched_at: str | None = None) -> dict:
    """Extract paragraphs only from a recognized article container."""
    url = normalize_article_url(url)
    if not isinstance(html, str) or not html.strip():
        raise ValueError("article HTML is empty")
    from bs4 import BeautifulSoup  # Lazy: CLI help and source planning need no bs4.

    soup = BeautifulSoup(NO_READ.sub("", html), "html.parser")
    title = ""
    for title_element in (soup.select_one("h1"), soup.title):
        if title_element:
            title = title_element.get_text(" ", strip=True)
            if title:
                break
    title = re.sub(r"[_|\-]\s*腾讯新闻\s*$", "", title).strip()
    if not title:
        raise ValueError("article title is missing")
    container = next((node for selector in ("#article-content", ".article-content", ".content-article", "article")
                      if (node := soup.select_one(selector)) is not None), None)
    if container is None:
        raise ValueError("recognized article body container is missing")
    noise_selectors = ("script, style, noscript, nav, aside, footer, .no-read, .NO_READ, [data-no-read], "
                       "[class*='recommend'], [id*='recommend'], .related-news, .related-articles")
    for node in container.select(noise_selectors):
        if node.parent is not None:
            node.decompose()
    paragraphs: list[str] = []
    for paragraph in container.find_all("p"):
        text = re.sub(r"\s+", " ", paragraph.get_text(" ", strip=True)).strip()
        if (not text or text == "举报" or CREDIT.fullmatch(text)
                or IMAGE_CREDIT.fullmatch(text)):
            continue
        paragraphs.append(text)
    content = "\n".join(paragraphs)
    if not content:
        raise ValueError("article body contains no readable paragraphs")
    publish_time = ""
    for selector in ('meta[property="article:published_time"]',
                     'meta[name="apub:time"]', 'meta[name="publish_time"]'):
        meta = soup.select_one(selector)
        if meta and isinstance(meta.get("content"), str) and meta["content"].strip():
            publish_time = meta["content"].strip()
            break
    return {
        "url": url,
        "news_id": news_id_of(url),
        "title": title,
        "content": content,
        "publish_time": publish_time,
        "content_version": 1,
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
    }


def valid_cached_article(article: object, url: str) -> bool:
    if not isinstance(article, dict):
        return False
    try:
        normalized = normalize_article_url(url)
        if article.get("url") != normalized or article.get("news_id") != news_id_of(normalized):
            return False
        if (type(article.get("content_version")) is not int or article["content_version"] != 1
                or not isinstance(article.get("publish_time"), str)):
            return False
        if any(not isinstance(article.get(field), str) or not article[field].strip()
               for field in ("title", "content", "fetched_at")):
            return False
        datetime.fromisoformat(article["fetched_at"])
    except (ValueError, TypeError):
        return False
    return True


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_urls(questions_paths: list[Path], url_file: Path | None,
              distractors: int, seed: int) -> tuple[list[str], dict, list[dict]]:
    if isinstance(distractors, bool) or not isinstance(distractors, int) or distractors < 0:
        raise ValueError("distractors must be a nonnegative integer")
    targets: set[str] = set()
    candidates: set[str] = set()
    rejected: list[dict] = []
    sources: dict = {"questions": [], "url_file": None}

    def add(raw: str, destination: set[str], source: str):
        try:
            destination.add(normalize_article_url(raw))
        except (ValueError, TypeError) as exc:
            rejected.append({"url": raw, "source": source, "reason": str(exc)})

    for path in questions_paths:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("question files must contain a JSON list")
        for question in data:
            if not isinstance(question, dict):
                raise ValueError("questions must be JSON objects")
            labels = question.get("expected_urls") or []
            if not isinstance(labels, list):
                raise ValueError("expected_urls must be a list")
            for raw in labels:
                add(raw, targets, str(path))
        sources["questions"].append({"path": str(path), "sha256": _file_hash(path),
                                      "questions": len(data)})
    if url_file:
        for line in url_file.read_text(encoding="utf-8").splitlines():
            raw = line.strip()
            if raw and not raw.startswith("#"):
                add(raw, candidates, str(url_file))
        sources["url_file"] = {"path": str(url_file), "sha256": _file_hash(url_file),
                               "valid_unique_urls": len(candidates)}
    eligible = sorted(candidates - targets)
    if distractors > len(eligible):
        raise ValueError(f"requested {distractors} distractors but only {len(eligible)} are available")
    sampled = random.Random(seed).sample(eligible, distractors)
    sources.update({"target_urls": len(targets), "eligible_distractors": len(eligible),
                    "sampled_distractors": len(sampled)})
    return sorted(targets | set(sampled)), sources, rejected


def collect_articles(urls: list[str], output_dir: Path, *, workers: int = 4,
                     timeout: float = 15, seed: int = 42, sources: dict | None = None,
                     rejected_sources: list[dict] | None = None) -> dict:
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError("workers must be an integer from 1 to 4")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    # Normalize before constructing any path or starting network operations.
    urls = sorted({normalize_article_url(url) for url in urls})
    output_dir.mkdir(parents=True, exist_ok=True)

    def collect_one(url: str) -> dict:
        path = output_dir / f"{news_id_of(url)}.json"
        try:
            if path.exists():
                try:
                    cached = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    cached = None
                if valid_cached_article(cached, url):
                    return {"url": url, "article": cached, "cached": True}
            article = parse_article(url, fetch_html(url, timeout))
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            return {"url": url, "article": article, "cached": False}
        except Exception as exc:
            return {"url": url, "reason": f"{type(exc).__name__}: {exc}"}

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(collect_one, urls))
    successful = [row for row in results if "article" in row]
    failures = [row for row in results if "reason" in row]
    corpus = [row["article"] for row in successful]
    digest = hashlib.sha256(json.dumps(corpus, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")).encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested": len(urls),
        "success": len(successful),
        "cached": sum(row["cached"] for row in successful),
        "failures": failures,
        "requested_urls": urls,
        "successful_news_ids": [article["news_id"] for article in corpus],
        "hash": digest,
        "hash_algorithm": "sha256 of ordered article JSON records",
        "seed": seed,
        "sources": sources or {},
        "rejected_source_urls": rejected_sources or [],
        "settings": {"workers": workers, "timeout_seconds": timeout,
                     "max_response_bytes": MAX_HTML_BYTES},
        "biases": [
            "Labeled articles are deliberately included: this is a closed-corpus local benchmark.",
            "Distractors are sampled from the supplied public URL list, not all Tencent News content.",
            "Public-page availability and parser support can bias coverage; audit failed labels before evaluation.",
            "Cached pages freeze collection-time content; missing publication times remain empty.",
            "No enterprise content permissions, scale, traffic distribution or embedding quality are validated.",
        ],
    }
    (output_dir / "collection_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", nargs="+", type=Path, default=[])
    parser.add_argument("--url-file", type=Path)
    parser.add_argument("--distractors", type=int, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    urls, sources, rejected = plan_urls(args.questions, args.url_file, args.distractors, args.seed)
    if not urls:
        parser.error("no valid article URLs selected")
    manifest = collect_articles(urls, args.output_dir, workers=args.workers, timeout=args.timeout,
                                seed=args.seed, sources=sources, rejected_sources=rejected)
    print(json.dumps({key: manifest[key] for key in ("requested", "success", "cached", "hash")},
                     ensure_ascii=False))
    print(f"manifest: {args.output_dir / 'collection_manifest.json'}")
    return 1 if manifest["failures"] or rejected else 0


if __name__ == "__main__":
    raise SystemExit(main())
