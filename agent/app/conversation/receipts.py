"""Deterministic receipts built from persisted service snapshots, never model text."""

from enum import StrEnum
from typing import Annotated
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    StringConstraints,
    model_validator,
)


class CapabilityEntityType(StrEnum):
    HOT_NEWS_RUN = "hot_news_run"
    WRITING_JOB = "writing_job"
    FEEDBACK_CASE = "feedback_case"
    MEMORY = "memory"
    CANDIDATE = "candidate"
    EVALUATION_RUN = "evaluation_run"


class CapabilityReceiptStatus(StrEnum):
    LANDED = "landed"
    PENDING_APPROVAL = "pending_approval"
    NOT_LANDED = "not_landed"


ReceiptIdentifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
ReceiptVersion = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=128, pattern=r"^[^\s\x00-\x1f\x7f]+$"
    ),
]
ReceiptDeepLink = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=512)
]

_ENTITY_LINKS = {
    CapabilityEntityType.HOT_NEWS_RUN: ("/hot-news", "run_id"),
    CapabilityEntityType.WRITING_JOB: ("/jobs", "job_id"),
    CapabilityEntityType.FEEDBACK_CASE: ("/data-loop", "case_id"),
    CapabilityEntityType.MEMORY: ("/data-loop", "memory_id"),
    CapabilityEntityType.CANDIDATE: ("/data-loop", "candidate_id"),
    CapabilityEntityType.EVALUATION_RUN: ("/data-loop", "run_id"),
}


class CapabilityReceipt(BaseModel):
    """A stored entity, a stored proposal, or an explicitly unpersisted answer.

    ``version`` identifies the persisted snapshot; adapters convert authoritative
    integer row versions to strings before construction. ``created_at`` is the
    snapshot time, which remains immutable when a UI later polls entity status.
    A confirmation request before persistence belongs outside this receipt.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_type: CapabilityEntityType
    entity_id: ReceiptIdentifier | None
    status: CapabilityReceiptStatus
    version: ReceiptVersion | None
    deep_link: ReceiptDeepLink | None
    created_at: AwareDatetime
    pending_gate: ReceiptIdentifier | None = None

    @model_validator(mode="after")
    def validate_persistence_and_link(self) -> "CapabilityReceipt":
        if self.status == CapabilityReceiptStatus.NOT_LANDED:
            if any(
                value is not None
                for value in (
                    self.entity_id,
                    self.version,
                    self.deep_link,
                    self.pending_gate,
                )
            ):
                raise ValueError("not_landed receipts must not claim a persisted entity")
            return self

        if self.entity_id is None or self.version is None:
            raise ValueError("persisted receipts require entity_id and version")
        if (
            self.status != CapabilityReceiptStatus.PENDING_APPROVAL
            and self.pending_gate is not None
        ):
            raise ValueError("pending_gate requires pending_approval status")
        if self.deep_link is None:
            return self

        # Reject syntax that URL parsing might normalize (including invisible
        # characters), then require one exact local path and a bound selector.
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.deep_link):
            raise ValueError("deep_link must not contain whitespace or control characters")
        if "\\" in self.deep_link or "#" in self.deep_link:
            raise ValueError("deep_link must not contain backslashes or fragments")
        parsed = urlsplit(self.deep_link)
        expected_path, expected_key = _ENTITY_LINKS[self.entity_type]
        if (self.entity_type == CapabilityEntityType.EVALUATION_RUN and self.entity_id is not None
                and self.entity_id.startswith("hot-news-data-loop-")):
            import re
            if not re.fullmatch(r"hot-news-data-loop-[0-9a-f]{64}", self.entity_id):
                raise ValueError("evaluation workflow identifier is invalid")
            expected_key = "workflow_id"
        if parsed.scheme or parsed.netloc or parsed.path != expected_path:
            raise ValueError("deep_link must use the allowlisted entity path")
        if not parsed.query:
            if "?" in self.deep_link:
                raise ValueError("deep_link query must target the receipt entity")
            return self

        try:
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise ValueError("deep_link query is invalid") from exc
        if query != [(expected_key, self.entity_id)]:
            raise ValueError("deep_link query must target only the receipt entity")
        # Canonical selectors have no URL escapes: the identifier alphabet is
        # already URL safe, so encoded aliases cannot introduce another target.
        if parsed.query != f"{expected_key}={self.entity_id}":
            raise ValueError("deep_link query must use an unencoded entity selector")
        return self
