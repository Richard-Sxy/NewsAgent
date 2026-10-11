from intelligence.extraction.numeric_extractor import (
    extract_key_numbers,
    numeric_similarity,
)


def test_extract_sports_distance():
    assert extract_key_numbers(
        "潘展乐夺得100米自由泳冠军"
    ) == {"100米"}

 
def test_different_distances_have_zero_similarity():
    left = extract_key_numbers("100米自由泳夺冠")
    right = extract_key_numbers("50米自由泳夺冠")

    assert numeric_similarity(left, right) == 0.0


def test_same_amount_has_full_similarity():
    left = extract_key_numbers("公司融资10亿元")
    right = extract_key_numbers("融资金额达到10亿元")

    assert numeric_similarity(left, right) == 1.0