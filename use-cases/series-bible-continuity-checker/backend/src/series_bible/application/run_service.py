from __future__ import annotations

import hashlib
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from series_bible.application.bible import BibleService
from series_bible.application.extraction import GroundedRuleExtractor
from series_bible.application.retrieval import FactRetriever
from series_bible.domain.continuity import ContinuityService
from series_bible.domain.schemas import ExtractedFact, FactKind, FindingType, SourceRef
from series_bible.infrastructure.database import (
    AuditEvent,
    BibleFact,
    Chapter,
    ContinuityFinding,
    Document,
    DocumentChunk,
    Evidence,
    WorkflowEvent,
    WorkflowRun,
)


class WorkflowRunner:
    """Executes durable, idempotent ingest-to-review workflow stages."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.extractor = GroundedRuleExtractor()
        self.continuity = ContinuityService()

    async def execute(self, run_id: UUID) -> WorkflowRun:
        run = await self.session.get(WorkflowRun, run_id, with_for_update=True)
        if run is None:
            raise ValueError("workflow run not found")
        run.status = "RUNNING"
        document_ids = [UUID(value) for value in run.state.get("document_ids", [])]
        query = select(Document).where(Document.series_id == run.series_id)
        if document_ids:
            query = query.where(Document.id.in_(document_ids))
        else:
            query = query.where(Document.status == "IMPORTED")
        documents = list((await self.session.scalars(query.order_by(Document.created_at))).all())
        await self._checkpoint(run, "INGEST", {"documents": len(documents)})

        for document in documents:
            chunks = list((await self.session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document.id))).all())
            chapters = list((await self.session.scalars(select(Chapter).where(Chapter.document_id == document.id))).all())
            chapter_names = {chapter.id: chapter.title for chapter in chapters}
            facts = self.extractor.extract(chunks, chapter_names)
            await self._checkpoint(run, "EXTRACT_FACTS", {"facts": len(facts)})
            await self._checkpoint(run, "VALIDATE_FACTS", {"supported": len(facts)})
            await self._process_facts(run, facts)
            document.status = "PROCESSED"

        for stage in ("BUILD_OR_UPDATE_BIBLE", "RETRIEVE_RELEVANT_FACTS", "CHECK_CONTINUITY", "CLASSIFY_FINDINGS", "PERSIST_FINDINGS"):
            await self._checkpoint(run, stage, {})
        findings = list((await self.session.scalars(select(ContinuityFinding).where(ContinuityFinding.run_id == run.id, ContinuityFinding.status == "OPEN"))).all())
        run.status = "AWAITING_REVIEW" if findings else "COMPLETED"
        run.current_stage = "HUMAN_REVIEW" if findings else "UPDATE_BIBLE"
        run.state = {**run.state, "finding_ids": [str(item.id) for item in findings]}
        await self.session.commit()
        return run

    async def _process_facts(self, run: WorkflowRun, facts: list[ExtractedFact]) -> None:
        retriever = FactRetriever(self.session)
        bible = BibleService(self.session)
        for fact in facts:
            candidates = await retriever.retrieve(run.series_id, fact.entity, fact.attribute, limit=1)
            existing = candidates[0] if candidates else None
            existing_fact = await self._as_fact(existing) if existing else None
            result = self.continuity.compare(existing_fact, fact)
            if result.type is FindingType.NEW_INFORMATION:
                record = await bible.add_version(run.series_id, fact)
                self.session.add(AuditEvent(run_id=run.id, actor="workflow", entity="bible_fact", entity_id=str(record.id), operation="BIBLE_FACT_CREATED", metadata_json={"entity": fact.entity, "attribute": fact.attribute}))
            elif result.type in (FindingType.CONTRADICTION, FindingType.POSSIBLE_CONTRADICTION):
                fingerprint = hashlib.sha256(f"{existing.id if existing else ''}|{fact.entity}|{fact.attribute}|{fact.value}|{fact.source.chunk_id}".encode()).hexdigest()
                duplicate = await self.session.scalar(
                    select(ContinuityFinding).where(
                        ContinuityFinding.run_id == run.id,
                        ContinuityFinding.fingerprint == fingerprint,
                    )
                )
                if duplicate:
                    continue
                finding = ContinuityFinding(run_id=run.id, series_id=run.series_id, existing_fact_id=existing.id if existing else None, fingerprint=fingerprint, type=result.type.value, entity=fact.entity, attribute=fact.attribute, existing_value=existing.value if existing else None, new_value=fact.value, explanation=result.explanation, confidence=result.confidence, severity=result.severity, new_fact=fact.model_dump(mode="json"))
                self.session.add(finding)
                await self.session.flush()
                self.session.add(Evidence(finding_id=finding.id, document_id=fact.source.document_id, chapter=fact.source.chapter, section=fact.source.section, page=fact.source.page, chunk_id=fact.source.chunk_id, exact_text=fact.source.evidence_text, role="NEW"))
                self.session.add(AuditEvent(run_id=run.id, actor="workflow", entity="finding", entity_id=str(finding.id), operation="FINDING_CREATED", metadata_json={"type": result.type.value}))

    async def _as_fact(self, record: BibleFact) -> ExtractedFact:
        evidence = await self.session.scalar(select(Evidence).where(Evidence.fact_id == record.id).order_by(Evidence.created_at))
        if evidence is None:
            raise ValueError("Bible fact has no evidence")
        return ExtractedFact(kind=FactKind(record.kind), entity=record.entity, attribute=record.attribute, value=record.value, confidence=record.confidence, supported=True, source=SourceRef(document_id=evidence.document_id, chapter=evidence.chapter, section=evidence.section, page=evidence.page, chunk_id=evidence.chunk_id, evidence_text=evidence.exact_text))

    async def _checkpoint(self, run: WorkflowRun, stage: str, payload: dict[str, object]) -> None:
        if stage not in run.completed_stages:
            self.session.add(WorkflowEvent(run_id=run.id, stage=stage, status="COMPLETED", payload=payload))
            run.completed_stages = [*run.completed_stages, stage]
        run.current_stage = stage
        await self.session.flush()
