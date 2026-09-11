from types import SimpleNamespace

from intelligence.title_nlp import LTPTitleAnalyzer


class FakeLTP:
    def __init__(self):
        self.custom_words = []

    def add_words(self, words, freq):
        self.custom_words.extend(words)

    def pipeline(self, titles, tasks):
        assert tasks == ["cws", "pos", "ner"]
        assert titles == ["雷军宣布小米集团与英伟达合作推出澎湃OS新品"]
        return SimpleNamespace(
            cws=[["雷军", "宣布", "小米", "集团", "与", "英伟达", "合作", "推出", "澎湃", "OS", "新品"]],
            pos=[["nh", "v", "n", "n", "p", "nz", "v", "v", "nz", "n", "n"]],
            ner=[[("Nh", "雷军", 0, 0), ("Ni", "集团", 3, 3)]],
        )


def test_ltp_title_analyzer_extracts_and_normalizes_entities(tmp_path):
    lexicon_path = tmp_path / "lexicon.json"
    lexicon_path.write_text(
        """{
          "entities": [
            {"canonical": "小米集团", "type": "organization", "aliases": ["小米"]},
            {"canonical": "NVIDIA", "type": "organization", "aliases": ["英伟达"]}
          ]
        }""",
        encoding="utf-8",
    )
    fake_model = FakeLTP()
    analyzer = LTPTitleAnalyzer(
        model_name="LTP/tiny",
        lexicon_path=str(lexicon_path),
        model=fake_model,
    )

    result = analyzer.analyze("雷军宣布小米集团与英伟达合作推出澎湃OS新品")

    assert [token.text for token in result.tokens] == [
        "雷军", "宣布", "小米", "集团", "与", "英伟达", "合作", "推出", "澎湃", "OS", "新品"
    ]
    entities = {
        (entity.normalized_text, entity.entity_type, entity.source)
        for entity in result.entities
    }
    assert ("雷军", "person", "ner") in entities
    assert ("小米集团", "organization", "lexicon") in entities
    assert ("NVIDIA", "organization", "lexicon") in entities
    assert ("澎湃", "proper_noun", "pos") in entities
    assert ("OS", "proper_noun", "pos") in entities
    assert "英伟达" in fake_model.custom_words


def test_fastgpt_metadata_groups_entities_by_type(tmp_path):
    lexicon_path = tmp_path / "lexicon.json"
    lexicon_path.write_text('{"entities": []}', encoding="utf-8")
    result = LTPTitleAnalyzer(
        lexicon_path=str(lexicon_path),
        model=FakeLTP(),
    ).analyze("雷军宣布小米集团与英伟达合作推出澎湃OS新品")

    metadata = result.to_fastgpt_metadata()

    assert metadata["title_persons"] == ["雷军"]
    assert "小米集团" in metadata["title_organizations"]
    assert "澎湃" in metadata["title_proper_nouns"]
    assert metadata["title_nlp_extractor"] == "ltp"
