from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Literal


EntityType = Literal["person", "organization", "proper_noun"]

_ENTITY_TYPE_PRIORITY: dict[str, int] = {
    "proper_noun": 1,
    "person": 2,
    "organization": 3,
}
_NER_TYPE_MAP = {
    "Nh": "person",
    "Ni": "organization",
}
_ORGANIZATION_SUFFIXES = {
    "公司",
    "集团",
    "银行",
    "大学",
    "学院",
    "研究院",
    "委员会",
    "协会",
    "俱乐部",
}
_ORGANIZATION_PREFIX_POS = {"n", "ni", "nz", "j", "ws"}
_FOREIGN_PROPER_NOUN = re.compile(r"(?=.*[A-Z0-9])[A-Za-z][A-Za-z0-9.+_-]+")


@dataclass(frozen=True)
class TitleToken:
    text: str
    pos: str
    start: int
    end: int


@dataclass(frozen=True)
class TitleEntity:
    text: str
    normalized_text: str
    entity_type: EntityType
    start: int
    end: int
    source: str
    confidence: float | None = None


@dataclass(frozen=True)
class TitleAnalysis:
    title: str
    tokens: tuple[TitleToken, ...]
    entities: tuple[TitleEntity, ...]
    extractor: str
    model_version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "tokens": [asdict(token) for token in self.tokens],
            "entities": [asdict(entity) for entity in self.entities],
            "extractor": self.extractor,
            "model_version": self.model_version,
        }

    def to_fastgpt_metadata(self) -> dict[str, Any]:
        by_type = {
            entity_type: [
                entity.normalized_text
                for entity in self.entities
                if entity.entity_type == entity_type
            ]
            for entity_type in _ENTITY_TYPE_PRIORITY
        }
        return {
            "title_tokens": [token.text for token in self.tokens],
            "title_entities": [
                entity.normalized_text for entity in self.entities
            ],
            "title_persons": by_type["person"],
            "title_organizations": by_type["organization"],
            "title_proper_nouns": by_type["proper_noun"],
            "title_nlp_extractor": self.extractor,
            "title_nlp_model_version": self.model_version,
        }


@dataclass(frozen=True)
class LexiconEntity:
    canonical: str
    entity_type: EntityType
    aliases: tuple[str, ...]


def load_entity_lexicon(path: str | Path | None) -> tuple[LexiconEntity, ...]:
    if not path:
        return ()
    lexicon_path = Path(path)
    if not lexicon_path.exists():
        raise FileNotFoundError(f"标题实体词典不存在: {lexicon_path}")
    payload = json.loads(lexicon_path.read_text(encoding="utf-8"))
    items = payload.get("entities") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("标题实体词典必须包含 entities 数组。")

    result: list[LexiconEntity] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"标题实体词典第 {index + 1} 项必须是对象。")
        canonical = str(item.get("canonical", "")).strip()
        entity_type = str(item.get("type", "")).strip()
        aliases = item.get("aliases", [])
        if not canonical or entity_type not in _ENTITY_TYPE_PRIORITY:
            raise ValueError(f"标题实体词典第 {index + 1} 项缺少有效名称或类型。")
        if not isinstance(aliases, list):
            raise ValueError(f"标题实体词典第 {index + 1} 项 aliases 必须是数组。")
        normalized_aliases = tuple(dict.fromkeys(
            value
            for value in [canonical, *(str(alias).strip() for alias in aliases)]
            if value
        ))
        result.append(LexiconEntity(
            canonical=canonical,
            entity_type=entity_type,  # type: ignore[arg-type]
            aliases=normalized_aliases,
        ))
    return tuple(result)


