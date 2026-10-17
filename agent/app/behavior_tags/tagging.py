"""Explainable content topics and news-window behavior labels.

Article text is searched as untrusted data. It cannot supply rules, thresholds,
permissions or instructions. These labels describe a news aggregate, never an
individual reader's interests or an enterprise editorial classification.
"""

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
import yaml

from app.audience.taxonomy import TopicCatalog


BEHAVIOR_RULE_VERSION = "news-window-behavior-labels-v1"
MAX_POLICY_BYTES = 1_048_576
_VERSION = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128,
    pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")]
_TERM = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=80)]


class TopicRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    rule_id: _VERSION
    topic_id: str = Field(strict=True, min_length=1, max_length=128)
    title_terms: tuple[_TERM, ...] = Field(default=(), max_length=32)
    body_terms: tuple[_TERM, ...] = Field(default=(), max_length=32)
    negative_terms: tuple[_TERM, ...] = Field(default=(), max_length=32)
    synthetic_categories: tuple[_TERM, ...] = Field(default=(), max_length=16)
    title_weight: float = Field(default=1.0, strict=True, gt=0, le=1)
    body_weight: float = Field(default=0.6, strict=True, gt=0, le=1)

    @field_validator("title_terms", "body_terms", "negative_terms", "synthetic_categories")
    @classmethod
    def validate_terms(cls, values):
        if any(value != value.strip() or any(ord(char) < 32 for char in value) for value in values):
            raise ValueError("topic terms must be single-line text without surrounding whitespace")
        if len({value.casefold() for value in values}) != len(values):
            raise ValueError("topic terms cannot contain case-insensitive duplicates")
        return values

    @model_validator(mode="after")
    def require_matcher(self) -> Self:
        if not (self.title_terms or self.body_terms or self.synthetic_categories):
            raise ValueError("topic rule requires keywords or explicit synthetic category fallback")
        return self


class TopicPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    policy_version: _VERSION
    catalog: TopicCatalog
    rules: tuple[TopicRule, ...] = Field(min_length=1, max_length=512)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_rules(self) -> Self:
        if len({rule.rule_id for rule in self.rules}) != len(self.rules):
            raise ValueError("topic rule_id values must be unique")
        for rule in self.rules:
            self.catalog.validate_ids((rule.topic_id,))
        return self


