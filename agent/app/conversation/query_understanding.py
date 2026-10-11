"""Bounded query semantics before the existing intent-to-SQL compiler.

The local parser is a deterministic test double. Model proposals pass separate
checks for detectable requirements; this is not a proof of arbitrary language
comprehension. Dates, coverage, canonical questions and user messages are owned
by Python. Neither this module nor the model can expand warehouse permissions.
"""

import re
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from app.schemas.sql_assistant import SqlAssistantIntent
from app.sql_assistant.input_boundary import SqlAssistantQuestionError, screen_question
from app.sql_assistant.planner import local_query_intent, validate_intent
from app.sql_assistant.presentation import ResponseStyle, extract_query_presentation
from app.sql_assistant.scenarios import SqlScenario
from app.conversation.query_time import extract_query_time


Condition = Annotated[str, StringConstraints(min_length=1, max_length=120)]
TimeExpression = Literal["sample", "today", "yesterday", "day_before_yesterday", "latest", "date"]
RankingSource = Literal["project_computed", "enterprise_board", "unspecified"]
ResolutionCode = Literal["ready", "input_boundary", "invalid_proposal", "unsupported_condition", "ambiguous_requirements",
                         "time_semantic_drift", "source_semantic_drift", "ranking_source_unavailable", "local_syntax",
                         "model_unresolved", "intent_not_approved", "intent_semantic_drift", "latest_window_unavailable",
                         "no_complete_hour", "time_coverage_unavailable", "window_not_approved",
                         "query_intent_mismatch", "semantic_mismatch", "presentation_semantic_drift"]


class QueryUnderstanding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "clarify", "unsupported"]
    intent: SqlAssistantIntent | None = None
    time_expression: TimeExpression = "sample"
    explicit_date: date | None = None
    hour_start: int | None = Field(default=None, ge=0, le=23)
    hour_end: int | None = Field(default=None, ge=1, le=24)
    time_basis: Literal["behavior", "publication"] = "behavior"
    ranking_source: RankingSource = "unspecified"
    response_style: ResponseStyle = "list"
    unsupported_conditions: list[Condition] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def complete_proposal(self):
        if self.status == "ready" and (self.intent is None or self.unsupported_conditions):
            raise ValueError("ready requires an intent and no unsupported conditions")
        if (self.time_expression == "date") != (self.explicit_date is not None):
            raise ValueError("explicit_date is required only for a date expression")
        if (self.hour_start is None) != (self.hour_end is None):
            raise ValueError("hour bounds must be supplied together")
        return self


