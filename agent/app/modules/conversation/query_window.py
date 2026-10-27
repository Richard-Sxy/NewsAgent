"""Resolve calendar intent before checking the selected adapter's capabilities.

A date is a complete request. Missing data or cross-bucket UV support is a
source limitation, never a reason to ask the user to invent a one-hour range.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class RequestedWindow:
    start: datetime | None
    end: datetime | None
    reason: str | None = None
    message: str = ""


def resolve_requested_window(proposal, policy, clock: datetime) -> RequestedWindow:
    zone = ZoneInfo(policy.timezone)
    clock = clock.astimezone(zone)
    sample_start = policy.supported_window_start.astimezone(zone)
    sample_end = policy.supported_window_end.astimezone(zone)
    coverage_start = (policy.coverage_window_start or sample_start).astimezone(zone)
    coverage_end = (policy.coverage_window_end or sample_end).astimezone(zone)
    watermark = (policy.data_watermark or coverage_end).astimezone(zone)
    complete_now = clock.replace(minute=0, second=0, microsecond=0)
    available_end = min(watermark, coverage_end, complete_now)
    expression = proposal.time_expression
    relative = {"today": 0, "yesterday": 1, "day_before_yesterday": 2}
    target = (proposal.explicit_date if expression == "date" else
              clock.date() - timedelta(days=relative[expression]) if expression in relative else
              (available_end - timedelta(hours=1)).date() if expression == "latest" else
              sample_start.date())
    try:
        if proposal.hour_start is not None:
            midnight = datetime.combine(target, datetime.min.time(), tzinfo=zone)
            start = midnight + timedelta(hours=proposal.hour_start)
            end = midnight + timedelta(hours=proposal.hour_end)
        elif expression == "sample":
            start, end = sample_start, sample_end
        elif expression == "latest":
            end = available_end
            start = end - (sample_end - sample_start)
            if policy.coverage_window_start is None and watermark != sample_end:
                return RequestedWindow(start, end, "latest_window_unavailable",
                    "最新完整数据窗口不在当前批准窗口内，没有自动替换查询范围。")
        else:
            start = datetime.combine(target, datetime.min.time(), tzinfo=zone)
            end = start + timedelta(days=1)
            if target == clock.date():
                # Preserve today's date even if the source watermark is stale.
                # Never produce an end in a previous day or relabel old data.
                end = min(complete_now, available_end) if available_end > start else complete_now
    except OverflowError:
        return RequestedWindow(None, None, "invalid_proposal", "日期超出可表示范围，没有执行查询。")

    if end <= start:
        if proposal.hour_start is None and target == clock.date():
            return RequestedWindow(start, end, "no_complete_hour",
                "已理解为今日累计热点，但今天尚无完整数据时间桶，请等待数据更新；无需补充某个小时。")
        return RequestedWindow(start, end, "window_not_approved",
            "结束时间必须晚于开始时间，没有缩短或替换请求范围。")

    if start < coverage_start or end > available_end:
        return RequestedWindow(start, end, "time_coverage_unavailable",
            f"已理解查询范围为{start:%Y-%m-%d %H:%M}至{end:%Y-%m-%d %H:%M}（{policy.timezone}）。"
            "当前数据源未完整覆盖这段时间，未执行查询；这是数据覆盖不足，无需补充某个小时，也没有替换为历史样本。")

    # This contract describes the actual hourly fixture adapter. In particular,
    # coverage across many hours does not establish exact cross-hour UV.
    if end - start != timedelta(hours=1):
        return RequestedWindow(start, end, "window_not_approved",
            f"已理解查询范围为{start:%Y-%m-%d %H:%M}至{end:%Y-%m-%d %H:%M}（{policy.timezone}）。"
            "当前聊天数据源只有小时聚合，缺少该区间的跨小时去重用户数，尚不能计算区间热点。"
            "需要接入支持区间去重聚合的数据源；无需改写时间表达，没有缩短查询范围。")
    if policy.coverage_window_start is None and (start, end) != (sample_start, sample_end):
        return RequestedWindow(start, end, "window_not_approved",
            "已理解时间范围，但该窗口不在当前数据源批准范围内，没有执行查询。")
    return RequestedWindow(start, end)
