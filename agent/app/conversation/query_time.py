"""Extract a finite set of time phrases without discarding other requirements.

The caller screens the complete original question first and supplies one trusted
clock for both model understanding and validation. This module neither approves
a query nor chooses a date to fit data coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class QueryTimeRequirements:
    query_text: str
    time_expressions: frozenset[str]
    dates: tuple[date, ...]
    hours: tuple[tuple[int, int], ...]
    unsupported_conditions: tuple[str, ...]
    requires_clarification: bool
    assumptions: tuple[str, ...]


_DATE_NUMBER = r"\d{1,2}|[零〇一二两三四五六七八九十]{1,3}"
_FULL_DATE = re.compile(
    r"(?<!\d)(?P<year>\d{4})(?:"
    r"年(?P<cn_month>" + _DATE_NUMBER + r")月(?P<cn_day>" + _DATE_NUMBER + r")(?:日|号)|"
    r"(?P<separator>[-/])(?P<month>\d{1,2})(?P=separator)(?P<day>\d{1,2})(?:日|号|(?!\d)))"
)
_MONTH_DAY = re.compile(r"(?<![\d零〇一二两三四五六七八九十])(?P<month>" + _DATE_NUMBER + r")月(?P<day>" + _DATE_NUMBER + r")(?:日|号)")
_DAY = re.compile(r"(?<![\d零〇一二两三四五六七八九十])(?P<day>" + _DATE_NUMBER + r")(?:日|号)")
_SHORT_DATE = re.compile(r"(?<![\d:/-])(?P<month>\d{1,2})[-/](?P<day>\d{1,2})(?![\d:/-])")
_NUMERIC_CHAIN = re.compile(r"(?<!\d)\d{1,2}[-/]\d{1,2}[-/]\d{1,2}(?::\d{2})?(?!\d)")
_RELATIVE = re.compile(r"昨晚|(?<!大)前天|昨日|昨天|今日|今天")
_SAMPLE = re.compile(r"当前样本|演示样本|样本窗口|模拟样本")
_LATEST = re.compile(r"最新|最近(?=热点|热榜)")
_PERIOD = r"上午|早上|早晨|清晨|下午|晚上|晚间|中午|早|晚"
_NUMBER = r"\d{1,3}|[零〇一二两三四五六七八九十]{1,3}"
_HOUR_RANGE = re.compile(
    r"(?<![\d:])(?P<start_period>" + _PERIOD + r")?\s*"
    r"(?P<start>" + _NUMBER + r")"
    r"(?:(?P<start_colon>:\d{2})|(?P<start_unit>点|时)"
    r"(?P<start_fraction>整|半|一刻|三刻|(?:\d{1,2}|[零〇一二三四五六七八九十]{1,3})分?)?)?"
    r"\s*(?:至|到|[-–—~～])\s*"
    r"(?P<end_period>" + _PERIOD + r")?\s*(?P<end>" + _NUMBER + r")"
    r"(?:(?P<end_colon>:\d{2})|(?P<end_unit>点|时)"
    r"(?P<end_fraction>整|半|一刻|三刻|(?:\d{1,2}|[零〇一二三四五六七八九十]{1,3})分?)?)?"
    r"(?![\d:零〇一二两三四五六七八九十])"
)
_DATE_CONTEXT = re.compile(r"(?:日期|日子)(?:为|是|在|：|:)?\s*$")
_TIME_CONTEXT = re.compile(r"(?:时段|时间)(?:为|是|在|：|:)?\s*$")
_DATE_SUFFIX = re.compile(r"\s*(?:日|号|当天|这天|那天)")
_UNRESOLVED_TIME = re.compile(
    r"上周|本周|这周|下周|上月|上个月|本月|这个月|下月|下个月|去年|明年|前年|今年|"
    r"明天|大前天|今早|今晚|昨早|昨夜|过去|最近\s*[\d零〇一二两三四五六七八九十]|"
    r"近\s*[\d零〇一二两三四五六七八九十]|"
    r"(?:\d+|[零〇一二两三四五六七八九十]+)\s*(?:年|月|日|号|点|时|小时|分钟)|"
    r"(?<!\d)\d{1,3}:\d{1,2}|(?:早|晚)[\d零〇一二两三四五六七八九十]|"
    r"上午|早上|早晨|清晨|下午|晚上|晚间|中午|半夜|凌晨"
)
_FILTER_CONTEXT = re.compile(
    r"(?:来源|标题|关键词|作者|包含|匹配|超过|大于|小于|不少于|不低于|至少|至多)"
    r"[^，,。;；\s]{0,16}$"
)


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < right and end > left for left, right in spans)


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    # NUL is an internal separator: removing a date must not concatenate two
    # numbers into a previously nonexistent hour range.
    chars = list(text)
    for start, end in spans:
        chars[start:end] = "\0" * (end - start)
    return "".join(chars)


def _remove(text: str, spans: list[tuple[int, int]]) -> str:
    pieces = []
    previous = 0
    for start, end in sorted(spans):
        pieces.extend((text[previous:start], " "))
        previous = end
    pieces.append(text[previous:])
    return "".join(pieces)


def _filter_value(text: str, start: int) -> bool:
    return bool(_FILTER_CONTEXT.search(text[:start]))


def _calendar_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    digits = {char: number for char, number in zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9))}
    if value in digits:
        return digits[value]
    match = re.fullmatch(r"([一二两三四五六七八九]?)十([一二三四五六七八九]?)", value)
    if match:
        return (digits[match[1]] if match[1] else 1) * 10 + (digits[match[2]] if match[2] else 0)
    raise ValueError("ambiguous Chinese date")


def _hour_number(value: str) -> int:
    if value.isdecimal():
        return int(value)
    digits = {char: number for char, number in zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9))}
    if value in digits:
        return digits[value]
    if value == "十":
        return 10
    if re.fullmatch(r"十[一二三四五六七八九]", value):
        return 10 + digits[value[1]]
    if re.fullmatch(r"二十[一二三四]?", value):
        return 20 + (digits[value[2]] if len(value) == 3 else 0)
    raise ValueError("ambiguous Chinese hour")


def _whole_hour(colon: str | None, fraction: str | None) -> bool:
    if colon is not None:
        return colon == ":00"
    if fraction in (None, "整"):
        return True
    return fraction.rstrip("分") in {"0", "00", "零", "〇"}


def _period_hour(hour: int, period: str | None, *, end: bool, start: int | None = None) -> int:
    if period is None:
        return hour
    if period in {"早", "早上", "早晨", "清晨", "上午"}:
        if 0 <= hour < 12:
            return hour
        raise ValueError("ambiguous morning hour")
    if period in {"下午", "晚上", "晚间", "晚"}:
        if 1 <= hour < 12:
            return hour + 12
        if 13 <= hour <= 23:
            return hour
        if period != "下午" and hour == 12 and end and start == 23:
            return 24
        raise ValueError("ambiguous afternoon/evening hour")
    if period == "中午":
        if hour in {11, 12, 13}:
            return hour
        if hour == 1:
            return 13
        raise ValueError("ambiguous noon hour")
    raise ValueError("unknown time period")


def extract_query_time(text: str, *, now: datetime, timezone: str) -> QueryTimeRequirements:
    """Parse common explicit phrases; unknown/ambiguous time never becomes sample.

    ``dates`` contains explicit calendar dates, with omitted year/month filled
    from the supplied server clock. Relative days remain named expressions so
    the caller resolves them with that same clock. Bare ranges need a direct,
    unambiguous date anchor or an explicit time marker.
    """
    if not isinstance(text, str):
        raise ValueError("question must be text")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("query clock must be timezone aware")
    try:
        clock = now.astimezone(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("unknown query timezone") from exc
    consumed: list[tuple[int, int]] = []
    blocked: list[tuple[int, int]] = []
    anchors: list[tuple[int, int, date | None, str | None]] = []
    expressions: set[str] = set()
    calendar_dates: list[tuple[int, date]] = []
    hours: list[tuple[int, tuple[int, int]]] = []
    conditions: list[str] = []
    assumptions: list[str] = []
    clarify = False

    def reject(code: str):
        nonlocal clarify
        clarify = True
        if code not in conditions:
            conditions.append(code)

    for match in _NUMERIC_CHAIN.finditer(text):
        blocked.append(match.span())
        reject("ambiguous_time_expression")

    for pattern, kind in ((_FULL_DATE, "full"), (_MONTH_DAY, "month"), (_DAY, "day")):
        for match in pattern.finditer(text):
            if _overlaps(*match.span(), blocked) or _filter_value(text, match.start()):
                continue
            blocked.append(match.span())
            try:
                year = int(match["year"]) if kind == "full" else clock.year
                month = (_calendar_number(match["cn_month"] or match["month"]) if kind == "full" else
                         _calendar_number(match["month"]) if kind == "month" else clock.month)
                day = _calendar_number(match["cn_day"] or match["day"]) if kind == "full" else _calendar_number(match["day"])
                value = date(year, month, day)
            except ValueError:
                reject("invalid_date")
                continue
            consumed.append(match.span())
            anchors.append((*match.span(), value, None))
            calendar_dates.append((match.start(), value))
            expressions.add("date")
            if kind != "full":
                omitted = "年份" if kind == "month" else "年份和月份"
                assumptions.append(f"“{match.group()}”未注明{omitted}，按服务端时区{timezone}补全为{value.isoformat()}。")

    for match in _RELATIVE.finditer(text):
        if _overlaps(*match.span(), blocked) or _filter_value(text, match.start()):
            continue
        word = match.group()
        kind, delta = ("day_before_yesterday", -2) if word == "前天" else (
            ("yesterday", -1) if word in {"昨日", "昨天", "昨晚"} else ("today", 0))
        consumed.append(match.span())
        blocked.append(match.span())
        anchors.append((*match.span(), clock.date() + timedelta(days=delta), "晚上" if word == "昨晚" else None))
        expressions.add(kind)

    for pattern, kind in ((_SAMPLE, "sample"), (_LATEST, "latest")):
        for match in pattern.finditer(text):
            if _overlaps(*match.span(), blocked) or _filter_value(text, match.start()):
                continue
            consumed.append(match.span())
            blocked.append(match.span())
            anchors.append((*match.span(), None, None))
            expressions.add(kind)

    # A short M-D is a date only with independent context. An unmarked 8-9
    # must not silently become August 9 rather than an hour or numeric filter.
    for match in _SHORT_DATE.finditer(text):
        if _overlaps(*match.span(), blocked) or _filter_value(text, match.start()):
            continue
        prefix_context = bool(_DATE_CONTEXT.search(text[:match.start()]))
        suffix = text[match.end():]
        suffix_context = _DATE_SUFFIX.match(suffix)
        following_hour = _HOUR_RANGE.match(suffix.lstrip(" ，,的"))
        marked_hour = following_hour and any(following_hour[name] is not None for name in (
            "start_period", "end_period", "start_unit", "end_unit", "start_colon", "end_colon"))
        period_context = bool(re.match(r"\s*(?:" + _PERIOD + r")", suffix))
        follows_date = any(right <= match.start() and not text[right:match.start()].strip(" ，,的")
                           for _, right, _, _ in anchors)
        if follows_date and not prefix_context and suffix_context is None:
            continue
        # An isolated descending/equal pair such as 10-9 is not a positive
        # one-hour 24h range. In an otherwise plain news/date request it can
        # name M-D; the caller still has to ask for the missing hour. Positive
        # ranges such as 8-9 remain ambiguous without independent context.
        plain_prefix = bool(re.fullmatch(r"(?:(?:查询|查看|看看|请|帮我|我想|热点|新闻|的)|\s)*", text[:match.start()]))
        plain_suffix = (not suffix.strip(" ，,。？！?!；;") or
                        bool(re.match(r"\s*(?:的)?(?:热点|新闻|热榜|榜单)", suffix)))
        standalone_date = int(match["month"]) >= int(match["day"]) and plain_prefix and plain_suffix
        if not (prefix_context or suffix_context or marked_hour or period_context or standalone_date):
            continue
        blocked.append(match.span())
        try:
            value = date(clock.year, int(match["month"]), int(match["day"]))
        except ValueError:
            reject("invalid_date")
            continue
        consumed.append(match.span())
        anchors.append((*match.span(), value, None))
        calendar_dates.append((match.start(), value))
        expressions.add("date")
        assumptions.append(f"“{match.group()}”未注明年份，按服务端时区{timezone}补全为{value.isoformat()}。")

    effective_dates = {value for _, _, value, _ in anchors if value is not None}
    masked = _mask(text, blocked)
    for match in _HOUR_RANGE.finditer(masked):
        if _filter_value(text, match.start()):
            continue
        # Row budgets and other numeric ranges must stay available to the
        # independent metric/filter parser, even after a valid date.
        if re.search(r"(?:前|top\s*)\s*$", text[:match.start()], re.I) or re.match(
            r"\s*(?:条|篇|个|名|项)", text[match.end():]
        ):
            continue
        nearby = [(left, right, value, hint) for left, right, value, hint in anchors
                  if right <= match.start() and not text[right:match.start()].strip(" ，,的")]
        start_period = match["start_period"] or (nearby[-1][3] if nearby else None)
        end_period = match["end_period"] or start_period
        marked = any(match[name] is not None for name in (
            "start_period", "end_period", "start_unit", "end_unit", "start_colon", "end_colon"))
        time_context = bool(_TIME_CONTEXT.search(text[:match.start()]))
        if not marked and not time_context and not (nearby and len(effective_dates) <= 1 and len(expressions) == 1):
            reject("ambiguous_time_expression")
            continue
        try:
            start = _period_hour(_hour_number(match["start"]), start_period, end=False)
            end = _period_hour(_hour_number(match["end"]), end_period, end=True, start=start)
        except ValueError:
            reject("ambiguous_hour")
            continue
        if not (0 <= start <= 23 and 1 <= end <= 24 and end - start == 1
                and _whole_hour(match["start_colon"], match["start_fraction"])
                and _whole_hour(match["end_colon"], match["end_fraction"])):
            reject("invalid_hour_window")
            continue
        consumed.append(match.span())
        hours.append((match.start(), (start, end)))
        if start_period is None and match["end_period"] is None:
            assumptions.append(f"“{text[match.start():match.end()]}”按24小时制解析为{start:02d}:00至{end:02d}:00。")

    remaining = _mask(text, consumed)
    if _UNRESOLVED_TIME.search(remaining) or _NUMERIC_CHAIN.search(remaining):
        reject("unsupported_time_expression")
    # Any otherwise-unconsumed bare numeric range could be a date, time or
    # value condition. Preserve it and ask, rather than defaulting to sample.
    if _SHORT_DATE.search(remaining) or _HOUR_RANGE.search(remaining):
        reject("ambiguous_time_expression")
    return QueryTimeRequirements(
        query_text=_remove(text, consumed), time_expressions=frozenset(expressions),
        dates=tuple(value for _, value in sorted(calendar_dates)),
        hours=tuple(value for _, value in sorted(hours)),
        unsupported_conditions=tuple(conditions), requires_clarification=clarify,
        assumptions=tuple(dict.fromkeys(assumptions)),
    )
