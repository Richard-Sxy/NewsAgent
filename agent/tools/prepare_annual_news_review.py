"""Build an offline public-news review catalogue without models or database writes.

Python 3.8+. The collector report binds parsed/raw/body hashes. Only verified
body_ready records enter the review/export files. Event membership, role hints
and relevance all remain pending human review; this tool creates no qrels.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from hashlib import sha256
from html import escape
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CATEGORIES = ("科技", "AI", "财经", "股票", "汽车", "数码", "国际", "军事", "体育", "游戏")
CATEGORY_ALIASES = {
    "tech": "科技", "technology": "科技", "ai": "AI", "finance": "财经",
    "stock": "股票", "stocks": "股票", "auto": "汽车", "digital": "数码",
    "world": "国际", "international": "国际", "military": "军事",
    "sport": "体育", "sports": "体育", "game": "游戏", "gaming": "游戏",
}
SHA = re.compile(r"[a-f0-9]{64}")
MAX_FILE_BYTES = 128 * 1024 * 1024


def digest(raw):
    return sha256(raw).hexdigest()


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("JSON input is linked, absent, or oversized")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def bounded_file(root, relative, suffix=None):
    if not isinstance(relative, str) or "\\" in relative:
        raise ValueError("invalid source relative path")
    pieces = PurePosixPath(relative)
    if pieces.is_absolute() or not pieces.parts or ".." in pieces.parts:
        raise ValueError("source path escapes corpus directory")
    path = root
    for piece in pieces.parts:
        path = path / piece
        if path.is_symlink():
            raise ValueError("linked source paths are forbidden")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError("source path escapes corpus directory")
    if suffix and path.suffix != suffix:
        raise ValueError("unexpected source file type")
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("source file is absent or oversized")
    return path


def public_link(value):
    """Return a credential-free HTTP(S) link; do not copy signed query strings."""
    if not isinstance(value, str) or len(value) > 4096 or "\\" in value or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.port not in (None, 443 if parsed.scheme == "https" else 80):
            return None
    except ValueError:
        return None
    query = [(k.lower(), v) for k, v in parse_qsl(parsed.query)
             if k.lower() in ("id", "cid", "newsid", "articleid", "article_id", "contentid") and re.fullmatch(r"\d{1,24}", v)]
    return urlunsplit((parsed.scheme, parsed.hostname.lower(), parsed.path or "/", urlencode(sorted(query)), ""))


def category_name(value):
    value = str(value)
    return CATEGORY_ALIASES.get(value.lower(), value)


def verify_records(corpus_dir):
    corpus_dir = Path(corpus_dir)
    if corpus_dir.is_symlink():
        raise ValueError("linked corpus directory is forbidden")
    report_path = bounded_file(corpus_dir, "report.json", ".json")
    report_raw = report_path.read_bytes()
    report = read_json(report_path)
    if report.get("schema_version") != 1 or not isinstance(report.get("articles"), list):
        raise ValueError("unsupported collector report")
    records, nonready, seen = [], [], set()
    for entry in report["articles"]:
        if not isinstance(entry, dict):
            raise ValueError("invalid report article entry")
        url = entry.get("url")
        if public_link(url) != url:
            raise ValueError("collector report contains an unsafe or noncanonical URL")
        if url in seen:
            raise ValueError("collector report repeats a source URL")
        seen.add(url)
        if entry.get("status") != "body_ready":
            nonready.append({key: entry.get(key) for key in (
                "url", "article_id", "status", "event_ids", "categories", "error",
                "quality_flags", "published_at", "date_precision")})
            continue
        identity = digest(url.encode("utf-8"))[:24]
        if entry.get("article_id") != identity or entry.get("relative_path") != "articles/" + identity + ".json":
            raise ValueError("report source identity or relative path mismatch")
        parsed_path = bounded_file(corpus_dir, entry["relative_path"], ".json")
        parsed_raw = parsed_path.read_bytes()
        if not SHA.fullmatch(str(entry.get("parsed_sha256", ""))) or digest(parsed_raw) != entry["parsed_sha256"]:
            raise ValueError("parsed article SHA-256 mismatch")
        article = json.loads(parsed_raw.decode("utf-8"))
        if not isinstance(article, dict) or article.get("status") != "body_ready" or article.get("url") != url or article.get("article_id") != identity:
            raise ValueError("parsed article identity or status mismatch")
        raw_relative = "raw/" + identity + ".html"
        if article.get("raw_path") != raw_relative:
            raise ValueError("parsed raw path mismatch")
        raw_path = bounded_file(corpus_dir, raw_relative, ".html")
        raw_sha = digest(raw_path.read_bytes())
        if raw_sha != entry.get("raw_sha256") or raw_sha != article.get("raw_sha256"):
            raise ValueError("raw HTML SHA-256 mismatch")
        title, body = article.get("title"), article.get("body")
        if not isinstance(title, str) or not title.strip() or not isinstance(body, str) or not body.strip():
            raise ValueError("body_ready article requires a title and body")
        body_sha = digest(body.encode("utf-8"))
        if body_sha != entry.get("body_sha256") or body_sha != article.get("body_sha256"):
            raise ValueError("body SHA-256 mismatch")
        for field in ("published_at", "date_precision", "date_basis", "event_ids", "categories"):
            if article.get(field) != entry.get(field):
                raise ValueError("parsed article metadata differs from report")
        if article.get("quality_flags"):
            raise ValueError("body_ready article contains unresolved collector quality flags")
        # Never inherit a prior automatic relevance label or unreviewed fact group.
        record = {key: article.get(key) for key in (
            "url", "article_id", "title", "body", "published_at", "publication_date",
            "date_precision", "source_timezone", "date_basis", "date_evidence",
            "fetched_at", "body_basis", "event_ids", "categories", "discovery_hints")}
        record.update({
            "status": "body_ready", "integrity_status": "verified",
            "event_membership_status": "pending_human",
            "fact_group_id": None, "relation_edges": [],
            "relevance_label": None, "relevance_review_status": "pending_human",
            "human_labels": {"query": None, "relevance": None, "annotator": None, "reviewed_at": None},
            "source_evidence": {
                "parsed_path": str(parsed_path.resolve()), "parsed_relative_path": entry["relative_path"],
                "parsed_sha256": digest(parsed_raw), "raw_path": str(raw_path.resolve()),
                "raw_relative_path": raw_relative, "raw_sha256": raw_sha, "body_sha256": body_sha,
                "report_path": str(report_path.resolve()), "report_sha256": digest(report_raw),
            },
        })
        records.append(record)
    return records, nonready, report


def load_discovery(paths):
    groups, sources, all_urls, article_entries = {}, [], set(), 0
    for path in paths:
        path = Path(path)
        data = read_json(path)
        if not isinstance(data.get("events"), list):
            raise ValueError("discovery manifest needs events")
        sources.append({"path": str(path.resolve()), "sha256": digest(path.read_bytes())})
        for event in data["events"]:
            if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
                raise ValueError("invalid discovery event")
            identity = event["event_id"]
            kind = event.get("discovery_group_type") or event.get("group_type") or "event"
            kind = "topic_collection" if kind == "topic_collection" else "event"
            group = groups.setdefault(identity, {
                "event_id": identity, "title": event.get("title", identity),
                "category": category_name(event.get("category", "unknown")),
                "event_date_hint": event.get("event_date"), "discovery_group_type": kind,
                "membership_status": "pending_human", "candidate_urls": set(),
            })
            if group["discovery_group_type"] != kind:
                raise ValueError("event and topic_collection identities must not collide")
            for article in event.get("articles", []):
                if not isinstance(article, dict):
                    raise ValueError("invalid discovery article")
                article_entries += 1
                link = public_link(article.get("url"))
                if link:
                    all_urls.add(link)
                    group["candidate_urls"].add(link)
    return groups, sources, all_urls, article_entries


def exact_duplicate_groups(records):
    by_hash = defaultdict(list)
    for record in records:
        by_hash[record["source_evidence"]["body_sha256"]].append(record)
    groups = []
    for body_hash, members in sorted(by_hash.items()):
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda item: item["url"])
        groups.append({
            "group_id": "exact-body-" + body_hash[:20], "body_sha256": body_hash,
            "comparison": "byte_identical_extracted_body_only",
            "fact_equivalence_status": "pending_human",
            "representative_suggestion": ordered[0]["article_id"],
            "representative_status": "suggestion_only",
            "sources": [{"article_id": row["article_id"], "url": row["url"],
                         "parsed_sha256": row["source_evidence"]["parsed_sha256"]} for row in ordered],
        })
    return groups


def exact_aware_time(record):
    value = record.get("published_at")
    if record.get("date_precision") not in ("minute", "second") or not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return None
        return timestamp.isoformat()
    except ValueError:
        return None


def content_candidates(records, repository_root):
    candidates, skipped = [], Counter()
    repository_root = Path(repository_root).resolve()
    for row in records:
        if not row["url"].startswith("https://"):
            skipped["http_source"] += 1
            continue
        evidence = row["source_evidence"]
        try:
            parsed_path = Path(evidence["parsed_path"]).relative_to(repository_root).as_posix()
            report_path = Path(evidence["report_path"]).relative_to(repository_root).as_posix()
            raw_path = Path(evidence["raw_path"]).relative_to(repository_root).as_posix()
        except ValueError:
            skipped["outside_repository"] += 1
            continue
        publication = exact_aware_time(row)
        candidate = {
            "news_id": "pub-" + digest(row["url"].encode("utf-8"))[:24],
            "title": row["title"], "body": row["body"], "url": row["url"],
            "content_type": "article", "content_state": "body_available",
            "fragments": [], "quality_flags": [] if publication else ["publication_time_requires_review"],
            "priority": 0, "date_precision": row.get("date_precision"),
            "publication_date": row.get("publication_date"), "relevance_label": None,
            "event_membership_status": "pending_human", "event_ids_hint": row.get("event_ids", []),
            "source": {
                "package": "annual-public-news-v1", "path": parsed_path,
                "file_sha256": evidence["parsed_sha256"], "manifest_path": report_path,
                "manifest_sha256": evidence["report_sha256"], "raw_path": raw_path,
                "raw_sha256": evidence["raw_sha256"], "body_sha256": evidence["body_sha256"],
                "published_at_original": row.get("published_at"), "date_precision": row.get("date_precision"),
            },
        }
        if publication:
            candidate["publish_time"] = publication
        candidates.append(candidate)
    return candidates, dict(skipped)


def make_summary(records, nonready, report, groups, sources, discovery_urls, article_entries, candidates, skipped):
    by_category = {category: 0 for category in CATEGORIES}
    by_month, ready_events = Counter(), Counter()
    urls_by_event = defaultdict(set)
    for row in records:
        for category in set(category_name(c) for c in row.get("categories", [])):
            by_category[category] = by_category.get(category, 0) + 1
        value = row.get("publication_date") or row.get("published_at") or ""
        if re.match(r"20\d{2}-\d{2}", value):
            by_month[value[:7]] += 1
        for event in row.get("event_ids", []):
            urls_by_event[event].add(row["url"])
            ready_events[event] += 1
    event_coverage = []
    for identity, group in sorted(groups.items()):
        requested = group["candidate_urls"]
        successful = urls_by_event[identity]
        event_coverage.append({
            key: group[key] for key in ("event_id", "title", "category", "event_date_hint",
                                       "discovery_group_type", "membership_status")
        })
        event_coverage[-1].update({
            "discovered_urls": len(requested), "body_ready": len(successful),
            "not_body_ready": len(requested - successful),
            "coverage_gap": len(successful) < 2,
            "coverage_note": "fewer_than_two_verified_bodies" if len(successful) < 2 else "unreviewed_membership",
        })
    duplicates = exact_duplicate_groups(records)
    return {
        "schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": report.get("scope"), "collector_original_url_count": report.get("unique_candidates"),
        "attempted_url_count": len(report["articles"]), "body_ready": len(records),
        "non_success_count": len(nonready), "non_success_by_status": dict(Counter(row["status"] for row in nonready)),
        "discovery_article_entries": article_entries, "discovery_unique_url_count": len(discovery_urls),
        "discovery_real_event_count": sum(g["discovery_group_type"] == "event" for g in groups.values()),
        "discovery_topic_collection_count": sum(g["discovery_group_type"] == "topic_collection" for g in groups.values()),
        "category_coverage": by_category, "month_coverage": dict(sorted(by_month.items())),
        "event_coverage": event_coverage, "events_without_verified_body": sum(row["body_ready"] == 0 for row in event_coverage),
        "events_with_fewer_than_two_bodies": sum(row["coverage_gap"] for row in event_coverage),
        "exact_duplicate_group_count": len(duplicates), "exact_duplicate_groups": duplicates,
        "content_candidate_count": len(candidates), "content_candidate_skipped": skipped,
        "content_candidates_with_aware_publish_time": sum("publish_time" in row for row in candidates),
        "content_candidates_publication_pending": sum("publish_time" not in row for row in candidates),
        "discovery_sources": sources, "event_membership_status": "pending_human",
        "relevance_review_status": "pending_human", "relevance_label": None,
        "retrieval_accuracy": None,
        "notes": [
            "Exact body hashes are source-preserving duplicate suggestions, not same-fact judgments.",
            "Topic collections and event membership are unreviewed discovery hints.",
            "No queries, qrels, relevance judgments, or retrieval accuracy are generated.",
            "Missing timezone or day-only dates are preserved without an invented publication instant.",
        ],
    }


def render_html(records, nonready, summary, groups):
    def text(value):
        return escape(str(value if value is not None else ""), quote=True)
    by_category = defaultdict(lambda: defaultdict(list))
    for row in records:
        for category in set(category_name(c) for c in row.get("categories", [])) or {"未分类"}:
            for event in row.get("event_ids", []) or ["ungrouped"]:
                by_category[category][event].append(row)
    parts = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
             '<meta name="viewport" content="width=device-width,initial-scale=1"><title>年度新闻语料人工核查</title>',
             '<style>body{font:16px/1.6 system-ui,sans-serif;max-width:1180px;margin:32px auto;padding:0 20px;color:#183044;background:#f5f7fa}h1,h2,h3{line-height:1.3}article,section.summary{background:white;border:1px solid #d5dde4;border-radius:8px;padding:16px;margin:12px 0}a{color:#125ca8}input{width:95%;padding:12px;font:inherit}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}.meta{color:#526575;font-size:14px}.pending{color:#915400}details{margin-top:8px}table{border-collapse:collapse}td,th{padding:6px 12px;border-bottom:1px solid #ccc}.group{margin-bottom:32px}[hidden]{display:none!important}</style>',
             '<h1>年度新闻语料人工核查</h1><p>正文来自本地公开新闻抓取。事件归组、角色提示、同事实关系和相关性均待人工核查。向量相似度与召回准确率尚未计算。</p>',
             '<section class="summary"><p>已核验正文：<b>' + str(len(records)) + '</b>；非成功：' + str(len(nonready)) +
             '；事件发现条目：' + str(summary["discovery_real_event_count"]) +
             '；主题合集：' + str(summary["discovery_topic_collection_count"]) +
             '；完全相同正文组：' + str(summary["exact_duplicate_group_count"]) + '。</p>',
             '<p>仅校验来源文件完整性；来源内容的事实真实性和新闻相关性需要人工判断。</p></section>',
             '<label for="search">搜索标题、正文、事件或类别</label><input id="search" type="search" placeholder="输入关键词筛选；展开正文查看来源"><p id="visible-count"></p>']
    ordered = list(CATEGORIES) + sorted(set(by_category) - set(CATEGORIES))
    for category in ordered:
        parts.append('<section class="category"><h2>' + text(category) + '</h2>')
        if not by_category[category]:
            parts.append('<p class="meta">暂无通过完整性核验的正文。</p>')
        for identity, members in sorted(by_category[category].items()):
            group = groups.get(identity, {})
            title = group.get("title", identity)
            kind = "主题合集" if group.get("discovery_group_type") == "topic_collection" else "事件发现分组"
            parts.append('<div class="group"><h3>' + text(title) + '</h3><p class="pending">' + kind +
                         ' · 归属待人工核查 · ' + str(len(members)) + ' 篇来源</p>')
            for row in members:
                hints = [h.get("role_hint") for h in row.get("discovery_hints", [])
                         if isinstance(h, dict) and h.get("event_id") == identity and h.get("role_hint")]
                parts.append('<article class="news"><h4>' + text(row["title"]) + '</h4><p class="meta">' +
                             text(category) + ' · ' + text(identity) + ' · 页面日期：' +
                             text(row["published_at"]) + ' · 精度：' + text(row.get("date_precision")) +
                             ' · 时区：' + text(row.get("source_timezone") or "来源未明确") + '</p>')
                parts.append('<p><a target="_blank" rel="noopener noreferrer" href="' + text(row["url"]) +
                             '">查看原始来源</a> · <span class="pending">角色提示：' + text(", ".join(sorted(set(hints))) or "unknown") +
                             '（待核查）；相关性：待人工标注</span></p>')
                parts.append('<details><summary>展开已采集正文与来源校验</summary><pre>' + text(row["body"]) +
                             '</pre><p class="meta">正文提取：' + text(row.get("body_basis")) +
                             '；日期证据：' + text(row.get("date_evidence")) +
                             '；原解析文件：' + text(row["source_evidence"]["parsed_relative_path"]) +
                             '；SHA-256：' + text(row["source_evidence"]["parsed_sha256"]) + '</p></details></article>')
            parts.append('</div>')
        parts.append('</section>')
    if nonready:
        parts.append('<section><h2>未进入正文语料的来源</h2><table><thead><tr><th>来源</th><th>状态</th><th>原因</th></tr></thead><tbody>')
        for row in nonready:
            parts.append('<tr><td><a rel="noopener noreferrer" target="_blank" href="' + text(row["url"]) + '">' +
                         text(row["url"]) + '</a></td><td>' + text(row.get("status")) + '</td><td>' +
                         text(row.get("error") or ", ".join(row.get("quality_flags") or [])) + '</td></tr>')
        parts.append('</tbody></table></section>')
    parts.append('<script>const box=document.getElementById("search");const cards=Array.from(document.querySelectorAll("article.news"));const count=document.getElementById("visible-count");function filter(){const q=box.value.toLocaleLowerCase().trim();let n=0;for(const card of cards){const show=card.textContent.toLocaleLowerCase().includes(q);card.hidden=!show;if(show)n++;}count.textContent="当前显示 "+n+" 条来源展示（跨类别或事件可能重复展示同一来源）";}box.addEventListener("input",filter);filter();</script></html>')
    return "\n".join(parts)


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def prepare(corpus_dir, discoveries, output_dir, repository_root=REPOSITORY_ROOT):
    # Complete verification before creating or replacing any output file.
    records, nonready, report = verify_records(Path(corpus_dir))
    groups, sources, urls, entries = load_discovery(discoveries)
    candidates, skipped = content_candidates(records, repository_root)
    summary = make_summary(records, nonready, report, groups, sources, urls, entries, candidates, skipped)
    html = render_html(records, nonready, summary, groups)
    output_dir = Path(output_dir)
    if output_dir.is_symlink():
        raise ValueError("linked output directory is forbidden")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name in ("review-records.jsonl", "content-candidates.jsonl", "summary.json", "index.html"):
        if (output_dir / name).is_symlink():
            raise ValueError("linked output files are forbidden")
    write_jsonl(output_dir / "review-records.jsonl", records)
    write_jsonl(output_dir / "content-candidates.jsonl", candidates)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "index.html").write_text(html, encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", type=Path, required=True)
    parser.add_argument("--discovery", type=Path, action="append", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = prepare(args.corpus_dir, args.discovery, args.output_dir)
    print(json.dumps({key: result[key] for key in (
        "body_ready", "non_success_count", "discovery_real_event_count",
        "discovery_topic_collection_count", "exact_duplicate_group_count",
        "content_candidate_count", "content_candidates_with_aware_publish_time")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

