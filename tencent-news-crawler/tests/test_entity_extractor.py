from intelligence.extraction.entity_extractor import (
    entity_similarity,
    extract_entities,
)

def test_extract_same_company():
    left = extract_entities("英伟达发布新一代GPU")
    right = extract_entities("NVIDIA公布新产品")

    assert "NVIDIA" in left
    assert "NVIDIA" in right

def test_different_entities_have_zero_similarity():
    assert entity_similarity(
        {"绿的谐波"},
        {"国投电力"},
    ) == 0.0

def test_same_entity_has_full_similarity():
    assert entity_similarity(
        {"NVIDIA"},
        {"NVIDIA"},
    ) == 1.0