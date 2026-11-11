"""Named-entity finders used by masking to hide leftover person, shop and place names."""

import importlib.util
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from shoplegacy.config import Settings


@dataclass(frozen=True)
class NameSpan:
    start: int
    end: int
    label: Literal["PERSON", "ORG", "LOCATION"]


class NameFinder(Protocol):
    def find(self, text: str) -> list[NameSpan]: ...


class NoopNameFinder:
    """Finds nothing; used when NER is turned off."""

    def find(self, text: str) -> list[NameSpan]:
        return []


_PRESIDIO_ENTITIES: dict[str, Literal["PERSON", "ORG", "LOCATION"]] = {
    "PERSON": "PERSON",
    "ORGANIZATION": "ORG",
    "LOCATION": "LOCATION",
}


class PresidioNameFinder:
    """Presidio analyzer with a Korean spaCy model, loaded lazily."""

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self._analyzer: Any = None

    def _load(self) -> Any:
        if self._analyzer is None:
            from presidio_analyzer import AnalyzerEngine
            from presidio_analyzer.nlp_engine import NlpEngineProvider

            config = {
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": "ko", "model_name": self.model_name}],
                "ner_model_configuration": {
                    # spaCy ko labels: PS person, OG organization, LC location
                    "model_to_presidio_entity_mapping": {
                        "PS": "PERSON",
                        "OG": "ORGANIZATION",
                        "LC": "LOCATION",
                    },
                },
            }
            engine = NlpEngineProvider(nlp_configuration=config).create_engine()
            self._analyzer = AnalyzerEngine(nlp_engine=engine, supported_languages=["ko"])
        return self._analyzer

    def find(self, text: str) -> list[NameSpan]:
        if not text.strip():
            return []
        results = self._load().analyze(text=text, language="ko", entities=list(_PRESIDIO_ENTITIES))
        spans = [
            NameSpan(int(r.start), int(r.end), _PRESIDIO_ENTITIES[r.entity_type])
            for r in results
            if r.entity_type in _PRESIDIO_ENTITIES and r.end > r.start
        ]
        return sorted(spans, key=lambda s: (s.start, s.end))


def build_name_finder(settings: Settings) -> NameFinder:
    if settings.ner == "none":
        return NoopNameFinder()
    if settings.ner == "presidio":
        if importlib.util.find_spec("presidio_analyzer") is None:
            raise RuntimeError("presidio not installed")
        return PresidioNameFinder(settings.ner_model)
    raise ValueError(f"unsupported SHOPLEGACY_NER: {settings.ner}")
