from uuid import uuid4

from series_bible.domain.continuity import ContinuityService
from series_bible.domain.schemas import ExtractedFact, FactKind, FindingType, SourceRef


def test_conflicting_facts_from_the_same_chapter_are_detected():
    source = lambda chunk, value: SourceRef(
        document_id=uuid4(), chapter="Chapter 1", chunk_id=chunk,
        evidence_text=f"Elena has {value} eyes.",
    )
    blue = ExtractedFact(kind=FactKind.CHARACTER, entity="Elena", attribute="eye_color", value="blue", confidence=.95, source=source("chunk-1", "blue"))
    green = ExtractedFact(kind=FactKind.CHARACTER, entity="Elena", attribute="eye_color", value="green", confidence=.95, source=source("chunk-2", "green"))
    assert ContinuityService().compare(blue, green).type is FindingType.CONTRADICTION