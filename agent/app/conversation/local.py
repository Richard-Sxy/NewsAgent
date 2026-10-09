"""本地规则模拟。local_conversation_plan() 根据关键词和历史工具调用结果生成行动计划，用于验证多轮聊天链路，不代表真实模型的语言能力。"""

import json
import re
from datetime import datetime
from uuid import UUID

from app.conversation.plan import ConversationPlan

"""时间信息+新闻关键词匹配。"""
def has_dated_news_request(message: str) -> bool:
    return has_requested_time(message) and any(
        word in message for word in ("新闻", "热点", "热门", "热榜", "榜单", "排行")
    )

"""为了匹配：关键字(时间) 或者 2012-07-07 的信息内容。同时防止 UUID 做匹配。"""
def has_requested_time(message: str) -> bool:
    return bool(re.search(
        r"今日|今天|昨日|昨天|明天|前天|本周|上周|本月|上月|今早|今晚"
        r"|(?<![A-Za-z\d])\d{4}[-年/]\d{1,2}[-月/]\d{1,2}日?(?![A-Za-z\d-])", message,
    ))


def local_conversation_summary(payload: dict) -> str:
    """Extractive test double only; enterprise semantic quality needs acceptance."""
    budget = int(payload["max_summary_chars"])
    previous = str(payload.get("previous_summary", ""))
    text = str(payload["conversation_fragment"])
    # Keep the first and latest material in this deterministic fixture. Never
    # claim it provides semantic summarization or instruction classification.
    joined = previous + "\n" + text
    while len(json.dumps(joined, ensure_ascii=False)) > budget:
        width = max(1, len(joined) // 3)
        joined = joined[:width] + " … " + joined[-width:]
    return json.dumps({"summary": joined.strip()}, ensure_ascii=False)

"""本地对话规划器：有限的多轮对话模型。"""
def local_conversation_plan(payload: dict) -> str:
    message = str(payload["message"]).strip()
    tools = payload.get("tool_results", [])
    history = payload.get("history", [])
    analysis_enabled = any(item["name"] == "analyze_hot_news_data" for item in payload.get("available_tools", []))
    wants_analysis = analysis_enabled and any(word in message.lower() for word in (
        "数据分析", "统计", "概况", "分布", "中位", "p90", "比较", "对比", "质量检查", "趋势", "基线"))
    wants_trend = wants_analysis and any(word in message for word in ("趋势", "跨窗口", "两窗口", "两个窗口"))
    has_requested_date = has_requested_time(message)
    query_available = any(tool["name"] == "query_hot_news" for tool in payload.get("available_tools", []))
    asks_hot_news = any(word in message for word in ("热点", "热门", "热榜", "榜单", "新闻排行")) or (
        has_requested_date and "新闻" in message
    )

    def respond(answer):
        return ConversationPlan(action="respond", answer=answer).model_dump_json()

    def call(name, **arguments):
        return ConversationPlan(action="tool", tool_name=name, arguments=arguments).model_dump_json()

    def analyze(run_id):
        operation = "overview"
        for words, selected in ((("基线",), "baseline"), (("趋势", "跨窗口", "两窗口", "两个窗口"), "trend"),
                                (("质量", "异常", "校验"), "quality"),
                                (("分布", "中位", "p90"), "distribution"),
                                (("比较", "对比"), "compare")):
            if any(word in message.lower() for word in words):
                operation = selected
                break
        metric = "ctr"
        for words, selected in ((("点击率", "ctr"), "ctr"), (("点击量", "点击数"), "clicks"),
                                (("曝光",), "impressions"), (("时长",), "total_duration_seconds"),
                                (("有效消费",), "effective_consumptions"), (("互动",), "interactions"),
                                (("热度",), "hot_score")):
            if any(word in message.lower() for word in words):
                metric = selected
                break
        if operation == "trend":
            candidates = known_windows()
            if len(candidates) < 2:
                return respond("当前没有两个已完成窗口的来源，无法生成跨窗口趋势；请先读取更多已完成热点运行。")
            return call("analyze_hot_news_data", run_id=candidates[0]["run_id"],
                        reference_run_id=candidates[1]["run_id"], operation=operation, metric=metric)
        return call("analyze_hot_news_data", run_id=run_id, operation=operation, metric=metric)

    def known_windows():
        found = {}
        all_traces = [item for turn in history for item in turn.get("tools", [])] + list(tools)
        for trace in all_traces:
            if trace.get("status") != "completed":
                continue
            result = trace.get("result", {})
            if trace["name"] == "list_hot_news":
                records = result.get("items", [])
            elif trace["name"] in {"read_hot_news", "query_hot_news"} and not result.get("not_found"):
                records = [result]
            elif trace["name"] == "analyze_hot_news_data":
                source = result.get("source", {})
                records = [source, source.get("reference", {})]
            else:
                records = []
            for record in records:
                try:
                    start = datetime.fromisoformat(record["window_start"])
                    end = datetime.fromisoformat(record["window_end"])
                    if start.tzinfo is None or end.tzinfo is None or start >= end:
                        continue
                    found[record["run_id"]] = {**record, "start": start, "end": end}
                except (KeyError, TypeError, ValueError):
                    continue
        ordered = sorted(found.values(), key=lambda item: (item["start"], item["run_id"]), reverse=True)
        if not ordered:
            return []
        current = ordered[0]
        reference = next((item for item in ordered[1:] if item["end"] <= current["start"]), None)
        return [current, reference] if reference else [current]

    if any(word in message.lower() for word in ("删除", "drop ", "发布", "修改配置", "系统指令", "忽略规则", "执行命令", "密钥")):
        return respond("聊天工具只有只读权限，不能删除数据、运行命令、修改配置或发布内容。请使用既有人工审批流程。")
    if not tools and message.startswith("读取热点运行"):
        match = re.fullmatch(r"读取热点运行 ([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})", message)
        if match is None:
            return respond("请使用完整格式读取热点运行，并提供规范的运行标识。")
        try:
            run_id = str(UUID(match.group(1)))
        except ValueError:
            return respond("运行标识未通过格式校验，未读取热点。")
        # A read still passes through the tenant-scoped Port. Only its completed
        # result can authorize a later analysis; this is not a direct analyze path.
        return call("read_hot_news", run_id=run_id)
    if tools:
        latest = tools[-1]
        if latest.get("result", {}).get("query_input_error") or (latest["name"] == "query_hot_news" and (
            latest.get("result", {}).get("query_resolution", {}).get("status") in {"clarify", "unsupported"}
        )):
            return respond("本轮尚未执行取数，查询条件和可用范围的具体说明见下方。")
        if tools[-1]["status"] != "completed":
            return respond("工具没有成功返回证据，本轮明确降级为失败说明，不编造数据。")
        latest = tools[-1]
        if wants_analysis and latest["name"] in {"read_hot_news", "query_hot_news"} and not latest["result"].get("not_found"):
            return analyze(latest["result"]["run_id"])
        if wants_analysis and latest["name"] == "list_hot_news" and latest["result"].get("items"):
            return analyze(latest["result"]["items"][0]["run_id"])
        if latest["name"] == "analyze_hot_news_data":
            return respond("已通过隔离执行器完成确定性分析。结果仅覆盖所关联的热点榜单，数值、范围和数据质量提示见计算快照。")
        if latest["name"] == "query_hot_news":
            return respond("本轮已按问题查询模拟数仓，并复用热点分析链路。以下展示取数与分析的已保存结果；当前解释仍为规则模拟。")
        if latest["name"] == "list_hot_news" and latest["result"].get("items"):
            return call("read_hot_news", run_id=latest["result"]["items"][0]["run_id"])
        return respond("以下内容来自同一租户的已完成运行或新闻知识；指标保持工具快照原值。")
    if has_dated_news_request(message) and not query_available:
        return respond("当前权限只支持读取已保存的热点报告，不能按指定日期发起新查询；已有报告不能冒充该日期的热点。")
    if wants_analysis:
        fresh_query = has_requested_date or any(word in message for word in ("查询", "重新取数"))
        if fresh_query and any(item["name"] == "query_hot_news" for item in payload.get("available_tools", [])):
            if len(message) > 1000:
                return respond("查询问题超出当前数仓工具长度限制，请缩短问题后再查询。")
            return call("query_hot_news", question=message)
        if not fresh_query and "最近" not in message and "最新" not in message:
            if wants_trend and len(known_windows()) >= 2:
                return analyze(known_windows()[0]["run_id"])
            for turn in reversed(history):
                for trace in reversed(turn.get("tools", [])):
                    if trace["status"] != "completed":
                        continue
                    result = trace["result"]
                    if trace["name"] in {"read_hot_news", "query_hot_news"}:
                        return analyze(result["run_id"])
                    if trace["name"] == "analyze_hot_news_data":
                        return analyze(result["source"]["run_id"])
        return call("list_hot_news", limit=5 if wants_trend else 1)
    if any(word in message for word in ("检索", "搜索", "知识")):
        query = re.sub(r"^(请|帮我|检索|搜索|知识库|一下|\s)+", "", message).strip() or message
        return call("search_knowledge", query=query[:500], limit=3)
    if "查询" not in message and any(word in message for word in ("第一", "第1", "这条", "它", "详细", "解释")):
        for turn in reversed(history):
            for trace in reversed(turn.get("tools", [])):
                if trace["name"] in {"read_hot_news", "query_hot_news"} and trace["status"] == "completed":
                    return call("read_hot_news", run_id=trace["result"]["run_id"], news_rank=1)
        return respond("当前对话还没有关联热点报告。请先说“查看最近热点”，再追问其中的新闻。")
    if query_available and (asks_hot_news or any(
        word in message for word in ("查询", "排行", "点击", "曝光", "互动", "热度")
    )):
        if message == "查看最近热点":
            return call("list_hot_news", limit=5)
        if len(message) > 1000:
            return respond("查询问题超出当前数仓工具长度限制，请缩短问题后再查询。")
        return call("query_hot_news", question=message)
    if asks_hot_news and has_requested_date:
        return respond("当前权限只支持读取已保存的热点报告，不能按指定日期发起新查询；已有报告不能冒充该日期的热点。")
    if asks_hot_news:
        return call("list_hot_news", limit=5)
    if any(word in message for word in ("记得", "刚才", "上一", "之前")):
        if history:
            latest = history[-1].get("user", history[-1].get("summary", ""))
            return respond(f"当前会话中，你上一轮说的是：{latest}。我只使用这个会话的有界上下文。")
        return respond("这是新会话，尚无上一轮消息。不同会话、不同用户的内容不会合并。")
    if any(word in message for word in ("你好", "您好")):
        return respond("你好，我是 NewsAgent。可以查看最近热点、追问第一条新闻，或检索相关新闻；当前是本地规则模拟。")
    if any(word in message for word in ("工具", "能做", "能力", "帮助")):
        return call("capabilities")
    return respond("已收到这轮消息，可以继续追问。当前本地规则模拟支持查看最近热点、解释第一条新闻、知识检索和回顾上一轮；开放式语言能力需接企业模型验收。")
