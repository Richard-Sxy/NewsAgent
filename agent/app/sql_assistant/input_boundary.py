"""检查用户输入。规范问题文字，筛查手写 SQL、绕过指令、跨租户查询、敏感信息等内容，是进入模型前的第一道检查。"""

import re
import unicodedata


class SqlAssistantQuestionError(ValueError):
    retryable = False

"""SQL 查询助手和审计的信号检测和规范化"""
_SIGNALS = (
    ("raw_sql", re.compile(
        r"(?<![A-Za-z0-9_])(?:SELECT|WITH|FROM|UNION|JOIN|LIMIT)(?![A-Za-z0-9_])|GROUP\s+BY|ORDER\s+BY"
        r"|\bOR\s+\d+\s*=\s*\d+|--|/\*|\*/", re.I,
    )),
    ("instruction_override", re.compile(
        r"(?:忽略|无视|忘记).{0,20}(?:规则|系统|指令|提示|之前)"
        r"|\b(?:ignore|disregard|forget|override|bypass)\b.{0,80}\b(?:instructions?|rules?|prompts?|system|safety)\b"
        r"|<\|(?:im_start|im_end|system|assistant)\|>|\[/?INST\]", re.I,
    )),
    ("cross_tenant_request", re.compile(
        r"(?:其他|跨|所有|别的).{0,8}租户"
        r"|\b(?:other|all|cross|across)\b.{0,30}\btenants?\b", re.I,
    )),
    ("privileged_action_request", re.compile(
        r"删除|修改数据|更新数据|删表|执行命令"
        r"|\b(?:DROP|DELETE|INSERT|UPDATE|ALTER|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE)\b", re.I,
    )),
    ("sensitive_data_request", re.compile(
        r"用户明细|用户ID|user_id|密码|密钥|pg_read"
        r"|\b(?:passwords?|secrets?|api[_ ]?keys?)\b", re.I,
    )),
)

"""拒绝隐藏控制文本；折叠全角表格而不隐藏意图。"""
def normalize_question(question: str, *, max_chars: int = 1000) -> str:
    if not isinstance(question, str) or not 1 <= len(question) <= max_chars:
        raise SqlAssistantQuestionError(f"查询问题必须是1至{max_chars}字符的文本")
    normalized = unicodedata.normalize("NFKC", question)
    if any(unicodedata.category(char).startswith("C") and char not in "\t\r\n" for char in normalized):
        raise SqlAssistantQuestionError("查询问题包含不可见或控制字符，请使用可见文本")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    if not normalized or len(normalized) > max_chars:
        raise SqlAssistantQuestionError("查询问题为空或规范化后超过长度限制")
    return normalized

"""合并拆开的字母/删除全部的空白"""
def question_signals(normalized_question: str) -> tuple[str, ...]:
    spelled = re.sub(
        r"(?<![A-Za-z])(?:[A-Za-z]\s+)+[A-Za-z](?![A-Za-z])",
        lambda match: re.sub(r"\s+", "", match.group()),
        normalized_question,
    )
    forms = (normalized_question, spelled, re.sub(r"\s+", "", normalized_question))
    return tuple(name for name, pattern in _SIGNALS if any(pattern.search(form) for form in forms))

"""前端屏幕的输出内容。"""
def screen_question(question: str) -> str:
    normalized = normalize_question(question)
    signals = question_signals(normalized)
    if "raw_sql" in signals:
        raise SqlAssistantQuestionError("请输入新闻指标问题，不提交手写 SQL")
    if signals:
        raise SqlAssistantQuestionError("此问题超出新闻聚合数据的只读查询范围，请查询当前租户的新闻指标")
    return normalized
