"""把查询意图转换为 SQL。validate_intent() 检查指标、栏目、数量是否允许。 compile_query() 生成参数化查询。也包含共本地测试使用的确定意图。"""

import re

from app.schemas.sql_assistant import SqlAssistantIntent
from app.sql_assistant.input_boundary import SqlAssistantQuestionError, screen_question
from app.sql_assistant.scenarios import SqlScenario

"""加载的字符"""
_LOCAL_WORDS = (
    "查询", "查一下", "查看", "请", "帮我", "我想", "看看", "展示", "统计", "了解", "取",
    "按照", "按", "排序", "排行", "排名", "指标", "的", "中", "所有", "全部", "只", "仅",
    "新闻", "热点", "图文", "视频", "科技", "财经", "体育", "社会", "article", "video",
    "点击率", "ctr", "点击量", "点击", "曝光量", "曝光", "互动量", "互动", "热度分", "热度",
    "每小时", "逐小时", "趋势", "前", "top", "最高", "最多", "最低", "最少",
    "从低到高", "从高到低", "降序", "升序", "条", "篇", "个",
)
_LOCAL_WORD_PATTERN = "|".join(re.escape(word) for word in sorted(_LOCAL_WORDS, key=len, reverse=True))


def _validate_local_vocabulary(question: str) -> None:
    # The local Port is a bounded simulator, not an LLM. Reject leftover words
    # rather than silently dropping an unknown source/category/filter condition.
    # A finite presentation request adds no warehouse filter or metric. Keep
    # rejecting every other unknown term; this is still not language inference.
    query_text = re.sub(r"(?:并|，|,)?分析(?:热点)?原因[。！!]?\s*$", "", question)
    remainder = re.sub(_LOCAL_WORD_PATTERN, "", query_text, flags=re.I)
    remainder = re.sub(r"[\d\s，。！？、：；,.?!:;]+", "", remainder)
    if remainder:
        raise SqlAssistantQuestionError("本地 v1 无法完整理解该问题，请按场景示例填写；未知栏目、来源或额外条件不会被忽略")


def local_query_intent(payload: dict) -> SqlAssistantIntent:
    """Deliberately limited, reproducible simulation; unsupported questions fail."""
    question = screen_question(payload["question"])
    scenario = SqlScenario.model_validate(payload["scenario"])
    if not re.search(r"新闻|图文|视频|热点|科技|财经|体育|社会|点击|曝光|互动|热度|趋势", question):
        raise SqlAssistantQuestionError("本地模拟支持新闻排行与小时趋势，请使用页面中的示例问题")
    if re.search(r"收入|销售额|订单|利润|用户画像|地区|城市|性别|年龄|正文|作者|转发|评论|点赞|阅读时长", question):
        raise SqlAssistantQuestionError("该问题包含 v1 聚合视图未提供的指标或维度")
    if re.search(r"标题|关键词|来源|包含|匹配|发布|发布时间|\bsource\b", question, re.I):
        raise SqlAssistantQuestionError("本地 v1 只支持内容类型和栏目过滤，不支持标题、来源或发布时间过滤")
    if re.search(r"今天|昨日|昨天|本周|上周|本月|上月|最近|过去|近\s*\d+|\d+\s*(?:小时|天|分钟)|\d{4}[-年/]", question):
        raise SqlAssistantQuestionError("本地 v1 请在页面明确选择时间范围，问题中不要另设相对时间或日期")
    if re.search(r"超过|大于|小于|不少于|不低于|不超过|低于|至少|至多|排除|不含|除了|对比|同比|环比|增长|平均|去重|人数|用户数|中位|分位|占比", question):
        raise SqlAssistantQuestionError("本地 v1 暂不支持阈值过滤、排除、对比或其他统计口径，请使用排行/小时趋势模板")
    if re.search(r"(?:前|最高的?|最多的?|最低的?)\s*[一二三四五六七八九十百千两]+", question):
        raise SqlAssistantQuestionError("本地 v1 请使用阿拉伯数字表示行数，例如前5条")
    _validate_local_vocabulary(question)
    metric_words = re.findall(r"点击率|ctr|互动|曝光|热度|点击", question, re.I)
    metrics = { {"点击率": "ctr", "ctr": "ctr", "互动": "interactions", "曝光": "impressions", "热度": "hot_score", "点击": "clicks"}[word.lower()] for word in metric_words }
    if len(metrics) > 1:
        raise SqlAssistantQuestionError("请每次指定一个排行或趋势指标，多个指标排序暂不支持")
    metric = next(iter(metrics), scenario.default_sort)
    if metric not in scenario.allowed_sort_metrics:
        raise SqlAssistantQuestionError("该指标不在当前场景白名单，请选择对应场景")
    has_video = bool(re.search(r"视频|(?<![A-Za-z])video(?![A-Za-z])", question, re.I))
    has_article = bool(re.search(r"图文|(?<![A-Za-z])article(?![A-Za-z])", question, re.I))
    content_type = "video" if has_video else "article" if has_article else None
    categories = [item for item in ("科技", "财经", "体育", "社会") if item in question]
    if len(categories) > 1 or (has_video and has_article):
        raise SqlAssistantQuestionError("本地 v1 每次支持一种内容类型和一个栏目，请拆分查询")
    category = categories[0] if categories else None
    matches = list(re.finditer(r"(?:前|top\s*|最高的?|最多的?|最低的?|最少的?)\s*(\d+)\s*(?:条|篇|个)?", question, re.I))
    if len({int(match.group(1)) for match in matches}) > 1:
        raise SqlAssistantQuestionError("问题指定了不同的返回行数，请保留一个明确的数量")
    remaining = question
    for item in reversed(matches):
        remaining = remaining[:item.start()] + remaining[item.end():]
    if re.search(r"\d", remaining):
        raise SqlAssistantQuestionError("问题包含无法归属的数值，请只指定一个整数行数")
    match = matches[0] if matches else None
    if re.search(r"所有|全部", question) and match is None:
        raise SqlAssistantQuestionError("查询必须限定返回行数，请将全部改为前N条")
    limit = int(match.group(1)) if match else scenario.default_limit
    has_trend = bool(re.search(r"趋势|每小时|逐小时", question))
    if scenario.result_mode == "trend" and not has_trend:
        raise SqlAssistantQuestionError("当前为小时趋势场景，请描述每小时趋势，或切换新闻排行场景")
    if has_trend:
        if scenario.result_mode != "trend":
            raise SqlAssistantQuestionError("请切换到小时趋势场景")
        if match or re.search(r"排行|最高|最低|最多|最少|升序|降序|从低到高|从高到低", question):
            raise SqlAssistantQuestionError("小时趋势固定按时间顺序返回，不支持新闻排行或前N条")
        limit = scenario.max_limit
    if not 1 <= limit <= scenario.max_limit:
        raise SqlAssistantQuestionError(f"当前场景最多返回 {scenario.max_limit} 行")
    ascending = bool(re.search(r"最低|最少|升序|从低到高", question))
    descending = bool(re.search(r"最高|最多|降序|从高到低", question))
    if ascending and descending:
        raise SqlAssistantQuestionError("问题指定了冲突的排序方向，请保留升序或降序之一")
    direction = "asc" if ascending else "desc"
    intent = SqlAssistantIntent(
        sort_by=metric, sort_direction=direction, content_type=content_type,
        category=category, row_limit=limit,
        explanation=f"本地规则推理：按页面确认的时间窗口查询，指标为 {metric}，结果形式为 {scenario.result_mode}。",
    )
    validate_intent(intent, scenario)
    return intent