class LTPTitleAnalyzer:
    """使用本地 LTP 模型完成标题分词、词性标注和实体抽取。"""

    extractor = "ltp"

    def __init__(
        self,
        *,
        model_name: str = "LTP/tiny",
        cache_dir: str | None = None,
        local_files_only: bool = True,
        lexicon_path: str | None = None,
        model: Any | None = None,
    ) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.local_files_only = local_files_only
        self.lexicon = load_entity_lexicon(lexicon_path)
        self._model = model
        self._load_lock = Lock()
        self._inference_lock = Lock()
        self._custom_words_loaded = False

    @property
    def model_version(self) -> str:
        try:
            import ltp

            package_version = getattr(ltp, "__version__", "unknown")
        except ImportError:
            package_version = "not-installed"
        return f"{self.model_name}@ltp-{package_version}"

    def _get_model(self) -> Any:
        with self._load_lock:
            if self._model is None:
                try:
                    from ltp import LTP
                except ImportError as exc:
                    raise RuntimeError(
                        "本地标题 NLP 未安装；请安装 requirements-nlp.txt。"
                    ) from exc
                self._model = LTP(
                    self.model_name,
                    cache_dir=self.cache_dir,
                    local_files_only=self.local_files_only,
                )
            self._add_custom_words(self._model)
            return self._model

    def _add_custom_words(self, model: Any) -> None:
        if self._custom_words_loaded:
            return
        words = list(dict.fromkeys(
            alias
            for item in self.lexicon
            for alias in item.aliases
        ))
        if words and hasattr(model, "add_words"):
            model.add_words(words=words, freq=2)
        elif words and hasattr(model, "add_word"):
            for word in words:
                model.add_word(word, freq=2)
        self._custom_words_loaded = True

    def analyze(self, title: str) -> TitleAnalysis:
        normalized_title = title.strip()
        if not normalized_title:
            raise ValueError("新闻标题为空，无法执行标题 NLP。")
        model = self._get_model()
        with self._inference_lock:
            result = model.pipeline(
                [normalized_title],
                tasks=["cws", "pos", "ner"],
            )
        words = list(result.cws[0])
        pos_tags = list(result.pos[0])
        if len(words) != len(pos_tags):
            raise RuntimeError("LTP 分词与词性结果长度不一致。")
        tokens = self._build_tokens(normalized_title, words, pos_tags)
        entities = self._collect_entities(
            normalized_title,
            tokens,
            list(result.ner[0]),
        )
        return TitleAnalysis(
            title=normalized_title,
            tokens=tuple(tokens),
            entities=tuple(entities),
            extractor=self.extractor,
            model_version=self.model_version,
        )

    @staticmethod
    def _build_tokens(
        title: str,
        words: list[str],
        pos_tags: list[str],
    ) -> list[TitleToken]:
        tokens: list[TitleToken] = []
        cursor = 0
        for word, pos in zip(words, pos_tags, strict=True):
            start = title.find(word, cursor)
            if start < 0:
                raise RuntimeError(f"LTP 分词无法映射回原始标题: {word}")
            end = start + len(word)
            tokens.append(TitleToken(word, pos, start, end))
            cursor = end
        return tokens

    def _collect_entities(
        self,
        title: str,
        tokens: list[TitleToken],
        ner_items: list[tuple[str, str, int, int]],
    ) -> list[TitleEntity]:
        candidates: list[TitleEntity] = []
        for label, _, start_index, end_index in ner_items:
            entity_type = _NER_TYPE_MAP.get(label)
            if entity_type is None or not (0 <= start_index <= end_index < len(tokens)):
                continue
            start_index = self._organization_prefix_start(
                tokens, start_index, end_index, entity_type
            )
            candidates.append(self._from_token_span(
                title,
                tokens,
                start_index,
                end_index,
                entity_type,
                "ner",
            ))

        for index, token in enumerate(tokens):
            if token.pos == "nh":
                candidates.append(self._from_token_span(
                    title, tokens, index, index, "person", "pos"
                ))
            elif token.pos == "nz" or _FOREIGN_PROPER_NOUN.fullmatch(token.text):
                candidates.append(self._from_token_span(
                    title, tokens, index, index, "proper_noun", "pos"
                ))

        for item in self.lexicon:
            for alias in item.aliases:
                cursor = 0
                while True:
                    start = title.find(alias, cursor)
                    if start < 0:
                        break
                    candidates.append(TitleEntity(
                        text=alias,
                        normalized_text=item.canonical,
                        entity_type=item.entity_type,
                        start=start,
                        end=start + len(alias),
                        source="lexicon",
                    ))
                    cursor = start + len(alias)

        return self._deduplicate(candidates)

    @staticmethod
    def _organization_prefix_start(
        tokens: list[TitleToken],
        start_index: int,
        end_index: int,
        entity_type: str,
    ) -> int:
        entity_text = "".join(token.text for token in tokens[start_index:end_index + 1])
        if (
            entity_type == "organization"
            and entity_text in _ORGANIZATION_SUFFIXES
            and start_index > 0
            and tokens[start_index - 1].pos in _ORGANIZATION_PREFIX_POS
        ):
            return start_index - 1
        return start_index

    @staticmethod
    def _from_token_span(
        title: str,
        tokens: list[TitleToken],
        start_index: int,
        end_index: int,
        entity_type: EntityType,
        source: str,
    ) -> TitleEntity:
        start = tokens[start_index].start
        end = tokens[end_index].end
        text = title[start:end]
        return TitleEntity(
            text=text,
            normalized_text=text,
            entity_type=entity_type,
            start=start,
            end=end,
            source=source,
        )

    @staticmethod
    def _deduplicate(candidates: list[TitleEntity]) -> list[TitleEntity]:
        # 词典结果可提供别名归一化；同一字符范围优先保留更具体的实体类型。
        selected: dict[tuple[int, int], TitleEntity] = {}
        for entity in candidates:
            if not entity.text.strip():
                continue
            key = (entity.start, entity.end)
            current = selected.get(key)
            if current is None or (
                entity.source == "lexicon"
                or _ENTITY_TYPE_PRIORITY[entity.entity_type]
                > _ENTITY_TYPE_PRIORITY[current.entity_type]
            ):
                selected[key] = entity
        values = list(selected.values())
        values = [
            entity
            for entity in values
            if not any(
                other is not entity
                and other.entity_type == entity.entity_type
                and other.start <= entity.start
                and other.end >= entity.end
                and (other.end - other.start) > (entity.end - entity.start)
                for other in values
            )
        ]
        return sorted(
            values,
            key=lambda entity: (entity.start, entity.end, entity.entity_type),
        )
