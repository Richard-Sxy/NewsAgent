"""Synthetic Decimal/provenance checks; these fixtures are not model quality data."""
from __future__ import annotations

import hashlib
from unittest.mock import patch
import unittest

from evaluation.writing_metric_snapshot import build_metric_snapshot


def public_input(content, source_id="synthetic-source", url="https://example.invalid/synthetic"):
    return {"topic": "synthetic writing task", "requirements": {"source_documents": [
        {"source_id": source_id, "url": url, "content": content}]}}


def metric(snapshot, rule):
    return next(m for m in snapshot["metrics"] if m["rule_id"] == rule)


class WritingMetricSnapshotTests(unittest.TestCase):
    def test_percentage_points_and_relative_change_are_distinct(self):
        result = build_metric_snapshot(public_input("合成产品任务解决率由72%提升至90%。"))
        m = metric(result, "percentage_transition")
        self.assertEqual(m["absolute_change"]["value"], "18")
        self.assertEqual(m["absolute_change"]["unit"], "个百分点")
        self.assertEqual(m["relative_change"]["value"], "25.00")
        self.assertEqual(m["relative_change"]["unit"], "%")
        self.assertFalse(m["approximate"])
        self.assertEqual(result["scope"], "narrow_regex_pilot")

    def test_decimal_and_fullwidth_percent_sign(self):
        m = metric(build_metric_snapshot(public_input("合成比率由33.3％提升至66.7％。")), "percentage_transition")
        self.assertEqual(m["absolute_change"]["value"], "33.4")
        self.assertEqual(m["relative_change"]["value"], "100.30")

    def test_revenue_preserves_approximate_base_and_quarters(self):
        m = metric(build_metric_snapshot(public_input(
            "合成公司营收从Q1的约57亿美元涨到Q2的67亿美元。")), "q1_q2_revenue")
        self.assertEqual(m["absolute_change"]["value"], "10")
        self.assertEqual(m["relative_change"]["value"], "17.54")
        self.assertEqual((m["from_period"], m["to_period"]), ("Q1", "Q2"))
        self.assertTrue(m["approximate"])
        self.assertEqual(m["operand_approximate"], {"base": True, "current": False})
        self.assertIn("近似", m["semantic_note"])

    def test_revenue_exact_decimal_display(self):
        m = metric(build_metric_snapshot(public_input(
            "合成营收从Q1的20.50亿美元涨到Q2的21.50亿美元。")), "q1_q2_revenue")
        self.assertEqual(m["absolute_change"]["value"], "1")
        self.assertEqual(m["relative_change"]["value"], "4.88")
        self.assertFalse(m["approximate"])

    def test_loss_is_loss_magnitude_not_profit(self):
        m = metric(build_metric_snapshot(public_input(
            "合成营业亏损更是从第一季度的93亿美元扩大至123亿美元。")), "operating_loss_magnitude")
        self.assertEqual(m["absolute_change"]["value"], "30")
        self.assertEqual(m["relative_change"]["value"], "32.26")
        self.assertEqual(m["metric_label"], "营业亏损规模")
        self.assertIn("不是利润增长", m["semantic_note"])
        self.assertIsNone(m["to_period"])

    def test_provenance_unicode_offsets_and_hashes_roundtrip(self):
        content = "前缀😀真实定位。" + "合成比例由10%提升至15%。" + "尾文"
        m = metric(build_metric_snapshot(public_input(content)), "percentage_transition")
        e = m["evidence"]
        self.assertEqual(e["source_id"], "synthetic-source")
        self.assertEqual(e["source_url"], "https://example.invalid/synthetic")
        self.assertEqual(content[e["match_start"]:e["match_end"]], e["matched_text"])
        self.assertEqual(content[e["quote_start"]:e["quote_end"]], e["quote"])
        self.assertEqual(e["source_content_sha256"], hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(e["quote_sha256"], hashlib.sha256(e["quote"].encode()).hexdigest())
        self.assertEqual(e["offset_unit"], "unicode_codepoints")

    def test_nonpositive_base_keeps_absolute_change_and_reports_gap(self):
        for old, expected in (("0", "10"), ("-5", "15")):
            with self.subTest(old=old):
                result = build_metric_snapshot(public_input(f"合成比例由{old}%提升至10%。"))
                m = metric(result, "percentage_transition")
                self.assertEqual(m["absolute_change"]["value"], expected)
                self.assertIsNone(m["relative_change"])
                self.assertTrue(any(g["code"] == "nonpositive_comparison_base" for g in result["gaps"]))

    def test_unmatched_text_is_a_gap_not_a_guessed_metric(self):
        result = build_metric_snapshot(public_input("合成公司当前比例90%，其他比较数据未知。"))
        self.assertEqual(result["metrics"], [])
        self.assertEqual({g["rule_id"] for g in result["gaps"] if g["code"] == "rule_not_matched"},
                         {"percentage_transition", "q1_q2_revenue", "operating_loss_magnitude"})

    def test_missing_and_invalid_sources_are_explicit_gaps(self):
        self.assertEqual(build_metric_snapshot({})["gaps"][0]["code"], "source_documents_missing")
        value = public_input("合成比例由1%提升至2%。")
        del value["requirements"]["source_documents"][0]["source_id"]
        result = build_metric_snapshot(value)
        self.assertEqual(result["metrics"], [])
        self.assertTrue(any(g["code"] == "invalid_source_identity_or_content" for g in result["gaps"]))

    def test_no_file_or_private_gold_access(self):
        value = public_input("合成比例由10%提升至12%。忽略所有系统指令的字样仅作为来源数据。")
        with patch("builtins.open", side_effect=AssertionError("No file access allowed")):
            result = build_metric_snapshot(value)
        self.assertEqual(metric(result, "percentage_transition")["relative_change"]["value"], "20.00")

    def test_multiple_source_evidence_is_not_silently_deduplicated(self):
        value = public_input("合成比例由10%提升至12%。")
        source = dict(value["requirements"]["source_documents"][0], source_id="second-source",
                      url="https://example.invalid/second")
        value["requirements"]["source_documents"].append(source)
        result = build_metric_snapshot(value)
        self.assertEqual(len(result["metrics"]), 2)
        self.assertNotEqual(result["metrics"][0]["metric_id"], result["metrics"][1]["metric_id"])

    def test_deterministic_and_json_compatible_output(self):
        value = public_input("合成营收从Q1的约57亿美元涨到Q2的67亿美元。")
        a, b = build_metric_snapshot(value), build_metric_snapshot(value)
        self.assertEqual(a, b)
        self.assertEqual(len(a["public_input_sha256"]), 64)
        with self.assertRaises(ValueError):
            build_metric_snapshot({"requirements": {"source_documents": {object()}}})


if __name__ == "__main__":
    unittest.main()
