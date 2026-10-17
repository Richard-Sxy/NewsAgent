"""Guard the authored August sample against answer hints and split leakage."""
from collections import Counter
import json
from pathlib import Path
import unittest

from evaluation.data_writing_dataset import load_seed, render_dataset, validate_seed


SEED = Path(__file__).resolve().parents[1] / 'evaluation/datasets/data_writing_august_2026_v1/seed.json'


class AugustDataWritingSampleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seed = validate_seed(load_seed(SEED))

    def test_coverage_is_event_grouped_and_balanced_by_stage(self):
        cases = self.seed['cases']
        self.assertEqual(len(cases), 27)
        groups = {c['event_group_id'] for c in cases}
        self.assertEqual(len(groups), 9)
        for group in groups:
            selected = [c for c in cases if c['event_group_id'] == group]
            self.assertEqual({c['task'] for c in selected}, {'research', 'write', 'review'})
            self.assertEqual(len({c['split'] for c in selected}), 1)
        self.assertEqual(Counter(c['domain'] for c in cases), {'finance': 12, 'ai': 12, 'technology': 3})

    def test_review_request_cannot_reveal_approve_or_revise(self):
        reviews = [c for c in self.seed['cases'] if c['task'] == 'review']
        self.assertEqual(len({c['input']['request'] for c in reviews}), 1)
        self.assertNotIn('错误稿', reviews[0]['input']['request'])
        self.assertNotIn('无错', reviews[0]['input']['request'])
        for split in ('development', 'validation', 'test'):
            selected = [c for c in reviews if c['split'] == split]
            self.assertEqual(Counter(c['expected']['action'] for c in selected), {'approve': 1, 'revise': 2})

    def test_derived_openai_answer_is_not_in_the_request(self):
        case = next(c for c in self.seed['cases'] if c['case_id'] == 'E07-OPENAI-BUSINESS-WRITE')
        self.assertNotIn('17.54', case['input']['request'])
        numeric = {c['check_id']: c for c in case['expected']['numeric_checks']}
        self.assertEqual(numeric['REV-REL']['expected_value'], '17.54')

    def test_inputs_export_does_not_contain_private_hints(self):
        artifacts = render_dataset(self.seed)
        records = [json.loads(line) for line in artifacts['inputs.jsonl'].splitlines()]
        self.assertEqual(len(records), 27)
        for record in records:
            self.assertNotIn('expected', record)
            self.assertNotIn('variant', record)
            self.assertNotIn('required_caveats', record['input'])
            self.assertNotIn('manual_rubric', record['input'])
        self.assertEqual(json.loads(artifacts['manifest.json'])['numeric_check_count'], 18)

    def test_sources_are_cache_only_and_do_not_claim_real_hotness(self):
        self.assertIs(self.seed['hotness_verified'], False)
        self.assertEqual(len(self.seed['sources']), 11)
        for source in self.seed['sources']:
            self.assertEqual(source['verification_status'], 'cache_only')
            self.assertTrue(source['published_at'].startswith('2026-08-'))
            self.assertTrue(source['fetched_at'].startswith('2026-10-'))
        for case in self.seed['cases']:
            self.assertNotIn('metric_fixture', case['input'])
            self.assertTrue(all(n['provenance'] == 'reported' for n in case['input']['numeric_inputs'].values()))

    def test_missing_publication_facts_require_evidence_instead_of_invention(self):
        case = next(c for c in self.seed['cases'] if c['case_id'] == 'E08-XIAOMI-PROTOTYPE-RESEARCH')
        self.assertEqual(case['expected']['action'], 'request_evidence')
        self.assertIn('MISSING_PRICE_AND_SCHEDULE', case['expected']['required_caveats'])


if __name__ == '__main__':
    unittest.main()
