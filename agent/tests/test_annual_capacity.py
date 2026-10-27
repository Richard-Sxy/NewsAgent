"""Capacity arithmetic and synthetic retention/replacement contract checks."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from evaluation.annual_capacity import CapacityProfile,capacity_plan,document_record,emit_dataset,replacement_probe,tier_at


class CapacityPlanTests(unittest.TestCase):
    def test_annual_business_upper_bound(self):
        plan=capacity_plan(CapacityProfile())
        self.assertEqual(plan["raw_articles_per_year"],2920000)
        self.assertEqual(plan["admitted_documents_upper_bound"],182500)
        self.assertEqual(plan["tier_documents"],{"hot":3500,"warm":11500,"cold":167500})
        self.assertEqual(plan["active_chunk_vectors"],912500)
        self.assertEqual(plan["float32_vector_bytes"],3737600000)
        self.assertAlmostEqual(plan["admission_ratio"],.0625)

    def test_tier_boundaries_and_expiry(self):
        p=CapacityProfile(); now=p.clock()
        for days,expected in [(0,"hot"),(6.999,"hot"),(7,"warm"),(29.999,"warm"),
                              (30,"cold"),(364.999,"cold"),(365,"expired"),(400,"expired")]:
            with self.subTest(days=days):
                self.assertEqual(tier_at(now-timedelta(days=days),now,p),expected)

    def test_future_and_naive_timestamps_fail(self):
        p=CapacityProfile(); now=p.clock()
        with self.assertRaises(ValueError):tier_at(now+timedelta(seconds=1),now,p)
        with self.assertRaises(ValueError):tier_at(now.replace(tzinfo=None),now,p)

    def test_invalid_profiles_fail(self):
        for p in [replace(CapacityProfile(),admitted_per_day=8001),
                  replace(CapacityProfile(),hot_days=30),replace(CapacityProfile(),dimensions=0),
                  replace(CapacityProfile(),replacement_probes=-1),
                  replace(CapacityProfile(),retention_days=364)]:
            with self.assertRaises(ValueError):p.validated()

    def test_detailed_version_covers_constructed_facts(self):
        p=CapacityProfile()
        old=document_record(p,20,0,superseded=True)
        new=document_record(p,20,0,detailed=True)
        probe=replacement_probe(old,new)
        self.assertTrue(set(old["facts"])<set(new["facts"]))
        self.assertEqual(old["replacement_news_id"],new["news_id"])
        self.assertEqual(probe["additional_fact_count"],2)
        self.assertIn("constructed_scenario",probe["oracle"])

    def test_different_events_cannot_be_replaced(self):
        p=CapacityProfile()
        with self.assertRaises(ValueError):
            replacement_probe(document_record(p,0,0),document_record(p,0,1,detailed=True))

    def test_expiry_prevents_serving_even_for_new_revision(self):
        p=CapacityProfile()
        self.assertEqual(document_record(p,365,0,detailed=True)["status"],"expired")
        self.assertIsNone(document_record(p,365,0,detailed=True)["target_tier"])


class MaterializedCapacityTests(unittest.TestCase):
    def profile(self):
        return CapacityProfile(raw_per_day=8,admitted_per_day=3,horizon_days=10,
            hot_days=2,warm_days=4,retention_days=10,dimensions=8,
            replacement_probes=4,expired_probe_days=2)

    def test_materialized_vectors_have_no_retired_or_expired_news(self):
        import numpy as np
        with tempfile.TemporaryDirectory() as temp:
            p=self.profile()
            out=Path(temp)/"data"
            result=emit_dataset(p,out,emit_raw=True,emit_vectors=True)
            docs=[json.loads(s) for s in (out/"synthetic-news.jsonl").read_text().splitlines()]
            chunks=[json.loads(s) for s in (out/"synthetic-chunks.jsonl").read_text().splitlines()]
            active={d["news_id"] for d in docs if d["status"]=="active"}
            nonactive={d["news_id"] for d in docs if d["status"]!="active"}
            self.assertEqual(len(active),30)
            self.assertEqual(len(docs),40)
            self.assertFalse(nonactive & {c["news_id"] for c in chunks})
            self.assertEqual(len(chunks),150)
            self.assertEqual(result["materialized_counts"]["raw_records"],80)
            self.assertEqual(result["materialized_counts"]["expired_documents"],6)
            self.assertEqual(result["materialized_counts"]["superseded_versions"],4)
            self.assertIsNone(result["quality_metrics"])
            self.assertFalse(result["production_acceptance"])
            self.assertTrue(all(d["synthetic"] for d in docs))
            for tier,info in result["vectors"].items():
                matrix=np.load(out/info["path"],allow_pickle=False)
                self.assertEqual(matrix.shape,(info["rows"],8))
                self.assertTrue(np.isfinite(matrix).all())
                self.assertTrue(np.allclose(np.linalg.norm(matrix,axis=1),1,atol=1e-5))

    def test_same_seed_produces_same_payloads(self):
        with tempfile.TemporaryDirectory() as temp:
            a=emit_dataset(self.profile(),Path(temp)/"a",emit_raw=True,emit_vectors=True)
            b=emit_dataset(self.profile(),Path(temp)/"b",emit_raw=True,emit_vectors=True)
            self.assertEqual(a["files"],b["files"])
            self.assertEqual(a["vectors"],b["vectors"])

    def test_existing_directory_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ValueError):
                emit_dataset(self.profile(),Path(temp))

    def test_standalone_validator_accepts_materialized_workload(self):
        import subprocess,sys
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/"data"
            emit_dataset(self.profile(),out,emit_raw=True,emit_vectors=True)
            target=Path(temp)/"validation.json"
            script=Path(__file__).resolve().parents[1]/"tools/verify_annual_news_capacity.py"
            result=subprocess.run([sys.executable,"-B",str(script),"--dataset",str(out),"--output",str(target)],
                capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(target.read_text())
            self.assertEqual(report["active_documents"],30)
            self.assertEqual(report["vector_rows_verified"],150)

    def test_standalone_validator_rejects_expired_chunk_membership(self):
        import subprocess,sys
        from evaluation.annual_capacity import sha_file
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/"data"
            emit_dataset(self.profile(),out)
            path=out/"synthetic-chunks.jsonl"
            with path.open("a") as stream:
                stream.write(json.dumps({"synthetic":True,"news_id":document_record(self.profile(),10,0)["news_id"],
                    "chunk_index":0,"tier":"cold"})+"\n")
            manifest=json.loads((out/"manifest.json").read_text())
            manifest["files"][path.name]={"sha256":sha_file(path),"bytes":path.stat().st_size}
            (out/"manifest.json").write_text(json.dumps(manifest))
            script=Path(__file__).resolve().parents[1]/"tools/verify_annual_news_capacity.py"
            result=subprocess.run([sys.executable,"-B",str(script),"--dataset",str(out),
                "--output",str(Path(temp)/"validation.json")],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn("retired or expired news leaked",result.stderr)



if __name__=="__main__":
    unittest.main()
