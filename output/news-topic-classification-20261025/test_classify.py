"""Small regression checks for evidence scope and obvious category boundaries."""
import copy
import unittest

from classify import classify, contains, source_units


def record(title="", body=None, fragments=None):
    return {"news_id":"test-news", "record_sha256":"a"*64, "body_sha256":None,
            "release_id":"test-release", "title":title, "body":body,
            "fragments":[{"text":s} for s in (fragments or [])], "url":None,
            "content_state":"body_available" if body else "fragments_only" if fragments else "title_only",
            "content_type":"article", "quality_flags":[], "selection_quality_flags":[]}


class TopicInventoryTests(unittest.TestCase):
    def test_clear_topics(self):
        for title, expected in [
            ("港股互联网ETF上涨，净流入创新高", "finance"),
            ("湖人NBA新赛季：詹姆斯送出助攻", "sports"),
            ("OpenAI发布新模型，AI智能体支持编程", "ai"),
            ("比亚迪新车上市，插混SUV开启试驾", "auto"),
            ("房地产楼市：二手房价格与公积金贷款", "house"),
            ("Steam游戏促销：玩家体验新DLC", "games"),
            ("高考志愿填报与高校招生政策", "education"),
            ("台风带来暴雨，多地应急救援", "society"),
            ("新片票房刷新纪录，电影导演回应", "entertainment"),
            ("高血压患者的血压监测和治疗", "health")]:
            with self.subTest(title=title):
                self.assertEqual(classify(record(title))["primary_category"], expected)

    def test_fragment_evidence_does_not_reconstruct_body(self):
        r=record(None, fragments=["# 湖人NBA新赛季\n来源地址: https://news.qq.com/\n篮球球队开始训练"])
        original=copy.deepcopy(r)
        out=classify(r)
        self.assertEqual(r,original)
        self.assertIsNone(r['body'])
        self.assertEqual(out['display_title_source'],'fragment_heading_unverified')
        self.assertIn('fragment_evidence_only',out['review_flags'])
        self.assertIsNone(out['original_title'])

    def test_conflicting_fragment_headings_are_not_selected(self):
        r=record(None,fragments=['# A\n片段一','# B\n片段二'])
        self.assertEqual(source_units(r)[0],'')

    def test_missing_evidence_is_unclassified(self):
        self.assertEqual(classify(record('最新消息'))['primary_category'],'unclassified')

    def test_english_words_do_not_match_inside_other_words(self):
        self.assertFalse(contains('chair','AI'))
        self.assertTrue(contains('AI技术','AI'))

    def test_equal_topics_require_review(self):
        out=classify(record('教育与健康'))
        self.assertIn('close_topic_scores',out['review_flags'])

    def test_nested_terms_not_double_counted(self):
        out=classify(record('人工智能'))
        self.assertEqual(out['evidence']['ai']['title_terms'],['人工智能'])

    def test_duplicate_fragments_do_not_inflate_scores(self):
        r=record(None,fragments=['# 资讯\n湖人球员篮球NBA'])
        original=classify(r)
        r['fragments']*=4
        self.assertEqual(classify(r)['scores'],original['scores'])

    def test_source_metadata_review_survives_classification(self):
        r=record('足球比赛')
        r['selection_quality_flags']=['metadata_review_required']
        self.assertIn('source_metadata_review',classify(r)['review_flags'])

    def test_geography_does_not_override_substantive_topics(self):
        self.assertEqual(classify(record('韩国股市收涨'))['primary_category'],'finance')
        self.assertEqual(classify(record('日本新星租借加盟比甲'))['primary_category'],'sports')
        self.assertEqual(classify(record('世界动力电池大会启幕',body='美国、日本、韩国、德国的参展商参加'))['primary_category'],'auto')


if __name__=='__main__':
    unittest.main()
