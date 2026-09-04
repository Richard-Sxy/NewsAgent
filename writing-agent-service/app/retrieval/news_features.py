from dataclasses import dataclass
import re

@dataclass(frozen=True, slots=True)
class NewsFeatures:
    entities: frozenset[str]
    keywords: frozenset[str]
    numbers: frozenset[str]
    event_type: str | None

COMPANY_WORDS = (
    "华为",
    "腾讯",
    "阿里巴巴",
    "英伟达",
    "苹果",
    "比亚迪",
    "国泰海通",
    "小米",
)

EVENT_TYPE_RULES = {
    "financial_report": (
        "营收",
        "净利润",
        "财报",
        "半年报",
        "年报",
        "经营业绩",
        "同比",
    ),
    "product_release": (
        "发布",
        "上市",
        "新品",
        "发布会",
    ),
    "investment": (
        "投资",
        "融资",
        "募资",
        "收购",
    ),
}

def extract_news_features(title: str, text: str = "") -> NewsFeatures:
    """提取新闻实体、关键词、数字、事件类型"""
    content = f"{title} {text}"
    entities = frozenset(
        company for company in COMPANY_WORDS if company in content
    )
    numbers = frozenset(
        re.findall(r"\d+(?:\.\d+)?%?亿元|\d+(?:\.\d+)?%|\d+", content)
    )
    event_type = detect_event_type(content)
    keywords = extract_keywords(title)

    return NewsFeatures(
        entities=entities,
        keywords=keywords,
        numbers=numbers,
        event_type=event_type,
    )

def detect_event_type(content: str) -> str | None:
    """根据关键词识别新闻类型。"""
    for event_type, words in EVENT_TYPE_RULES.items():
        if any(word in content for word in words):
            return event_type
    return None

def extract_keywords(title: str) -> frozenset[str]:
    """这边实现的是不依赖分词模型的简单关键词提取器。"""
    words = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9]+", title)

    # TODO继续补充新闻标题中的无意义词
    stop_words = {
        "最新",
        "消息",
        "表示",
        "目前",
        "已经",
        "正在",
        "如何",
        "哪些",
    }

    return frozenset(
        word.lower()
        for word in words
        if word not in stop_words
    )
