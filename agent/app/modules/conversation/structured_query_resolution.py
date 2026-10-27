from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.modules.conversation.query_understanding import (
    QueryPolicy, QueryResolution, QueryUnderstanding, _canonical_question,
)
from app.sql_assistant.input_boundary import SqlAssistantQuestionError, normalize_question
from app.sql_assistant.planner import validate_intent


def resolve_structured_query(
    original_question: str,
    proposal: QueryUnderstanding | dict,
    policy: QueryPolicy,
    now: datetime | None = None,
) -> QueryResolution:
    """Validate model-owned semantics against server-owned execution limits.

    The original question receives basic text checks only. Natural-language
    interpretation belongs to the model, not a second regex parser.
    """
    zone = ZoneInfo(policy.timezone)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None or clock.utcoffset() is None:
        raise ValueError("query clock must be timezone aware")
    clock = clock.astimezone(zone)
    start = policy.supported_window_start.astimezone(zone)
    end = policy.supported_window_end.astimezone(zone)
    ranged = policy.coverage_window_start is not None
    coverage_start = (policy.coverage_window_start or start).astimezone(zone)
    coverage_end = (policy.coverage_window_end or end).astimezone(zone)
    fields = {}
    requested = {}

    def resolved(status, code, message, **extras):
        return QueryResolution(
            status=status, reason_code=code, message=message,
            supported_window_start=start, supported_window_end=end,
            coverage_window_start=coverage_start if ranged else None,
            coverage_window_end=coverage_end if ranged else None,
            **fields, **requested, **extras,
        )

    try:
        normalize_question(original_question)
    except SqlAssistantQuestionError:
        return resolved("unsupported", "input_boundary", "问题为空、过长或包含隐藏控制字符，没有执行查询。")

    try:
        data = proposal.model_dump() if isinstance(proposal, QueryUnderstanding) else proposal
        proposal = QueryUnderstanding.model_validate(data)
    except ValidationError:
        return resolved("unsupported", "invalid_proposal", "模型输出未通过结构校验，没有执行查询。")

    source = "project_computed" if proposal.ranking_source == "unspecified" else proposal.ranking_source
    fields = {
        "time_expression": proposal.time_expression,
        "explicit_date": proposal.explicit_date,
        "time_basis": proposal.time_basis,
        "response_style": proposal.response_style,
        "ranking_source": source,
    }
    if proposal.status == "clarify":
        return resolved("clarify", "model_unresolved", "查询条件尚不明确，请补充有歧义的时间或筛选要求，没有执行查询。")
    if proposal.status == "unsupported" or proposal.unsupported_conditions:
        return resolved("unsupported", "unsupported_condition", "问题包含当前未支持的查询条件，没有执行查询。")
    if proposal.time_basis != "behavior":
        return resolved("unsupported", "unsupported_condition", "当前数仓按行为发生时间取数，不支持发布时间查询。")
    if source not in policy.allowed_ranking_sources or source != "project_computed":
        return resolved("unsupported", "ranking_source_unavailable", "当前仅接通项目计算榜，未执行其他来源的榜单查询。")

    intent = proposal.intent
    try:
        validate_intent(intent, policy.scenario)
        if intent.row_limit > policy.max_limit:
            raise ValueError("policy row limit exceeded")
    except (SqlAssistantQuestionError, ValueError):
        return resolved("unsupported", "intent_not_approved", f"指标、栏目、类型或数量超出批准范围，最多支持{policy.max_limit}条。")

    defaults = {}
    if intent.content_type is None and len(policy.scenario.allowed_content_types) == 1:
        defaults["content_type"] = policy.scenario.allowed_content_types[0]
    if intent.category is None and len(policy.scenario.allowed_categories) == 1:
        defaults["category"] = policy.scenario.allowed_categories[0]
    intent = intent.model_copy(update=defaults)

    from app.modules.conversation.query_window import resolve_requested_window
    expression = proposal.time_expression
    window = resolve_requested_window(proposal, policy, clock)
    requested_start, requested_end = window.start, window.end
    requested = {"requested_window_start": requested_start, "requested_window_end": requested_end}
    if window.reason:
        return resolved("unsupported", window.reason, window.message)
    labels = {"clicks": "点击量", "ctr": "点击率", "impressions": "曝光量", "interactions": "互动量", "hot_score": "SQL候选热度分"}
    notes = [
        "按行为指标发生时间取数，不代表该时段发布的全部新闻。",
        f"SQL按{labels[intent.sort_by]}选择至多{intent.row_limit}条候选；最终榜单由Python计算权威热度。",
        "查询仅覆盖获选候选，不代表全站或企业原榜。",
    ]
    if proposal.ranking_source == "unspecified":
        notes.append("未指定榜单来源，使用批准的项目计算榜。")
    if expression == "latest":
        notes.append("最新指数据水位前的完整窗口，不等于今日实时新闻。")
    if proposal.response_style == "summary":
        notes.append("总结依据已保存报告，不改变查询条件或权威数字。")
    message = (f"查询项目计算榜{requested_start:%Y-%m-%d %H:%M}至{requested_end:%H:%M}"
               f"（{policy.timezone}），按{labels[intent.sort_by]}选取最多{intent.row_limit}条候选，再计算权威热度。")
    return resolved("ready", "ready", message, intent=intent,
                    canonical_question=_canonical_question(intent), notes=notes)
