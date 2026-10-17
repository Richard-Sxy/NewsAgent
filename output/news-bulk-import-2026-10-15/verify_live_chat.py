#!/usr/bin/env python3
"""Eight-day local chat acceptance. Run only after the v2 deployment is active.

Requires --execute before any HTTP request. The fixed local gateway supplies its
own fixture identity; this script neither reads credentials nor sends tokens.
Only metadata and hashes are persisted, never article excerpts or full answers.
An interrupted run can use --execute --resume with the same conversation and
request IDs. Never replace a pending request ID merely because HTTP timed out.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
from http.client import HTTPException
import json
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo


HERE = Path(__file__).resolve().parent
API = "http://127.0.0.1:5179/api/v1/conversations"
TITLE = "每日千篇导入验收"
SNAPSHOT = "news-bge-incremental-20261014-v2"
PACKAGE = HERE.parents[1] / "agent/data" / SNAPSHOT
CASES_PATH = HERE / "live-chat-cases.json"
RECEIPT = HERE / "live-chat-verification.json"
TURN_DIR = HERE / "live-chat-turns"
MAX_RESPONSE = 4 * 1024 * 1024
TZ = ZoneInfo("Asia/Shanghai")


class ApiError(Exception):
    def __init__(self, kind: str, status_code: int | None = None):
        super().__init__(kind)
        self.kind, self.status_code = kind, status_code

    def audit(self):
        return {"kind": self.kind, **({"status_code": self.status_code} if self.status_code else {})}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("redirect_refused", code)


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("publication time must be a string")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("publication time must include a timezone")
    return result.astimezone(TZ)


def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix="." + path.name + ".", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def persist(state, entry=None):
    state["updated_at"] = now()
    state["counts"] = dict(Counter(row["status"] for row in state["checks"]))
    if entry is not None:
        write_json(TURN_DIR / (entry["date"] + ".json"), {"conversation_id": state.get("conversation_id"),
                   "conversation_url": state.get("conversation_url"), **entry})
    write_json(RECEIPT, state)


def request_json(method, suffix="", body=None, *, timeout=120):
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = Request(API + suffix, method=method, data=raw, headers={"Content-Type": "application/json"})
    try:
        # Bypass shell proxy settings and reject every redirect from this fixed
        # local origin. No Authorization or identity headers are constructed.
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE + 1)
        if len(payload) > MAX_RESPONSE:
            raise ApiError("response_size_limit")
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ApiError("invalid_response_object")
        return parsed
    except HTTPError as exc:
        raise ApiError("http_error", exc.code) from None
    except (TimeoutError, URLError, OSError, HTTPException) as exc:
        reason = getattr(exc, "reason", exc)
        raise ApiError("timeout" if isinstance(reason, TimeoutError) else "transport_error") from None
    except (ValueError, UnicodeError):
        raise ApiError("invalid_json_response") from None


def load_inputs():
    raw = CASES_PATH.read_bytes()
    cases = json.loads(raw)
    days = [(date(2026, 10, 7) + timedelta(days=offset)).isoformat() for offset in range(8)]
    if not isinstance(cases, list) or len(cases) != 8:
        raise ValueError("exactly eight daily cases are required")
    if [row.get("date") for row in cases] != days or len({row.get("news_id") for row in cases}) != 8:
        raise ValueError("cases must contain one distinct target per ordered day, October 7 through 14")
    for row in cases:
        if (not isinstance(row.get("news_id"), str) or not isinstance(row.get("title"), str)
                or not 1 <= len(row["title"]) <= 200 or "\n" in row["title"]
                or row.get("url") != "https://news.qq.com/rain/a/" + row["news_id"]
                or timestamp(row.get("publish_time")).date().isoformat() != row["date"]):
            raise ValueError("invalid expected news metadata")
    digest = hashlib.sha256((PACKAGE / "SHA256SUMS").read_bytes()).hexdigest()
    build = json.loads((PACKAGE / "build_report.json").read_text(encoding="utf-8"))
    if (build.get("status") != "completed" or build.get("snapshot_version") != SNAPSHOT
            or build.get("package_manifest_sha256") != digest):
        raise ValueError("completed v2 package metadata is required before live acceptance")
    return cases, hashlib.sha256(raw).hexdigest(), digest, "bge-large-zh-v1.5:" + digest[:16]


def poll_terminal(state, entry, *, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            detail = request_json("GET", f"/{state['conversation_id']}?limit=100", timeout=15)
            turns = detail.get("turns")
            if not isinstance(turns, list):
                raise ApiError("invalid_history_turns")
            matched = [row for row in turns if isinstance(row, dict) and row.get("request_id") == entry["request_id"]]
            if len(matched) > 1:
                raise ApiError("duplicate_request_id_in_history")
            if matched:
                turn = matched[0]
                entry["turn_id"], entry["turn_status"] = turn.get("id"), turn.get("status")
                persist(state, entry)
                if turn.get("status") in {"completed", "failed"}:
                    return turn
        except ApiError as exc:
            entry["last_poll_error"] = exc.audit()
            persist(state, entry)
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    return None


def validate_turn(turn, entry, expected_embedding):
    errors, traces = [], turn.get("tools")
    if turn.get("request_id") != entry["request_id"] or turn.get("user_content") != "检索 " + entry["title"]:
        errors.append("turn_request_identity_mismatch")
    if turn.get("status") != "completed":
        errors.append("turn_not_completed")
    searches = [trace for trace in traces if isinstance(trace, dict) and trace.get("name") == "search_knowledge"] if isinstance(traces, list) else []
    if not searches:
        errors.append("search_knowledge_trace_missing")
    summaries, target_found = [], False
    for trace in searches:
        result = trace.get("result")
        if not isinstance(result, dict):
            errors.append("invalid_search_result")
            continue
        checks = {"tool_completed": trace.get("status") == "completed",
                  "snapshot_v2": result.get("snapshot_version") == SNAPSHOT,
                  "bge_backend": result.get("retrieval_backend") == "bge_snapshot",
                  "evidence_is_untrusted": result.get("evidence_is_untrusted") is True,
                  "embedding_version": result.get("embedding_version") == expected_embedding}
        errors.extend(key for key, passed in checks.items() if not passed)
        items = result.get("items")
        if not isinstance(items, list):
            errors.append("invalid_search_items")
            items = []
        metadata = []
        for item in items:
            if not isinstance(item, dict):
                errors.append("invalid_search_item")
                continue
            # Explicit allowlist excludes excerpt, text, answer and all headers.
            metadata.append({key: item.get(key) for key in ("news_id", "title", "source_url", "publish_time",
                                                           "embedding_version", "content_version", "chunk_id")})
            if item.get("embedding_version") != expected_embedding:
                errors.append("item_embedding_version_mismatch")
            if item.get("news_id") == entry["news_id"]:
                target_found = True
                if item.get("title") != entry["title"]:
                    errors.append("target_title_mismatch")
                if item.get("source_url") != entry["url"]:
                    errors.append("target_source_url_mismatch")
                try:
                    if timestamp(item.get("publish_time")) != timestamp(entry["publish_time"]):
                        errors.append("target_actual_publication_time_mismatch")
                except (ValueError, TypeError):
                    errors.append("target_publication_time_invalid")
        summaries.append({"name": "search_knowledge", "status": trace.get("status"),
                          "attempts": trace.get("attempts"), "checks": checks,
                          "snapshot_version": result.get("snapshot_version"),
                          "embedding_version": result.get("embedding_version"), "items": metadata})
    if not target_found:
        errors.append("target_news_id_not_recalled")
    answer = turn.get("assistant_content")
    entry.update(turn_id=turn.get("id"), turn_status=turn.get("status"), completed_at=turn.get("completed_at"),
                 status="failed" if errors else "completed", errors=sorted(set(errors)),
                 target_news_id_recalled=target_found, tools=summaries,
                 answer_characters=len(answer) if isinstance(answer, str) else 0,
                 answer_sha256=hashlib.sha256(answer.encode("utf-8")).hexdigest() if isinstance(answer, str) else None)


def run(*, resume=False, timeout=120):
    cases, cases_digest, package_digest, embedding = load_inputs()
    if RECEIPT.exists():
        if not resume:
            raise ValueError("existing audit receipt: use --resume to retain its conversation and request IDs")
        state = json.loads(RECEIPT.read_text(encoding="utf-8"))
        if (state.get("cases_sha256") != cases_digest or state.get("package_manifest_sha256") != package_digest
                or state.get("expected_embedding_version") != embedding):
            raise ValueError("resume inputs differ from the original acceptance run")
        if not state.get("conversation_id"):
            raise ValueError("conversation creation had an unknown outcome; audit that attempt before creating another")
    else:
        state = {"schema_version": 1, "status": "processing", "created_at": now(), "api_base": API,
                 "conversation_title": TITLE, "conversation_id": None, "conversation_url": None,
                 "cases_sha256": cases_digest, "snapshot_version": SNAPSHOT,
                 "package_manifest_sha256": package_digest, "expected_embedding_version": embedding,
                 "checks": [{**row, "request_id": str(uuid4()), "status": "not_started"} for row in cases]}
    try:
        if not state.get("conversation_id"):
            state["phase"], state["conversation_create_started_at"] = "creating_conversation", now()
            persist(state)  # Audit the create attempt before the first mutation.
            conversation = request_json("POST", body={"title": TITLE}, timeout=timeout)
            state["conversation_id"] = str(UUID(conversation["id"]))
            state["conversation_url"] = "http://127.0.0.1:5179/chat?conversation=" + state["conversation_id"]
            persist(state)
        state["phase"] = "daily_searches"
        for entry in state["checks"]:
            if entry["status"] == "completed":
                continue
            # Reuse the same request ID on an explicit resume. The API's
            # existing idempotency contract prevents duplicate turns.
            entry.update(status="submitted", submitted_at=now())
            persist(state, entry)
            print(json.dumps({"phase": "submitted", "date": entry["date"], "request_id": entry["request_id"],
                              "conversation_id": state["conversation_id"]}), flush=True)
            turn = None
            try:
                turn = request_json("POST", f"/{state['conversation_id']}/messages",
                                    {"request_id": entry["request_id"], "content": "检索 " + entry["title"]}, timeout=timeout)
            except ApiError as exc:
                entry["post_error"] = exc.audit()
                persist(state, entry)
                if exc.status_code is not None and 400 <= exc.status_code < 500 and exc.status_code != 409:
                    entry.update(status="failed", errors=["message_request_rejected"])
                    persist(state, entry)
                    continue
            if turn is None or turn.get("status") == "processing":
                turn = poll_terminal(state, entry, seconds=timeout)
            if turn is None:
                entry.update(status="timeout", errors=["message_outcome_pending_or_unknown"],
                             recovery="Read this conversation by the saved request_id; retain IDs when using --execute --resume.")
                state["status"], state["phase"] = "partial_failed", "awaiting_audit"
                persist(state, entry)
                return state  # Do not send the next day while this turn is unknown.
            validate_turn(turn, entry, embedding)
            persist(state, entry)
            print(json.dumps({"phase": "verified", "date": entry["date"], "request_id": entry["request_id"],
                              "turn_id": entry.get("turn_id"), "status": entry["status"], "errors": entry["errors"]}), flush=True)
        state["status"] = "completed" if all(row["status"] == "completed" for row in state["checks"]) else "partial_failed"
        state["phase"], state["finished_at"] = "finished", now()
    except Exception as exc:
        state["status"] = "partial_failed"
        state["failure"] = exc.audit() if isinstance(exc, ApiError) else {"kind": type(exc).__name__}
        state["recovery"] = "Retain the conversation_id and all request_ids; audit an unknown create outcome before retrying."
    persist(state)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Run only after the root task explicitly authorizes the live chat")
    parser.add_argument("--resume", action="store_true", help="Continue the saved conversation using the same request IDs")
    parser.add_argument("--timeout", type=int, default=120, help="Bounded HTTP and recovery-poll budgets in seconds")
    args = parser.parse_args()
    if not args.execute:
        parser.error("no HTTP requests sent; --execute requires the root task's explicit go-ahead")
    if not 15 <= args.timeout <= 180:
        parser.error("timeout must be between 15 and 180 seconds")
    try:
        state = run(resume=args.resume, timeout=args.timeout)
    except Exception as exc:
        print(json.dumps({"status": "not_started", "error_kind": type(exc).__name__}), flush=True)
        return 1
    print(json.dumps({"status": state["status"], "counts": state.get("counts"),
                      "conversation_id": state.get("conversation_id"), "conversation_url": state.get("conversation_url"),
                      "receipt": str(RECEIPT)}, ensure_ascii=False, indent=2), flush=True)
    return 0 if state["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
