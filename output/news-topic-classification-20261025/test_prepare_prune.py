import unittest
from prepare_prune import classify_action, target_category


def row(primary, secondary=(), flags=(), terms=()):
    return {'primary_category':primary, 'secondary_candidates':list(secondary),
            'review_flags':list(flags), 'evidence':{primary:{'title_terms':list(terms)}}}


class PrunePlanTests(unittest.TestCase):
    def test_target_primary_always_retained(self):
        self.assertEqual(classify_action(row('tech',flags=['close_topic_scores'])),'keep_target')

    def test_unknown_is_held(self):
        self.assertEqual(classify_action(row('unclassified')),'hold_unclassified')

    def test_target_secondary_is_held(self):
        self.assertEqual(classify_action(row('health',secondary=['ai'])),'hold_target_secondary')

    def test_ambiguous_non_target_is_held(self):
        self.assertEqual(classify_action(row('culture',flags=['close_topic_scores'])),'hold_close_scores')

    def test_clear_other_is_removal_candidate(self):
        self.assertEqual(classify_action(row('health',secondary=['lifestyle'])),'remove_other')

    def test_stock_and_digital_subdivision(self):
        self.assertEqual(target_category(row('finance',terms=['股价'])),'stocks')
        self.assertEqual(target_category(row('finance',terms=['ETF'])),'finance')
        self.assertEqual(target_category(row('tech',terms=['手机'])),'digital')
        self.assertEqual(target_category(row('tech',terms=['芯片'])),'tech')


if __name__=='__main__':
    unittest.main()
