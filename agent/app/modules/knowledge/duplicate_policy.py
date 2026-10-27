"""Strict duplicate evidence, independent of model similarity and arrival order."""
from datetime import UTC, datetime
from hashlib import sha256


POLICY_VERSION = "exact-news-duplicate-v1"
MIN_BODY_CHARS = 100


def normalized_text(value: str) -> str:
    # Preserve punctuation, numbers, negation and word boundaries.
    return " ".join(value.split())


def duplicate_evidence(source: dict, replacement: dict) -> dict:
    def day(value):
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        # SQLite loses tzinfo; production uses timestamptz.
        return stamp.replace(tzinfo=stamp.tzinfo or UTC).astimezone(UTC).date()

    body = normalized_text(source["body"])
    if (len(body) < MIN_BODY_CHARS
            or not normalized_text(source["title"])
            or normalized_text(source["title"]) != normalized_text(replacement["title"])
            or body != normalized_text(replacement["body"])
            or not source["publish_time"] or not replacement["publish_time"]
            or day(source["publish_time"]) != day(replacement["publish_time"])):
        raise ValueError("articles are not strict same-day duplicates; retain both vectors")
    return {"policy_version": POLICY_VERSION,
            "normalized_body_sha256": sha256(body.encode()).hexdigest(),
            "normalized_title_sha256": sha256(normalized_text(source["title"]).encode()).hexdigest()}
