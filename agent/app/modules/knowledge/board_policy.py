"""Versioned news-board admission, before model calls or index writes.

Only explicit classification fields are inspected. Body/title keywords and
inferred audience/content tags are not authoritative board classifications.
Missing/unknown boards retain the existing path; callers must supply the source
board to guarantee exclusion. This policy does not delete historical data.
"""

from collections.abc import Mapping
import unicodedata

from app.modules.knowledge.document import IngestOutcome, IngestStatus, KnowledgeDocument


POLICY_VERSION = "news-board-admission-v1"
EXCLUDED_BOARDS = frozenset({"health"})
BOARD_FIELDS = ("board", "board_id", "category", "channel", "source_topic")
_HEALTH_ALIASES = frozenset({"health", "健康", "健康频道", "健康板块", "news_news_health"})


def board_exclusion_detail(metadata: Mapping[str, str]) -> str | None:
    """Any explicit excluded board wins when multiple source fields disagree."""
    for key in BOARD_FIELDS:
        value = metadata.get(key, "")
        if not isinstance(value, str):
            raise TypeError(f"news board field {key} must be a string")
        normalized = unicodedata.normalize("NFKC", value).strip().casefold()
        board = "health" if normalized in _HEALTH_ALIASES else normalized
        if board in EXCLUDED_BOARDS:
            return f"news_board_excluded: board={board}; policy={POLICY_VERSION}"
    return None


def excluded_board_outcome(document: KnowledgeDocument) -> IngestOutcome | None:
    detail = board_exclusion_detail(document.metadata)
    if detail is None:
        return None
    return IngestOutcome(document.document_id, IngestStatus.SKIPPED, detail=detail)


class NewsBoardExcluded(ValueError):
    """Direct lifecycle accept callers must not enqueue excluded news."""

