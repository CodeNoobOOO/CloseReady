"""Durable storage boundary for uploaded documents and processing jobs."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text as SQLText,
    delete,
    insert,
    select,
    update,
)

from .config import Principal
from .document_extraction import calculate_file_hash
from .document_models import (
    DocumentFinding,
    DocumentExtraction,
    DocumentJobRecord,
    DocumentPage,
    DocumentRecord,
    DocumentReviewDecisionPage,
    DocumentReviewDecisionRecord,
    DocumentReviewDecisionRequest,
    DocumentUploadRequest,
)
from .models import CaseSnapshot
from .store import DomainError, Store, audit, cases, forbidden

MAX_DOCUMENT_BYTES = 5 * 1024 * 1024

document_metadata = MetaData()
documents = Table(
    "documents",
    document_metadata,
    Column("document_id", String, primary_key=True),
    Column("case_id", String, nullable=False, index=True),
    Column("requirement_id", String, nullable=True),
    Column("file_hash", String, nullable=False, index=True),
    Column("content", LargeBinary, nullable=False),
    Column("record", SQLText, nullable=False),
)
document_jobs = Table(
    "document_jobs",
    document_metadata,
    Column("job_id", String, primary_key=True),
    Column("document_id", String, ForeignKey("documents.document_id"), nullable=False, unique=True),
    Column("case_id", String, nullable=False, index=True),
    Column("status", String, nullable=False, index=True),
    Column("claim_token", String, nullable=True),
    Column("lease_until", Integer, nullable=True),
    Column("record", SQLText, nullable=False),
)
document_extractions = Table(
    "document_extractions",
    document_metadata,
    Column("document_id", String, primary_key=True),
    Column("case_id", String, nullable=False, index=True),
    Column("record", SQLText, nullable=False),
)
document_findings = Table(
    "document_findings",
    document_metadata,
    Column("document_id", String, primary_key=True),
    Column("case_id", String, nullable=False, index=True),
    Column("record", SQLText, nullable=False),
)
document_analysis_telemetry = Table(
    "document_analysis_telemetry",
    document_metadata,
    Column("telemetry_id", String, primary_key=True),
    Column("job_id", String, ForeignKey("document_jobs.job_id"), nullable=False, index=True),
    Column("document_id", String, ForeignKey("documents.document_id"), nullable=False, index=True),
    Column("case_id", String, nullable=False, index=True),
    Column("attempt_number", Integer, nullable=False),
    Column("record", SQLText, nullable=False),
)
document_upload_responses = Table(
    "document_upload_responses",
    document_metadata,
    Column("actor_id", String, primary_key=True),
    Column("case_id", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("request_hash", String, nullable=False),
    Column("response", SQLText, nullable=False),
)
document_review_decisions = Table(
    "document_review_decisions",
    document_metadata,
    Column("decision_id", String, primary_key=True),
    Column("case_id", String, nullable=False, index=True),
    Column("document_id", String, nullable=False, index=True),
    Column("record", SQLText, nullable=False),
)
document_review_responses = Table(
    "document_review_responses",
    document_metadata,
    Column("actor_id", String, primary_key=True),
    Column("case_id", String, primary_key=True),
    Column("key", String, primary_key=True),
    Column("request_hash", String, nullable=False),
    Column("response", SQLText, nullable=False),
)


@dataclass(frozen=True)
class DocumentProcessingContext:
    case: CaseSnapshot
    document: DocumentRecord
    content: bytes


class DocumentStore:
    def __init__(self, store: Store, on_requirements_resolved=None):
        self.store = store
        self.on_requirements_resolved = on_requirements_resolved
        document_metadata.create_all(store.engine)

    @staticmethod
    def _request_digest(request: DocumentUploadRequest, file_hash: str) -> str:
        payload = request.model_dump(mode="json")
        payload["file_hash"] = file_hash
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _upload_request(
        expected_state_version: int,
        requirement_id: str | None,
        filename: str,
        media_type: str,
    ) -> DocumentUploadRequest:
        try:
            return DocumentUploadRequest.model_validate(
                {
                    "expected_state_version": expected_state_version,
                    "requirement_id": requirement_id,
                    "original_filename": filename,
                    "media_type": media_type,
                }
            )
        except ValidationError:
            if media_type != "application/pdf":
                raise DomainError(
                    "UNSUPPORTED_MEDIA_TYPE", "Only PDF documents are accepted.", 415
                ) from None
            raise DomainError("INVALID_DOCUMENT", "Document metadata is invalid.", 422) from None

    def upload(
        self,
        actor: Principal,
        case_id: str,
        *,
        requirement_id: str | None,
        expected_state_version: int,
        filename: str,
        media_type: str,
        content: bytes,
        key: str,
    ) -> DocumentJobRecord:
        current = None
        error = None
        with self.store.write() as conn:
            try:
                current = self.store._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                request = self._upload_request(
                    expected_state_version, requirement_id, filename, media_type
                )
                if not content:
                    raise DomainError(
                        "INVALID_DOCUMENT", "Document content is empty.", 422
                    )
                if len(content) > MAX_DOCUMENT_BYTES:
                    raise DomainError(
                        "DOCUMENT_TOO_LARGE", "Document exceeds the 5 MiB limit.", 413
                    )
                file_hash = calculate_file_hash(content)
                digest = self._request_digest(request, file_hash)

                replay = conn.execute(
                    select(document_upload_responses).where(
                        document_upload_responses.c.actor_id == actor.user_id,
                        document_upload_responses.c.case_id == case_id,
                        document_upload_responses.c.key == key,
                    )
                ).mappings().one_or_none()
                if replay is not None:
                    if replay["request_hash"] != digest:
                        raise DomainError(
                            "IDEMPOTENCY_CONFLICT",
                            "Key was already used with different input.",
                            409,
                        )
                    return DocumentJobRecord.model_validate_json(replay["response"])

                if current.state_version != request.expected_state_version:
                    raise DomainError("STALE_STATE", "Reload case before retrying.", 409)
                if request.requirement_id is not None:
                    bound_requirement = next(
                        (
                            requirement
                            for requirement in current.requirements
                            if requirement.requirement_id == request.requirement_id
                        ),
                        None,
                    )
                    if bound_requirement is None:
                        raise DomainError(
                            "INVALID_REQUIREMENT",
                            "Requirement does not belong to this case.",
                            422,
                        )
                    if bound_requirement.status in ("accepted", "waived"):
                        raise DomainError(
                            "REQUIREMENT_RESOLVED",
                            "Requirement is already resolved.",
                            409,
                        )

                duplicate_id = conn.execute(
                    select(documents.c.document_id)
                    .where(
                        documents.c.case_id == case_id,
                        documents.c.file_hash == file_hash,
                    )
                    .order_by(documents.c.document_id)
                    .limit(1)
                ).scalar_one_or_none()
                now = datetime.now(timezone.utc)
                document = DocumentRecord(
                    document_id="document_" + uuid4().hex,
                    case_id=case_id,
                    requirement_id=request.requirement_id,
                    input_state_version=current.state_version,
                    original_filename=request.original_filename,
                    media_type=request.media_type,
                    size_bytes=len(content),
                    file_hash=file_hash,
                    status="queued",
                    duplicate_of_document_id=duplicate_id,
                    created_by=actor.user_id,
                    created_at=now,
                )
                job = DocumentJobRecord(
                    job_id="document_job_" + uuid4().hex,
                    document_id=document.document_id,
                    case_id=case_id,
                    status="queued",
                    attempt_count=0,
                    created_at=now,
                )
                conn.execute(
                    insert(documents).values(
                        document_id=document.document_id,
                        case_id=case_id,
                        requirement_id=document.requirement_id,
                        file_hash=file_hash,
                        content=content,
                        record=document.model_dump_json(),
                    )
                )
                conn.execute(
                    insert(document_jobs).values(
                        job_id=job.job_id,
                        document_id=document.document_id,
                        case_id=case_id,
                        status=job.status,
                        record=job.model_dump_json(),
                    )
                )
                conn.execute(
                    insert(document_upload_responses).values(
                        actor_id=actor.user_id,
                        case_id=case_id,
                        key=key,
                        request_hash=digest,
                        response=job.model_dump_json(),
                    )
                )
                self.store._audit(
                    conn,
                    actor,
                    "queue_document",
                    "executed",
                    "Document accepted for processing.",
                    current,
                    old=current.state_version,
                    new=current.state_version,
                )
            except DomainError as exc:
                error = exc
                self.store._audit(
                    conn,
                    actor,
                    "queue_document",
                    "stale" if exc.code == "STALE_STATE" else "blocked",
                    exc.code,
                    current,
                    old=current.state_version if current else None,
                )
        if error:
            raise error
        return job

    def get_document(
        self, actor: Principal, case_id: str, document_id: str
    ) -> DocumentRecord:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            raw = conn.execute(
                select(documents.c.record).where(
                    documents.c.case_id == case_id,
                    documents.c.document_id == document_id,
                )
            ).scalar_one_or_none()
        if raw is None:
            raise DomainError("NOT_FOUND", "Document not found.", 404)
        return DocumentRecord.model_validate_json(raw)

    def get_content(
        self, actor: Principal, case_id: str, document_id: str
    ) -> tuple[DocumentRecord, bytes]:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            if not actor.can_manage:
                raise forbidden()
            row = conn.execute(
                select(documents.c.record, documents.c.content).where(
                    documents.c.case_id == case_id,
                    documents.c.document_id == document_id,
                )
            ).one_or_none()
        if row is None:
            raise DomainError("NOT_FOUND", "Document not found.", 404)
        return DocumentRecord.model_validate_json(row.record), row.content

    def list_documents(
            self, actor: Principal, case_id: str, cursor: str | None,
            limit: int) -> DocumentPage:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            query = select(documents.c.record).where(documents.c.case_id == case_id)
            if cursor is not None:
                query = query.where(documents.c.document_id > cursor)
            rows = conn.execute(
                query.order_by(documents.c.document_id).limit(limit + 1)
            ).scalars().all()
        items = [DocumentRecord.model_validate_json(row) for row in rows[:limit]]
        return DocumentPage(
            items=items,
            next_cursor=items[-1].document_id if len(rows) > limit else None,
        )

    @staticmethod
    def _review_digest(document_id: str, request: DocumentReviewDecisionRequest):
        payload = {"document_id": document_id, **request.model_dump(mode="json")}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def decide_review(
            self, actor: Principal, case_id: str, document_id: str,
            request: DocumentReviewDecisionRequest, key: str
            ) -> DocumentReviewDecisionRecord:
        error, current = None, None
        digest = self._review_digest(document_id, request)
        with self.store.write() as conn:
            try:
                current = self.store._case(conn, actor, case_id)
                if not actor.can_manage:
                    raise forbidden()
                replay = conn.execute(select(document_review_responses).where(
                    document_review_responses.c.actor_id == actor.user_id,
                    document_review_responses.c.case_id == case_id,
                    document_review_responses.c.key == key,
                )).mappings().one_or_none()
                if replay is not None:
                    if replay["request_hash"] != digest:
                        raise DomainError(
                            "IDEMPOTENCY_CONFLICT",
                            "Key was already used with different input.", 409)
                    return DocumentReviewDecisionRecord.model_validate_json(
                        replay["response"])
                if current.state_version != request.expected_state_version:
                    raise DomainError("STALE_STATE", "Reload case before retrying.", 409)
                row = conn.execute(select(documents).where(
                    documents.c.case_id == case_id,
                    documents.c.document_id == document_id,
                )).mappings().one_or_none()
                if row is None:
                    raise DomainError("NOT_FOUND", "Document not found.", 404)
                document = DocumentRecord.model_validate_json(row["record"])
                job_row = conn.execute(select(document_jobs).where(
                    document_jobs.c.document_id == document_id,
                    document_jobs.c.case_id == case_id,
                )).mappings().one()
                job = DocumentJobRecord.model_validate_json(job_row["record"])
                if document.status != "needs_review" or job.status != "needs_review":
                    raise DomainError(
                        "DOCUMENT_NOT_REVIEWABLE",
                        "Only a document waiting for review can be decided.", 409)
                finding_raw = conn.execute(select(document_findings.c.record).where(
                    document_findings.c.document_id == document_id,
                    document_findings.c.case_id == case_id,
                )).scalar_one_or_none()
                if finding_raw is None:
                    raise DomainError(
                        "DOCUMENT_NOT_REVIEWABLE", "Document finding is missing.", 409)
                finding = DocumentFinding.model_validate_json(finding_raw)
                target = None
                if request.target_requirement_id is not None:
                    target = next((item for item in current.requirements
                        if item.requirement_id == request.target_requirement_id), None)
                    if target is None:
                        raise DomainError(
                            "INVALID_REQUIREMENT",
                            "Requirement does not belong to this case.", 422)
                    if target.status in ("accepted", "waived"):
                        raise DomainError(
                            "REQUIREMENT_RESOLVED",
                            "Requirement is already resolved.", 409)

                next_version = current.state_version + 1
                requirements = [item.model_dump(mode="json")
                                for item in current.requirements]
                if request.decision == "accept_for_requirement":
                    if not finding.evidence_refs:
                        raise DomainError(
                            "INSUFFICIENT_EVIDENCE",
                            "A document without readable evidence cannot be accepted.", 422)
                    requirements = [{
                        **item.model_dump(mode="json"),
                        "status": "accepted",
                        "reviewer_status": "approved",
                        "evidence_refs": [ref.model_dump(mode="json")
                                          for ref in finding.evidence_refs],
                    } if item.requirement_id == target.requirement_id
                        else item.model_dump(mode="json")
                        for item in current.requirements]
                    next_document = DocumentRecord.model_validate({
                        **document.model_dump(mode="json"),
                        "requirement_id": target.requirement_id,
                        "status": "processed",
                    })
                    next_job = DocumentJobRecord.model_validate({
                        **job.model_dump(mode="json"),
                        "status": "completed", "error_code": None,
                    })
                elif request.decision == "reject_document":
                    next_document = DocumentRecord.model_validate({
                        **document.model_dump(mode="json"), "status": "rejected",
                    })
                    next_job = DocumentJobRecord.model_validate({
                        **job.model_dump(mode="json"), "status": "rejected",
                        "error_code": "REJECTED_BY_REVIEWER",
                    })
                else:
                    next_document = DocumentRecord.model_validate({
                        **document.model_dump(mode="json"),
                        "requirement_id": target.requirement_id,
                        "input_state_version": next_version,
                        "status": "queued",
                    })
                    next_job = DocumentJobRecord.model_validate({
                        **job.model_dump(mode="json"),
                        "status": "queued", "started_at": None,
                        "finished_at": None, "lease_expires_at": None,
                        "error_code": None,
                    })
                    conn.execute(delete(document_findings).where(
                        document_findings.c.document_id == document_id))
                    conn.execute(delete(document_extractions).where(
                        document_extractions.c.document_id == document_id))

                all_resolved = all(item["status"] in ("accepted", "waived")
                                   for item in requirements)
                changed = CaseSnapshot.model_validate({
                    **current.model_dump(mode="json"),
                    "state_version": next_version,
                    "readiness_status": (
                        "ready_for_confirmation" if all_resolved else "collecting"),
                    "requirements": requirements,
                })
                updated = conn.execute(update(cases).where(
                    cases.c.case_id == case_id,
                    cases.c.state_version == request.expected_state_version,
                ).values(
                    state_version=changed.state_version,
                    snapshot=changed.model_dump_json(),
                ))
                if updated.rowcount != 1:
                    raise DomainError("STALE_STATE", "Reload case before retrying.", 409)
                conn.execute(update(documents).where(
                    documents.c.document_id == document_id).values(
                    requirement_id=next_document.requirement_id,
                    record=next_document.model_dump_json(),
                ))
                self._replace_job(
                    conn, next_job, claim_token=None, lease_until=None)
                if request.decision == "accept_for_requirement" \
                        and self.on_requirements_resolved is not None:
                    self.on_requirements_resolved(
                        conn, actor, changed, [target.requirement_id])
                result = DocumentReviewDecisionRecord(
                    decision_id="document_decision_" + uuid4().hex,
                    case_id=case_id, document_id=document_id, job_id=job.job_id,
                    decision=request.decision,
                    target_requirement_id=request.target_requirement_id,
                    reviewer_user_id=actor.user_id, reason=request.reason,
                    decided_at=datetime.now(timezone.utc),
                    resulting_state_version=changed.state_version,
                    document_status=next_document.status,
                    source_finding=finding,
                )
                conn.execute(insert(document_review_decisions).values(
                    decision_id=result.decision_id, case_id=case_id,
                    document_id=document_id, record=result.model_dump_json()))
                conn.execute(insert(document_review_responses).values(
                    actor_id=actor.user_id, case_id=case_id, key=key,
                    request_hash=digest, response=result.model_dump_json()))
                audit_action, audit_outcome = {
                    "accept_for_requirement": ("accept_document_evidence", "accepted"),
                    "reject_document": ("reject_document", "rejected"),
                    "reassign_for_processing": ("reassign_document_processing", "queued"),
                }[request.decision]
                self.store._audit(
                    conn, actor, audit_action, audit_outcome,
                    request.reason, changed, old=current.state_version,
                    new=changed.state_version, details={
                        "decision": request.decision,
                        "document_id": document_id,
                        "document_filename": document.original_filename,
                        "target_requirement_id": request.target_requirement_id,
                    })
            except DomainError as exc:
                error = exc
                self.store._audit(
                    conn, actor, "decide_document_review",
                    "stale" if exc.code == "STALE_STATE" else "blocked",
                    exc.code, current,
                    old=current.state_version if current else None)
        if error:
            raise error
        return result

    def list_review_decisions(
            self, actor: Principal, case_id: str, document_id: str,
            cursor: str | None, limit: int) -> DocumentReviewDecisionPage:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            document_exists = conn.execute(select(documents.c.document_id).where(
                documents.c.case_id == case_id,
                documents.c.document_id == document_id,
            )).scalar_one_or_none()
            if document_exists is None:
                raise DomainError("NOT_FOUND", "Document not found.", 404)
            query = select(document_review_decisions.c.record).where(
                document_review_decisions.c.case_id == case_id,
                document_review_decisions.c.document_id == document_id,
            )
            if cursor is not None:
                query = query.where(document_review_decisions.c.decision_id > cursor)
            rows = conn.execute(query.order_by(
                document_review_decisions.c.decision_id).limit(limit + 1)).scalars().all()
        items = [DocumentReviewDecisionRecord.model_validate_json(row)
                 for row in rows[:limit]]
        return DocumentReviewDecisionPage(
            items=items,
            next_cursor=items[-1].decision_id if len(rows) > limit else None,
        )

    def get_job(
        self, actor: Principal, case_id: str, job_id: str
    ) -> DocumentJobRecord:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            raw = conn.execute(
                select(document_jobs.c.record).where(
                    document_jobs.c.case_id == case_id,
                    document_jobs.c.job_id == job_id,
                )
            ).scalar_one_or_none()
        if raw is None:
            raise DomainError("NOT_FOUND", "Document job not found.", 404)
        return DocumentJobRecord.model_validate_json(raw)

    def get_finding(
        self, actor: Principal, case_id: str, document_id: str
    ) -> DocumentFinding | None:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            raw = conn.execute(
                select(document_findings.c.record).where(
                    document_findings.c.case_id == case_id,
                    document_findings.c.document_id == document_id,
                )
            ).scalar_one_or_none()
        return DocumentFinding.model_validate_json(raw) if raw is not None else None

    def get_extraction(
        self, actor: Principal, case_id: str, document_id: str
    ) -> DocumentExtraction | None:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            raw = conn.execute(
                select(document_extractions.c.record).where(
                    document_extractions.c.case_id == case_id,
                    document_extractions.c.document_id == document_id,
                )
            ).scalar_one_or_none()
        return DocumentExtraction.model_validate_json(raw) if raw is not None else None

    def queued_candidates(self) -> list[str]:
        with self.store.engine.connect() as conn:
            return list(
                conn.execute(
                    select(document_jobs.c.job_id)
                    .where(document_jobs.c.status == "queued")
                    .order_by(document_jobs.c.job_id)
                ).scalars()
            )

    def expired_job_candidates(self) -> list[str]:
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        with self.store.engine.connect() as conn:
            return list(
                conn.execute(
                    select(document_jobs.c.job_id)
                    .where(
                        document_jobs.c.status == "processing",
                        document_jobs.c.lease_until <= now_epoch,
                    )
                    .order_by(document_jobs.c.job_id)
                ).scalars()
            )

    @staticmethod
    def _replace_document(conn, document: DocumentRecord) -> None:
        conn.execute(
            update(documents)
            .where(documents.c.document_id == document.document_id)
            .values(record=document.model_dump_json())
        )

    @staticmethod
    def _replace_job(
        conn,
        job: DocumentJobRecord,
        *,
        claim_token: str | None,
        lease_until: int | None,
    ) -> None:
        conn.execute(
            update(document_jobs)
            .where(document_jobs.c.job_id == job.job_id)
            .values(
                status=job.status,
                claim_token=claim_token,
                lease_until=lease_until,
                record=job.model_dump_json(),
            )
        )

    def claim(self, job_id: str) -> str | None:
        with self.store.write() as conn:
            row = conn.execute(
                select(document_jobs).where(document_jobs.c.job_id == job_id)
            ).mappings().one_or_none()
            if row is None or row["status"] != "queued":
                return None
            job = DocumentJobRecord.model_validate_json(row["record"])
            token = "document_claim_" + uuid4().hex
            started = datetime.now(timezone.utc)
            lease = started + timedelta(seconds=300)
            claimed = DocumentJobRecord.model_validate(
                {
                    **job.model_dump(mode="json"),
                    "status": "processing",
                    "attempt_count": job.attempt_count + 1,
                    "started_at": started,
                    "finished_at": None,
                    "lease_expires_at": lease,
                    "error_code": None,
                }
            )
            document = DocumentRecord.model_validate_json(
                conn.execute(
                    select(documents.c.record).where(
                        documents.c.document_id == job.document_id
                    )
                ).scalar_one()
            )
            processing_document = DocumentRecord.model_validate(
                {**document.model_dump(mode="json"), "status": "processing"}
            )
            self._replace_document(conn, processing_document)
            self._replace_job(
                conn,
                claimed,
                claim_token=token,
                lease_until=int(lease.timestamp()),
            )
            return token

    def recover_expired_system(self, job_id: str) -> DocumentJobRecord | None:
        with self.store.write() as conn:
            row = conn.execute(
                select(document_jobs).where(document_jobs.c.job_id == job_id)
            ).mappings().one_or_none()
            if row is None:
                return None
            job = DocumentJobRecord.model_validate_json(row["record"])
            if row["status"] != "processing":
                return job
            if row["lease_until"] is not None and row["lease_until"] > int(
                datetime.now(timezone.utc).timestamp()
            ):
                raise DomainError("JOB_ACTIVE", "Job still has an active claim.", 409)
            queued = DocumentJobRecord.model_validate(
                {
                    **job.model_dump(mode="json"),
                    "status": "queued",
                    "started_at": None,
                    "lease_expires_at": None,
                    "error_code": None,
                }
            )
            document = DocumentRecord.model_validate_json(
                conn.execute(
                    select(documents.c.record).where(
                        documents.c.document_id == job.document_id
                    )
                ).scalar_one()
            )
            queued_document = DocumentRecord.model_validate(
                {**document.model_dump(mode="json"), "status": "queued"}
            )
            self._replace_document(conn, queued_document)
            self._replace_job(conn, queued, claim_token=None, lease_until=None)
            return queued

    def processing_context(self, job_id: str, token: str) -> DocumentProcessingContext:
        with self.store.engine.connect() as conn:
            row = conn.execute(
                select(document_jobs).where(document_jobs.c.job_id == job_id)
            ).mappings().one_or_none()
            if (
                row is None
                or row["status"] != "processing"
                or row["claim_token"] != token
            ):
                raise DomainError("INVALID_CLAIM", "Document job claim is invalid.", 409)
            document_row = conn.execute(
                select(documents).where(
                    documents.c.document_id == row["document_id"]
                )
            ).mappings().one()
            raw_case = conn.execute(
                select(cases.c.snapshot).where(cases.c.case_id == row["case_id"])
            ).scalar_one_or_none()
            if raw_case is None:
                raise DomainError("NOT_FOUND", "Case not found.", 404)
            return DocumentProcessingContext(
                case=CaseSnapshot.model_validate_json(raw_case),
                document=DocumentRecord.model_validate_json(document_row["record"]),
                content=document_row["content"],
            )

    @staticmethod
    def _document_only_version_drift(conn, case_id: str, old: int, new: int) -> bool:
        """Allow rebasing only across successful automatic document updates."""
        transitions: dict[int, set[str]] = {}
        for raw in conn.execute(select(audit.c.record).where(
                audit.c.case_id == case_id)).scalars():
            record = json.loads(raw)
            previous = record.get("old_state_version")
            current = record.get("new_state_version")
            if (
                isinstance(previous, int)
                and isinstance(current, int)
                and current == previous + 1
                and old <= previous < new
            ):
                transitions.setdefault(current, set()).add(record.get("action"))
        return all(
            transitions.get(version) == {"apply_document_finding"}
            for version in range(old + 1, new + 1)
        )

    def refresh_claimed_processing_context(
            self, job_id: str, token: str) -> DocumentProcessingContext | None:
        """Refresh a claimed attachment after sibling documents changed the Case.

        Other Case mutations retain the existing stale-state guard. A bound
        document is also never refreshed after its Requirement is resolved.
        """
        with self.store.write() as conn:
            row = conn.execute(
                select(document_jobs).where(document_jobs.c.job_id == job_id)
            ).mappings().one_or_none()
            if (
                row is None
                or row["status"] != "processing"
                or row["claim_token"] != token
            ):
                raise DomainError("INVALID_CLAIM", "Document job claim is invalid.", 409)
            document_row = conn.execute(select(documents).where(
                documents.c.document_id == row["document_id"]
            )).mappings().one()
            document = DocumentRecord.model_validate_json(document_row["record"])
            case = CaseSnapshot.model_validate_json(conn.execute(
                select(cases.c.snapshot).where(cases.c.case_id == row["case_id"])
            ).scalar_one())
            if case.state_version == document.input_state_version:
                return DocumentProcessingContext(
                    case=case, document=document, content=document_row["content"])
            if not self._document_only_version_drift(
                    conn, case.case_id, document.input_state_version, case.state_version):
                return None
            outstanding = {
                requirement.requirement_id
                for requirement in case.requirements
                if requirement.status not in ("accepted", "waived")
            }
            if (
                not outstanding
                or (
                    document.requirement_id is not None
                    and document.requirement_id not in outstanding
                )
            ):
                return None
            refreshed = DocumentRecord.model_validate({
                **document.model_dump(mode="json"),
                "input_state_version": case.state_version,
            })
            self._replace_document(conn, refreshed)
            self.store._audit(
                conn,
                self._system_actor(case),
                "refresh_document_processing_context",
                "executed",
                "Queued attachment refreshed after sibling document processing.",
                case,
                old=case.state_version,
                new=case.state_version,
                details={
                    "document_id": document.document_id,
                    "previous_input_state_version": str(document.input_state_version),
                    "current_input_state_version": str(case.state_version),
                },
            )
            return DocumentProcessingContext(
                case=case, document=refreshed, content=document_row["content"])

    def bind_claimed_requirement(
        self,
        job_id: str,
        token: str,
        requirement_id: str,
        match_source: str,
        candidate_requirement_ids: list[str],
    ) -> DocumentRecord:
        """Persist a deterministic worker match while retaining the claim gate."""
        with self.store.write() as conn:
            row = conn.execute(
                select(document_jobs).where(document_jobs.c.job_id == job_id)
            ).mappings().one_or_none()
            if (
                row is None
                or row["status"] != "processing"
                or row["claim_token"] != token
            ):
                raise DomainError("INVALID_CLAIM", "Document job claim is invalid.", 409)
            document = DocumentRecord.model_validate_json(
                conn.execute(select(documents.c.record).where(
                    documents.c.document_id == row["document_id"]
                )).scalar_one()
            )
            case = CaseSnapshot.model_validate_json(
                conn.execute(select(cases.c.snapshot).where(
                    cases.c.case_id == row["case_id"]
                )).scalar_one()
            )
            if case.state_version != document.input_state_version:
                raise DomainError(
                    "STALE_STATE",
                    "Reload case before matching this document.",
                    409,
                )
            if document.requirement_id not in (None, requirement_id):
                raise DomainError(
                    "INVALID_REQUIREMENT",
                    "Document is already bound to another requirement.",
                    409,
                )
            requirement = next((
                item for item in case.requirements
                if item.requirement_id == requirement_id
            ), None)
            if requirement is None:
                raise DomainError(
                    "INVALID_REQUIREMENT",
                    "Requirement does not belong to this case.",
                    422,
                )
            if requirement.status in ("accepted", "waived"):
                raise DomainError(
                    "REQUIREMENT_RESOLVED",
                    "Requirement is already resolved.",
                    409,
                )
            bound = DocumentRecord.model_validate({
                **document.model_dump(mode="json"),
                "requirement_id": requirement_id,
            })
            self._replace_document(conn, bound)
            self.store._audit(
                conn,
                self._system_actor(case),
                "bind_document_requirement",
                "executed",
                (
                    "Document assigned because it was the only outstanding "
                    "requirement."
                    if match_source == "single_candidate"
                    else "Document matched one requirement from verified type, "
                    "period, entity and document identifiers."
                ),
                case,
                old=case.state_version,
                new=case.state_version,
                details={
                    "document_id": document.document_id,
                    "target_requirement_id": requirement_id,
                    "match_source": match_source,
                    "candidate_requirement_ids": candidate_requirement_ids,
                },
            )
            return bound

    @staticmethod
    def _system_actor(case: CaseSnapshot) -> Principal:
        return Principal(
            user_id="system_document_worker",
            token_sha256="0" * 64,
            client_ids=frozenset({case.client_id}),
            can_manage=True,
        )

    def _terminal_without_case_change(
        self, job_id: str, token: str, status: str, error_code: str
    ) -> DocumentJobRecord:
        with self.store.write() as conn:
            row = conn.execute(
                select(document_jobs).where(document_jobs.c.job_id == job_id)
            ).mappings().one_or_none()
            if (
                row is None
                or row["status"] != "processing"
                or row["claim_token"] != token
            ):
                raise DomainError("INVALID_CLAIM", "Document job claim is invalid.", 409)
            job = DocumentJobRecord.model_validate_json(row["record"])
            finished = datetime.now(timezone.utc)
            terminal = DocumentJobRecord.model_validate(
                {
                    **job.model_dump(mode="json"),
                    "status": status,
                    "finished_at": finished,
                    "lease_expires_at": None,
                    "error_code": error_code,
                }
            )
            document = DocumentRecord.model_validate_json(
                conn.execute(
                    select(documents.c.record).where(
                        documents.c.document_id == job.document_id
                    )
                ).scalar_one()
            )
            raw_case = conn.execute(
                select(cases.c.snapshot).where(cases.c.case_id == job.case_id)
            ).scalar_one()
            case = CaseSnapshot.model_validate_json(raw_case)
            terminal_document = DocumentRecord.model_validate(
                {**document.model_dump(mode="json"), "status": status}
            )
            self._replace_document(conn, terminal_document)
            self._replace_job(conn, terminal, claim_token=None, lease_until=None)
            self.store._audit(
                conn,
                self._system_actor(case),
                "process_document",
                status,
                error_code,
                case,
                old=case.state_version,
                new=case.state_version,
            )
            return terminal

    def fail(self, job_id: str, token: str, error_code: str) -> DocumentJobRecord:
        return self._terminal_without_case_change(job_id, token, "failed", error_code)

    def mark_stale(self, job_id: str, token: str) -> DocumentJobRecord:
        return self._terminal_without_case_change(job_id, token, "stale", "STALE_STATE")

    def persist_analysis_telemetry(self, job_id: str, token: str, telemetry) -> None:
        usage = telemetry.usage
        safe_usage = None
        if isinstance(usage, dict):
            safe_usage = {
                key: value for key, value in usage.items()
                if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
                and isinstance(value, int) and not isinstance(value, bool) and value >= 0
            }
        record = {
            "provider": telemetry.provider,
            "model": telemetry.model,
            "prompt_schema_version": telemetry.prompt_schema_version,
            "latency_ms": telemetry.latency_ms,
            "usage": safe_usage or None,
            "request_id": telemetry.request_id,
            "error_code": telemetry.error_code,
            "repair_count": telemetry.repair_count,
        }
        with self.store.write() as conn:
            row = conn.execute(select(document_jobs).where(
                document_jobs.c.job_id == job_id
            )).mappings().one_or_none()
            if row is None or row["claim_token"] != token:
                raise DomainError("INVALID_CLAIM", "Document job claim is invalid.", 409)
            conn.execute(insert(document_analysis_telemetry).values(
                telemetry_id="document_telemetry_" + uuid4().hex,
                job_id=job_id,
                document_id=row["document_id"],
                case_id=row["case_id"],
                attempt_number=DocumentJobRecord.model_validate_json(row["record"]).attempt_count,
                record=json.dumps(record),
            ))

    def get_analysis_telemetry(
        self, actor: Principal, case_id: str, job_id: str
    ) -> list[dict]:
        with self.store.engine.connect() as conn:
            self.store._case(conn, actor, case_id)
            rows = conn.execute(select(
                document_analysis_telemetry.c.attempt_number,
                document_analysis_telemetry.c.record,
            ).where(
                document_analysis_telemetry.c.case_id == case_id,
                document_analysis_telemetry.c.job_id == job_id,
            ).order_by(
                document_analysis_telemetry.c.attempt_number,
                document_analysis_telemetry.c.telemetry_id,
            )).all()
        if not rows:
            raise DomainError("NOT_FOUND", "Document analysis telemetry not found.", 404)
        return [
            {"attempt_number": attempt_number, **json.loads(record)}
            for attempt_number, record in rows
        ]

    def complete(
        self,
        job_id: str,
        token: str,
        extraction: DocumentExtraction,
        finding: DocumentFinding,
    ) -> DocumentJobRecord:
        with self.store.write() as conn:
            row = conn.execute(
                select(document_jobs).where(document_jobs.c.job_id == job_id)
            ).mappings().one_or_none()
            if (
                row is None
                or row["status"] != "processing"
                or row["claim_token"] != token
            ):
                raise DomainError("INVALID_CLAIM", "Document job claim is invalid.", 409)
            job = DocumentJobRecord.model_validate_json(row["record"])
            document = DocumentRecord.model_validate_json(
                conn.execute(
                    select(documents.c.record).where(
                        documents.c.document_id == job.document_id
                    )
                ).scalar_one()
            )
            raw_case = conn.execute(
                select(cases.c.snapshot).where(cases.c.case_id == job.case_id)
            ).scalar_one()
            case = CaseSnapshot.model_validate_json(raw_case)
            if (
                finding.case_id != case.case_id
                or finding.document_id != document.document_id
                or finding.input_state_version != document.input_state_version
                or extraction.file_hash != document.file_hash
            ):
                raise DomainError(
                    "INVALID_DOCUMENT_RESULT", "Document result failed validation.", 422
                )

            conn.execute(
                insert(document_extractions).values(
                    document_id=document.document_id,
                    case_id=case.case_id,
                    record=extraction.model_dump_json(),
                )
            )
            conn.execute(
                insert(document_findings).values(
                    document_id=document.document_id,
                    case_id=case.case_id,
                    record=finding.model_dump_json(),
                )
            )

            if case.state_version != document.input_state_version:
                status, error_code = "stale", "STALE_STATE"
            elif finding.result == "satisfies":
                requirement = next(
                    (
                        item
                        for item in case.requirements
                        if item.requirement_id == document.requirement_id
                    ),
                    None,
                )
                coverage_satisfied = (
                    requirement is not None
                    and requirement.completion_rule.kind == "coverage"
                    and finding.coverage_start is not None
                    and finding.coverage_end is not None
                    and requirement.scope.coverage_start is not None
                    and requirement.scope.coverage_end is not None
                    and finding.coverage_start <= requirement.scope.coverage_start
                    and finding.coverage_end >= requirement.scope.coverage_end
                )
                expected_items_satisfied = (
                    requirement is not None
                    and requirement.document_type in ("invoice", "receipt")
                    and requirement.completion_rule.kind == "explicit_items"
                    and len(requirement.completion_rule.expected_item_refs) == 1
                    and set(finding.matched_item_refs)
                    == set(requirement.completion_rule.expected_item_refs)
                    and finding.detected_period == requirement.accounting_period
                    and all(
                        any(
                            expected.casefold() in evidence.excerpt.casefold()
                            for evidence in finding.evidence_refs
                        )
                        for expected in requirement.completion_rule.expected_item_refs
                    )
                )
                if (
                    requirement is None
                    or finding.requirement_id != requirement.requirement_id
                    or not finding.evidence_refs
                    or not extraction.readable
                    or document.duplicate_of_document_id is not None
                    or finding.detected_type != requirement.document_type
                    or finding.entity_match != "match"
                    or (
                        requirement.scope.account_ref is not None
                        and finding.account_match != "match"
                    )
                    or finding.uncertainty_reasons
                    or finding.issues
                    or any(
                        evidence.document_id != document.document_id
                        for evidence in finding.evidence_refs
                    )
                    or not (coverage_satisfied or expected_items_satisfied)
                ):
                    raise DomainError(
                        "INVALID_DOCUMENT_RESULT",
                        "Satisfaction evidence failed validation.",
                        422,
                    )
                requirements = []
                for item in case.requirements:
                    if item.requirement_id == requirement.requirement_id:
                        requirements.append(
                            {
                                **item.model_dump(mode="json"),
                                "status": "accepted",
                                "evidence_refs": [
                                    ref.model_dump(mode="json")
                                    for ref in finding.evidence_refs
                                ],
                            }
                        )
                    else:
                        requirements.append(item.model_dump(mode="json"))
                all_resolved = all(
                    item["status"] in ("accepted", "waived") for item in requirements
                )
                changed = CaseSnapshot.model_validate(
                    {
                        **case.model_dump(mode="json"),
                        "state_version": case.state_version + 1,
                        "readiness_status": (
                            "ready_for_confirmation" if all_resolved else "collecting"
                        ),
                        "requirements": requirements,
                    }
                )
                updated = conn.execute(
                    update(cases)
                    .where(
                        cases.c.case_id == case.case_id,
                        cases.c.state_version == case.state_version,
                    )
                    .values(
                        state_version=changed.state_version,
                        snapshot=changed.model_dump_json(),
                    )
                )
                if updated.rowcount != 1:
                    raise DomainError("STALE_STATE", "Reload case before retrying.", 409)
                self.store._audit(
                    conn,
                    self._system_actor(changed),
                    "apply_document_finding",
                    "executed",
                    "Verified document evidence accepted.",
                    changed,
                    old=case.state_version,
                    new=changed.state_version,
                )
                if self.on_requirements_resolved is not None:
                    self.on_requirements_resolved(
                        conn,
                        self._system_actor(changed),
                        changed,
                        [requirement.requirement_id],
                    )
                status, error_code = "completed", None
            else:
                status, error_code = "needs_review", None

            finished = datetime.now(timezone.utc)
            completed = DocumentJobRecord.model_validate(
                {
                    **job.model_dump(mode="json"),
                    "status": status,
                    "finished_at": finished,
                    "lease_expires_at": None,
                    "error_code": error_code,
                }
            )
            document_status = "processed" if status == "completed" else status
            completed_document = DocumentRecord.model_validate(
                {**document.model_dump(mode="json"), "status": document_status}
            )
            self._replace_document(conn, completed_document)
            self._replace_job(conn, completed, claim_token=None, lease_until=None)
            if status != "completed":
                self.store._audit(
                    conn,
                    self._system_actor(case),
                    "process_document",
                    status,
                    error_code or finding.result,
                    case,
                    old=case.state_version,
                    new=case.state_version,
                )
            return completed
