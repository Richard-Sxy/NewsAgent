"""HTTP contract tests with synthetic news, a fake encoder and temporary SQLite."""
import copy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from app.modules.retrieval_lab.api import LabService, create_app
from app.modules.retrieval_lab.store import RetrievalLabStore

HEADERS = {"X-Lab-Request": "1", "Origin": "http://localhost"}


class FakeEngine:
    def __init__(self):
        self.versions = {"corpus": "corpus-test-v1", "model": "fake-test-model-v1", "strategy": "test-strategy-v1"}
        self.semantic_quality_ready = False
        self.calls = []

    def search(self, query, *, limit=10, category=None):
        self.calls.append((query, limit, category))
        return {"versions": copy.deepcopy(self.versions), "trace": {"test_double": True, "tier_counts": {"cold": limit}},
                "candidates": [{"news_id": "test-news-{}".format(index), "rank": index,
                                "title": "测试标题{}".format(index), "body": "仅用于测试的正文<script>文本</script>",
                                "url": "https://example.com/news/{}".format(index),
                                "publication_date": "2026-10-20", "tier": "cold", "score": 1 / index,
                                "matched_excerpt": "仅用于测试", "duplicate_sources": [], "relations": []}
                               for index in range(1, limit + 1)]}


def write_package(path):
    queries = [{"query_id": "preset-test-1", "text": "测试事件有哪些报道？", "category": "科技", "limit": 10}]
    raw = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in queries).encode("utf-8")
    (path / "queries.jsonl").write_bytes(raw)
    manifest = {"schema_version": 1, "corpus_version": "corpus-test-v1", "as_of_date": "2026-10-27",
                "source_count": 11, "canonical_count": 10, "tier_counts": {"cold": 10},
                "category_counts": {"科技": 10}, "tier_policy": {"version": "test-days-v1"},
                "warnings": ["合成测试数据，不能作为质量结论"],
                "files": {"queries.jsonl": {"sha256": sha256(raw).hexdigest(), "rows": len(queries)}}}
    (path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


class RetrievalLabApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.package = Path(self.temporary.name)
        write_package(self.package)
        self.database = self.package / "reviews.sqlite3"
        self.engine = FakeEngine()
        self.store = RetrievalLabStore(self.database)
        self.service = LabService(self.package, self.engine, self.store)
        self.client = TestClient(create_app(self.service), base_url="http://localhost")
        response = self.client.post("/api/search", json={"query": "测试查询", "category": "科技", "limit": 10}, headers=HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        self.run = response.json()
        self.route = "/api/runs/{}/judgments/test-news-1".format(self.run["run_id"])

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def put(self, **overrides):
        payload = {"label": "relevant", "expected_revision": 0, "notes": "直接回答测试问题", "reviewer": "synthetic-test-reviewer"}
        payload.update(overrides)
        return self.client.put(self.route, json=payload, headers=HEADERS)

    def test_search_freezes_versions_trace_and_no_labels(self):
        self.assertEqual(self.run["versions"], self.engine.versions)
        self.assertTrue(self.run["trace"]["test_double"])
        self.assertEqual(self.engine.calls, [("测试查询", 10, "科技")])
        self.assertEqual(len(self.run["candidates"]), 10)
        self.assertIsNone(self.run["metrics"]["precision_at_k"])
        self.assertTrue(all(c["judgment"]["label"] is None for c in self.run["candidates"]))
        self.assertEqual(self.run["as_of_date"], "2026-10-27")
        self.assertEqual(self.run["category"], "科技")

    def test_manual_label_survives_http_refresh_and_reopened_app(self):
        saved = self.put()
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["judgment"]["grade"], 2)
        refreshed = self.client.get("/api/runs/" + self.run["run_id"]).json()
        self.assertEqual(refreshed["candidates"][0]["judgment"], saved.json()["judgment"])
        reopened = LabService(self.package, FakeEngine(), RetrievalLabStore(self.database))
        with TestClient(create_app(reopened), base_url="http://localhost") as client:
            restored = client.get("/api/runs/" + self.run["run_id"]).json()
            self.assertEqual(restored["candidates"][0]["judgment"], saved.json()["judgment"])
            self.assertEqual(client.get("/api/queries").json()["items"][0]["progress"]["judged"], 1)

    def test_stale_revision_returns_409_and_current_value(self):
        saved = self.put().json()["judgment"]
        conflict = self.put(label="irrelevant")
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["current"], saved)
        changed = self.put(label="partial", expected_revision=1)
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()["judgment"]["revision"], 2)
        audit = self.client.get("/api/export").json()["judgment_audit"]
        self.assertEqual([r["judgment"]["label"] for r in audit], ["relevant", "partial"])

    def test_unsure_counts_reviewed_but_not_judged_or_negative(self):
        result = self.put(label="unsure")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["judgment"]["status"], "unable")
        self.assertIsNone(result.json()["judgment"]["grade"])
        self.assertEqual(result.json()["progress"]["unable"], 1)
        self.assertEqual(result.json()["progress"]["judged"], 0)
        self.assertIsNone(result.json()["metrics"]["precision_at_k"])

    def test_completed_top10_precision_is_deterministic_and_recall_stays_null(self):
        for i in range(1, 11):
            response = self.client.put("/api/runs/{}/judgments/test-news-{}".format(self.run["run_id"], i),
                json={"label": "relevant" if i <= 5 else "partial", "expected_revision": 0}, headers=HEADERS)
            self.assertEqual(response.status_code, 200)
        metrics = response.json()["metrics"]
        self.assertEqual(metrics["precision_at_k"], 0.5)
        self.assertEqual(metrics["pool_ndcg_at_k"], 1.0)
        self.assertIsNone(metrics["recall_at_k"])
        self.assertIsNone(metrics["accuracy"])

    def test_unknown_run_and_candidate_are_404(self):
        self.assertEqual(self.client.get("/api/runs/missing").status_code, 404)
        for route in ("/api/runs/missing/judgments/test-news-1", "/api/runs/{}/judgments/missing".format(self.run["run_id"])):
            result = self.client.put(route, json={"label": "relevant", "expected_revision": 0}, headers=HEADERS)
            self.assertEqual(result.status_code, 404)

    def test_cross_origin_and_missing_application_header_are_rejected(self):
        payload = {"label": "relevant", "expected_revision": 0}
        for headers in ({}, {"X-Lab-Request": "1", "Origin": "https://example.org"}, {"Origin": "http://localhost"}):
            with self.subTest(headers=headers):
                response = self.client.put(self.route, json=payload, headers=headers)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.store.get_run(self.run["run_id"])["progress"]["reviewed"], 0)

    def test_malformed_origin_is_rejected_without_server_error(self):
        response = self.client.put(self.route, json={"label": "relevant", "expected_revision": 0},
                                   headers={"X-Lab-Request": "1", "Origin": "http://["})
        self.assertEqual(response.status_code, 403)

    def test_foreign_host_and_test_only_host_are_rejected(self):
        for host in ("evil.example", "testserver"):
            with self.subTest(host=host):
                response = self.client.get("/api/status", headers={"Host": host})
                self.assertEqual(response.status_code, 400)

    def test_non_json_and_oversized_requests_are_rejected(self):
        response = self.client.put(self.route, content="not json", headers={**HEADERS, "Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 415)
        response = self.client.put(self.route, content=b" " * 70000, headers={**HEADERS, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)
        response = self.client.put(self.route, content=iter([b" " * 40000, b" " * 40000]),
                                   headers={**HEADERS, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 413)

    def test_input_limits_labels_and_unknown_fields_are_validated(self):
        for payload in ({"label": "auto-gold", "expected_revision": 0}, {"label": "relevant", "expected_revision": True},
                        {"label": "relevant", "expected_revision": 0, "gold": True}, {"label": [], "expected_revision": 0}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.put(self.route, json=payload, headers=HEADERS).status_code, 422)
        for payload in ({"query": " "}, {"query": "x", "limit": 51}, {"query": "x", "limit": True}, {"query": "x", "surprise": True}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/api/search", json=payload, headers=HEADERS).status_code, 422)

    def test_export_has_frozen_evidence_and_human_audit_not_promoted_gold(self):
        self.put()
        response = self.client.get("/api/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["content-disposition"])
        result = response.json()
        self.assertEqual(result["review_scope"], "single_human_review_not_promoted_gold")
        self.assertEqual(result["runs"][0]["snapshot_sha256"], self.run["snapshot_sha256"])
        self.assertEqual(result["runs"][0]["candidates"][0]["body"], self.run["candidates"][0]["body"])
        self.assertEqual(len(result["judgment_audit"]), 1)

    def test_static_page_and_api_responses_include_local_security_headers(self):
        for route in ("/", "/api/status", "/api/runs/" + self.run["run_id"]):
            response = self.client.get(route)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")
            self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])

    def test_query_task_tampering_is_rejected(self):
        with (self.package / "queries.jsonl").open("ab") as output:
            output.write(b"\n")
        with self.assertRaisesRegex(ValueError, "integrity"):
            LabService(self.package, FakeEngine(), RetrievalLabStore(self.database))

    def test_engine_corpus_mismatch_cannot_be_saved(self):
        self.engine.versions["corpus"] = "wrong-corpus"
        response = self.client.post("/api/search", json={"query": "wrong identity"}, headers=HEADERS)
        self.assertIn(response.status_code, (409, 422))
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_changed_model_or_strategy_cannot_mix_in_the_same_experiment(self):
        for field in ("model", "strategy"):
            original = self.engine.versions[field]
            self.engine.versions[field] = "changed-" + field
            with self.subTest(field=field):
                response = self.client.post("/api/search", json={"query": "changed identity"}, headers=HEADERS)
                self.assertIn(response.status_code, (409, 422))
                self.assertEqual(len(self.store.list_runs()), 1)
            self.engine.versions[field] = original

    def test_reopen_refuses_existing_runs_from_another_model_or_strategy(self):
        for field in ("model", "strategy"):
            engine = FakeEngine()
            engine.versions[field] = "changed-version"
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "new experiment"):
                LabService(self.package, engine, RetrievalLabStore(self.database))
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_seed_is_idempotent_and_preserves_human_work_after_restart(self):
        with patch("builtins.print"):
            self.service.seed()
        preset = next(r for r in self.store.list_runs() if r["query_id"] == "preset-test-1")
        self.store.save_judgment(preset["run_id"], "test-news-1", label="partial", expected_revision=0)
        restarted = LabService(self.package, FakeEngine(), RetrievalLabStore(self.database))
        with patch("builtins.print"):
            restarted.seed()
        self.assertEqual(restarted.engine.calls, [])
        self.assertEqual(len(restarted.store.list_runs()), 2)
        saved = restarted.store.get_run(preset["run_id"])["candidates"][0]["judgment"]
        self.assertEqual(saved["label"], "partial")
        self.assertEqual(saved["revision"], 1)

    def test_status_versions_are_the_frozen_experiment_identity(self):
        self.engine.versions["model"] = "changed-after-start"
        status = self.client.get("/api/status").json()
        self.assertEqual(status["corpus"]["version"], "corpus-test-v1")
        self.assertEqual(status["model"]["version"], "fake-test-model-v1")
        self.assertEqual(status["strategy"]["version"], "test-strategy-v1")
        self.assertEqual(status["persistence"], "server_sqlite")

    def test_search_lock_returns_conflict_without_creating_run(self):
        self.service.search_lock.acquire()
        try:
            response = self.client.post("/api/search", json={"query": "busy"}, headers=HEADERS)
        finally:
            self.service.search_lock.release()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.store.list_runs()), 1)


if __name__ == "__main__":
    unittest.main()
