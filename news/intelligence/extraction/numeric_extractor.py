"""实体关联中加入关键数字提取"""
import re

NUMBER_PATTERN = re.compile(
    r"\d+(?:\.\d+)?"
    r"(?:亿元|万元|美元|公里|米|岁|人|名|分|秒|%|强)"
)

def extract_key_numbers(text: str) -> set[str]:
    """提取带业务单位的关键数字。"""
    return set(NUMBER_PATTERN.findall(text))

def numeric_similarity(
    left_numbers: set[str],
    right_numbers: set[str],
) -> float:
    """同样是匹配后的数字相似度去做近似度计算"""
    if not left_numbers and not right_numbers:
        return 0.5

    if not left_numbers or not right_numbers:
        return 0.0

    intersection = left_numbers & right_numbers
    union = left_numbers | right_numbers

    return len(intersection) / len(union)
