"""Versioned two-level interest topics shared by audience APIs and the UI.

Catalogs describe business labels, not a user-tagging algorithm. Approval of a
catalog is enforced by the runtime factory so local demonstrations may use an
explicitly draft catalog without representing enterprise approval.
"""

from collections.abc import Sequence
from pathlib import Path
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator
import yaml


MAX_TOPICS = 512
MAX_CATALOG_BYTES = 1_048_576
_SLUG = r"^[a-z][a-z0-9]*(?:[-_./][a-z0-9]+)*$"
TopicId = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128, pattern=_SLUG)]
CatalogVersion = Annotated[
    str, StringConstraints(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
]


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    topic_id: TopicId
    name: str = Field(strict=True, min_length=1, max_length=100)
    parent_topic_id: TopicId | None = None
    enabled: bool = Field(default=True, strict=True)
    provenance: Literal["user_confirmed", "draft_extension"]

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if value != value.strip() or any(character in value for character in "\r\n\t"):
            raise ValueError("topic name must be nonblank single-line text without surrounding whitespace")
        return value


class TopicCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    taxonomy_version: CatalogVersion
    status: Literal["draft", "approved"]
    topics: tuple[Topic, ...] = Field(min_length=1, max_length=MAX_TOPICS)

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_schema(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value

    @model_validator(mode="after")
    def validate_hierarchy(self) -> Self:
        by_id = {topic.topic_id: topic for topic in self.topics}
        if len(by_id) != len(self.topics):
            raise ValueError("topic_id values must be unique")
        for topic in self.topics:
            if topic.parent_topic_id is None:
                if "." in topic.topic_id or "/" in topic.topic_id:
                    raise ValueError("root topic_id must be a single namespace segment")
                continue
            parent = by_id.get(topic.parent_topic_id)
            if parent is None:
                raise ValueError(f"unknown parent_topic_id for {topic.topic_id}")
            if parent.parent_topic_id is not None:
                raise ValueError("topic hierarchy must have at most two levels")
            suffix = None
            for separator in (".", "/"):
                prefix = parent.topic_id + separator
                if topic.topic_id.startswith(prefix):
                    suffix = topic.topic_id[len(prefix):]
                    break
            if not suffix or re.fullmatch(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*", suffix) is None:
                raise ValueError("child topic_id must belong to its parent namespace and have one leaf segment")
            if topic.enabled and not parent.enabled:
                raise ValueError("enabled child cannot belong to a disabled parent")
        return self

    def validate_ids(self, topic_ids: Sequence[str]) -> None:
        """Reject unknown, disabled, repeated or unbounded requested topics."""
        if not isinstance(topic_ids, Sequence) or isinstance(topic_ids, (str, bytes)) or len(topic_ids) > MAX_TOPICS:
            raise ValueError("topic_ids must be a bounded sequence of topic IDs")
        if any(not isinstance(topic_id, str) for topic_id in topic_ids):
            raise ValueError("topic_ids must contain strings")
        if len(topic_ids) != len(set(topic_ids)):
            raise ValueError("topic_ids cannot contain duplicates")
        by_id = {topic.topic_id: topic for topic in self.topics}
        for topic_id in topic_ids:
            topic = by_id.get(topic_id)
            if topic is None:
                raise ValueError(f"unknown topic_id: {topic_id}")
            if not topic.enabled:
                raise ValueError(f"disabled topic_id: {topic_id}")

    def ancestor_ids(self, topic_id: str) -> tuple[str, ...]:
        """Return the enabled topic itself, followed by its parent when present."""
        self.validate_ids((topic_id,))
        topic = next(topic for topic in self.topics if topic.topic_id == topic_id)
        if topic.parent_topic_id is None:
            return (topic.topic_id,)
        return (topic.topic_id, topic.parent_topic_id)


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML parsing also rejects ambiguous repeated mapping keys."""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False):
    loader.flatten_mapping(node)
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as exc:
            raise ValueError("topic catalog YAML keys must be scalar") from exc
        if duplicate:
            raise ValueError("topic catalog YAML contains a duplicate key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def load_topic_catalog(path: str | Path) -> TopicCatalog:
    """Read at most 1 MiB of safe YAML, then validate the complete catalog."""
    source = Path(path)
    try:
        if not source.is_file():
            raise ValueError("topic catalog YAML is missing")
        with source.open("rb") as stream:
            payload = stream.read(MAX_CATALOG_BYTES + 1)
        if len(payload) > MAX_CATALOG_BYTES:
            raise ValueError("topic catalog YAML exceeds 1 MiB")
        value = yaml.load(payload.decode("utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError("topic catalog YAML cannot be read safely") from exc
    return TopicCatalog.model_validate(value)
