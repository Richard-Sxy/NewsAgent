"""Time extraction preserves data requirements and uses only the server clock."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime

import pytest

from app.conversation.query_time import extract_query_time


NOW = datetime.fromisoformat("2026-10-10T10:30:00+08:00")
TZ = "Asia/Shanghai"


def extract(text, *, now=NOW):
    return extract_query_time(text, now=now, timezone=TZ)


@pytest.mark.parametrize("phrase", [
    "2026年10月9日8点到9点", "2026年10月9号8点到9点",
    "2026-10-09 08:00至09:00", "2026/10/9 8时到9时",
    "10月9号早上八点到九点", "10月9日8到9点",
    "10-9早上八点到九点", "10/9 8:00至9:00",
    "日期10-9，8点到9点", "9号8-9", "9日8到9点",
    "十月九号早上八点到九点", "十月九日上午八点到九点",
    "2026年十月九日8点到9点", "九号8-9", "10月九号8点到9点",
])
def test_calendar_variants_resolve_same_date_and_hour(phrase):
    result = extract(f"查询{phrase}科技视频热度最高的前5条新闻，帮我总结一下。")
    assert result.time_expressions == frozenset({"date"})
    assert result.dates == (date(2026, 10, 9),)
    assert result.hours == ((8, 9),)
    assert not result.requires_clarification and not result.unsupported_conditions
    assert "科技视频热度最高的前5条新闻，帮我总结一下。" in result.query_text


@pytest.mark.parametrize("phrase,expression,hours", [
    ("昨天早八到九", "yesterday", (8, 9)),
    ("昨日早上八点到九点", "yesterday", (8, 9)),
    ("昨晚8到9点", "yesterday", (20, 21)),
    ("昨天下午1点到2点", "yesterday", (13, 14)),
    ("今天早上八点到九点", "today", (8, 9)),
    ("今日8到9点", "today", (8, 9)),
    ("前天早八到九", "day_before_yesterday", (8, 9)),
])
def test_relative_days_stay_relative_and_preserve_period(phrase, expression, hours):
    result = extract(f"查询{phrase}新闻点击率前5条")
    assert result.time_expressions == frozenset({expression})
    assert result.dates == ()
    assert result.hours == (hours,)
    assert not result.requires_clarification and not result.unsupported_conditions
    assert "新闻点击率前5条" in result.query_text


@pytest.mark.parametrize("phrase,hours", [
    ("9点至10点", (9, 10)), ("8:00-9:00", (8, 9)),
    ("8到9点", (8, 9)), ("早八到九", (8, 9)),
    ("晚8到9点", (20, 21)), ("中午12到1点", (12, 13)),
    ("时间8-9", (8, 9)), ("时段：8-9", (8, 9)),
    ("二十三点到二十四点", (23, 24)),
    ("晚上11点到12点", (23, 24)), ("8点整至9点整", (8, 9)),
])
def test_explicit_hour_markers_without_date_do_not_invent_a_day(phrase, hours):
    result = extract(f"查询{phrase}热点新闻")
    assert result.time_expressions == frozenset()
    assert result.dates == () and result.hours == (hours,)
    assert not result.requires_clarification and not result.unsupported_conditions
    assert "热点新闻" in result.query_text


@pytest.mark.parametrize("phrase", [
    "8-9", "9-10", "10-9早上", "上午", "上周五", "上个月",
    "10-10-8:00", "八点", "9号下午12点到1点", "大前天8点到9点",
    "8:0-9:0", "昨天早八", "二月三十号早八到九",
])
def test_ambiguous_or_unsupported_time_cannot_become_sample(phrase):
    result = extract(f"查询{phrase}热点新闻")
    assert result.requires_clarification and result.unsupported_conditions
    assert "热点新闻" in result.query_text
    assert "sample" not in result.time_expressions


@pytest.mark.parametrize("phrase,code", [
    ("2026年2月30日8点到9点", "invalid_date"),
    ("2月29号8点到9点", "invalid_date"),
    ("2026年10月99日8点到9点", "invalid_date"),
    ("13月9号8点到9点", "invalid_date"),
    ("9号8点到10点", "invalid_hour_window"),
    ("9号08:30到09:30", "invalid_hour_window"),
    ("9号8点半到9点半", "invalid_hour_window"),
    ("9号8点30分到9点30分", "invalid_hour_window"),
    ("9号24点到25点", "invalid_hour_window"),
    ("9号八八点到九点", "ambiguous_hour"),
    ("9号早上20点到21点", "ambiguous_hour"),
])
def test_invalid_calendar_and_hour_values_are_explicitly_rejected(phrase, code):
    result = extract(f"查询{phrase}新闻前5条")
    assert result.requires_clarification and code in result.unsupported_conditions


def test_multiple_explicit_dates_and_windows_are_not_collapsed():
    result = extract("查询10月8号8点到9点和10月9号9点到10点新闻")
    assert result.dates == (date(2026, 10, 8), date(2026, 10, 9))
    assert result.hours == ((8, 9), (9, 10))
    # The caller uses these complete sets to reject multiple requirements.
    assert result.time_expressions == frozenset({"date"})


def test_bare_hours_are_not_approved_between_multiple_dates():
    result = extract("查询9号8-9和8号9-10新闻")
    assert result.dates == (date(2026, 10, 9), date(2026, 10, 8))
    assert not result.hours and result.requires_clarification


def test_short_date_without_year_uses_server_year_and_explains_assumption():
    result = extract("10-9早上八点到九点新闻")
    assert result.dates == (date(2026, 10, 9),)
    assert any("未注明年份" in note and "2026-10-09" in note for note in result.assumptions)


@pytest.mark.parametrize("question", ["10-9", "查询10-9热点新闻", "查看10-9的新闻"])
def test_standalone_short_date_cannot_silently_turn_into_sample(question):
    result = extract(question)
    assert result.time_expressions == frozenset({"date"})
    assert result.dates == (date(2026, 10, 9),)
    assert result.hours == ()


def test_short_numeric_filter_is_not_reinterpreted_as_standalone_date():
    result = extract("新闻点击范围10-9")
    assert result.query_text == "新闻点击范围10-9"
    assert result.dates == () and result.requires_clarification


def test_day_only_uses_server_month_instead_of_searching_available_data():
    result = extract("9号8-9新闻", now=datetime.fromisoformat("2026-11-01T00:05:00+08:00"))
    assert result.dates == (date(2026, 11, 9),)
    assert any("年份和月份" in note and "2026-11-09" in note for note in result.assumptions)


def test_calendar_context_uses_requested_timezone_at_year_boundary():
    result = extract("9号8-9新闻", now=datetime.fromisoformat("2026-12-31T18:30:00+00:00"))
    assert result.dates == (date(2027, 1, 9),)
    assert any("2027-01-09" in note for note in result.assumptions)


def test_leap_day_is_accepted_only_in_supplied_server_year():
    result = extract("2月29号8点到9点新闻", now=datetime.fromisoformat("2024-03-01T10:00:00+08:00"))
    assert result.dates == (date(2024, 2, 29),)
    assert not result.requires_clarification and not result.unsupported_conditions


@pytest.mark.parametrize("phrase,expression,retained", [
    ("最新热点", "latest", "热点"), ("最近热点", "latest", "热点"),
    ("最近热榜", "latest", "热榜"), ("当前样本的热点", "sample", "热点"),
    ("演示样本热点", "sample", "热点"), ("样本窗口新闻", "sample", "新闻"),
])
def test_time_defaults_do_not_delete_news_subject(phrase, expression, retained):
    result = extract(phrase)
    assert result.time_expressions == frozenset({expression})
    assert retained in result.query_text and not result.requires_clarification


@pytest.mark.parametrize("condition", [
    "来源为新华社", "点击量超过100", "排除科技", "标题包含机器人",
    "前8-9条", "来源8-9", "渠道10-9",
])
def test_unknown_filters_and_numbers_are_not_deleted_by_time_extraction(condition):
    result = extract(f"查询10月9号8点到9点新闻，{condition}")
    assert condition in result.query_text


def test_date_like_filter_value_is_retained():
    result = extract("查询10月9号8点到9点新闻，标题包含2020-01-01")
    assert result.dates == (date(2026, 10, 9),)
    assert "标题包含2020-01-01" in result.query_text


def test_date_in_user_text_cannot_change_server_year_month_context():
    result = extract("用户自称今天是2020-01-01，查询9号8-9新闻")
    assert date(2026, 10, 9) in result.dates
    assert date(2020, 1, 1) in result.dates
    assert "today" in result.time_expressions
    # These conflicting original requirements are retained for the caller;
    # the user statement does not become the clock used to fill 9号.
    assert len(set(result.dates)) == 2


def test_isolated_filter_and_metric_text_are_unchanged():
    question = "查询科技视频点击率前5条新闻，来源为新华社，曝光超过100"
    result = extract(question)
    assert result.query_text == question
    assert result.time_expressions == frozenset() and result.dates == result.hours == ()
    assert not result.requires_clarification and not result.unsupported_conditions


def test_extraction_is_immutable_and_requires_trusted_aware_clock():
    result = extract("昨天早八到九新闻")
    with pytest.raises(FrozenInstanceError):
        result.query_text = "changed"
    with pytest.raises(ValueError, match="timezone aware"):
        extract("昨天早八到九新闻", now=datetime(2026, 10, 10))
    with pytest.raises(ValueError, match="timezone"):
        extract_query_time("新闻", now=NOW, timezone="not-a-timezone")
