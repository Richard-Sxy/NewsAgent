"""Synthetic offline tests only; never read actual evaluation drafts."""
from __future__ import annotations
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch
from evaluation import audit_writing_batch_outputs as audit

DOCS = [{"source_id": "S1", "url": "https://example.invalid/a"},
        {"source_id": "S-2", "url": "https://example.invalid/b"}]

def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")

def fixture(root):
    run = Path(root) / "synthetic-run"
    run.mkdir()
    rows, requests, states = [], [], []
    for i in range(100):
        row = {"case_id": "C"+str(i), "event_group_id": "E"+str(i//2),
               "input": {"requirements": {"source_documents": DOCS}}}
        rows.append(row)
        for arm in ("baseline", "candidate"):
            req = {"case_id": row["case_id"], "event_group_id": row["event_group_id"],
                   "arm": arm, "request_id": "R"+str(i)+"-"+arm}
            requests.append(req)
            states.append({**req, "status": "not_run"})
    plan = {"planned_tasks": 100, "planned_requests": 200, "planned_event_groups": 50,
            "requests": requests, "source_sha256": "a"*64, "run_id": "synthetic-run"}
    put(run/"plan.json", plan)
    return run, plan, rows, {"requests": states}

def artifact(run, plan, execution, index=0, arm="baseline", state="completed", text=None, finish="stop"):
    req = next(r for r in plan["requests"] if r["case_id"] == "C"+str(index) and r["arm"] == arm)
    next(r for r in execution["requests"] if r["request_id"] == req["request_id"])["status"] = state
    folder = audit.case_dir(run, req)
    folder.mkdir(parents=True, exist_ok=True)
    if state not in ("completed", "failed"):
        put(folder/"start.json", {"synthetic": True})
        return folder
    meta = {"status": state}
    if text is not None:
        name = "draft.md" if state == "completed" else "draft.partial.md"
        (folder/name).write_text(text, encoding="utf-8")
        meta["draft_sha256" if state == "completed" else "partial_draft_sha256"] = audit.file_sha(folder/name)
    put(folder/"response.json", {"choices": [{"finish_reason": finish}]})
    meta["response_sha256"] = audit.file_sha(folder/"response.json")
    put(folder/"metadata.json", meta)
    return folder

class OutputAuditTests(unittest.TestCase):
    def checks(self, plan, rows, execution):
        stack = ExitStack()
        stack.enter_context(patch.object(audit, "verify", return_value=(plan, rows)))
        stack.enter_context(patch.object(audit, "status", return_value=execution))
        return stack

    def test_exact_ids_urls_unknown_id_precedence(self):
        result = audit.analyze_text("[S1] https://example.invalid/b.", DOCS)
        self.assertEqual(result["citation_status"], "valid")
        self.assertEqual(result["matched_source_ids"], ["S1"])
        self.assertEqual(result["matched_source_urls"], ["https://example.invalid/b"])
        result = audit.analyze_text("[S1] [S-999] https://example.invalid/other", DOCS)
        self.assertEqual(result["citation_status"], "unknown_source_id")
        self.assertTrue(result["explicit_known_source_match"])
        self.assertEqual(result["unknown_source_ids"], ["S-999"])
        self.assertEqual(result["unknown_urls"], ["https://example.invalid/other"])
        self.assertEqual(audit.analyze_text("S1-extra https://example.invalid/a.evil", DOCS)["citation_status"], "missing")
        self.assertEqual(audit.analyze_text("An institution reported this.", DOCS)["citation_status"], "missing")

    def test_unicode_length_markdown_title_urls_ids_whitespace(self):
        text = "# "+"\u7532"*250+"\n[S1](https://example.invalid/a)\n"
        self.assertEqual(audit.count_length(text, {"S1", "S-2"}), 250)
        self.assertEqual(audit.count_length("# AB\n- C\n1. D\n---\n[S-999]", {"S1"}), 4)
        self.assertEqual(audit.count_length("\U0001f600 A\u0301\u3000", set()), 3)
        self.assertEqual(audit.count_length("A-2", set()), 3)

    def test_inclusive_bounds_and_reference_marker(self):
        for size, expected in ((249, False), (250, True), (350, True), (351, False)):
            self.assertIs(audit.analyze_text("\u7532"*size, DOCS)["length_within_250_350"], expected)
        self.assertTrue(audit.analyze_text("(@ref) (@ref:S1)", DOCS)["residual_ref_marker"])
        self.assertFalse(audit.analyze_text("@ref outside parentheses", DOCS)["residual_ref_marker"])

    def test_all_states_fixed_denominators_and_no_semantic_labels(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan, rows, execution = fixture(root)
            artifact(run, plan, execution, text="\u7532"*250+" [S1]")
            artifact(run, plan, execution, arm="candidate", state="failed", finish="length", text="partial [S-999] (@ref)")
            artifact(run, plan, execution, index=1, state="unknown")
            with self.checks(plan, rows, execution):
                report = audit.collect(run)
            self.assertEqual(len(report["records"]), 200)
            a, b = [report["summary_by_arm"][arm] for arm in ("baseline", "candidate")]
            self.assertEqual(a["generation_states"], {"completed": 1, "failed": 0, "unknown": 1, "not_run": 98})
            self.assertEqual(b["generation_states"], {"completed": 0, "failed": 1, "unknown": 0, "not_run": 99})
            for item in (a, b):
                self.assertEqual(item["planned_denominator"], 100)
                self.assertEqual(sum(item["generation_states"].values()), 100)
                self.assertEqual(sum(item["citation_status"].values()), 100)
                for count in item["signals"].values():
                    self.assertEqual(count["true"]+count["false"]+count["unobserved"], 100)
            failed, unknown = report["records"][1:3]
            self.assertEqual(failed["draft_kind"], "failed_partial_draft")
            self.assertIs(failed["signals"]["finish_stop"], False)
            self.assertTrue(failed["draft_sha256"])
            self.assertIsNone(unknown["draft_sha256"])
            self.assertIsNone(unknown["signals"]["length_within_250_350"])
            self.assertFalse(any("critical_error" in item for item in report["records"]))
            self.assertEqual(report["human_quality_scoring"], "not_performed")

    def test_missing_partial_signals_stay_unobserved(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan, rows, execution = fixture(root)
            artifact(run, plan, execution, state="failed", finish=None)
            with self.checks(plan, rows, execution):
                record = audit.collect(run)["records"][0]
            self.assertIsNone(record["draft_sha256"])
            self.assertIsNone(record["signals"]["finish_stop"])
            self.assertEqual(record["signals"]["citation_status"], "unobserved")
            self.assertTrue(record["signals"]["terminal"])

    def test_tamper_blocks_before_output(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan, rows, execution = fixture(root)
            folder = artifact(run, plan, execution, text="synthetic [S1]")
            (folder/"draft.md").write_text("tamper", encoding="utf-8")
            out = Path(root)/"audit"
            with self.checks(plan, rows, execution), self.assertRaisesRegex(ValueError, "SHA"):
                audit.audit(run, out)
            self.assertFalse(out.exists())

    def test_exports_sealed_json_markdown_and_never_changes_run(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan, rows, execution = fixture(root)
            artifact(run, plan, execution, text="\u7532"*250+" [S1]")
            before = {str(p.relative_to(run)): p.read_bytes() for p in run.rglob("*") if p.is_file()}
            out = Path(root)/"audit"
            with self.checks(plan, rows, execution):
                result = audit.audit(run, out)
            self.assertEqual(result["planned_requests"], 200)
            report, seal = audit.read(out/"audit.json"), audit.read(out/"audit-seal.json")
            self.assertEqual(seal["audit_json_sha256"], audit.file_sha(out/"audit.json"))
            self.assertEqual(seal["audit_md_sha256"], audit.file_sha(out/"audit.md"))
            self.assertEqual(report["algorithm_sha256"], hashlib.sha256(audit.canonical(audit.ALGORITHM)).hexdigest())
            self.assertEqual(report["tool_source_sha256"], audit.file_sha(audit.__file__))
            self.assertIn("not factual correctness", (out/"audit.md").read_text())
            self.assertEqual(before, {str(p.relative_to(run)): p.read_bytes() for p in run.rglob("*") if p.is_file()})
            with self.assertRaisesRegex(ValueError, "output exists"):
                audit.audit(run, out)

    def test_rejects_small_denominator_or_missing_status(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan, rows, execution = fixture(root)
            plan["planned_tasks"] = 2
            with self.checks(plan, rows, execution), self.assertRaisesRegex(ValueError, "100-task"):
                audit.collect(run)
            plan["planned_tasks"] = 100
            execution["requests"].pop()
            with self.checks(plan, rows, execution), self.assertRaisesRegex(ValueError, "all 200"):
                audit.collect(run)

    def test_dataset_override_uses_portable_verifier_only(self):
        with tempfile.TemporaryDirectory() as root:
            run, plan, rows, execution = fixture(root)
            module = ModuleType("evaluation.verify_writing_run_portable")
            module.verify_portable = Mock(return_value=(plan, rows))
            module.status_portable = Mock(return_value=execution)
            dataset = Path(root)/"relocated"
            with patch.dict(sys.modules, {"evaluation.verify_writing_run_portable": module}), patch.object(audit, "verify", side_effect=AssertionError("absolute path used")), patch.object(audit, "status", side_effect=AssertionError("absolute status used")):
                report = audit.collect(run, dataset)
            self.assertEqual(report["verification_mode"], "portable_read_only")
            module.verify_portable.assert_called_once_with(run.resolve(), dataset.resolve())
            module.status_portable.assert_called_once_with(run.resolve(), dataset.resolve())

    def test_import_has_no_gpu_dependency(self):
        code = (
            "import builtins;original=builtins.__import__;"
            "exec('def guarded(name,*a,**k):\\n if name.split(\\\".\\\")[0] in (\\\"torch\\\",\\\"transformers\\\",\\\"assets\\\"): raise AssertionError(name)\\n return original(name,*a,**k)');"
            "builtins.__import__=guarded;from evaluation import audit_writing_batch_outputs"
        )
        subprocess.run([sys.executable, "-B", "-c", code], check=True, capture_output=True,
                       cwd=Path(__file__).resolve().parents[1])

if __name__ == "__main__":
    unittest.main()
