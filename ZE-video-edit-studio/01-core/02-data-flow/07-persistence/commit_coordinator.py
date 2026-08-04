"""One ordered commit coordinator for HTTP, MCP, CLI, pipeline, undo, and redo."""

from __future__ import annotations

import copy
import sys
import threading
import uuid
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping, Union


_HERE = Path(__file__).resolve().parent
_CORE_DIR = _HERE.parent.parent
_OPERATION_DIR = _CORE_DIR / "03-commands-operations" / "02-operation"
_COMMAND_DIR = _CORE_DIR / "03-commands-operations" / "01-command"
_DOCUMENT_DIR = _CORE_DIR / "01-data-structure" / "01-document"
_ENTITY_DIR = _CORE_DIR / "01-data-structure" / "02-entity"
_PROTOCOL_DIR = _CORE_DIR / "_protocol"
for _directory in (
    _OPERATION_DIR, _COMMAND_DIR, _DOCUMENT_DIR, _ENTITY_DIR, _PROTOCOL_DIR,
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from change_set import affected_entities, to_broadcast_payload  # noqa: E402
from effect_context import (  # noqa: E402
    after_commit_effect_phase,
    producer_effect_phase,
)
from commit_contracts import (  # noqa: E402
    CommitFailure,
    CommitReceipt,
    CommitStage,
    CommitStatus,
    DurabilityFacts,
    DurabilityState,
    EventStatus,
    SavePolicy,
)
from operation import Operation, PendingConfirmation  # noqa: E402
from command import CommandMutation, DeferredAction  # noqa: E402
from mutation_operation_audit import (  # noqa: E402
    validate_mutation_operation_coverage,
)
from entity import ZeError  # noqa: E402
from mutation_policy_coverage import (  # noqa: E402
    MUTATION_POLICY_COVERAGE_CAPABILITY as _MUTATION_POLICY_COVERAGE_CAPABILITY,
    MUTATION_POLICY_COVERAGE_VALIDATED as _MUTATION_POLICY_COVERAGE_VALIDATED,
)
from history_authority import (  # noqa: E402
    _HISTORY_COORDINATOR_CAPABILITY,
    _history_coordinator_port,
    HistoryAuthorityError,
)
from pipeline_authority_commit import (  # noqa: E402
    discard_pipeline_authority_commit,
    invalidate_pipeline_authority_attestations,
    PipelineAuthorityCommitError,
    prepare_pipeline_authority_commit,
    reconcile_durable_pipeline_authority,
)
from ports import (  # noqa: E402
    ActionReceipt,
    ActionStatus,
    CommitExecution,
    CommitIntent,
    CommitStore,
    EventDeliveryError,
    EventFactory,
    EventPublisher,
    MutationBatch,
    MutationRollbackError,
    MutationSession,
    NestedCommitError,
)


Producer = Callable[
    [],
    Union[Operation, MutationBatch, CommandMutation, PendingConfirmation, None],
]


class CanonicalEventFactory:
    """Build the current ``entity_change`` payload plus commit identity.

    Surfaces consume ``CommitExecution.events`` directly.  They must not derive
    changes again after the coordinator releases its lock because the document
    may already have moved to a newer state by then.
    """

    def build(
        self,
        operation: Operation,
        document: Any,
        receipt: CommitReceipt,
    ) -> Mapping[str, Any]:
        payload = to_broadcast_payload(
            operation,
            affected_entities(operation, document),
            doc_id=receipt.document_id,
        )
        payload["commit_id"] = receipt.commit_id
        payload["document_version"] = receipt.document_version
        payload["trace_id"] = receipt.trace_id
        return payload


class BrokerEventPublisher:
    """Adapter for the existing RealtimeBroker.publish_change API."""

    def __init__(self, broker: Any) -> None:
        self.broker = broker

    def publish(self, payload: Mapping[str, Any]) -> EventStatus:
        self.broker.publish_change(dict(payload))
        return EventStatus.PUBLISHED


class CommitCoordinator:
    """Serialize mutation production and persistence, then publish afterwards.

    The producer, ``CommitStore.commit``, event derivation, and publisher
    acceptance all run under one shared in-process lock.  Keeping that sequence
    locked prevents commit B from publishing before an earlier durable commit
    A.  The lock is shared with every project write surface.  Re-entry while a
    producer is active is rejected: one user action must return one explicit
    ``CommandMutation`` instead of secretly opening a second commit.  W08's
    durable outbox tailer will later decouple delivery latency from this lock.
    """

    def __init__(
        self,
        document: Any,
        store: CommitStore,
        *,
        publisher: EventPublisher | None = None,
        lock: Any | None = None,
        event_factory: EventFactory | None = None,
        lifecycle_guard: Callable[[], Any] | None = None,
    ) -> None:
        self.document = document
        self.store = store
        self.publisher = publisher
        self.lock = lock or threading.RLock()
        self.event_factory = event_factory or CanonicalEventFactory()
        self.lifecycle_guard = lifecycle_guard or nullcontext
        self._local = threading.local()

    def execute(self, producer: Producer, trace_id: str = "") -> CommitExecution:
        if not callable(producer):
            raise TypeError("producer must be callable")

        # The generation lease is outermost: topology cutovers never wait for a
        # local Context lock held by a writer that is itself waiting for the lease.
        with self.lifecycle_guard():
            with self.lock:
                if getattr(self._local, "producer_active", False):
                    raise NestedCommitError()
                self._local.producer_active = True
                self._local.pipeline_authority_permit = None
                execution: CommitExecution | None = None
                try:
                    history_port = _history_coordinator_port(
                        self,
                        _capability=_HISTORY_COORDINATOR_CAPABILITY,
                    )
                    history_context = (
                        history_port.open(
                            coordinator=self, store=self.store,
                            _capability=_HISTORY_COORDINATOR_CAPABILITY,
                        )
                        if history_port is not None
                        else nullcontext(None)
                    )
                    history_transaction = history_context.__enter__()
                    history_context_open = True

                    def finalize_history_context(
                        exc_type=None,
                        exc=None,
                        traceback=None,
                    ) -> None:
                        nonlocal history_context_open
                        if not history_context_open:
                            return
                        # A failed __exit__ may already have partially released
                        # its authority.  Never invoke it twice.
                        history_context_open = False
                        history_context.__exit__(exc_type, exc, traceback)

                    try:
                        execution, actions = self._execute_business(
                            producer,
                            trace_id=trace_id,
                            history_transaction=history_transaction,
                            history_finalizer=finalize_history_context,
                        )
                    except BaseException as business_error:
                        try:
                            finalize_history_context(*sys.exc_info())
                        except BaseException as finalization_error:
                            # A cleanup fault must never hide an already typed
                            # replay-unsafe result produced after storage.
                            if isinstance(
                                business_error,
                                (HistoryAuthorityError, PipelineAuthorityCommitError),
                            ) and business_error.outcome_unknown:
                                raise business_error from finalization_error
                            raise
                        raise
                    else:
                        try:
                            finalize_history_context()
                        except BaseException as finalization_error:
                            receipt = execution.receipt
                            if (
                                receipt is None
                                or _receipt_proves_storage_untouched(receipt)
                            ):
                                raise
                            authority_error = _post_store_authority_error(
                                finalization_error,
                                receipt,
                                reason="history_context_finalize_failed",
                                message=(
                                    "canonical history context failed after storage"
                                ),
                            )
                            raise authority_error from finalization_error
                finally:
                    # Post-commit actions are deliberately outside the producer phase.
                    # They may use another coordinator transaction, but can never be
                    # mistaken for part of the already-finished logical commit.
                    active_error = sys.exc_info()[1]
                    pending_permit = getattr(
                        self._local,
                        "pipeline_authority_permit",
                        None,
                    )
                    try:
                        if pending_permit is not None:
                            discard_pipeline_authority_commit(pending_permit)
                    except BaseException as cleanup_error:
                        # Permit cleanup must never replace the typed commit or
                        # authority failure already leaving this transaction.
                        if active_error is None:
                            receipt = execution.receipt if execution is not None else None
                            if (
                                receipt is not None
                                and not _receipt_proves_storage_untouched(receipt)
                            ):
                                authority_error = _post_store_authority_error(
                                    cleanup_error,
                                    receipt,
                                    reason="pipeline_authority_cleanup_failed",
                                    message=(
                                        "Pipeline authority cleanup failed after storage"
                                    ),
                                )
                                raise authority_error from cleanup_error
                            raise
                        add_note = getattr(active_error, "add_note", None)
                        if callable(add_note):
                            add_note(
                                "Pipeline authority permit cleanup also failed: "
                                f"{type(cleanup_error).__name__}"
                            )
                    finally:
                        self._local.pipeline_authority_permit = None
                        self._local.producer_active = False
                return self._run_actions(execution, actions)

    def _execute_business(
        self,
        producer: Producer,
        *,
        trace_id: str,
        history_transaction: Any | None = None,
        history_finalizer: Callable[[], None] | None = None,
    ) -> tuple[CommitExecution, tuple[DeferredAction, ...]]:
        # Validate stable document-level inputs before allowing the producer
        # to mutate in memory.
        document_id = _document_id(self.document)
        save_policy = _save_policy(self.document)
        session = _begin_mutation_session(self.document)
        pipeline_authority_permit = None
        try:
            _activate_policy_coverage(session)
            with producer_effect_phase():
                produced = producer()

            # Neither branch is allowed to leak an accidental in-memory edit.
            # A real Document session restores its snapshot; old/fake documents
            # without the optional port retain the W03 compatibility behavior.
            if produced is None:
                _rollback_session(session)
                return CommitExecution(result=None), ()
            if isinstance(produced, PendingConfirmation):
                _rollback_session(session)
                return CommitExecution(result=produced), ()
            actions: tuple[DeferredAction, ...] = ()
            if isinstance(produced, Operation):
                operations = (produced,)
            elif isinstance(produced, MutationBatch):
                operations = produced.operations
            elif isinstance(produced, CommandMutation):
                operations = produced.operations
                actions = produced.after_commit
            else:
                raise TypeError(
                    "producer must return None, PendingConfirmation, Operation, "
                    "MutationBatch, or CommandMutation"
                )

            # Every protected mutation must now be accounted for by one or
            # more exact root-command/history segments.  Keep this and every
            # other fallible pre-store step inside the rollback boundary: a
            # malformed operation/identity must not leak a live session or a
            # half-mutated graph.
            _validate_policy_coverage(self.document, session, operations)
            pipeline_authority_permit = prepare_pipeline_authority_commit(
                self.store,
                operations,
                document=self.document,
            )
            self._local.pipeline_authority_permit = pipeline_authority_permit
            normalized_result: Operation | MutationBatch = (
                operations[0] if len(operations) == 1 else MutationBatch(tuple(operations))
            )

            logical_trace_id = trace_id or operations[0].trace_id or uuid.uuid4().hex
            intent = CommitIntent(
                commit_id=uuid.uuid4().hex,
                document=self.document,
                operations=operations,
                trace_id=logical_trace_id,
                save_policy=save_policy,
            )
            # The id was read before mutation to reject malformed document
            # objects early; CommitIntent independently validates it again.
            if intent.document_id != document_id:
                raise ZeError(
                    "commit_document_identity_changed",
                    "producer changed Document identity before persistence",
                    context={
                        "before_document_id": document_id,
                        "after_document_id": intent.document_id,
                    },
                )
            if history_transaction is not None:
                history_transaction.prepare_store(operations)
        except MutationRollbackError:
            raise
        except BaseException as producer_error:
            # Preserve the producer/type exception exactly when restoration
            # succeeds.  If restoration fails, surface one stable ZeError and
            # retain the producer exception as its explicit cause.
            _rollback_session(session, original_error=producer_error)
            raise

        if history_transaction is None:
            receipt = self._commit_safely(intent)
            try:
                _invalidate_pipeline_authority_after_storage(
                    pipeline_authority_permit,
                    receipt,
                )
            except PipelineAuthorityCommitError as authority_error:
                try:
                    _commit_session(session)
                except BaseException as finalization_error:
                    raise authority_error from finalization_error
                raise
        else:
            receipt = None
            try:
                with history_transaction.write_scope():
                    receipt = self._commit_safely(intent)
            except BaseException as write_scope_error:
                if receipt is None or _receipt_proves_storage_untouched(receipt):
                    _rollback_session(
                        session,
                        original_error=write_scope_error,
                        receipt=receipt,
                    )
                    raise
                # The store returned a receipt that cannot prove both resources
                # untouched before the history write scope failed.  Preserve
                # the current in-memory graph and convert every exception type
                # (including process-control interruptions) into one typed,
                # replay-unsafe authority result.
                try:
                    _invalidate_pipeline_authority_after_storage(
                        pipeline_authority_permit,
                        receipt,
                    )
                except PipelineAuthorityCommitError as invalidation_error:
                    authority_error = invalidation_error
                else:
                    authority_error = _post_store_authority_error(
                        write_scope_error,
                        receipt,
                        reason="history_write_scope_finalize_failed",
                        message="canonical history write scope failed after storage",
                    )
                try:
                    _commit_session(session)
                except BaseException as finalization_error:
                    raise authority_error from finalization_error
                raise authority_error from write_scope_error
            try:
                _invalidate_pipeline_authority_after_storage(
                    pipeline_authority_permit,
                    receipt,
                )
            except PipelineAuthorityCommitError as authority_error:
                try:
                    _commit_session(session)
                except BaseException as finalization_error:
                    raise authority_error from finalization_error
                raise
            if _is_publishable(receipt):
                # Durable OpLog truth advances before event derivation.  An
                # event failure cannot make an already committed history
                # source ticket replayable again.
                try:
                    history_transaction.confirm_durable()
                except HistoryAuthorityError as history_error:
                    authority_error = _post_store_authority_error(
                        history_error,
                        receipt,
                        reason="history_durable_verification_failed",
                        message=(
                            "canonical history durable verification failed after storage"
                        ),
                    )
                    # The store has already proved both durable resources.
                    # Close the in-memory mutation guard without restoring an
                    # older snapshot before surfacing the repair-required
                    # history result.  A finalizer fault must not replace the
                    # typed UNKNOWN classification with an arbitrary error.
                    try:
                        _commit_session(session)
                    except BaseException as finalization_error:
                        raise authority_error from finalization_error
                    if authority_error is history_error:
                        raise
                    raise authority_error from history_error
                except BaseException as verification_error:
                    # The store has already proved both durable resources.
                    # An unexpected verifier interruption is therefore just as
                    # replay-unsafe as a typed suffix mismatch; never let an
                    # idempotency ledger mistake it for a clean pre-store error.
                    authority_error = HistoryAuthorityError(
                        "history_durable_verification_interrupted",
                        "canonical history durable verification was interrupted",
                        outcome_unknown=True,
                        durability_phase="post_write_verification",
                    ).with_commit_facts(_history_commit_facts(receipt))
                    try:
                        _commit_session(session)
                    except BaseException as finalization_error:
                        raise authority_error from finalization_error
                    raise authority_error from verification_error
        if session is not None and _receipt_proves_storage_untouched(receipt):
            _rollback_session(session, receipt=receipt)
            receipt = _receipt_after_rollback(receipt)
            if history_transaction is not None:
                history_transaction.discard_if_unchanged()
        else:
            # UNKNOWN/DURABLE may already exist on disk.  Restoring the memory
            # snapshot would make this process older than canonical storage, so
            # accept the current graph and leave reconciliation to repair flow.
            try:
                _commit_session(session)
            except BaseException as finalization_error:
                # We reach this branch only when storage may already have
                # changed.  A local session-finalizer fault therefore cannot be
                # propagated as an ordinary retryable exception: the same
                # command (and the same idempotency key) must remain fenced from
                # replay even though Pipeline reconcile has not run yet.
                authority_error = HistoryAuthorityError(
                    "history_session_finalize_failed",
                    "canonical history was stored but the local mutation session "
                    "could not be finalized",
                    outcome_unknown=True,
                    durability_phase="post_write_verification",
                ).with_commit_facts(_history_commit_facts(receipt))
                raise authority_error from finalization_error

        if (
            history_transaction is not None
            and not _is_publishable(receipt)
            and history_transaction.state not in {"DISCARDED", "DURABLE"}
        ):
            try:
                history_transaction.freeze(
                    "history_commit_outcome_indeterminate",
                    "canonical history cannot prove the complete commit suffix",
                )
            except BaseException as freeze_error:
                if _receipt_proves_storage_untouched(receipt):
                    raise
                authority_error = _post_store_authority_error(
                    freeze_error,
                    receipt,
                    reason="history_commit_freeze_failed",
                    message=(
                        "canonical history repair fence failed after storage"
                    ),
                )
                raise authority_error from freeze_error

        if not _is_publishable(receipt):
            return (
                CommitExecution(
                    result=normalized_result,
                    receipt=receipt,
                    actions=_skipped_actions(actions),
                ),
                (),
            )

        if pipeline_authority_permit is not None:
            if history_finalizer is not None:
                try:
                    # The transaction context itself can still reject/freeze
                    # while releasing its private runtime authority.  Finish
                    # that boundary before a durable attestation can be minted.
                    history_finalizer()
                except HistoryAuthorityError as history_error:
                    authority_error = _post_store_authority_error(
                        history_error,
                        receipt,
                        reason="pipeline_authority_history_finalize_failed",
                        message=(
                            "history authority finalization failed before Pipeline reconcile"
                        ),
                    )
                    if authority_error is history_error:
                        raise
                    raise authority_error from history_error
                except BaseException as finalization_error:
                    authority_error = PipelineAuthorityCommitError(
                        "pipeline_authority_unavailable",
                        "pipeline_authority_history_finalize_failed",
                        "history authority finalization failed before Pipeline reconcile",
                        outcome_unknown=True,
                        durability_phase="post_write_verification",
                    )
                    authority_error.with_commit_facts(
                        _history_commit_facts(receipt)
                    )
                    raise authority_error from finalization_error
            try:
                # This must stay ahead of event derivation/publication.  The
                # business receipt already proves both canonical resources
                # durable, so an event failure cannot suppress fresh authority
                # reconciliation or make the business command replayable.
                # The authority module retains the sealed attestations in its
                # private per-store/run registry.  They intentionally never
                # enter CommitExecution, CommitReceipt, events, or SDK output.
                reconcile_durable_pipeline_authority(
                    self.store,
                    pipeline_authority_permit,
                )
            except PipelineAuthorityCommitError as authority_error:
                # A failed fresh scan never leaves a reusable pre-store permit
                # behind and never mints an attestation.  The typed authority
                # error propagates with the durable business commit untouched.
                authority_error.with_commit_facts(
                    _history_commit_facts(receipt)
                )
                raise
            except BaseException as unexpected_error:
                authority_error = PipelineAuthorityCommitError(
                    "pipeline_authority_unavailable",
                    "pipeline_authority_reconcile_unexpected",
                    "fresh Pipeline authority reconciliation failed unexpectedly",
                    outcome_unknown=True,
                    durability_phase="post_write_verification",
                )
                authority_error.with_commit_facts(
                    _history_commit_facts(receipt)
                )
                raise authority_error from unexpected_error
            else:
                self._local.pipeline_authority_permit = None
                pipeline_authority_permit = None

        try:
            events = tuple(
                copy.deepcopy(
                    dict(self.event_factory.build(operation, self.document, receipt))
                )
                for operation in operations
            )
        except Exception as exc:
            # Event derivation is part of the event phase.  Business data is
            # already committed and must never be replayed because a payload
            # helper failed.
            failed = _event_failure_receipt(
                receipt,
                exc,
                status=EventStatus.FAILED,
                fallback_code="event_build_failed",
            )
            return (
                CommitExecution(
                    result=normalized_result,
                    receipt=failed,
                    actions=_skipped_actions(actions),
                ),
                (),
            )

        if self.publisher is None:
            return (
                CommitExecution(result=normalized_result, receipt=receipt, events=events),
                actions,
            )

        # Acceptance remains inside the shared lock so durable version and
        # event order cannot invert under concurrent execute() calls.
        published_receipt = self._publish(receipt, events)
        if published_receipt.event_status is EventStatus.FAILED:
            return (
                CommitExecution(
                    result=normalized_result,
                    receipt=published_receipt,
                    events=events,
                    actions=_skipped_actions(actions),
                ),
                (),
            )
        return (
            CommitExecution(
                result=normalized_result,
                receipt=published_receipt,
                events=events,
            ),
            actions,
        )

    @staticmethod
    def _run_actions(
        execution: CommitExecution,
        actions: tuple[DeferredAction, ...],
    ) -> CommitExecution:
        if not actions:
            return execution
        if execution.receipt is None or not execution.is_committed:
            raise ZeError(
                "after_commit_authority_invalid",
                "after-commit actions require one committed receipt",
            )
        receipts: list[ActionReceipt] = []
        for index, action in enumerate(actions):
            try:
                with after_commit_effect_phase():
                    action.run(execution.receipt.commit_id)
            except Exception as exc:
                code = getattr(exc, "code", "")
                if not isinstance(code, str) or not code:
                    code = "after_commit_action_failed"
                receipts.append(ActionReceipt(
                    action_id=action.id,
                    name=action.name,
                    status=ActionStatus.FAILED,
                    code=code,
                    message=str(exc).strip() or "after-commit action failed",
                ))
                # Actions are ordered.  Running later actions after a failed
                # prerequisite would create another partial effect chain; the
                # repair runner may resume from the first skipped action.
                receipts.extend(_skipped_actions(actions[index + 1:]))
                break
            else:
                receipts.append(ActionReceipt(
                    action_id=action.id,
                    name=action.name,
                    status=ActionStatus.SUCCEEDED,
                ))
        return CommitExecution(
            result=execution.result,
            receipt=execution.receipt,
            events=execution.events,
            actions=tuple(receipts),
        )

    def _commit_safely(self, intent: CommitIntent) -> CommitReceipt:
        try:
            receipt = self.store.commit(intent)
        except BaseException as exc:
            # Once a store call begins, even process-control interruptions
            # cannot prove that an atomic rename or fsync did not happen.  Turn
            # the interruption into the same replay-unsafe UNKNOWN receipt as
            # any other opaque transport/storage failure; only its bounded
            # message is retained, never a misleading clean rollback signal.
            return _unknown_store_receipt(intent, exc)
        if not isinstance(receipt, CommitReceipt) or not _receipt_matches(receipt, intent):
            return _unknown_store_receipt(
                intent,
                RuntimeError("CommitStore returned a receipt for a different intent"),
            )
        return receipt

    def _publish(
        self,
        receipt: CommitReceipt,
        events: tuple[Mapping[str, Any], ...],
    ) -> CommitReceipt:
        if self.publisher is None:
            raise ZeError(
                "event_publisher_missing",
                "event publication requires a configured publisher",
            )
        any_pending = False
        for payload in events:
            try:
                # A publisher cannot mutate the canonical tuple returned to
                # surfaces; payloads are JSON-like by protocol and therefore
                # safe to deep-copy at this boundary.
                status = self.publisher.publish(copy.deepcopy(dict(payload)))
            except EventDeliveryError as exc:
                if exc.status is EventStatus.PENDING:
                    # PENDING means this event was durably accepted for later
                    # delivery.  Continue so every operation in the batch is
                    # accepted before the whole commit is labelled pending.
                    any_pending = True
                    continue
                return _event_failure_receipt(
                    receipt,
                    exc,
                    status=EventStatus.FAILED,
                    fallback_code=exc.code,
                )
            except Exception as exc:
                return _event_failure_receipt(
                    receipt,
                    exc,
                    status=EventStatus.FAILED,
                    fallback_code="event_publish_failed",
                )

            if status is None or status is EventStatus.PUBLISHED:
                continue
            if status is EventStatus.PENDING:
                any_pending = True
                continue
            if status is EventStatus.FAILED:
                return _event_failure_receipt(
                    receipt,
                    RuntimeError("publisher reported failed delivery"),
                    status=EventStatus.FAILED,
                    fallback_code="event_publish_failed",
                )
            return _event_failure_receipt(
                receipt,
                RuntimeError(f"publisher returned invalid status {status!r}"),
                status=EventStatus.FAILED,
                fallback_code="event_status_invalid",
            )
        return _event_status_receipt(
            receipt,
            EventStatus.PENDING if any_pending else EventStatus.PUBLISHED,
        )


def _document_id(document: Any) -> str:
    value = getattr(document, "id", "")
    if not isinstance(value, str) or not value:
        raise ValueError("document.id must be a non-empty string")
    return value


def _begin_mutation_session(document: Any) -> MutationSession | None:
    """Open the optional Document snapshot without breaking W03 fake objects."""
    begin = getattr(document, "begin_mutation_session", None)
    if begin is None:
        return None
    if not callable(begin):
        raise TypeError("document.begin_mutation_session must be callable")
    session = begin()
    if not isinstance(session, MutationSession):
        raise TypeError("begin_mutation_session must return a MutationSession")
    return session


def _activate_policy_coverage(session: MutationSession | None) -> None:
    if session is None:
        return
    activate = getattr(session, "_activate_policy_coverage", None)
    if activate is not None:
        if not callable(activate):
            raise TypeError("mutation policy coverage activator must be callable")
        activate(_capability=_MUTATION_POLICY_COVERAGE_CAPABILITY)


def _validate_policy_coverage(
    document: Any,
    session: MutationSession | None,
    operations: tuple[Operation, ...],
) -> None:
    if session is None:
        return
    validate = getattr(session, "_validate_policy_coverage", None)
    if validate is not None:
        if not callable(validate):
            raise TypeError("mutation policy coverage validator must be callable")
        protected_coverage = validate(
            operations=operations,
            _capability=_MUTATION_POLICY_COVERAGE_CAPABILITY,
        )
        if protected_coverage is _MUTATION_POLICY_COVERAGE_VALIDATED:
            return
        if protected_coverage is True:
            # Compatibility for an older/custom MutationSession: it can ask
            # the coordinator to perform the complete fallback audit, but only
            # the framework-owned exact seal may skip that duplicate work.
            snapshot = getattr(session, "snapshot", None)
            validate_mutation_operation_coverage(
                document,
                snapshot,
                None,
                operations,
                reject_duplicate_transitions=False,
            )


def _rollback_session(
    session: MutationSession | None,
    *,
    original_error: BaseException | None = None,
    receipt: CommitReceipt | None = None,
) -> None:
    if session is None:
        return
    try:
        session.rollback()
    except BaseException as rollback_error:
        error = MutationRollbackError(
            rollback_error,
            original_error=original_error,
            receipt=receipt,
        )
        if original_error is not None:
            # The triggering producer/type error is the primary causal fact;
            # rollback_error remains available as a structured attribute.
            raise error from original_error
        raise error from rollback_error


def _commit_session(session: MutationSession | None) -> None:
    if session is not None:
        session.commit()


def _receipt_proves_storage_untouched(receipt: CommitReceipt) -> bool:
    """Whether restoring the in-memory snapshot cannot contradict disk state."""
    failure = receipt.failure
    if failure is None:
        return False
    return (
        failure.facts.document is DurabilityState.NOT_DURABLE
        and failure.facts.oplog is DurabilityState.NOT_DURABLE
    )


def _history_commit_facts(receipt: CommitReceipt) -> dict[str, Any]:
    """Project the receipt facts preserved by typed history failures."""
    facts: dict[str, Any] = {
        "phase": receipt.stage.value,
        "outcome": receipt.status.value,
        "commit_id": receipt.commit_id,
        "retry_safe": receipt.retry_safe,
        "document_durable": receipt.document_durable,
        "oplog_durable": receipt.oplog_durable,
    }
    if receipt.failure is not None:
        facts["failure_facts"] = receipt.failure.facts.to_dict()
    return facts


def _post_store_authority_error(
    error: BaseException,
    receipt: CommitReceipt,
    *,
    reason: str,
    message: str,
) -> HistoryAuthorityError | PipelineAuthorityCommitError:
    """Never let a post-store exception regain ordinary replay semantics."""
    facts = _history_commit_facts(receipt)
    if isinstance(error, (HistoryAuthorityError, PipelineAuthorityCommitError)):
        if error.outcome_unknown:
            return error.with_commit_facts(facts)
    return HistoryAuthorityError(
        reason,
        message,
        outcome_unknown=True,
        durability_phase="post_write_verification",
    ).with_commit_facts(facts)


def _invalidate_pipeline_authority_after_storage(
    permit: Any | None,
    receipt: CommitReceipt,
) -> None:
    """Revoke prior run proof once a receipt cannot prove storage untouched."""
    if permit is None or _receipt_proves_storage_untouched(receipt):
        return
    facts = _history_commit_facts(receipt)
    try:
        invalidate_pipeline_authority_attestations(permit)
    except PipelineAuthorityCommitError as error:
        if error.outcome_unknown:
            raise error.with_commit_facts(facts)
        wrapped = PipelineAuthorityCommitError(
            "pipeline_authority_unavailable",
            "pipeline_authority_invalidation_failed",
            "Pipeline authority proof could not be revoked after storage",
            outcome_unknown=True,
            durability_phase="post_write_verification",
        ).with_commit_facts(facts)
        raise wrapped from error
    except BaseException as error:
        wrapped = PipelineAuthorityCommitError(
            "pipeline_authority_unavailable",
            "pipeline_authority_invalidation_failed",
            "Pipeline authority proof could not be revoked after storage",
            outcome_unknown=True,
            durability_phase="post_write_verification",
        ).with_commit_facts(facts)
        raise wrapped from error


def _receipt_after_rollback(receipt: CommitReceipt) -> CommitReceipt:
    """Reclassify a proven non-durable mutation after snapshot restoration."""
    failure = receipt.failure
    if failure is None or not failure.facts.mutation_applied:
        # A validate-time abort already says no mutation was applied.  The
        # session was restored defensively, but no durability fact is changed.
        return receipt
    rolled_back = CommitFailure(
        stage=failure.stage,
        code=failure.code,
        message=failure.message,
        facts=DurabilityFacts(
            mutation_applied=True,
            rollback_succeeded=True,
            document=DurabilityState.NOT_DURABLE,
            oplog=DurabilityState.NOT_DURABLE,
        ),
    )
    return CommitReceipt.from_failure(
        commit_id=receipt.commit_id,
        document_id=receipt.document_id,
        operation_ids=receipt.operation_ids,
        trace_id=receipt.trace_id,
        save_policy=receipt.save_policy,
        failure=rolled_back,
    )


def _skipped_actions(actions: tuple[DeferredAction, ...]) -> tuple[ActionReceipt, ...]:
    return tuple(
        ActionReceipt(
            action_id=action.id,
            name=action.name,
            status=ActionStatus.SKIPPED,
        )
        for action in actions
    )


def _save_policy(document: Any) -> SavePolicy:
    raw = getattr(document, "effective_save_policy", None)
    if raw is None:
        raw = getattr(document, "save_policy", None) or SavePolicy.AUTO_COMMIT.value
    if isinstance(raw, SavePolicy):
        return raw
    try:
        return SavePolicy(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported document save policy {raw!r}") from exc


def _receipt_matches(receipt: CommitReceipt, intent: CommitIntent) -> bool:
    return (
        receipt.commit_id == intent.commit_id
        and receipt.document_id == intent.document_id
        and receipt.operation_ids == intent.operation_ids
        and receipt.trace_id == intent.trace_id
        and receipt.save_policy is intent.save_policy
        and receipt.event_status is EventStatus.NOT_REQUIRED
    )


def _is_publishable(receipt: CommitReceipt) -> bool:
    return (
        receipt.status is CommitStatus.COMMITTED
        and receipt.document_durable
        and receipt.oplog_durable
        and receipt.event_status is EventStatus.NOT_REQUIRED
    )


def _unknown_store_receipt(intent: CommitIntent, exc: BaseException) -> CommitReceipt:
    failure = CommitFailure(
        stage=CommitStage.OPLOG,
        code="commit_store_outcome_unknown",
        message=str(exc).strip() or "commit store outcome is unknown",
        facts=DurabilityFacts(
            mutation_applied=True,
            document=DurabilityState.UNKNOWN,
            oplog=DurabilityState.UNKNOWN,
        ),
    )
    return CommitReceipt.from_failure(
        commit_id=intent.commit_id,
        document_id=intent.document_id,
        operation_ids=intent.operation_ids,
        trace_id=intent.trace_id,
        save_policy=intent.save_policy,
        failure=failure,
    )


def _event_status_receipt(receipt: CommitReceipt, status: EventStatus) -> CommitReceipt:
    return CommitReceipt.committed(
        commit_id=receipt.commit_id,
        document_id=receipt.document_id,
        operation_ids=receipt.operation_ids,
        trace_id=receipt.trace_id,
        save_policy=receipt.save_policy,
        document_version=receipt.document_version,
        event_status=status,
    )


def _event_failure_receipt(
    receipt: CommitReceipt,
    exc: Exception,
    *,
    status: EventStatus,
    fallback_code: str,
) -> CommitReceipt:
    code = getattr(exc, "code", "")
    if not isinstance(code, str) or not code:
        code = fallback_code
    failure = CommitFailure(
        stage=CommitStage.EVENT,
        code=code,
        message=str(exc).strip() or "event delivery failed",
        facts=DurabilityFacts(
            mutation_applied=True,
            document=DurabilityState.DURABLE,
            oplog=DurabilityState.DURABLE,
        ),
        event_status=status,
    )
    return CommitReceipt.from_failure(
        commit_id=receipt.commit_id,
        document_id=receipt.document_id,
        operation_ids=receipt.operation_ids,
        trace_id=receipt.trace_id,
        save_policy=receipt.save_policy,
        failure=failure,
        document_version=receipt.document_version,
    )


__all__ = [
    "BrokerEventPublisher",
    "CanonicalEventFactory",
    "CommitCoordinator",
    "Producer",
]