class _PolicyLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ValueError("policy keys must be scalar") from exc
        if duplicate:
            raise ValueError("duplicate policy key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_PolicyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_topic_policy(path: str | Path) -> TopicPolicy:
    source = Path(path)
    try:
        if not source.is_file():
            raise ValueError("topic policy is missing")
        with source.open("rb") as stream:
            payload = stream.read(MAX_POLICY_BYTES + 1)
        if len(payload) > MAX_POLICY_BYTES:
            raise ValueError("topic policy exceeds 1 MiB")
        data = yaml.load(payload.decode("utf-8"), Loader=_PolicyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError("topic policy cannot be read safely") from exc
    return TopicPolicy.model_validate(data)


def _positions(text: str, term: str) -> tuple[tuple[int, int], ...]:
    # ASCII words require boundaries: AI must not match "said" or "retail".
    pattern = re.escape(term)
    if re.fullmatch(r"[A-Za-z0-9_ -]+", term):
        pattern = r"(?<![A-Za-z0-9_])" + pattern + r"(?![A-Za-z0-9_])"
    matches = re.finditer(pattern, text, flags=re.IGNORECASE)
    spans = []
    for match in matches:
        spans.append((match.start(), match.end()))
        if len(spans) == 3:
            break
    return tuple(spans)


def _required_text(article, field, maximum):
    value = article.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise ValueError(f"invalid article {field}")
    return value


def tag_article(article: dict, policy: TopicPolicy) -> dict:
    if not isinstance(article, Mapping):
        raise ValueError("article must be a mapping")
    tenant_id = _required_text(article, "tenant_id", 128)
    news_id = _required_text(article, "news_id", 160)
    title = _required_text(article, "title", 1024)
    body = article.get("content")
    if not isinstance(body, str) or len(body) > 1_000_000 or "\x00" in body:
        raise ValueError("invalid article content")
    digest = sha256(body.encode("utf-8")).hexdigest()
    if article.get("content_sha256") not in (None, digest):
        raise ValueError("article content_sha256 differs from its content")
    scores, matches = {}, []
    negative_rules = set()
    for rule in policy.rules:
        if any(_positions(title, term) or _positions(body, term) for term in rule.negative_terms):
            negative_rules.add(rule.rule_id)
            continue
        for field, text, terms, weight in (("title", title, rule.title_terms, rule.title_weight),
                                           ("content", body, rule.body_terms, rule.body_weight)):
            for term in terms:
                for start, end in _positions(text, term):
                    matches.append(dict(topic_id=rule.topic_id, rule_id=rule.rule_id,
                        field=field, term=term, start=start, end=end, score=weight,
                        source="rule_keyword_match"))
                    scores[rule.topic_id] = max(scores.get(rule.topic_id, 0), weight)
    provenance = "rule_inferred" if matches else "unclassified"
    # Fallback cannot invent an authoritative editorial topic or overwrite a
    # more specific keyword match. Only explicitly marked demo categories qualify.
    if not matches and article.get("category_data_synthetic") is True:
        category = article.get("category")
        for rule in policy.rules:
            if rule.rule_id not in negative_rules and category in rule.synthetic_categories:
                scores[rule.topic_id] = max(scores.get(rule.topic_id, 0), 0.25)
                matches.append(dict(topic_id=rule.topic_id, rule_id=rule.rule_id,
                    field="category", term=category, start=None, end=None, score=0.25,
                    source="synthetic_category_fallback"))
        if matches:
            provenance = "synthetic_category_fallback"
    inherited = {}
    for topic_id, score in scores.items():
        for ancestor in policy.catalog.ancestor_ids(topic_id):
            inherited[ancestor] = max(inherited.get(ancestor, 0), score)
    output = {key: article[key] for key in (
        "content_version", "source_content_version", "fixture_content_version", "publish_time",
        "source_url", "category", "category_data_synthetic", "news_content_synthetic",
        "metric_data_synthetic", "lifecycle_revision", "status", "source_refs",
        "source_dataset_version", "scenario_id", "content_scope", "source_news_id") if key in article}
    return {**output, "tenant_id": tenant_id, "news_id": news_id, "title": title,
        "content_sha256": digest, "taxonomy_version": policy.catalog.taxonomy_version,
        "tagging_policy_version": policy.policy_version, "topic_ids": sorted(inherited),
        "topic_scores": {key: inherited[key] for key in sorted(inherited)}, "matches": matches,
        "provenance": provenance, "labels_are_inferred": True}


def _count(observation, key):
    value = observation.get(key)
    if value is None and key in {"detail_page_views", "direct_page_views", "detail_visitors",
                                 "likes", "comments", "shares", "favorites", "negative_feedback"}:
        return None
    if key == "total_duration_seconds":
        if value is None:
            raise ValueError("behavior metric total_duration_seconds is required")
        return _baseline(value, key)
    if type(value) is not int or value < 0:
        raise ValueError(f"behavior metric {key} must be a nonnegative integer")
    return value


def _baseline(value, name):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"invalid {name}")
    return number


def behavior_labels(observation: dict) -> dict:
    """Return original metrics plus deterministic, independently explainable labels.

    No labels are user profiles. Ratios are calculated within this one news
    window; UV is never summed across windows, channels, news or topics.
    """
    if not isinstance(observation, Mapping):
        raise ValueError("behavior observation must be a mapping")
    keys = ("impressions", "clicks", "unique_users", "detail_page_views", "direct_page_views",
            "detail_visitors", "total_duration_seconds", "effective_consumptions", "interactions",
            "likes", "comments", "shares", "favorites", "negative_feedback")
    values = {key: _count(observation, key) for key in keys}
    for left, right in (("clicks", "impressions"), ("unique_users", "clicks"),
                        ("direct_page_views", "detail_page_views"), ("detail_visitors", "detail_page_views"),
                        ("effective_consumptions", "clicks")):
        if values[left] is not None and values[right] is not None and values[left] > values[right]:
            raise ValueError(f"behavior metric {left} exceeds {right}")
    components = [values[key] for key in ("likes", "comments", "shares", "favorites")]
    follows = observation.get("follows", 0)
    if follows is not None and (type(follows) is not int or follows < 0):
        raise ValueError("behavior follows must be a nonnegative integer")
    if all(value is not None for value in components) and follows is not None and values["interactions"] != sum(components) + follows:
        raise ValueError("behavior interactions differ from their component counts")
    raw_sample_count = observation.get("sample_count")
    sample_count = 0 if raw_sample_count is None else raw_sample_count
    if type(sample_count) is not int or sample_count < 0:
        raise ValueError("baseline sample_count must be a nonnegative integer")
    bases = {key: _baseline(observation.get(key), key) for key in (
        "baseline_impressions", "baseline_clicks", "baseline_effective_consumptions", "baseline_interactions")}
    reasons = []

    def emit(label_id, selected, formula, value, operator, threshold):
        reasons.append(dict(label_id=label_id, rule_id=label_id + ".v1", rule_version=BEHAVIOR_RULE_VERSION,
            metric_values={key: str(number) if isinstance(number, Decimal) else number for key, number in selected.items()}, formula=formula,
            value=str(value) if isinstance(value, Decimal) else value,
            operator=operator, threshold=threshold))

    def ratio_rule(label_id, numerator, denominator, operator, threshold, minimum):
        if values[numerator] is None or values[denominator] is None or values[denominator] < minimum or values[denominator] == 0:
            return
        ratio = Decimal(values[numerator]) / Decimal(values[denominator])
        cutoff = Decimal(threshold)
        if (operator == ">=" and ratio >= cutoff) or (operator == "<=" and ratio <= cutoff):
            emit(label_id, {numerator: values[numerator], denominator: values[denominator],
                "minimum_denominator": minimum}, numerator + " / " + denominator, ratio, operator, threshold)

    impressions, clicks = values["impressions"], values["clicks"]
    missing = [key for key, value in values.items() if value is None]
    if missing:
        emit("data.partial_metric_coverage", {"unavailable_metrics": missing},
             "source fields absent; corresponding ratios are not calculated", len(missing), ">", 0)
    if impressions == 0:
        emit("data.zero_impressions", {"impressions": 0}, "impressions", 0, "==", 0)
        emit("data.ctr_undefined", {"impressions": 0, "clicks": clicks}, "clicks / impressions", None, "denominator==", 0)
    if impressions < 100:
        emit("data.low_sample", {"impressions": impressions}, "impressions", impressions, "<", 100)
    if clicks == 0:
        emit("click.zero", {"clicks": clicks}, "clicks", clicks, "==", 0)
    for label_id, metric, threshold in (("reach.high_impressions", "impressions", 10000),
                                      ("reach.high_clicks", "clicks", 1000),
                                      ("reach.broad_click_uv", "unique_users", 500)):
        if values[metric] >= threshold:
            emit(label_id, {metric: values[metric]}, metric, values[metric], ">=", threshold)
    for args in (
        ("click.high_ctr", "clicks", "impressions", ">=", "0.15", 100),
        ("click.low_ctr", "clicks", "impressions", "<=", "0.03", 100),
        ("reading.deep", "total_duration_seconds", "detail_page_views", ">=", "60", 20),
        ("reading.shallow", "total_duration_seconds", "detail_page_views", "<=", "10", 20),
        ("reading.high_effective_rate", "effective_consumptions", "clicks", ">=", "0.6", 20),
        ("reading.low_effective_rate", "effective_consumptions", "clicks", "<=", "0.3", 20),
        ("interaction.high_rate", "interactions", "clicks", ">=", "0.1", 20),
        ("interaction.low_rate", "interactions", "clicks", "<=", "0.02", 20),
        ("interaction.share_driven", "shares", "clicks", ">=", "0.02", 20),
        ("interaction.favorite_driven", "favorites", "clicks", ">=", "0.02", 20),
        ("feedback.high_negative_rate", "negative_feedback", "detail_page_views", ">=", "0.05", 100),
        ("feedback.low_negative_rate", "negative_feedback", "detail_page_views", "<=", "0.005", 100),
        ("traffic.direct_heavy", "direct_page_views", "detail_page_views", ">=", "0.4", 20),
        ("traffic.repeated_visits", "detail_page_views", "detail_visitors", ">=", "1.5", 20),
    ):
        ratio_rule(*args)
    if raw_sample_count is None and all(value is not None for value in bases.values()):
        emit("baseline.sample_count_unknown", {"sample_count": None, "baseline_version": observation.get("baseline_version")},
             "baseline values exist but source has no sample_count", None, "is", "unknown")
    if raw_sample_count == 0 or any(value is None for value in bases.values()):
        emit("baseline.missing", {"sample_count": raw_sample_count, **{key: observation.get(key) for key in bases}},
             "sample_count == 0 or any baseline is missing", None, "is", "missing")
    elif bases["baseline_clicks"] == 0:
        emit("baseline.zero_clicks", {"baseline_clicks": str(bases["baseline_clicks"]), "clicks": clicks},
             "baseline_clicks", "0", "==", 0)
        if clicks > 0:
            emit("growth.from_zero", {"baseline_clicks": "0", "clicks": clicks},
                 "clicks > 0 and baseline_clicks == 0; relative growth undefined", clicks, ">", 0)
    else:
        growth = (Decimal(clicks) - bases["baseline_clicks"]) / bases["baseline_clicks"]
        if growth >= Decimal("0.5") and clicks >= 20:
            emit("growth.rising", {"clicks": clicks, "baseline_clicks": str(bases["baseline_clicks"]),
                "sample_count": sample_count}, "(clicks - baseline_clicks) / baseline_clicks", growth, ">=", "0.5")
        elif growth <= Decimal("-0.3"):
            emit("growth.cooling", {"clicks": clicks, "baseline_clicks": str(bases["baseline_clicks"]),
                "sample_count": sample_count}, "(clicks - baseline_clicks) / baseline_clicks", growth, "<=", "-0.3")
        else:
            emit("growth.steady", {"clicks": clicks, "baseline_clicks": str(bases["baseline_clicks"]),
                "sample_count": sample_count}, "(clicks - baseline_clicks) / baseline_clicks", growth, "between", "(-0.3, 0.5); or rising below 20 clicks")
    return {**observation, "label_ids": sorted(reason["label_id"] for reason in reasons),
        "reasons": reasons, "behavior_rule_version": BEHAVIOR_RULE_VERSION,
        "behavior_label_scope": "news_window_aggregate", "behavior_labels_are_inferred": True}
