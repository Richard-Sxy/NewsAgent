"""Import a verified incremental public-news cache into the local knowledge API.

No .env is read. Credentials are supplied through an existing environment
variable; the destination is restricted to a loopback NewsAgent instance.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_ARTICLE_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def load_documents(manifest_path: Path) -> tuple[dict, list[dict]]:
    manifest_path = manifest_path.resolve()
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds size budget")
    raw = manifest_path.read_bytes()
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds size budget")
    manifest = json.loads(raw)
    manifest_sha256 = sha256(raw).hexdigest()
    if manifest.get("schema_version") != 1 or manifest.get("timezone") != "Asia/Shanghai":
        raise ValueError("unsupported incremental manifest")
    start, end = date.fromisoformat(manifest["start_date"]), date.fromisoformat(manifest["end_date"])
    if start > end:
        raise ValueError("invalid manifest date window")
    records = manifest.get("articles")
    if not isinstance(records, list) or not 1 <= len(records) <= 10000:
        raise ValueError("manifest must contain 1..10000 verified articles")
    documents, roster, seen = [], [], set()
    root = manifest_path.parent
    for row in records:
        news_id = row.get("news_id", "")
        if not isinstance(news_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,155}", news_id) or news_id in seen:
            raise ValueError("invalid or duplicate news_id")
        seen.add(news_id)
        relative = Path(row["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("article path escapes cache")
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path.name != f"{news_id}.json":
            raise ValueError("article path or identity mismatch")
        data = path.read_bytes()
        digest = sha256(data).hexdigest()
        if len(data) > MAX_ARTICLE_BYTES or digest != row.get("sha256"):
            raise ValueError("article hash or size mismatch")
        article = json.loads(data)
        if article.get("news_id") != news_id or article.get("url") != f"https://news.qq.com/rain/a/{news_id}":
            raise ValueError("article identity or source URL mismatch")
        version = article.get("content_version")
        if type(version) is not int or version < 1:
            raise ValueError("invalid content version")
        title, body = article.get("title"), article.get("content")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise ValueError("invalid article title")
        if not isinstance(body, str) or not body.strip() or len(body) > 200000:
            raise ValueError("invalid article body")
        published = datetime.fromisoformat(article["publish_time"].replace("Z", "+00:00"))
        if published.tzinfo is None:
            published = published.replace(tzinfo=TZ)
        published = published.astimezone(TZ)
        if not start <= published.date() <= end:
            raise ValueError("article publication is outside the manifest window")
        if datetime.fromisoformat(row["publish_time"]) != published:
            raise ValueError("manifest publication differs from article")
        document_id = f"news-{news_id}"
        documents.append({
            "document_id": document_id, "news_id": news_id, "content_version": version,
            "title": title, "text": body, "publish_time": published.isoformat(),
            "source_url": article["url"], "media_type": "article",
            "text_source": "tencent-news-public-incremental",
            "metadata": {"source": "tencent_news", "source_file_sha256": digest,
                         "ingest_manifest_sha256": manifest_sha256,
                         "discovery_channel": str(row.get("category", ""))},
        })
        roster.append({"document_id": document_id, "news_id": news_id,
                       "publish_time": published.isoformat(), "source_sha256": digest})
    return {"manifest_path": str(manifest_path), "manifest_sha256": manifest_sha256,
            "start_date": start.isoformat(), "end_date": end.isoformat(),
            "timezone": "Asia/Shanghai", "documents": roster}, documents


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("knowledge API redirects are not allowed")


def validate_local_url(base_url: str) -> str:
    parts = urlsplit(base_url)
    if (parts.scheme not in {"http", "https"} or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}):
        raise ValueError("destination must be a loopback NewsAgent API root")
    _ = parts.port  # Reject malformed ports before any network call.
    return base_url.rstrip("/")


def post_documents(base_url: str, tenant_id: str, token: str, documents: list[dict]) -> list[dict]:
    request = Request(f"{base_url}/api/v1/knowledge/documents", method="POST",
                      data=json.dumps({"documents": documents}, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                               "X-Tenant-ID": tenant_id})
    with build_opener(NoRedirect()).open(request, timeout=120) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("knowledge API response exceeds size budget")
    envelope = json.loads(raw)
    if not isinstance(envelope, dict):
        raise ValueError("knowledge API response must be an object")
    outcomes = envelope.get("outcomes")
    expected = {doc["document_id"] for doc in documents}
    if (not isinstance(outcomes, list) or len(outcomes) != len(documents)
            or any(not isinstance(item, dict) or not isinstance(item.get("document_id"), str)
                   or not isinstance(item.get("status"), str) for item in outcomes)
            or {item.get("document_id") for item in outcomes} != expected
            or any(item.get("status") not in {"created", "updated", "skipped", "failed"} for item in outcomes)):
        raise ValueError("knowledge API outcome identity or status mismatch")
    return [{"document_id": item["document_id"], "status": item["status"],
             "collection_id": item.get("collection_id"), "detail": item.get("detail")}
            for item in outcomes]


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def import_cache(*, manifest_path: Path, base_url: str, tenant_id: str, token: str,
                 report_path: Path, batch_size: int = 20, post=post_documents) -> dict:
    base_url = validate_local_url(base_url)
    tenant_id = str(UUID(tenant_id))
    if not token.strip() or not 1 <= batch_size <= 20:
        raise ValueError("credential or batch size is invalid")
    source, documents = load_documents(manifest_path)
    report = {"schema_version": 1, "generated_at": datetime.now(TZ).isoformat(),
              "base_url": base_url, "tenant_id": tenant_id, "source": source,
              "requested": len(documents), "outcomes": [], "errors": [], "counts": {},
              "status": "processing"}
    write_report(report_path, report)
    for offset in range(0, len(documents), batch_size):
        batch = documents[offset:offset + batch_size]
        try:
            report["outcomes"].extend(post(base_url, tenant_id, token, batch))
        except HTTPError as exc:
            report["errors"].append({"batch_offset": offset, "kind": "http_error", "status_code": exc.code})
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            # Do not persist request/header data or raw server responses.
            report["errors"].append({"batch_offset": offset, "kind": type(exc).__name__})
        report["counts"] = dict(Counter(item["status"] for item in report["outcomes"]))
        write_report(report_path, report)
        if report["errors"]:
            break  # Rerun the same immutable cache after checking the destination.
        print(json.dumps({"processed": len(report["outcomes"]), "requested": len(documents),
                          "counts": report["counts"]}), flush=True)
    report["status"] = ("completed" if len(report["outcomes"]) == len(documents)
                        and not report["errors"] and not report["counts"].get("failed") else "partial_failed")
    write_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--base-url", default="http://127.0.0.1:28070")
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--token-env", default="NEWSAGENT_KNOWLEDGE_TOKEN")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()
    report = import_cache(manifest_path=args.manifest, base_url=args.base_url, tenant_id=args.tenant_id,
                          token=os.environ.get(args.token_env, ""), report_path=args.report,
                          batch_size=args.batch_size)
    print(json.dumps({"status": report["status"], "requested": report["requested"],
                      "counts": report["counts"], "report": str(args.report)}, ensure_ascii=False))
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
