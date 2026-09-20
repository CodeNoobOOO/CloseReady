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
from .store import DomainError, Store, cases, forbidden

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
                reopening = request.decision == "reopen_review"
                if reopening and (document.status != "processed" or job.status != "completed"):
                    raise DomainError("DOCUMENT_NOT_REOPENABLE", "Only accepted document evidence can be reopened.", 409)
                if not reopening and (document.status != "needs_review" or job.status != "needs_review"):
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
                if reopening:
                    affected = {item.requirement_id for item in current.requirements
                                if item.status == "accepted" and any(
                                    ref.document_id == document_id for ref in item.evidence_refs)}
                    if not affected:
                        raise DomainError("DOCUMENT_NOT_REOPENABLE", "Document does not support an accepted requirement.", 409)
                    requirements = [{
                        **item.model_dump(mode="json"),
                        "status": "awaiting_review", "reviewer_status": "pending",
                        "evidence_refs": [ref.model_dump(mode="json") for ref in item.evidence_refs
                                          if ref.document_id != document_id],
                    } if item.requirement_id in affected else item.model_dump(mode="json")
                        for item in current.requirements]
                    next_document = DocumentRecord.model_validate({
                        **document.model_dump(mode="json"), "status": "needs_review",
                    })
                    next_job = DocumentJobRecord.model_validate({
                        **job.model_dump(mode="json"), "status": "needs_review",
                        "error_code": None, "lease_expires_at": None,
                    })
                elif request.decision == "accept_for_requirement":
                    if (finding.detected_type is not None and finding.detected_type != target.document_type
                            or finding.detected_period is not None and finding.detected_period != target.accounting_period
                            or finding.entity_match == "mismatch" or finding.account_match == "mismatch"
                            or target.completion_rule.kind == "coverage" and (
                                finding.coverage_start is not None and finding.coverage_start > target.scope.coverage_start
                                or finding.coverage_end is not None and finding.coverage_end < target.scope.coverage_end)):
                        raise DomainError("DOCUMENT_REQUIREMENT_CONFLICT",
                            "Document type, period, identity or coverage conflicts with the requirement. Reject it or supply corrected evidence.", 409)
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
                    requirements = [{
                        **item.model_dump(mode="json"), "status": "missing",
                        "reviewer_status": "not_required",
                    } if item.requirement_id == document.requirement_id
                        and item.status == "awaiting_review" and not item.evidence_refs
                        else item.model_dump(mode="json") for item in current.requirements]
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
                self.store._audit(
                    conn, actor, "decide_document_review", "executed",
                    request.reason, changed, old=current.state_version,
                    new=changed.state_version)
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
                    or requirement.completion_rule.kind != "coverage"
                    or finding.coverage_start is None
                    or finding.coverage_end is None
                    or finding.coverage_start > requirement.scope.coverage_start
                    or finding.coverage_end < requirement.scope.coverage_end
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
