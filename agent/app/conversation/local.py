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
    available_names = {item["name"] for item in payload.get("available_tools", [])}
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

    # Publication filters are query semantics; keep them in the original tool
    # question for the resolver to reject. A separate publish action still blocks.
    action_text = re.sub(r"发布(?:的|时间|日期)", "", message.lower())
    if any(word in action_text for word in ("删除", "drop ", "发布", "批准", "激活", "回滚", "二审", "晋升", "approve", "activate", "rollback", "系统指令", "忽略规则", "执行命令", "密钥")):
        return respond("对话不能删除数据、执行审批、配置激活、回滚或发布；请在现有工作台按人工审批流程处理。")
    if not tools and any(word in message for word in ("评测", "评估运行", "DataLoop", "data loop")):
        if any(word in message for word in ("进度", "状态", "跑完", "完成", "结果")):
            if "evaluation_status" not in available_names:
                return respond("当前没有读取评测运行的权限或运行环境不可用。")
            workflow = re.search(r"hot-news-data-loop-[0-9a-f]{64}(?![0-9a-f])", message)
            if workflow:
                return call("evaluation_status", workflow_id=workflow.group())
            for turn in reversed(history):
                for trace in reversed(turn.get("tools", [])):
                    run = trace.get("result", {}).get("run", {})
                    if trace.get("status") == "completed" and run.get("workflow_id"):
                        return call("evaluation_status", workflow_id=run["workflow_id"])
            return respond("当前会话没有评测运行引用，请提供真实 Workflow ID。")
        if any(word in message for word in ("启动", "发起", "开始", "运行")):
            if "start_data_loop" not in available_names:
                return respond("当前没有发起评测的权限或运行环境不可用。")
            candidate = re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", message, flags=re.I)
            if candidate:
                return call("start_data_loop", candidate_id=str(UUID(candidate.group())))
            for turn in reversed(history):
                for trace in reversed(turn.get("tools", [])):
                    candidate = trace.get("result", {}).get("candidate", {})
                    if trace.get("status") == "completed" and candidate.get("candidate_id"):
                        return call("start_data_loop", candidate_id=candidate["candidate_id"])
            return respond("请提供待评测候选 ID；确认卡片会读取真实冻结数据集和门禁策略。")
    if (not tools and any(word in message for word in ("配置", "Bundle", "bundle", "model_version", "analysis_prompt_version", "重排"))
            and any(word in message for word in ("候选", "调成", "调为", "修改", "重排参数"))):
        if any(word in message for word in ("重排", "reranker", "vector_weight", "entity_weight", "minimum_score")):
            return respond("当前 Bundle 和回放契约不支持重排参数变更，此建议未落地；需先增加版本化重排资产与完整证据重放。")
        if "propose_bundle_candidate" not in available_names:
            return respond("当前没有可用的已注册候选预设或提交权限；建议未创建配置候选。")
        match = re.search(r"(model_version|analysis_prompt_version)\s*(?:=|：|:|调为|调成|改为|改成)\s*([A-Za-z0-9._:/-]+)", message)
        if match:
            return call("propose_bundle_candidate", parameter=match.group(1), value=match.group(2), reason=message[:500])
        return respond("请指定已注册的 model_version 或 analysis_prompt_version 和目标版本；完整规格与差异由服务端构造。")
    if not tools and any(word in message for word in ("沉淀", "保存", "记录")) and any(word in message for word in ("案例", "反馈")):
        if "save_feedback_case" not in available_names:
            return respond("当前没有记录此类反馈的权限，请使用评审台或联系网关管理员。")
        for turn in reversed(history):
            for trace in reversed(turn.get("tools", [])):
                result = trace.get("result", {})
                if trace.get("name") in {"read_hot_news", "query_hot_news"} and trace.get("status") == "completed" and result.get("items"):
                    label = re.search(r"(?:期望标签|期望|标签草稿)[：:为\s]*(.+)$", message)
                    draft = label.group(1).strip() if label else "需人工核对事实、证据与归因。"
                    if len(draft) > 96:
                        return respond("期望标签草稿过长，请缩短后再沉淀反馈。")
                    return call("save_feedback_case", run_id=result["run_id"], news_id=result["items"][0]["news_id"],
                                issue_type="analysis_incorrect", expected_label_draft=draft)
        return respond("当前会话没有已读取的新闻分析来源，请先读取热点报告再沉淀反馈。")
    if not tools and re.match(r"^(?:请|帮我|为我|能否|可以|帮忙|我希望你|我想让你|你能|\s)*(?:长期|短期)?(?:记住|记录偏好|保存偏好)", message):
        name = "propose_long_term_memory" if "长期" in message else "save_short_term_memory"
        if name not in available_names:
            return respond("当前没有保存用户记忆的权限，请联系网关管理员。")
        content = re.sub(r"^(?:请|帮我|为我|能否|可以|帮忙|我希望你|我想让你|你能|\s)*(?:长期|短期)?(?:记住|记录偏好|保存偏好)[：:\s]*", "", message).strip()
        if not content or len(content) > 1000:
            return respond("记忆内容为空或过长，请重新描述希望保存的偏好。")
        return call(name, content=content)
    if not tools and any(word in message for word in ("记忆", "偏好", "长期候选", "记住的")):
        if "memory_status" in available_names:
            for turn in reversed(history):
                for trace in reversed(turn.get("tools", [])):
                    memory = trace.get("result", {}).get("memory")
                    if trace.get("status") == "completed" and memory and memory.get("id"):
                        return call("memory_status", memory_id=memory["id"], kind=memory["tier"])
        if "状态" in message or "候选" in message:
            return respond("当前会话没有可读取的记忆引用或相应读取权限；长期候选不会自动晋升。")
    if not tools and any(word in message for word in ("写一篇", "写一份", "写篇", "写个", "撰写", "起草")):
        if "start_writing_intent" not in available_names:
            return respond("当前没有通过聊天启动写作任务的权限，请在写作工作台操作或联系网关管理员。")
        topic = re.sub(r"^(?:请|帮我|为我|写一篇|写一份|写篇|写个|撰写|起草|\s)+", "", message).strip()
        words = re.search(r"(\d+)\s*字", message)
        target = int(words.group(1)) if words else 1000
        if not 200 <= target <= 5000 or not topic or len(topic) > 500:
            return respond("写作要求超出当前参数范围，请缩短主题或调整篇幅后重新描述。")
        tone = "concise" if "简洁" in message else "explanatory" if any(word in message for word in ("解释", "科普")) else "neutral"
        return call("start_writing_intent", topic=topic, target_words=target, tone=tone)
    wants_writing_status = any(word in message for word in ("进度", "阶段", "审批状态", "跑完", "完成了吗"))
    has_writing_history = any(trace.get("name") in {"list_writing_jobs", "writing_job_status", "start_writing_intent"}
        and trace.get("status") == "completed" for turn in history for trace in turn.get("tools", []))
    writing_request = "写作任务" in message or (wants_writing_status and (
        "写作" in message or "任务" in message or has_writing_history))
    if not tools and writing_request:
        if "list_writing_jobs" not in available_names:
            return respond("当前没有读取写作任务的权限，请联系网关管理员授权后再查询。")
        match = re.search(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", message, flags=re.I)
        if match and "writing_job_status" in available_names:
            return call("writing_job_status", job_id=str(UUID(match.group())))
        if wants_writing_status and "writing_job_status" in available_names:
            for turn in reversed(history):
                for trace in reversed(turn.get("tools", [])):
                    if trace.get("status") != "completed":
                        continue
                    result = trace.get("result", {})
                    if trace.get("name") in {"writing_job_status", "start_writing_intent"} and result.get("job"):
                        return call("writing_job_status", job_id=result["job"]["id"])
                    if trace.get("name") == "list_writing_jobs" and result.get("items"):
                        return call("writing_job_status", job_id=result["items"][0]["id"])
        return call("list_writing_jobs", limit=5)
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
        if latest["name"] in {"save_short_term_memory", "propose_long_term_memory", "memory_status"}:
            return respond("已读取本轮用户记忆快照。有效期和候选状态见回执；长期候选仍需人工晋升。")
        if latest["name"] in {"list_writing_jobs", "writing_job_status"}:
            return respond("已读取当前租户的写作任务快照。任务阶段与人工审批状态见回执卡片，审批请在写作工作台完成。")
        if latest["name"] == "evaluation_status":
            return respond("已读取评测运行快照。阶段、门禁和人工审批状态见回执；审批请在 DataLoop 评审台执行。")
        if wants_analysis and latest["name"] in {"read_hot_news", "query_hot_news"} and not latest["result"].get("not_found"):
            return analyze(latest["result"]["run_id"])
        if wants_analysis and latest["name"] == "list_hot_news" and latest["result"].get("items"):
            return analyze(latest["result"]["items"][0]["run_id"])
        if latest["name"] == "analyze_hot_news_data":
            return respond("已通过隔离执行器完成确定性分析。结果仅覆盖所关联的热点榜单，数值、范围和数据质量提示见计算快照。")
        if latest["name"] == "query_hot_news":
            if latest["result"].get("query_resolution", {}).get("response_style") == "summary":
                return respond("已根据本次查询的已保存报告整理热点摘要。时间、候选范围和权威指标见下方。")
            return respond("本轮已按问题查询模拟数仓，并复用热点分析链路。以下展示取数与分析的已保存结果；当前解释仍为规则模拟。")
        if latest["name"] == "list_hot_news" and latest["result"].get("items"):
            return call("read_hot_news", run_id=latest["result"]["items"][0]["run_id"])
        return respond("以下内容来自同一租户的已完成运行或新闻知识；指标保持工具快照原值。")
    if (not has_requested_date and any(word in message for word in ("相关", "关联"))
            and any(word in message for word in ("第一", "第1", "这条", "它", "该新闻"))):
        if not any(tool["name"] == "search_knowledge" for tool in payload.get("available_tools", [])):
            return respond("当前没有新闻知识检索权限，无法召回相关新闻。")
        for turn in reversed(history):
            for trace in reversed(turn.get("tools", [])):
                if trace["name"] not in {"read_hot_news", "query_hot_news"} or trace["status"] != "completed":
                    continue
                items = trace.get("result", {}).get("items", [])
                source = next((item for item in items if item.get("rank") == 1), None)
                if source and source.get("title") and source.get("news_id"):
                    return call("search_knowledge", query=source["title"][:500],
                                source_news_id=source["news_id"], limit=3)
        return respond("当前对话没有可关联的新闻标题和来源，请先查看热点，再追问第一条的相关新闻。")
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
