"""Check the actual running endpoint with synthetic inputs and protocol failures."""
import argparse
import json
import time

import httpx

from assets import ROOT
from runtime import MODEL_ID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8008)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("invalid port")
    report = {"scope": "standalone local model HTTP contract; no NewsAgent Workflow/OTel trace", "checks": []}
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=120, trust_env=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        assert health.json()["model"] == MODEL_ID
        assert health.json()["platform"] == "linux"
        report["health"] = health.json()
        report["checks"].append({"name": "health", "status": "passed"})
        models = client.get("/v1/models")
        models.raise_for_status()
        assert models.json()["data"][0]["id"] == MODEL_ID
        report["checks"].append({"name": "models", "status": "passed"})
        payload = {"model": MODEL_ID, "messages": [
            {"role": "system", "content": "你是新闻运营助手。仅输出JSON对象，包含decision和reason两个字符串字段。不要编造数字。"},
            {"role": "user", "content": "阅读量上升，但没有对照实验。请判断能否证明推送有效。decision使用insufficient_evidence。"},
        ], "max_tokens": 128, "temperature": 0, "stream": False}
        started = time.monotonic()
        response = client.post("/v1/chat/completions", json=payload)
        response.raise_for_status()
        body = response.json()
        content = body["choices"][0]["message"]["content"]
        assert body["model"] == MODEL_ID
        assert body["choices"][0]["finish_reason"] == "stop"
        assert response.headers["x-request-id"] == body["id"]
        decoded = json.loads(content)
        assert set(decoded) == {"decision", "reason"} and decoded["decision"] == "insufficient_evidence"
        assert isinstance(decoded["reason"], str) and decoded["reason"]
        report["checks"].append({"name": "synthetic_json_generation", "status": "passed", "request_id": body["id"],
                                 "seconds": round(time.monotonic() - started, 3), "usage": body["usage"],
                                 "synthetic_output": decoded})
        for name, change, status in [
            ("unknown_model", {"model": "unapproved"}, 404),
            ("stream_rejected", {"stream": True}, 400),
            ("json_constraint_not_claimed", {"response_format": {"type": "json_object"}}, 400),
            ("generation_limit", {"max_tokens": 2049}, 422),
            ("context_limit", {"messages": [{"role": "user", "content": "新闻" * 3000}]}, 400),
        ]:
            actual = client.post("/v1/chat/completions", json={**payload, **change})
            assert actual.status_code == status, (name, actual.status_code)
            report["checks"].append({"name": name, "status": "passed", "http_status": status})
    target = ROOT / "small_model/runtime/hunyuan/api-report.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
