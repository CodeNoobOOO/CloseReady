"""Authenticated case API. Start with uvicorn closeready.api:from_env --factory."""
from contextlib import asynccontextmanager
import os
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from .case_requests import AuditPage, CasePage, ChangeDeadlineRequest, CreateCaseRequest
from .config import AccessConfig, Principal, load_access_config
from .models import CaseSnapshot
from .store import DomainError, Store
from .runtime import AgentRuntime
from .runtime_models import (
    AnalyseRequest, OutboxPage, ReviewDecisionRequest, ReviewTaskPage,
    ReviewTaskRecord, RunRecord,
)
from .runtime_store import RuntimeStore
from .llm import LLMProvider
from .mail import MailBackend, mail_backend
from .provider_factory import provider_from_environment
from .health import HealthStatus
from .communication_models import (
    AssessReplyRequest, AssessReplyResult, CommitmentPage, DeliverOutboxRequest,
    DeliveryResult, DispatchRemindersResult, FindingPage, IngestReplyRequest,
    IngestReplyResult, MailboxPage, ReminderPage, ReplyPage,
)
from .communication_store import CommunicationStore
from .communication_store import communication_metadata
from .document_models import DocumentFinding, DocumentJobRecord, DocumentRecord
from .document_store import MAX_DOCUMENT_BYTES, DocumentStore, document_metadata
from .runtime_store import runtime_metadata