class QueryPolicy(BaseModel):
    """Server-owned policy. The current adapter approves one complete hour."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario: SqlScenario
    default_limit: int = Field(default=5, ge=1, le=1000)
    max_limit: int = Field(default=100, ge=1, le=1000)
    supported_window_start: AwareDatetime
    supported_window_end: AwareDatetime
    data_watermark: AwareDatetime | None = None
    coverage_window_start: AwareDatetime | None = None
    coverage_window_end: AwareDatetime | None = None
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    data_kind: Literal["synthetic", "enterprise"] = "synthetic"
    allowed_ranking_sources: tuple[Literal["project_computed", "enterprise_board"], ...] = ("project_computed",)

    @model_validator(mode="after")
    def approved_policy(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("unknown policy timezone") from exc
        if self.scenario.result_mode != "ranking":
            raise ValueError("conversation query requires a ranking scenario")
        if not self.default_limit <= self.max_limit <= self.scenario.max_limit:
            raise ValueError("policy limits exceed the approved scenario")
        if self.supported_window_end - self.supported_window_start != timedelta(hours=1):
            raise ValueError("the current adapter supports one complete hour only")
        if (self.coverage_window_start is None) != (self.coverage_window_end is None):
            raise ValueError("coverage bounds must be supplied together")
        if self.coverage_window_start is not None and not (
            self.coverage_window_start <= self.supported_window_start < self.supported_window_end <= self.coverage_window_end
        ):
            raise ValueError("default hourly window must lie within warehouse coverage")
        for value in (self.supported_window_start, self.supported_window_end, self.data_watermark,
                      self.coverage_window_start, self.coverage_window_end):
            if value is not None and (value.minute or value.second or value.microsecond):
                raise ValueError("policy windows and complete watermark must align to hours")
        if not self.allowed_ranking_sources or len(set(self.allowed_ranking_sources)) != len(self.allowed_ranking_sources):
            raise ValueError("ranking source whitelist must be nonempty and unique")
        return self


class QueryResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready", "clarify", "unsupported"]
    reason_code: ResolutionCode
    message: str = Field(min_length=1, max_length=1200)
    intent: SqlAssistantIntent | None = None
    canonical_question: str = Field(default="", max_length=1000)
    time_expression: TimeExpression = "sample"
    explicit_date: date | None = None
    time_basis: Literal["behavior", "publication"] = "behavior"
    ranking_source: RankingSource = "project_computed"
    response_style: ResponseStyle = "list"
    requested_window_start: AwareDatetime | None = None
    requested_window_end: AwareDatetime | None = None
    supported_window_start: AwareDatetime
    supported_window_end: AwareDatetime
    coverage_window_start: AwareDatetime | None = None
    coverage_window_end: AwareDatetime | None = None
    notes: list[Annotated[str, StringConstraints(min_length=1, max_length=500)]] = Field(default_factory=list, max_length=8)

    def require_ready(self):
        if self.status != "ready":
            raise QueryResolutionError(self)
        return self


class QueryResolutionError(ValueError):
    retryable = False

    def __init__(self, resolution: QueryResolution, *, request_id: str | None = None):
        self.resolution = resolution
        self.request_id = request_id
        super().__init__(resolution.message)


class QueryRequirementsError(ValueError):
    """Invalid date/count language requires clarification, not a safety denial."""


_ENTERPRISE = re.compile(r"官方|企业原榜|内部榜|原始榜单|腾讯新闻|腾讯(?:热榜|榜单)|微信|微博|百度|抖音|热搜|(?<![A-Za-z_])(?:tencent|wechat)(?![A-Za-z_])", re.I)
_PROJECT = re.compile(r"NewsAgent(?:计算)?榜|项目计算榜|项目榜|计算榜", re.I)
_METRICS = {"点击率": "ctr", "ctr": "ctr", "点击量": "clicks", "点击数": "clicks", "点击": "clicks",
            "曝光量": "impressions", "曝光": "impressions", "互动量": "interactions", "互动": "interactions",
            "热度分": "hot_score", "热度": "hot_score", "clicks": "clicks", "impressions": "impressions",
            "interactions": "interactions", "hot_score": "hot_score"}
_METRIC_PATTERN = re.compile("|".join(
    r"(?<![A-Za-z_])" + re.escape(word) + r"(?![A-Za-z_])" if word.isascii() else re.escape(word)
    for word in sorted(_METRICS, key=len, reverse=True)), re.I)
_NUMERALS = "零〇一二三四五六七八九十百千两"
_LIMIT = re.compile(r"(?:前|top\s*|最高的?|最多的?|最低的?|最少的?)\s*(\d+|[" + _NUMERALS + r"]+)\s*(?:条|篇|个)?", re.I)
_COUNT = re.compile(r"(?<![\d.])(\d+|[" + _NUMERALS + r"]+)\s*(?:条|篇|个)")


def _chinese_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    digits = dict(zip("零〇一二三四五六七八九两", (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2)))
    if value in {"零", "〇"}:
        return 0
    if not re.fullmatch(r"(?:[一二三四五六七八九两]千[零〇]?)?(?:[一二三四五六七八九两]百[零〇]?)?(?:[一二三四五六七八九两]?十)?[一二三四五六七八九两]?", value):
        raise ValueError("ambiguous Chinese count")
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in {"十", "百", "千"}:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
        else:
            raise ValueError("invalid count")
    return total + current


def _requirements(question: str, *, now: datetime | None = None, timezone_name: str = "Asia/Shanghai") -> dict:
    presentation = extract_query_presentation(screen_question(question))
    time_requirements = extract_query_time(presentation.query_text,
                                          now=now or datetime.now(timezone.utc), timezone=timezone_name)
    text = time_requirements.query_text
    temporal = set(time_requirements.time_expressions)
    dates, hours = list(time_requirements.dates), list(time_requirements.hours)
    try:
        counts = {_chinese_number(match.group(1)) for match in [*_LIMIT.finditer(text), *_COUNT.finditer(text)]}
    except ValueError as exc:
        raise QueryRequirementsError("invalid date or count") from exc
    metrics = {_METRICS[match.group().lower()] for match in _METRIC_PATTERN.finditer(text)}
    categories = {value for value in ("科技", "财经", "体育", "社会") if value in text}
    content = set()
    if re.search(r"图文|(?<![A-Za-z])article(?![A-Za-z])", text, re.I):
        content.add("article")
    if re.search(r"视频|(?<![A-Za-z])video(?![A-Za-z])", text, re.I):
        content.add("video")
    sources = set()
    if _ENTERPRISE.search(text):
        sources.add("enterprise_board")
    if _PROJECT.search(text):
        sources.add("project_computed")
    unsupported = list(time_requirements.unsupported_conditions)
    # These are warehouse/adapter limits, independent of which inference Port
    # proposed the plan. Other unknown semantics rely on a model declining and
    # on quality evaluations; the finite local vocabulary adds a stricter check.
    for pattern, code in (
        (r"发布|刊发|published_at|publish_time|publication_time", "publication_time"),
        (r"标题|关键词|来源|新华社|人民日报|央视|包含|匹配|(?<![A-Za-z_])source(?![A-Za-z_])", "source_or_text_filter"),
        (r"超过|大于|小于|不少于|不低于|不超过|低于|至少|至多|排除|不含|除了|不要|不看|不包括|不是|非(?:科技|财经|体育|社会|图文|视频)", "threshold_or_exclusion"),
        (r"收入|销售额|订单|利润|用户画像|地区|城市|性别|年龄|正文|作者|转发|评论|点赞|阅读时长|有效消费|消费时长|UV|用户数|人数|effective_consumptions|duration|(?<![A-Za-z_])(?:author|category|content_type|region|age|gender)(?![A-Za-z_])", "unsupported_dimension_or_metric"),
        (r"对比|同比|环比|增长|平均|去重|中位|分位|占比|趋势|每小时|逐小时", "unsupported_aggregation"),
        (r"明天|本周|上周|本月|上月|过去|最近\s*\d|近\s*\d|实时|\d+\s*(?:小时|天|分钟)", "unsupported_time_range"),
        (r"娱乐|军事|汽车|健康|教育|游戏|国际", "unsupported_category"),
    ):
        if re.search(pattern, text, re.I):
            unsupported.append(code)
    remaining_numbers = _COUNT.sub("", _LIMIT.sub("", text))
    if re.search(r"\d", remaining_numbers):
        unsupported.append("unassigned_number")
    return dict(text=text, response_style=presentation.response_style, temporal=temporal, dates=dates, metrics=metrics, counts=counts,
                categories=categories, content=content, sources=sources, unsupported=unsupported, hours=hours,
                publication=bool(re.search(r"发布|刊发|published_at|publish_time|publication_time", text)),
                time_clarification=(time_requirements.requires_clarification
                                    and not {"invalid_hour_window", "unsupported_time_range"}.intersection(unsupported)),
                time_assumptions=time_requirements.assumptions,
                ascending=bool(re.search(r"最低|最少|升序|从低到高", text)),
                descending=bool(re.search(r"最高|最多|降序|从高到低", text)))


def _local_text(requirements: dict) -> str:
    text = requirements["text"]
    text = _PROJECT.sub("", text)
    # Only fixed courtesy phrases and representation aliases are removed.
    # Unknown filters and conditions remain for the old finite parser to reject.
    prefixes = r"^(?:你好|您好|请问|你能(?:否)?告诉我|能(?:不能|否)?告诉我|告诉我|我想知道|我想看看|我想看|请给我|给我|麻烦你|麻烦|能不能|能否|可以|请你|请|有哪些)[，,、\s]*"
    while re.search(prefixes, text):
        text = re.sub(prefixes, "", text)
    text = re.sub(r"(?:有哪些|有哪几条|有什么|是什么|有哪些新闻)(?:吗|呢|呀)?[?？。!！\s]*$", "", text)
    text = re.sub(r"(?:吗|呢|吧|呀|啊|呗)[?？。!！\s]*$", "", text)
    for before, after in (("热门", "热点"), ("热榜", "热点排行"), ("排行榜", "排行"), ("榜单", "排行"), ("点击数", "点击量"), ("看一下", "查看"), ("列出", "展示")):
        text = text.replace(before, after)
    for before, after in (("clicks", "点击量"), ("impressions", "曝光量"), ("interactions", "互动量"), ("hot_score", "热度分")):
        text = re.sub(r"(?<![A-Za-z_])" + before + r"(?![A-Za-z_])", after, text, flags=re.I)
    text = _LIMIT.sub(lambda match: match.group().replace(match.group(1), str(_chinese_number(match.group(1)))), text)
    def normalize_count(match):
        value = str(_chinese_number(match.group(1)))
        if re.search(r"(?:前|top\s*|最高的?|最多的?|最低的?|最少的?)\s*$", text[:match.start()], re.I):
            return match.group().replace(match.group(1), value)
        return f"前{value}条"
    text = _COUNT.sub(normalize_count, text)
    return text.strip(" ，,。？！?!")


def local_query_understanding(payload: dict) -> str:
    """返回有界确定性 JSON; 没有 SQL、时钟或数据访问。"""
    policy_data = payload.get("policy")
    policy = QueryPolicy.model_validate(policy_data) if policy_data is not None else None
    scenario = policy.scenario if policy else SqlScenario.model_validate(payload["scenario"])
    scenario = scenario.model_copy(update={"default_limit": policy.default_limit if policy else min(5, scenario.max_limit)})
    try:
        context = payload.get("temporal_context", {})
        clock = datetime.fromisoformat(context["server_now"]) if "server_now" in context else None
        requirements = _requirements(payload["question"], now=clock,
                                     timezone_name=policy.timezone if policy else "Asia/Shanghai")
    except QueryRequirementsError:
        return QueryUnderstanding(status="clarify", unsupported_conditions=["invalid_date_or_count"]).model_dump_json()
    except (SqlAssistantQuestionError, ValueError):
        return QueryUnderstanding(status="unsupported", unsupported_conditions=["input_boundary"]).model_dump_json()
    temporal = requirements["temporal"]
    if requirements["time_clarification"]:
        return QueryUnderstanding(status="clarify", unsupported_conditions=["ambiguous_time"]).model_dump_json()
    fields = dict(response_style=requirements["response_style"], time_expression=next(iter(temporal)) if len(temporal) == 1 else "sample",
                  explicit_date=requirements["dates"][0] if temporal == {"date"} else None,
                  hour_start=requirements["hours"][0][0] if len(requirements["hours"]) == 1 and not requirements["unsupported"] else None,
                  hour_end=requirements["hours"][0][1] if len(requirements["hours"]) == 1 and not requirements["unsupported"] else None,
                  time_basis="publication" if requirements["publication"] else "behavior",
                  ranking_source=next(iter(requirements["sources"])) if len(requirements["sources"]) == 1 else "unspecified")
    if len(temporal) > 1 or len(set(requirements["dates"])) > 1 or len(requirements["hours"]) > 1 or any(len(requirements[key]) > 1 for key in ("metrics", "counts", "categories", "content", "sources")):
        return QueryUnderstanding(status="clarify", **fields).model_dump_json()
    if requirements["unsupported"] or fields["ranking_source"] == "enterprise_board":
        return QueryUnderstanding(status="unsupported", unsupported_conditions=requirements["unsupported"] or ["enterprise_board"], **fields).model_dump_json()
    try:
        intent = local_query_intent({"question": _local_text(requirements), "scenario": scenario.model_dump(mode="json")})
    except SqlAssistantQuestionError:
        return QueryUnderstanding(status="clarify", unsupported_conditions=["local_syntax"], **fields).model_dump_json()
    return QueryUnderstanding(status="ready", intent=intent, **fields).model_dump_json()


def _canonical_question(intent: SqlAssistantIntent) -> str:
    labels = {"clicks": "点击量", "ctr": "点击率", "impressions": "曝光量", "interactions": "互动量", "hot_score": "热度分"}
    content = {None: "", "article": "图文", "video": "视频"}[intent.content_type]
    direction = "从低到高" if intent.sort_direction == "asc" else "从高到低"
    return f"查询{intent.category or ''}{content}新闻按{labels[intent.sort_by]}{direction}前{intent.row_limit}条"

"""聊天问题的前置语义校验，验证已知的原始要求，然后解决批准的时间范围。"""
def resolve_query_understanding(original_question: str, proposal: QueryUnderstanding | dict, policy: QueryPolicy,
                                now: datetime | None = None) -> QueryResolution:
    # 模型时间校验
    zone = ZoneInfo(policy.timezone)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("query clock must be timezone aware")
    clock = clock.astimezone(zone)
    start, end = policy.supported_window_start.astimezone(zone), policy.supported_window_end.astimezone(zone)
    ranged = policy.coverage_window_start is not None
    coverage_start = (policy.coverage_window_start or start).astimezone(zone)
    coverage_end = (policy.coverage_window_end or end).astimezone(zone)
    proposal_fields = {}
    requested = {}

    def resolved(status, code, message, **extras):
        return QueryResolution(status=status, reason_code=code, message=message,
                               supported_window_start=start, supported_window_end=end,
                               coverage_window_start=coverage_start if ranged else None,
                               coverage_window_end=coverage_end if ranged else None,
                               **proposal_fields, **requested, **extras)

    try:
        requirements = _requirements(original_question, now=clock, timezone_name=policy.timezone)
    except QueryRequirementsError:
        return resolved("clarify", "ambiguous_requirements", "日期或数量无法明确解析，请使用有效日期和一个整数条数；没有执行查询。")
    except (SqlAssistantQuestionError, ValueError):
        return resolved("unsupported", "input_boundary", "该问题未通过只读查询输入校验，没有执行查询。")
    try:
        proposal = QueryUnderstanding.model_validate(proposal)
    except ValidationError:
        return resolved("unsupported", "invalid_proposal", "查询理解结果未通过结构校验，没有执行查询。")
    proposal_fields = {"time_expression": proposal.time_expression, "explicit_date": proposal.explicit_date,
                       "time_basis": proposal.time_basis,
                       "response_style": proposal.response_style,
                       "ranking_source": "project_computed" if proposal.ranking_source == "unspecified" else proposal.ranking_source}
    if requirements["time_clarification"]:
        return resolved("clarify", "ambiguous_requirements", "已识别查询请求，但日期或小时仍有歧义，请补充具体哪一天或哪个小时；不要求固定日期格式，没有执行查询。")
    if requirements["unsupported"] or proposal.time_basis == "publication":
        return resolved("unsupported", "unsupported_condition", "当前聚合查询不支持问题中的时间口径、来源、条件、维度或统计方式，没有执行查询。")
    if len(requirements["temporal"]) > 1 or len(set(requirements["dates"])) > 1 or len(requirements["hours"]) > 1 or any(len(requirements[key]) > 1 for key in ("metrics", "counts", "categories", "content", "sources")) or (requirements["ascending"] and requirements["descending"]):
        return resolved("clarify", "ambiguous_requirements", "问题包含多个时间、指标、筛选、数量或排序要求，请明确一种查询口径。")
    expected_time = next(iter(requirements["temporal"]), "sample")
    expected_date = requirements["dates"][0] if expected_time == "date" else None
    relative_days = {"today": 0, "yesterday": 1, "day_before_yesterday": 2}
    relative_date = (clock.date() - timedelta(days=relative_days[expected_time])
                     if expected_time in relative_days else None)
    normalized_date = None
    # A model may spell a relative day as its exact calendar equivalent. Prove
    # that equivalence using the original question and the same server clock;
    # never use dataset coverage to repair a wrong model date.
    if (relative_date is not None and proposal.time_expression == "date"
            and proposal.explicit_date == relative_date):
        normalized_date = proposal.explicit_date
        proposal = proposal.model_copy(update={"time_expression": expected_time, "explicit_date": None})
        proposal_fields.update(time_expression=expected_time, explicit_date=None)
    if proposal.time_expression != expected_time or proposal.explicit_date != expected_date:
        return resolved("unsupported", "time_semantic_drift", "查询理解遗漏或改变了原问题的时间条件，没有执行查询。")
    expected_hours = requirements["hours"][0] if requirements["hours"] else (None, None)
    if (proposal.hour_start, proposal.hour_end) != expected_hours:
        return resolved("unsupported", "time_semantic_drift", "查询理解遗漏或改变了原问题的小时条件，没有执行查询。")
    if requirements["sources"] and proposal.ranking_source not in requirements["sources"]:
        return resolved("unsupported", "source_semantic_drift", "查询理解遗漏或改变了原问题的榜单来源，没有执行查询。")
    if proposal_fields["ranking_source"] not in policy.allowed_ranking_sources or proposal_fields["ranking_source"] != "project_computed":
        return resolved("unsupported", "ranking_source_unavailable", "当前仅接通项目计算榜，企业原榜尚未接入，没有执行查询。")
    if proposal.status != "ready" or proposal.unsupported_conditions:
        code = "local_syntax" if "local_syntax" in proposal.unsupported_conditions else "model_unresolved"
        message = "尚不能确认这句话的全部查询条件，请明确新闻范围、指标或数量；这是理解澄清，未判定为提示词注入，也没有执行查询。" if code == "local_syntax" else "模型尚不能确认全部查询条件，可以用自然语言补充哪一天、哪个小时或筛选范围；无需固定日期格式，没有执行查询。"
        return resolved(proposal.status if proposal.status != "ready" else "unsupported", code, message)
    if proposal.response_style != requirements["response_style"]:
        return resolved("clarify", "presentation_semantic_drift", "查询理解改变了原问题的展示要求，请确认需要榜单还是简要总结；没有执行查询。")
    intent = proposal.intent
    try:
        validate_intent(intent, policy.scenario)
        if intent.row_limit > policy.max_limit:
            raise ValueError("policy limit")
    except (SqlAssistantQuestionError, ValueError):
        return resolved("unsupported", "intent_not_approved", f"查询指标、过滤或行数不在批准范围内，最多支持{policy.max_limit}条。")
    for key, value in (("metrics", intent.sort_by), ("counts", intent.row_limit), ("categories", intent.category), ("content", intent.content_type)):
        if requirements[key] and value not in requirements[key]:
            return resolved("unsupported", "intent_semantic_drift", "查询理解遗漏或改变了原问题的指标、数量或筛选条件，没有执行查询。")
    if not requirements["counts"] and intent.row_limit != policy.default_limit:
        return resolved("unsupported", "intent_semantic_drift", "查询理解改变了服务端批准的默认数量，没有执行查询。")
    if not requirements["metrics"] and intent.sort_by != policy.scenario.default_sort:
        return resolved("unsupported", "intent_semantic_drift", "查询理解改变了服务端批准的默认候选指标，没有执行查询。")
    for key, value, approved in (("categories", intent.category, policy.scenario.allowed_categories),
                                 ("content", intent.content_type, policy.scenario.allowed_content_types)):
        if not requirements[key] and value is not None and (len(approved) != 1 or value != approved[0]):
            return resolved("unsupported", "intent_semantic_drift", "查询理解增加了原问题未指定的筛选条件，没有执行查询。")
    if (requirements["ascending"] and intent.sort_direction != "asc") or (requirements["descending"] and intent.sort_direction != "desc"):
        return resolved("unsupported", "intent_semantic_drift", "查询理解改变了原问题的排序方向，没有执行查询。")
    if not requirements["ascending"] and not requirements["descending"] and intent.sort_direction != "desc":
        return resolved("unsupported", "intent_semantic_drift", "查询理解改变了默认降序候选选择，没有执行查询。")
    defaults = {}
    if intent.content_type is None and len(policy.scenario.allowed_content_types) == 1:
        defaults["content_type"] = policy.scenario.allowed_content_types[0]
    if intent.category is None and len(policy.scenario.allowed_categories) == 1:
        defaults["category"] = policy.scenario.allowed_categories[0]
    intent = intent.model_copy(update=defaults)
    watermark = (policy.data_watermark or end).astimezone(zone)
    if proposal.hour_start is not None:
        target = (relative_date if relative_date is not None else expected_date if expected_time == "date"
                  else (min(watermark, coverage_end) - timedelta(hours=1)).date()
                  if expected_time == "latest" else start.date())
        requested_start = datetime.combine(target, datetime.min.time(), tzinfo=zone) + timedelta(hours=proposal.hour_start)
        requested_end = requested_start + timedelta(hours=1)
    elif expected_time in {"sample", "latest"}:
        requested_start, requested_end = start, end
        if ranged and expected_time == "latest":
            requested_end = min(watermark, coverage_end)
            requested_start = requested_end - timedelta(hours=1)
        elif expected_time == "latest" and watermark != end:
            return resolved("unsupported", "latest_window_unavailable", "最新完整数据窗口不在当前批准窗口内，没有自动替换查询范围。")
    else:
        target = relative_date if relative_date is not None else expected_date
        requested_start = datetime.combine(target, datetime.min.time(), tzinfo=zone)
        requested_end = requested_start + timedelta(days=1)
        if ranged:
            requested = {"requested_window_start": requested_start, "requested_window_end": requested_end}
            if requested_start < coverage_start or requested_start >= min(watermark, coverage_end):
                return resolved("unsupported", "time_coverage_unavailable", "该日期不在当前完整样本覆盖内，没有替换为其他日期。")
            example_start = max(requested_start, coverage_start)
            return resolved("clarify", "ambiguous_requirements", "已识别新闻查询和展示意图；热点分析每次取一个整点小时，请补充开始和结束小时，"
                            f"例如“{example_start:%Y-%m-%d %H:%M}至{example_start + timedelta(hours=1):%H:%M}点击量前5条新闻”。")
        if expected_time == "today":
            completed_hour = clock.replace(minute=0, second=0, microsecond=0)
            requested_end = min(completed_hour, watermark) if watermark > requested_start else completed_hour
        requested = {"requested_window_start": requested_start, "requested_window_end": requested_end}
        if requested_start >= requested_end:
            return resolved("clarify", "no_complete_hour", "当前日期尚无完整小时可供查询，请稍后查询或明确选择演示样本。")
    requested = {"requested_window_start": requested_start, "requested_window_end": requested_end}
    if watermark < requested_end or requested_start < coverage_start or requested_end > coverage_end:
        sample = "合成样本" if policy.data_kind == "synthetic" else "批准数据"
        return resolved("unsupported", "time_coverage_unavailable", f"请求的时间范围不在当前完整数据覆盖内；当前{sample}仅覆盖{coverage_start:%Y-%m-%d %H:%M}至{coverage_end:%Y-%m-%d %H:%M}（{policy.timezone}）。没有查询今日实时数据，也没有自动替换为样本。")
    if not ranged and (requested_start, requested_end) != (start, end):
        return resolved("unsupported", "window_not_approved", "当前热点接口只批准一个完整小时，不能直接执行全天或其他窗口查询；跨小时UV不能相加。")
    labels = {"clicks": "点击量", "ctr": "点击率", "impressions": "曝光量", "interactions": "互动量", "hot_score": "SQL候选热度分"}
    notes = ["按行为指标发生时间取数，不代表该时段发布的全部新闻。",
             f"SQL按{labels[intent.sort_by]}选择至多{intent.row_limit}条候选；最终榜单由Python权威热度计算，候选指标顺序不保证保留。",
             "只覆盖查询选中的候选范围，不代表全站或企业原始榜单。"]
    if proposal.ranking_source == "unspecified":
        notes.append("未指定榜单来源，使用已批准的项目计算榜。")
    if expected_time == "latest":
        notes.append("最新指当前数据源可用的完整样本窗口，不等于当前日期的实时新闻。")
    if expected_time == "today":
        notes.append(f"今日仅指截至完整数据水位{requested_end:%H:%M}的范围，不是全天；当前时刻为{clock:%H:%M}。")
    if proposal.response_style == "summary":
        notes.append("按已验证的候选报告提供简要总结，展示要求不改变SQL指标、筛选或查询范围。")
    notes.extend(dict.fromkeys(requirements["time_assumptions"]))
    if normalized_date is not None:
        notes.append(f"相对日期已按同一服务端时钟核对为{normalized_date:%Y-%m-%d}，保留原问的时间口径。")
    sample = "合成样本" if policy.data_kind == "synthetic" else "批准数据"
    message = f"本次使用项目计算榜，查询{sample}{requested_start:%Y-%m-%d %H:%M}至{requested_end:%H:%M}（{policy.timezone}），按{labels[intent.sort_by]}选取最多{intent.row_limit}条候选，再由Python计算权威热度排行。"
    return resolved("ready", "ready", message, intent=intent, canonical_question=_canonical_question(intent), notes=notes)
