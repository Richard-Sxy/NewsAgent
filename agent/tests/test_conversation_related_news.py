"""Related-news followups use the source already authorized in the conversation."""

import json

from app.modules.conversation.local import local_conversation_plan


def plan(message, *, history=None, knowledge=True):
    return json.loads(local_conversation_plan({
        "message": message,
        "history": history or [],
        "available_tools": [{"name": "search_knowledge"}] if knowledge else [],
    }))


def source_history():
    return [{"tools": [{"name": "read_hot_news", "status": "completed", "result": {
        "run_id": "33333333-3333-4333-8333-333333333333",
        "items": [{"rank": 1, "news_id": "source-news", "title": "WAIC智能体与机器人进展"}],
    }}]}]


def test_related_followup_uses_source_title_and_excludes_it():
    result = plan("帮我检索第一条的相关新闻", history=source_history())
    assert result["tool_name"] == "search_knowledge"
    assert result["arguments"] == {"query": "WAIC智能体与机器人进展",
                                    "source_news_id": "source-news", "limit": 3}


def test_missing_context_or_permission_does_not_guess_source():
    assert plan("第一条的相关新闻")["action"] == "respond"
    assert plan("第一条的相关新闻", history=source_history(), knowledge=False)["action"] == "respond"


def test_ordinary_explanation_and_dated_requests_keep_existing_routes():
    result = plan("解释第一条", history=source_history())
    assert result["tool_name"] == "read_hot_news"
    assert result["arguments"]["news_rank"] == 1
    assert plan("今天第一条的相关新闻", history=source_history())["action"] == "respond"