def validate_intent(intent: SqlAssistantIntent, scenario: SqlScenario) -> None:
    if intent.sort_by not in scenario.allowed_sort_metrics:
        raise SqlAssistantQuestionError("模型选择了当前场景未批准的指标")
    if intent.content_type is not None and intent.content_type not in scenario.allowed_content_types:
        raise SqlAssistantQuestionError("当前场景不允许该内容类型")
    if intent.category is not None and intent.category not in scenario.allowed_categories:
        raise SqlAssistantQuestionError("当前场景不允许该栏目")
    if intent.row_limit > scenario.max_limit:
        raise SqlAssistantQuestionError("模型选择的行数超过场景上限")

""""查询意图转SQL"""
def compile_query(intent: SqlAssistantIntent, scenario: SqlScenario) -> str:
    validate_intent(intent, scenario)
    dimensions = "news_id, title, content_type, category, source" if scenario.result_mode == "ranking" else "event_time"
    measures = ",\n  ".join(f"SUM({name}) AS {name}" for name in ("impressions", "clicks", "effective_consumptions", "interactions"))
    sql = (
        f"SELECT {dimensions},\n  {measures},\n"
        "  COALESCE(ROUND(SUM(clicks) * 1.0 / NULLIF(SUM(impressions), 0), 4), 0) AS ctr,\n"
        "  COALESCE(ROUND((SUM(clicks) * 0.4 + SUM(effective_consumptions) * 0.35 + SUM(interactions) * 0.25) / NULLIF(SUM(impressions), 0), 4), 0) AS hot_score\n"
        "FROM dw.news_behavior_aggregate\n"
        "WHERE tenant_id = :tenant_id\n"
        "  AND event_time >= :window_start\n"
        "  AND event_time < :window_end"
    )
    if intent.content_type is not None:
        sql += "\n  AND content_type = :content_type"
    if intent.category is not None:
        sql += "\n  AND category = :category"
    sql += f"\nGROUP BY {dimensions}"
    sql += f"\nORDER BY {intent.sort_by} {intent.sort_direction.upper()}, news_id ASC" if scenario.result_mode == "ranking" else "\nORDER BY event_time ASC"
    return sql + "\nLIMIT :row_limit"
