"""Boundary tests for annual corpus preparation and review-task generation."""
import copy
from datetime import date, timedelta
from hashlib import sha256
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent" / "tools"))
import prepare_annual_retrieval_lab as preparation

AS_OF = date(2026, 10, 27)


def record(identity="a", publication="2026-10-20", body=None, category="科技", event_id="event-1", published_at=None, precision="day"):
    body = body or ("这是新闻正文及明确事实。" + identity)
    return {"article_id": identity, "title": "新闻标题" + identity, "body": body,
            "url": "https://example.com/" + identity, "publication_date": publication,
            "published_at": published_at or publication, "date_precision": precision,
            "categories": [category], "event_ids": [event_id],
            "source_evidence": {"body_sha256": sha256(body.encode("utf-8")).hexdigest(),
                                "parsed_sha256": "a" * 64, "raw_sha256": "b" * 64},
            "relevance_label": "MUST_NOT_COPY", "fact_group_id": "unreviewed-upstream"}


def group(identity="event-1", category="科技", kind="event"):
    return {"event_id": identity, "title": "测试事件", "category": category,
            "discovery_group_type": kind, "membership_status": "pending_human"}


def prepare(records, groups=None):
    return preparation.prepare_records(records, groups or {"event-1": group()}, as_of=AS_OF)


