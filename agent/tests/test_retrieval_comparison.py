"""Contract tests for real fusion branches and supplemental human review."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import numpy as np
from fastapi.testclient import TestClient

from app.modules.retrieval_lab.api import create_app
from app.modules.retrieval_lab.comparison import (
    ComparisonReviewService, REVIEW_VERSION, SupplementalStore, calculate_comparison,
    exact_news_candidates, read_supplemental_labels, rank_pair, sha_file, supplemental_runs,
)
from app.retrieval.query_policy import QueryRetrievalPolicy
from app.retrieval.tiered_vector import VectorSearchHit, VectorTier


def article(nid):
    body = "原文证据" + nid
    return {"news_id": nid, "title": nid, "body": body,
            "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "url": "https://example.com/" + nid, "tier": "cold",
            "publication_date": "2026-01-01", "publish_time": None}


def fixture():
    articles = {str(i): article(str(i)) for i in range(11)}
    preds = lambda ids: [{"news_id": str(i), "rank": n + 1,
                         "body_sha256": articles[str(i)]["body_sha256"]} for n, i in enumerate(ids)]
    comp = {"config": {"version": "test"}, "as_of_date": "2026-10-27",
            "versions": {"corpus": "test-corpus", "model": "test-model", "strategy": "original"},
            "queries": [{"query_id": "q", "query": "问题", "category": "AI",
                "predictions": {"legacy_rrf": preds(range(1, 11)), "global_similarity": preds(range(10))}}]}
    labels = {("q", str(i)): {"body_sha256": articles[str(i)]["body_sha256"],
              "judgment": {"status": "judged", "label": "relevant" if i < 3 else "irrelevant"}} for i in range(10)}
    return comp, articles, labels


class FusionContracts(unittest.TestCase):
    def hits(self):
        result = {}
        for tier, prefix, base in ((VectorTier.HOT, "h", .50),
                                   (VectorTier.WARM, "w", .70), (VectorTier.COLD, "c", .99)):
            result[tier] = [VectorSearchHit(chunk_id=prefix+str(i), news_id=prefix+str(i),
                title=prefix+str(i), excerpt="证据", source_url=None, publish_time=None,
                raw_score=base-i*.001, tier=tier, content_version=1, embedding_version="test")
                for i in range(12)]
        return result

    def test_actual_legacy_branch_keeps_tier_rank_bias(self):
        result = asyncio.run(rank_pair(hits=self.hits(), vector=[1.,0.], embedding_version="test",
            query="问题", category=None, policy=QueryRetrievalPolicy(version="test"),
            candidate_limit=20, rrf_k=60, weights={"hot":1., "warm":1., "cold":1.}))
        self.assertEqual([h.news_id for h in result["legacy_rrf"]["hits"][:3]], ["c0", "h0", "w0"])
        self.assertEqual([h.news_id for h in result["global_similarity"]["hits"]],
                         ["c"+str(i) for i in range(10)])
        self.assertEqual(result["global_similarity"]["trace"]["ranking"]["mode"], "global_similarity")
        self.assertNotIn("semantics", result["legacy_rrf"]["trace"])

    def test_invalid_weight_cannot_silently_close_tier(self):
        with self.assertRaises(ValueError):
            asyncio.run(rank_pair(hits=self.hits(), vector=[1.,0.], embedding_version="test",
                query="问题", category=None, policy=QueryRetrievalPolicy(version="test"),
                candidate_limit=20, rrf_k=60, weights={"hot":1., "warm":1., "cold":float("nan")}))

    def test_exact_pool_deduplicates_chunks_and_preserves_best_evidence(self):
        engine = SimpleNamespace(vectors=np.asarray([[1.,0.],[.9,.1],[.8,.2]],dtype=np.float32),
            chunks=[{"tier":"hot","news_id":"a","chunk_id":"a1","excerpt":"最佳"},
                    {"tier":"hot","news_id":"a","chunk_id":"a2","excerpt":"重复"},
                    {"tier":"cold","news_id":"b","chunk_id":"b1","excerpt":"历史"}],
            by_id={"a":article("a"),"b":article("b")}, model_version="test")
        result=exact_news_candidates(engine,np.asarray([1.,0.],dtype=np.float32))
        self.assertEqual(len(result[VectorTier.HOT]),1)
        self.assertEqual(result[VectorTier.HOT][0].excerpt,"最佳")


class ScoringContracts(unittest.TestCase):
    def test_missing_common_pool_label_blocks_both_aggregates(self):
        comp, _, labels = fixture()
        result = calculate_comparison(comp, labels)
        self.assertEqual(result["missing_pairs"],1)
        self.assertIsNone(result["metrics"])
        self.assertIsNone(result["per_query"][0]["metrics"])

    def test_unsure_is_not_zero(self):
        comp, articles, labels=fixture()
        labels["q","10"]={"body_sha256":articles["10"]["body_sha256"],
                           "judgment":{"status":"unable","label":"unsure"}}
        self.assertEqual(calculate_comparison(comp,labels)["missing_pairs"],1)

    def test_shared_ideal_dcg_not_separate_self_selected_pools(self):
        comp, articles, labels=fixture()
        labels["q","10"]={"body_sha256":articles["10"]["body_sha256"],
                           "judgment":{"status":"judged","label":"irrelevant"}}
        result=calculate_comparison(comp,labels)
        self.assertEqual(result["status"],"complete_single_human_pooled_ablation")
        self.assertAlmostEqual(result["metrics"]["global_similarity"]["5"]["precision"],.6)
        self.assertAlmostEqual(result["metrics"]["legacy_rrf"]["5"]["precision"],.4)
        self.assertAlmostEqual(result["metrics"]["global_similarity"]["5"]["pooled_ndcg"],1.)
        self.assertLess(result["metrics"]["legacy_rrf"]["5"]["pooled_ndcg"],1.)
        self.assertEqual(result["required_pool_pairs"],11)

    def test_no_positive_pool_does_not_invent_ndcg(self):
        comp, articles, _=fixture()
        labels={("q",nid):{"body_sha256":a["body_sha256"],
                 "judgment":{"status":"judged","label":"irrelevant"}} for nid,a in articles.items()}
        result=calculate_comparison(comp,labels)
        self.assertIsNone(result["metrics"]["legacy_rrf"]["5"]["pooled_ndcg"])
        self.assertEqual(result["metrics"]["legacy_rrf"]["5"]["ndcg_query_count"],0)


class SupplementalContracts(unittest.TestCase):
    def test_only_new_pairs_are_sent_to_blind_review(self):
        comp, articles, labels=fixture()
        tasks,pending=supplemental_runs(comp,articles,labels)
        self.assertEqual(len(pending),1)
        self.assertEqual(tasks[0]["candidates"][0]["news_id"],"10")
        for field in ("score","cosine_similarity","fusion_score","predictions","original_rank","judgment"):
            self.assertNotIn(field,tasks[0]["candidates"][0])

    def test_existing_label_requires_identical_body(self):
        comp,articles,labels=fixture()
        labels["q","0"]["body_sha256"]="changed"
        with self.assertRaises(ValueError):
            supplemental_runs(comp,articles,labels)

    def test_http_save_is_persistent_and_blind_order_has_no_quality_score(self):
        comp,articles,labels=fixture()
        tasks,_=supplemental_runs(comp,articles,labels)
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)
            (p/"comparison.json").write_text(json.dumps(comp))
            store=SupplementalStore(p/"supplemental.sqlite3")
            store.set_manifest({"comparison_sha256":sha_file(p/"comparison.json"),
                "source_count":11,"canonical_count":11,"tier_counts":{"cold":11},
                "category_counts":{"AI":11}})
            store.put_run(tasks[0])
            client=TestClient(create_app(ComparisonReviewService(p)), base_url="http://127.0.0.1:8879")
            rid=tasks[0]["run_id"]
            url="/api/runs/"+rid+"/judgments/10"
            body={"label":"irrelevant","reviewer":"human-one","notes":"核对正文",
                  "expected_revision":0}
            self.assertEqual(client.put(url,json=body).status_code,403)
            response=client.put(url,json=body,headers={"X-Lab-Request":"1"})
            self.assertEqual(response.status_code,200)
            self.assertIsNone(response.json()["metrics"]["pool_ndcg_at_k"])
            self.assertEqual(client.put(url,json=body,headers={"X-Lab-Request":"1"}).status_code,409)
            reopened=SupplementalStore(p/"supplemental.sqlite3")
            self.assertEqual(reopened.get_run(rid)["candidates"][0]["judgment"]["reviewer"],"human-one")
            extra=read_supplemental_labels(p/"supplemental.sqlite3",comp)
            self.assertEqual(extra["q","10"]["judgment"]["label"],"irrelevant")
            self.assertEqual(client.post("/api/search",json={"query":"新问题"},
                headers={"X-Lab-Request":"1"}).status_code,409)
            self.assertTrue(client.get("/api/status").json()["ready"])


class LegacyConfigurationContracts(unittest.TestCase):
    def read(self, value):
        from app.modules.retrieval_lab.comparison import read_legacy_ranking
        with tempfile.TemporaryDirectory() as temp:
            p=Path(temp)/"local.yml"
            p.write_text(value)
            return read_legacy_ranking(p)

    def test_local_flow_style_fields_have_no_hidden_defaults(self):
        got=self.read("query_rrf_k: 60\nquery_weights: {hot: 1.0, warm: 1.0, cold: 1.0}\n")
        self.assertEqual(got,{"rrf_k":60,"weights":{"hot":1.,"warm":1.,"cold":1.}})

    def test_duplicate_tier_is_rejected(self):
        with self.assertRaises(ValueError):
            self.read("query_rrf_k: 60\nquery_weights: {hot: 1, hot: 2, cold: 1}\n")

    def test_unrecognized_layout_cannot_silently_change_baseline(self):
        with self.assertRaises(ValueError):
            self.read("query_rrf_k: 60\nquery_weights:\n  hot: 1\n  warm: 1\n  cold: 1\n")


if __name__=="__main__":
    unittest.main()
