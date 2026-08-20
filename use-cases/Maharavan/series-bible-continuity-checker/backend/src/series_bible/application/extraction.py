"""Grounded extraction that never treats manuscript text as instructions."""
from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

from series_bible.domain.schemas import ExtractedFact, FactKind, SourceRef
from series_bible.infrastructure.database import DocumentChunk


class GroundedRuleExtractor:
    """Deterministic baseline extractor for explicit demo claims.

    Production deployments can inject an ``LLMProvider`` with the same structured
    result. These conservative rules emit only values present in the exact text.
    """

    EYES = re.compile(r"\b(?P<name>[A-Z][a-z]+)(?:'s|\s+has|\s+had)?\s+.*?\b(?P<value>blue|green|brown|gray|grey|hazel|silver)\s+eyes\b", re.I)
    SIBLING = re.compile(r"\b(?P<a>[A-Z][a-z]+).*?\b(?P<b>[A-Z][a-z]+)\b.*?\b(?P<relation>brother|sister|sibling)\b", re.I)
    PLACE = re.compile(r"\b(?P<place>Castle\s+[A-Z][a-z]+)\s+(?:stood|stands|is)\s+(?P<value>north|south|east|west)\s+of\s+(?P<target>the\s+city|[A-Z][a-z]+)", re.I)
    TIMELINE = re.compile(r"\b(?P<value>\w+\s+days?\s+after\s+the\s+[^,.]+)", re.I)
    RULE = re.compile(r"\b(?:in this world,\s*)?(?P<value>every\s+spell\s+[^.]+)", re.I)

    def extract(self, chunks: Iterable[DocumentChunk], chapter_names: dict[UUID, str]) -> list[ExtractedFact]:
        facts: list[ExtractedFact] = []
        for chunk in chunks:
            current_character: str | None = None
            source = lambda evidence: SourceRef(
                document_id=chunk.document_id,
                chapter=chapter_names.get(chunk.chapter_id, "Unknown chapter"),
                section=chunk.section,
                page=chunk.page,
                chunk_id=chunk.source_chunk_id,
                evidence_text=evidence,
            )
            for sentence in self._sentences(chunk.text):
                names = re.findall(r"\b[A-Z][a-z]+\b", sentence)
                if names and names[0] not in {"Her", "His", "The", "In", "Three"}:
                    current_character = names[0]
                if match := self.EYES.search(sentence):
                    entity = match["name"].title()
                    if entity in {"Her", "His"} and current_character:
                        entity = current_character
                    facts.append(self._fact(FactKind.CHARACTER, entity, "eye_color", match["value"].lower(), source(sentence)))
                if match := self.SIBLING.search(sentence):
                    facts.append(self._fact(FactKind.RELATIONSHIP, match["a"].title(), f"relationship_{match['b'].lower()}", match["relation"].lower(), source(sentence)))
                if match := self.PLACE.search(sentence):
                    value = f"{match['value'].lower()} of {match['target'].lower()}"
                    facts.append(self._fact(FactKind.LOCATION, match["place"].title(), "location", value, source(sentence)))
                if match := self.TIMELINE.search(sentence):
                    facts.append(self._fact(FactKind.TIMELINE, "Story timeline", "relative_date", match["value"], source(sentence)))
                if match := self.RULE.search(sentence):
                    facts.append(self._fact(FactKind.WORLD_RULE, "Magic", "spell_cost", match["value"], source(sentence)))
        return facts

    @staticmethod
    def _sentences(text: str) -> list[str]:
        cleaned = " ".join(line.strip("# ") for line in text.splitlines() if line.strip())
        return [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]

    @staticmethod
    def _fact(kind: FactKind, entity: str, attribute: str, value: str, source: SourceRef) -> ExtractedFact:
        return ExtractedFact(kind=kind, entity=entity, attribute=attribute, value=value, source=source, confidence=0.95, supported=True)
