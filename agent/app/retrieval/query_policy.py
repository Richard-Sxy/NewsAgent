"""Bounded query-aware budgets and comparable-vector candidate ranking.

All available tiers remain searchable. Global ranking requires callers to
establish the same embedding space and higher-is-better metric. Exported scores
are rank features, not calibrated relevance probabilities.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Annotated, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.retrieval.tiered_vector import VectorSearchHit, VectorTier

QueryIntent = Literal["general", "recent", "background"]
PlanReason = Literal[
    "explicit_intent", "conflicting_intent_cues", "recent_cues", "background_cues",
    "explicit_topic", "unique_topic_rule", "ambiguous_topic_rules", "general_default",
]
Multiplier = Annotated[int, Field(strict=True, ge=1, le=8)]


def _default_intent_multipliers() -> dict[str, dict[VectorTier, int]]:
    return {
        "general": {tier: 1 for tier in VectorTier},
        "recent": {VectorTier.HOT: 2, VectorTier.WARM: 1, VectorTier.COLD: 1},
        "background": {VectorTier.HOT: 1, VectorTier.WARM: 1, VectorTier.COLD: 2},
    }


@dataclass(frozen=True, slots=True)
class QueryPlan:
    intent: QueryIntent
    topic: str | None
    reason: PlanReason
    tier_limits: dict[VectorTier, int]


class QueryRetrievalPolicy(BaseModel):
    """Versioned candidate allocation without topic scores or hard topic gates."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    version: str = Field(min_length=1, max_length=256)
    fusion: Literal["global_similarity"] = "global_similarity"
    candidate_multiplier: int = Field(default=2, strict=True, ge=1, le=8)
    min_per_tier: int = Field(default=20, strict=True, ge=1, le=1000)
    intent_multipliers: dict[str, dict[VectorTier, Multiplier]] = Field(
        default_factory=_default_intent_multipliers,
    )
    topic_rules: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    topic_intents: dict[str, QueryIntent] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_policy(self):
        if self.version != self.version.strip() or not self.version.strip():
            raise ValueError("query retrieval version must be nonblank and trimmed")
        if set(self.intent_multipliers) != {"general", "recent", "background"}:
            raise ValueError("intent_multipliers must define general, recent and background")
        if any(set(weights) != set(VectorTier) for weights in self.intent_multipliers.values()):
            raise ValueError("each intent must define hot, warm and cold exactly once")
        for topic, terms in self.topic_rules.items():
            if not topic.strip() or topic != topic.strip() or len(topic) > 128:
                raise ValueError("topic names must be nonblank, trimmed and at most 128 characters")
            if not terms or any(not term.strip() or term != term.strip() for term in terms):
                raise ValueError("topic rules require nonblank, trimmed terms")
            if len(set(term.casefold() for term in terms)) != len(terms):
                raise ValueError("topic rule terms must be unique")
        if set(self.topic_intents) - set(self.topic_rules):
            raise ValueError("topic_intents must reference configured topic_rules")
        return self

    def plan(
        self, query_text: str, *, limit: int, default_candidates: int,
        max_candidates: int, available_tiers: Sequence[VectorTier],
        intent: Literal["auto", "general", "recent", "background"] = "auto",
        topic: str | None = None,
    ) -> QueryPlan:
        for value, label in ((limit, "limit"), (default_candidates, "default_candidates"),
                             (max_candidates, "max_candidates")):
            if type(value) is not int or value < 1:
                raise ValueError(f"{label} must be a positive integer")
        if limit > max_candidates:
            raise ValueError("limit must not exceed max_candidates")
        if not isinstance(query_text, str):
            raise ValueError("query_text must be text")
        if intent not in {"auto", "general", "recent", "background"}:
            raise ValueError("unknown query retrieval intent")
        if topic is not None and topic not in self.topic_rules:
            raise ValueError("unknown query retrieval topic")
        tiers = tuple(dict.fromkeys(VectorTier(tier) for tier in available_tiers))
        if not tiers:
            raise ValueError("at least one available tier is required")

        folded = query_text.casefold()
        matched_topics = [
            name for name, terms in self.topic_rules.items()
            if any(term.casefold() in folded for term in terms)
        ]
        resolved_topic = topic or (matched_topics[0] if len(matched_topics) == 1 else None)
        if intent != "auto":
            resolved_intent, reason = intent, "explicit_intent"
        else:
            recent = any(term in query_text for term in ("最新", "今天", "刚刚", "实时"))
            background = any(term in query_text for term in ("历史背景", "发展历程", "来龙去脉", "演变"))
            if recent and background:
                resolved_intent, reason = "general", "conflicting_intent_cues"
            elif recent:
                resolved_intent, reason = "recent", "recent_cues"
            elif background:
                resolved_intent, reason = "background", "background_cues"
            elif resolved_topic is not None:
                resolved_intent = self.topic_intents.get(resolved_topic, "general")
                reason = "explicit_topic" if topic is not None else "unique_topic_rule"
            else:
                resolved_intent = "general"
                reason = "ambiguous_topic_rules" if len(matched_topics) > 1 else "general_default"

        base = max(self.min_per_tier, default_candidates, limit * self.candidate_multiplier)
        tier_limits = {
            tier: min(max_candidates, max(limit, base * self.intent_multipliers[resolved_intent][tier]))
            for tier in tiers
        }
        return QueryPlan(intent=resolved_intent, topic=resolved_topic, reason=reason,
                         tier_limits=tier_limits)


def rank_candidates(
    hits: Sequence[VectorSearchHit], *, embedding_version: str, rrf_k: int,
    limit: int, exclude_news_ids: Sequence[str] = (),
) -> tuple[list[VectorSearchHit], dict[str, float], dict]:
    """Rank comparable similarities and select each news article's best chunk.

    rrf_k controls the compatibility rank feature only. Callers retain their
    lifecycle and tenant serving gates and establish metric comparability.
    """
    if not isinstance(embedding_version, str) or not embedding_version.strip():
        raise ValueError("embedding_version must be nonblank")
    if type(rrf_k) is not int or rrf_k < 1 or type(limit) is not int or limit < 1:
        raise ValueError("rrf_k and limit must be positive integers")
    excluded = frozenset(exclude_news_ids)
    representatives: dict[str, VectorSearchHit] = {}
    excluded_count = 0
    for hit in hits:
        if hit.embedding_version != embedding_version:
            raise ValueError("candidate embedding version drift")
        try:
            finite = not isinstance(hit.raw_score, bool) and math.isfinite(hit.raw_score)
        except (TypeError, ValueError):
            finite = False
        if not finite:
            raise ValueError("candidate similarity must be finite")
        if hit.news_id in excluded:
            excluded_count += 1
            continue
        previous = representatives.get(hit.news_id)
        if previous is None or (-hit.raw_score, hit.chunk_id) < (-previous.raw_score, previous.chunk_id):
            representatives[hit.news_id] = hit
    ranked = sorted(representatives.values(), key=lambda hit: (-hit.raw_score, hit.news_id, hit.chunk_id))[:limit]
    scores = {hit.news_id: (rrf_k + 1) / (rrf_k + rank)
              for rank, hit in enumerate(ranked, start=1)}
    details = {
        "mode": "global_similarity", "score_kind": "global_rank_feature",
        "input_chunks": len(hits), "excluded_chunks": excluded_count,
        "unique_news": len(representatives),
        "duplicate_chunks": len(hits) - excluded_count - len(representatives),
        "returned_news": len(ranked),
    }
    return ranked, scores, details
