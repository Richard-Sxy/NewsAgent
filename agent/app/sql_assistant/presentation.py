"""Separate finite presentation phrases from data requirements.

Call screen_question on the complete original input first. This helper never
screens input, approves a query, or removes arbitrary clauses/conditions.
"""

import re
from dataclasses import dataclass
from typing import Literal


ResponseStyle = Literal["list", "summary"]
_SUMMARY = re.compile(
    r"(?:帮我|给我|为我|请|麻烦你)?(?:简要|简单|简短|大致)?"
    r"(?:总结|概括|梳理|简要介绍|简单介绍)(?:一下|下)?(?![的过完])"
)
# These exact phrases control length, not row selection. Do not remove a
# phrase embedded in a filter such as “不要太长的新闻” or “不用展开的新闻”.
_BREVITY = re.compile(r"(?:不要太长|不用展开|简短一点|简洁一点)(?=$|[，,。.!！?？;；])")


@dataclass(frozen=True)
class QueryPresentation:
    query_text: str
    response_style: ResponseStyle


def extract_query_presentation(screened_question: str) -> QueryPresentation:
    style: ResponseStyle = "summary" if _SUMMARY.search(screened_question) else "list"
    text = _SUMMARY.sub("", screened_question)
    text = _BREVITY.sub("", text)
    # Only the connector directly left behind by a presentation-only clause.
    text = re.sub(r"(?:并|然后|再)\s*(?=$|[，,。.!！?？;；])", "", text)
    return QueryPresentation(text.strip(" ，,。？！?!；;"), style)