def create_app(database_url: str, access: AccessConfig, provider: LLMProvider | None = None,
               mail: MailBackend | None = None) -> FastAPI:
    store = Store(database_url, access)
    runtime_store = RuntimeStore(store)
    communication = CommunicationStore(store, runtime_store, mail)
    document_store = DocumentStore(store)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            store.engine.dispose()

    app = FastAPI(title='CloseReady Case API', version='0.1.0', lifespan=lifespan)
    app.state.store = store
    app.state.runtime_store = runtime_store
    app.state.communication = communication
    app.state.document_store = document_store
    bearer = HTTPBearer(auto_error=False)

    def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Principal:
        actor = access.authenticate(credentials.credentials) if credentials else None
        if actor is None:
            raise DomainError('UNAUTHENTICATED', 'Valid bearer credentials are required.', 401)
        return actor

    Actor = Annotated[Principal, Depends(authenticate)]
    Key = Annotated[str, Header(alias='Idempotency-Key', min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')]

    @app.middleware('http')
    async def request_id(request: Request, call_next):
        request.state.request_id = 'request_' + uuid4().hex
        response = await call_next(request)
        response.headers['X-Request-ID'] = request.state.request_id
        return response

    def error_response(request, code, message, status, retryable=False):
        return JSONResponse(status_code=status,
            content={'error': {'code': code, 'message': message, 'retryable': retryable},
                     'request_id': request.state.request_id},
            headers={'WWW-Authenticate': 'Bearer'} if status == 401 else None)

    @app.get('/health/live', response_model=HealthStatus, include_in_schema=True)
    def live():
        return HealthStatus(status='ok')

    @app.get('/health/ready', response_model=HealthStatus, include_in_schema=True)
    def ready():
        try:
            store.check_ready(
                set(runtime_metadata.tables)
                .union(communication_metadata.tables)
                .union(document_metadata.tables)
            )
        except (SQLAlchemyError, RuntimeError):
            raise DomainError('NOT_READY', 'Persistent storage is not ready.', 503) from None
        return HealthStatus(status='ready')

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return error_response(request, exc.code, exc.message, exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError):
        # Validation errors can include raw bodies and secrets. Do not echo them.
        return error_response(request, 'INVALID_INPUT', 'Input does not match the API schema.', 422)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return error_response(request, 'STORAGE_UNAVAILABLE', 'Storage operation failed; retry with the same idempotency key.', 503, True)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(request, 'HTTP_ERROR', 'Request could not be handled.', exc.status_code)

    @app.post('/api/v1/cases', response_model=CaseSnapshot, status_code=201)
    def create_case(body: CreateCaseRequest, actor: Actor, key: Key):
        return store.create_case(actor, body, key)

    @app.get('/api/v1/cases', response_model=CasePage)
    def list_cases(actor: Actor, cursor: Annotated[str | None, Query(max_length=128)] = None,
                   limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return store.list_cases(actor, cursor, limit)

    @app.get('/api/v1/cases/{case_id}', response_model=CaseSnapshot)
    def get_case(case_id: str, actor: Actor):
        return store.get_case(actor, case_id)

    @app.patch('/api/v1/cases/{case_id}/deadline', response_model=CaseSnapshot)
    def change_deadline(case_id: str, body: ChangeDeadlineRequest, actor: Actor, key: Key):
        return store.change_deadline(actor, case_id, body, key)

    @app.get('/api/v1/cases/{case_id}/audit-events', response_model=AuditPage)
    def get_audit(case_id: str, actor: Actor, cursor: Annotated[int, Query(ge=0)] = 0,
                  limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return store.audit_events(actor, case_id, cursor, limit)

    @app.post(
        '/api/v1/cases/{case_id}/documents',
        response_model=DocumentJobRecord,
        status_code=202,
    )
    async def upload_document(
        case_id: str,
        actor: Actor,
        key: Key,
        file: Annotated[UploadFile, File()],
        expected_state_version: Annotated[int, Form(gt=0)],
        requirement_id: Annotated[str | None, Form()] = None,
    ):
        try:
            content = await file.read(MAX_DOCUMENT_BYTES + 1)
        finally:
            await file.close()
        return document_store.upload(
            actor,
            case_id,
            requirement_id=requirement_id,
            expected_state_version=expected_state_version,
            filename=file.filename or 'document.pdf',
            media_type=file.content_type or '',
            content=content,
            key=key,
        )

    @app.get(
        '/api/v1/cases/{case_id}/documents/{document_id}',
        response_model=DocumentRecord,
    )
    def get_document(case_id: str, document_id: str, actor: Actor):
        return document_store.get_document(actor, case_id, document_id)

    @app.get(
        '/api/v1/cases/{case_id}/document-jobs/{job_id}',
        response_model=DocumentJobRecord,
    )
    def get_document_job(case_id: str, job_id: str, actor: Actor):
        return document_store.get_job(actor, case_id, job_id)

    @app.get(
        '/api/v1/cases/{case_id}/documents/{document_id}/finding',
        response_model=DocumentFinding,
    )
    def get_document_finding(case_id: str, document_id: str, actor: Actor):
        finding = document_store.get_finding(actor, case_id, document_id)
        if finding is None:
            raise DomainError('NOT_FOUND', 'Document finding not found.', 404)
        return finding

    @app.post('/api/v1/cases/{case_id}/runs', response_model=RunRecord)
    def analyse_case(case_id: str, body: AnalyseRequest, actor: Actor, key: Key):
        store.get_case(actor, case_id)
        if not actor.can_manage:
            raise DomainError('FORBIDDEN', 'Analysis requires a manager.', 403)
        if provider is None:
            raise DomainError('LLM_UNAVAILABLE', 'Live LLM configuration is not enabled.', 503)
        return AgentRuntime(runtime_store, provider).analyse(actor, case_id, body.expected_state_version, key)

    @app.post('/api/v1/cases/{case_id}/activate', response_model=RunRecord, status_code=202)
    def activate_case(case_id: str, body: AnalyseRequest, actor: Actor, key: Key):
        store.get_case(actor, case_id)
        if not actor.can_manage:
            raise DomainError('FORBIDDEN', 'Activation requires a manager.', 403)
        if provider is None:
            raise DomainError('LLM_UNAVAILABLE', 'Live LLM configuration is not enabled.', 503)
        if not provider.live:
            raise DomainError('LIVE_LLM_REQUIRED', 'Case activation requires a live LLM provider.', 503)
        return runtime_store.start(actor, case_id, body.expected_state_version,
            'activate|' + key, provider.provider_name, provider.model, provider.live,
            event_type='case_activated', audit_action='activate_case')

    @app.get('/api/v1/runs/{run_id}', response_model=RunRecord)
    def get_run(run_id: str, actor: Actor):
        return runtime_store.get_run(actor, run_id)

    @app.post('/api/v1/runs/{run_id}/recover', response_model=RunRecord)
    def recover_run(run_id: str, actor: Actor, key: Key):
        # Terminal transition is intrinsically idempotent; no inference is retried.
        return runtime_store.recover(actor, run_id)

    @app.get('/api/v1/cases/{case_id}/review-tasks', response_model=ReviewTaskPage)
    def review_tasks(case_id: str, actor: Actor,
                     cursor: Annotated[str | None, Query(max_length=128)] = None,
                     limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return runtime_store.review_tasks(actor, case_id, cursor, limit)

    @app.post('/api/v1/cases/{case_id}/review-decisions', response_model=ReviewTaskRecord)
    def decide_review(case_id: str, body: ReviewDecisionRequest, actor: Actor, key: Key):
        return runtime_store.decide_review(actor, case_id, body, key)

    @app.get('/api/v1/cases/{case_id}/outbox', response_model=OutboxPage)
    def list_outbox(case_id: str, actor: Actor,
                    cursor: Annotated[str | None, Query(max_length=128)] = None,
                    limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return runtime_store.outbox_records(actor, case_id, cursor, limit)

    @app.post('/api/v1/cases/{case_id}/outbox/{outbox_id}/deliver', response_model=DeliveryResult)
    def deliver_outbox(case_id: str, outbox_id: str, actor: Actor, key: Key,
                       body: DeliverOutboxRequest = DeliverOutboxRequest()):
        return communication.deliver_outbox(actor, case_id, outbox_id, body, key)

    @app.get('/api/v1/cases/{case_id}/mailbox', response_model=MailboxPage)
    def list_mailbox(case_id: str, actor: Actor,
                     cursor: Annotated[str | None, Query(max_length=128)] = None,
                     limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return communication.list_mailbox(actor, case_id, cursor, limit)

    @app.post('/api/v1/cases/{case_id}/replies', response_model=IngestReplyResult, status_code=201)
    def ingest_reply(case_id: str, body: IngestReplyRequest, actor: Actor, key: Key):
        return communication.ingest_reply(actor, case_id, body, key)

    @app.get('/api/v1/cases/{case_id}/replies', response_model=ReplyPage)
    def list_replies(case_id: str, actor: Actor,
                     cursor: Annotated[str | None, Query(max_length=128)] = None,
                     limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return communication.list_replies(actor, case_id, cursor, limit)

    @app.post('/api/v1/cases/{case_id}/replies/{reply_id}/assess', response_model=AssessReplyResult)
    def assess_reply(case_id: str, reply_id: str, body: AssessReplyRequest, actor: Actor, key: Key):
        return communication.assess_reply(
            actor, case_id, reply_id, body.expected_state_version, key, provider)

    @app.get('/api/v1/cases/{case_id}/findings', response_model=FindingPage)
    def list_findings(case_id: str, actor: Actor,
                      cursor: Annotated[str | None, Query(max_length=128)] = None,
                      limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return communication.list_findings(actor, case_id, cursor, limit)

    @app.get('/api/v1/cases/{case_id}/commitments', response_model=CommitmentPage)
    def list_commitments(case_id: str, actor: Actor,
                         cursor: Annotated[str | None, Query(max_length=128)] = None,
                         limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return communication.list_commitments(actor, case_id, cursor, limit)

    @app.get('/api/v1/cases/{case_id}/reminders', response_model=ReminderPage)
    def list_reminders(case_id: str, actor: Actor,
                       cursor: Annotated[str | None, Query(max_length=128)] = None,
                       limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return communication.list_reminders(actor, case_id, cursor, limit)

    @app.post('/api/v1/cases/{case_id}/reminders/dispatch-due', response_model=DispatchRemindersResult)
    def dispatch_reminders(case_id: str, actor: Actor, key: Key):
        return communication.dispatch_due_reminders(actor, case_id, key)

    return app


def from_env() -> FastAPI:
    path = os.environ.get('CLOSEREADY_ACCESS_CONFIG')
    database_url = os.environ.get('CLOSEREADY_DATABASE_URL')
    if not path or not database_url:
        raise RuntimeError('Set CLOSEREADY_ACCESS_CONFIG and CLOSEREADY_DATABASE_URL; see docs/backend.md.')
    provider = provider_from_environment() if os.environ.get('CLOSEREADY_LLM_ENABLED') == '1' else None
    return create_app(database_url, load_access_config(path), provider=provider,
                      mail=mail_backend(os.environ.get('CLOSEREADY_MAIL_BACKEND')))
