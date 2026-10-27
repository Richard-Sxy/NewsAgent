"""Versioned news understanding contracts. Model assertions are not verified facts."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SemanticConfig(Contract):
    policy_version: Literal["news-semantics-v1"] = "news-semantics-v1"
    model_route: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: Literal["news-semantics-v1"] = "news-semantics-v1"
    candidate_limit: int = Field(default=8, ge=1, le=30)
    max_body_chars: int = Field(default=18000, ge=100, le=60000)
    timeout_seconds: float = Field(default=45, gt=0, le=120)
    task_timeout_seconds: float = Field(default=240, ge=30, le=900)
    lease_seconds: int = Field(default=300, ge=60, le=1200)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_seconds: int = Field(default=60, ge=1, le=3600)
    refresh_seconds: int = Field(default=900, ge=1, le=86400)
    poll_seconds: float = Field(default=5, ge=1, le=300)
    query_expansion_limit: int = Field(default=3, ge=0, le=10)
    query_timeout_seconds: float = Field(default=1, gt=0, le=10)
    # Exact host names from reviewed metadata; unknown sources all rank equally.
    source_priority: dict[str, int] = Field(default_factory=dict)
    auto_retire: bool = False
    evaluation_version: str | None = None

    @model_validator(mode="after")
    def budgets(self):
        if self.lease_seconds <= self.task_timeout_seconds:
            raise ValueError("lease must outlive the task timeout")
        if self.auto_retire and not self.evaluation_version:
            raise ValueError("automatic semantic retirement requires a reviewed evaluation version")
        if any(not host or ":" in host or "/" in host or type(rank) is not int
               or not 0 <= rank <= 100 for host, rank in self.source_priority.items()):
            raise ValueError("source priorities require exact hosts and integer ranks 0..100")
        return self

    @property
    def bundle_id(self):
        from app.modules.knowledge.news_semantics_model import EXTRACT, COMPARE
        return digest({"config": self.model_dump(mode="json"), "extract_prompt": EXTRACT,
                       "compare_prompt": COMPARE, "features_schema": Features.model_json_schema(),
                       "pair_schema": PairUnderstanding.model_json_schema()})


def load_semantic_config(path):
    return SemanticConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def blocks(body):
    """Stable, exhaustive non-whitespace spans; never silently truncate input."""
    result = []
    for match in re.finditer(r"[^\n]+", body):
        for start in range(match.start(), match.end(), 600):
            end = min(start + 600, match.end())
            if body[start:end].strip():
                result.append(dict(id=f"b{len(result)}", start=start, end=end, text=body[start:end]))
    return result


class Quote(Contract):
    block_id: str = Field(min_length=1, max_length=20)
    text: str = Field(min_length=1, max_length=600)


class Claim(Contract):
    id: str = Field(pattern=r"^c[0-9]{1,4}$")
    statement: str = Field(min_length=1, max_length=1000)
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=200)
    object: str = Field(default="", max_length=300)
    period: str = Field(default="", max_length=100)
    value: str = Field(default="", max_length=100)
    unit: str = Field(default="", max_length=50)
    polarity: Literal["positive", "negative", "unknown"]
    modality: Literal["reported", "forecast", "alleged", "unknown"]
    attribution: str = Field(default="", max_length=200)
    independent_evidence: bool
    evidence: list[Quote] = Field(min_length=1, max_length=12)


class EventIdentity(Contract):
    subject: str = Field(default="", max_length=200)
    action: str = Field(default="", max_length=100)
    object: str = Field(default="", max_length=200)
    occurrence_date: str = Field(default="", max_length=10)
    period: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=100)
    evidence: list[Quote] = Field(default_factory=list, max_length=12)


class Features(Contract):
    event: EventIdentity
    multiple_events: bool
    extraction_complete: bool
    claims: list[Claim] = Field(max_length=100)


class Match(Contract):
    left: str
    right: str
    relation: Literal["equivalent", "left_entails_right", "right_entails_left", "conflict"]


class PairUnderstanding(Contract):
    # Non-same-event relations point from LEFT to RIGHT, never inferred from time alone.
    relation: Literal["same_event", "follow_up", "response", "background", "correction", "unrelated", "uncertain"]
    direction: Literal["left_to_right", "right_to_left"] = "left_to_right"
    matches: list[Match] = Field(default_factory=list, max_length=200)
    evidence_left: list[str] = Field(default_factory=list, max_length=20)
    evidence_right: list[str] = Field(default_factory=list, max_length=20)
    unresolved: bool


def validate_features(features: Features, body: str):
    source = {b["id"]: b["text"] for b in blocks(body)}
    ids = [c.id for c in features.claims]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate claim identifiers")
    for quote in [q for c in features.claims for q in c.evidence] + features.event.evidence:
        if quote.block_id not in source or quote.text not in source[quote.block_id]:
            raise ValueError("semantic evidence is not an exact source quote")
    for claim in features.claims:
        evidence = " ".join(q.text for q in claim.evidence)
        if claim.value and claim.value not in evidence:
            raise ValueError("numeric value lacks literal evidence")
        if claim.unit and claim.unit not in evidence:
            raise ValueError("unit lacks literal evidence")
        if claim.value and claim.unit:
            compact = ["".join(q.text.split()) for q in claim.evidence]
            if not any(claim.value + claim.unit in part or claim.unit + claim.value in part for part in compact):
                raise ValueError("value and unit lack a joint literal evidence span")
    if features.event.occurrence_date:
        from datetime import date
        date.fromisoformat(features.event.occurrence_date)


def event_identity(tenant_id, features, news_id, revision):
    event = features.event
    # Sparse/multi-event articles must not bridge clusters. Identity does NOT use member IDs.
    if (features.multiple_events or not event.subject or not event.action
            or not event.object or not event.occurrence_date or not event.evidence):
        return digest([tenant_id, "unresolved", news_id, revision])
    return digest([tenant_id, "event-v1", *[
        "".join(str(v).casefold().split()) for k, v in event.model_dump().items() if k != "evidence"]])


def fully_accounted(features, body):
    if not features.extraction_complete or features.multiple_events or not features.claims:
        return False
    # Full span coverage is a necessary safeguard, not proof of semantic completeness.
    for block in blocks(body):
        covered = [False] * len(block["text"])
        for claim in features.claims:
            for quote in claim.evidence:
                if quote.block_id == block["id"]:
                    start = block["text"].find(quote.text)
                    covered[start:start + len(quote.text)] = [True] * len(quote.text)
        if any(not ok and not char.isspace() for ok, char in zip(covered, block["text"])):
            return False
    return True


def quality(row, features, config):
    """Deterministic lexicographic priority; traffic and text length are not quality."""
    host = (urlsplit(row.get("source_url") or "").hostname or "").lower()
    return (config.source_priority.get(host, 0), int(fully_accounted(features, row["body"])),
            len({(c.subject, c.predicate, c.object, c.period, c.value, c.unit,
                  c.polarity, c.modality, c.attribution) for c in features.claims}))


def decide_pair(left, right, lf, rf, understanding, config):
    lc, rc = {c.id: c for c in lf.claims}, {c.id: c for c in rf.claims}
    if not set(understanding.evidence_left) <= lc.keys() or not set(understanding.evidence_right) <= rc.keys():
        raise ValueError("unknown relationship evidence")
    covered_left, covered_right, conflicts = set(), set(), []
    for m in understanding.matches:
        if m.left not in lc or m.right not in rc:
            raise ValueError("unknown coverage claim")
        a, b = lc[m.left], rc[m.right]
        if m.relation == "conflict":
            conflicts.append([m.left, m.right])
            continue
        # Do not trust semantic matching to erase numbers, periods, modality or attribution.
        if any(getattr(a, k) != getattr(b, k) for k in
               ("subject", "predicate", "object", "period", "value", "unit", "polarity", "modality", "attribution")):
            raise ValueError("coverage changes a critical qualifier")
        if a.polarity == "unknown" or a.modality == "unknown":
            continue
        if m.relation in ("equivalent", "right_entails_left"):
            covered_left.add(m.left)
        if m.relation in ("equivalent", "left_entails_right"):
            covered_right.add(m.right)
    same = (understanding.relation == "same_event" and not lf.multiple_events and not rf.multiple_events
            and event_identity(left["tenant_id"], lf, left["news_id"], left["source_revision"])
            == event_identity(right["tenant_id"], rf, right["news_id"], right["source_revision"]))
    relation = understanding.relation
    if relation == "same_event" and not same:
        relation = "uncertain"
    if relation not in ("same_event", "unrelated", "uncertain") and (
            not understanding.evidence_left or not understanding.evidence_right):
        raise ValueError("event relation requires evidence on both sides")
    eligible = same and not conflicts and not understanding.unresolved
    preferred = sorted((left, right), key=lambda row: (
        tuple(-v for v in quality(row, lf if row is left else rf, config)), row["news_id"]))[0]
    redundant, replacement = None, None
    for source, target, sf, covered in ((left, right, lf, covered_left), (right, left, rf, covered_right)):
        if (eligible and preferred is target and fully_accounted(sf, source["body"])
                and set(c.id for c in sf.claims) <= covered
                and not any(c.independent_evidence for c in sf.claims)):
            redundant, replacement = source["news_id"], target["news_id"]
    return dict(relation=relation, direction=understanding.direction,
                preferred_news_id=preferred["news_id"] if same else None,
                redundant_news_id=redundant, replacement_news_id=replacement,
                only_left=sorted(lc.keys() - covered_left), only_right=sorted(rc.keys() - covered_right),
                conflicts=conflicts, unresolved=understanding.unresolved,
                understanding=understanding.model_dump(mode="json"))
