"""
    这边是实体抽取，针对标题的IF-IDF很相似，但是实体不同的内容不应该做合并
    这边是标题实体的抽取，就是考虑标题语义+标题实体+正文语义+时间关联
    这边对于评测集
    TF-IDF + 时间             Precision:0.623  Recall:0.550  F1:0.584
    TF-IDF + 实体 + 时间      Precision:0.600  Recall:0.800  F1:0.686
"""
import re

ENTITY_SUFFIXES = (
    "公司",
    "集团",
    "大学",
    "学院",
    "银行",
    "委员会",
    "研究院",
    "俱乐部",
    "协会",
)

ENTITY_ALIASES = {
    "国家发改委": "国家发展和改革委员会",
    "发改委": "国家发展和改革委员会",
    "英伟达": "NVIDIA",
    "腾讯公司": "腾讯",
    "绿的谐波": "绿的谐波",
    "国投电力": "国投电力",
}

def normalize_entity(entity: str) -> str:
    """实体切割"""
    entity = entity.strip("，。！？：；、”“《》（）")
    return ENTITY_ALIASES.get(entity, entity)

def extract_entities(text: str) -> set[str]:
    """从新闻标题中提取机构、公司及英文实体。"""
    entities: set[str] = set()

    for alias, normalized in ENTITY_ALIASES.items():
        if alias in text:
            entities.add(normalized)

    # TODO 这边实现实体提取
    suffix_pattern = "|".join(
        re.escape(suffix)
        for suffix in ENTITY_SUFFIXES
    )

    chinese_pattern = re.compile(
        rf"[\u4e00-\u9fff]{{2,16}}(?:{suffix_pattern})"
    )

    english_pattern = re.compile(
        r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9.+-]{1,30}(?![A-Za-z0-9])"
    )

    entities.update(chinese_pattern.findall(text))
    entities.update(english_pattern.findall(text))

    return {
        normalize_entity(entity)
        for entity in entities
        if len(entity.strip()) >= 2
    }

def entity_similarity(
    left_entities: set[str],
    right_entities: set[str],
) -> float:
    """使用Jaccard计算实体集合相似度。"""
    if not left_entities and not right_entities:
        return 0.5

    if not left_entities or not right_entities:
        return 0.0

    intersection = left_entities & right_entities
    union = left_entities | right_entities

    return len(intersection) / len(union)
