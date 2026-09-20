"""Durable single-iteration worker for queued agent runs."""
import argparse
import logging
import os
import time

from .config import load_access_config
from .communication_store import CommunicationStore
from .document_processor import DocumentProcessor
from .document_store import DocumentStore
from .mail import mail_backend
from .provider_factory import provider_from_environment
from .runtime import AgentRuntime
from .runtime_store import RuntimeStore
from .store import DomainError, Store


logger = logging.getLogger(__name__)


class AgentWorker:
    def __init__(self, runtime_store, provider, access,
                 document_store=None, document_processor=None, communication=None):
        self.runtime_store = runtime_store
        self.provider = provider
        self.access = access
        self.document_store = document_store
        self.document_processor = document_processor
        self.communication = communication

    def _actor(self, actor_id, client_id):
        return next((principal for principal in self.access.principals
            if principal.user_id == actor_id and principal.can_manage
            and client_id in principal.client_ids), None)

    def run_once(self):
        for run_id, _actor_id in self.runtime_store.expired_run_candidates():
            self.runtime_store.recover_expired_system(run_id)
        if self.document_store is not None:
            for job_id in self.document_store.expired_job_candidates():
                self.document_store.recover_expired_system(job_id)

        for run_id, actor_id, client_id in self.runtime_store.queued_candidates():
            actor = self._actor(actor_id, client_id)
            if actor is None:
                logger.error('ACTOR_CONFIGURATION_MISSING: queued run was not executed.')
                continue
            token = self.runtime_store.claim(actor, run_id)
            if token is None:
                continue
            return AgentRuntime(self.runtime_store, self.provider).execute_claimed(
                actor, run_id, token)

        if self.document_store is not None and self.document_processor is not None:
            for job_id in self.document_store.queued_candidates():
                token = self.document_store.claim(job_id)
                if token is None:
                    continue
                return self.document_processor.execute_claimed(job_id, token)
        if self.communication is not None and self.communication.mail is not None:
            try:
                inbound = self.communication.poll_inbound()
                reminders = self.communication.dispatch_due_all()
            except DomainError as exc:
                logger.error('MAIL_WORKER_BLOCKED: %s', exc.code)
                return None
            except Exception:
                logger.error('MAIL_WORKER_FAILED')
                return None
            if inbound.items or reminders.items:
                return inbound if inbound.items else reminders
        return None


def worker_from_environment():
    path = os.environ.get('CLOSEREADY_ACCESS_CONFIG')
    database_url = os.environ.get('CLOSEREADY_DATABASE_URL')
    if not path or not database_url:
        raise RuntimeError(
            'Set CLOSEREADY_ACCESS_CONFIG and CLOSEREADY_DATABASE_URL; see docs/backend.md.')
    if os.environ.get('CLOSEREADY_LLM_ENABLED') != '1':
        raise RuntimeError('Set CLOSEREADY_LLM_ENABLED=1 for the agent worker.')
    access = load_access_config(path)
    store = Store(database_url, access)
    runtime_store = RuntimeStore(store)
    mail = mail_backend(os.environ.get('CLOSEREADY_MAIL_BACKEND'))
    communication = CommunicationStore(store, runtime_store, mail)
    document_store = DocumentStore(
        store, on_requirements_resolved=communication.cancel_scheduled_for_resolved)
    communication.document_store = document_store
    provider = provider_from_environment()
    from .document_ai_review import AIDocumentReviewer
    mode = os.environ.get('CLOSEREADY_DOCUMENT_REVIEW_MODE', 'llm')
    if mode not in ('llm', 'rules'):
        raise RuntimeError('Document review mode must be llm or rules.')
    processor = (DocumentProcessor(document_store, assessor=AIDocumentReviewer(provider))
                 if mode == 'llm' else DocumentProcessor(document_store))
    return AgentWorker(
        runtime_store,
        provider,
        access,
        document_store=document_store,
        document_processor=processor,
        communication=communication,
    )


def polling_seconds(value):
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError('Polling interval must be a number.') from None
    if not 0.1 <= seconds <= 60:
        raise argparse.ArgumentTypeError('Polling interval must be between 0.1 and 60 seconds.')
    return seconds


def main(argv=None):
    parser = argparse.ArgumentParser(description='Process durable CloseReady agent runs.')
    parser.add_argument('--once', action='store_true',
        help='Process recovery and at most one queued run, then exit.')
    parser.add_argument('--poll-seconds', type=polling_seconds, default=2.0)
    args = parser.parse_args(argv)
    worker = worker_from_environment()
    try:
        while True:
            result = worker.run_once()
            if args.once:
                return 0
            if result is None:
                time.sleep(args.poll_seconds)
    finally:
        worker.runtime_store.store.engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