class AnnualPreparationTests(unittest.TestCase):
    def test_calendar_day_tier_boundaries_are_explicit(self):
        for days, expected in [(0, "hot"), (7, "hot"), (8, "warm"), (90, "warm"), (91, "cold"), (365, "cold")]:
            with self.subTest(days=days):
                self.assertEqual(preparation.tier_for((AS_OF - timedelta(days=days)).isoformat(), AS_OF), expected)
        with self.assertRaisesRegex(ValueError, "future"):
            preparation.tier_for("2026-10-28", AS_OF)

    def test_day_precision_does_not_invent_midnight_or_timezone(self):
        article = prepare([record()])[0][0]
        self.assertIsNone(article["publish_time"])
        self.assertEqual(article["published_at_original"], "2026-10-20")
        self.assertEqual(article["date_precision"], "day")
        self.assertEqual(article["publication_date"], "2026-10-20")

    def test_naive_clock_time_does_not_invent_timezone(self):
        article = prepare([record(published_at="2026-10-20T08:30:00", precision="second")])[0][0]
        self.assertIsNone(article["publish_time"])
        self.assertEqual(article["published_at_original"], "2026-10-20T08:30:00")

    def test_explicit_aware_timestamp_is_preserved(self):
        article = prepare([record(published_at="2026-10-20T08:30:00+08:00", precision="second")])[0][0]
        self.assertEqual(article["publish_time"], "2026-10-20T08:30:00+08:00")

    def test_identical_body_collapses_only_retrieval_identity_and_keeps_sources(self):
        older = record("z", "2026-10-19", body="相同正文", category="科技", event_id="event-1")
        newer = record("a", "2026-10-20", body="相同正文", category="AI", event_id="event-2")
        articles, aliases, _, _, _ = prepare([newer, older])
        self.assertEqual(len(articles), 1)
        article = articles[0]
        self.assertEqual(article["news_id"], "pub-z")
        self.assertEqual(article["publication_date"], "2026-10-19")
        self.assertEqual(article["categories"], ["AI", "科技"])
        self.assertEqual(article["event_ids"], ["event-1", "event-2"])
        self.assertEqual(article["duplicate_sources"][0]["news_id"], "pub-a")
        self.assertEqual(article["duplicate_sources"][0]["url"], newer["url"])
        self.assertEqual(aliases, {"pub-a": "pub-z", "pub-z": "pub-z"})
        self.assertEqual(article["dedup_basis"], "byte_identical_body_only")
        self.assertEqual(article["source_evidence"], older["source_evidence"])

    def test_similar_title_and_shared_event_with_new_facts_are_not_collapsed(self):
        first, second = record("a"), record("b", body="此后新增具体事实")
        second["title"] = first["title"]
        articles, aliases, _, _, _ = prepare([first, second])
        self.assertEqual(len(articles), 2)
        self.assertEqual(set(aliases.values()), {"pub-a", "pub-b"})

    def test_future_duplicate_source_cannot_escape_snapshot_date_guard(self):
        older = record("a", "2026-10-20", body="相同正文")
        future = record("b", "2026-10-28", body="相同正文")
        with self.assertRaisesRegex(ValueError, "future"):
            prepare([older, future])

    def test_future_unique_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "future"):
            prepare([record("a", "2026-10-28")])

    def test_chronological_edges_are_unreviewed_noncausal_and_not_rank_boosts(self):
        records = [record("later", "2026-10-25"), record("earlier", "2026-10-20")]
        articles, _, chains, edges, _ = prepare(records)
        self.assertEqual([n["news_id"] for n in chains[0]["nodes"]], ["pub-earlier", "pub-later"])
        edge = edges[0]
        self.assertEqual(edge["relation"], "chronological_next_candidate")
        self.assertEqual(edge["status"], "unreviewed_candidate")
        self.assertFalse(edge["causal_claim"])
        self.assertFalse(edge["affects_ranking"])
        self.assertEqual(chains[0]["status"], "discovery_membership_unreviewed")
        by_id = {r["news_id"]: r for r in articles}
        for evidence in edge["evidence"]:
            self.assertIn(evidence["quote"], by_id[evidence["news_id"]]["body"])
            self.assertEqual(evidence["body_sha256"], by_id[evidence["news_id"]]["body_sha256"])
        self.assertEqual(by_id["pub-earlier"]["relations"][0]["related_news_id"], "pub-later")

    def test_same_day_ties_do_not_claim_temporal_order(self):
        _, _, _, edges, _ = prepare([record("a"), record("b")])
        self.assertEqual(edges[0]["relation"], "same_day_candidate")
        self.assertFalse(edges[0]["causal_claim"])

    def test_topic_collections_do_not_become_event_chains(self):
        _, _, chains, edges, queries = prepare([record("a"), record("b")], {"event-1": group(kind="topic_collection")})
        self.assertEqual(chains, [])
        self.assertEqual(edges, [])
        self.assertEqual(len(queries), 1)

    def test_query_tasks_cover_all_available_ten_categories_without_gold(self):
        groups, records = {}, []
        for index, category in enumerate(preparation.CATEGORIES):
            for event_index in range(4):
                eid = "event-{}-{}".format(index, event_index)
                groups[eid] = group(eid, category)
                records.append(record(eid, category=category, event_id=eid))
        articles, _, _, _, queries = prepare(records, groups)
        self.assertEqual(len(queries), 30)
        self.assertEqual({q["category"] for q in queries}, set(preparation.CATEGORIES))
        self.assertEqual(len({q["query_id"] for q in queries}), 30)
        for query in queries:
            self.assertIsNone(query["relevance_labels"])
            self.assertEqual(query["query_source"], "discovery_based_task_draft")
            self.assertEqual(query["limit"], 10)
            self.assertNotIn("relevant_news_ids", query)
        for article in articles:
            self.assertNotIn("relevance_label", article)
            self.assertNotIn("fact_group_id", article)
            self.assertNotIn("judgment", article)

    def test_event_hint_with_no_body_does_not_generate_empty_review_task(self):
        groups = {"event-1": group(), "empty": group("empty", "AI")}
        queries = prepare([record()], groups)[4]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["event_id_hint"], "event-1")

    def test_preparation_is_deterministic_and_does_not_mutate_source_records(self):
        records = [record("b"), record("a")]
        before = copy.deepcopy(records)
        first = prepare(records)
        second = prepare(list(reversed(records)))
        self.assertEqual(first, second)
        self.assertEqual(records, before)

    def test_existing_output_is_preserved_before_input_or_db_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            sentinel = output / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            argv = ["prepare", "--corpus-dir", "missing-corpus", "--discovery", "missing-discovery", "--output-dir", str(output)]
            with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as caught:
                preparation.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
