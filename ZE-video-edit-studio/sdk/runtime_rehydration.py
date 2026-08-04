"""Single synchronous runtime-load coordinator for detached business heads.

Persisted business snapshots deliberately exclude executable declarations and
runtime-only overlays.  This facade owns the one in-memory transition from a
verified, unpublished business head to a manifest-bound RuntimeAdmission.

Carrier identity and the issuance ledger live in the private state module;
declaration/projection logic lives in the private projection module.  The
dependency direction is strictly state <- projection <- this coordinator.
Public classes below are direct re-exports of the state-layer classes, so their
exact identity is preserved.

The transition is deliberately synchronous.  It keeps an exclusive Document
mutation session from baseline verification through declaration installation,
canonical migration, authority CAS (when required), overlay materialization,
and ready-head issuance.  No candidate or mutable Document escapes in between.

These modules import no storage, topology, cache, log, event, or supervisor
implementation.  A future authority adapter is injected as a typed callback;
the production bootstrap must call this coordinator while holding the project
generation/load lease and must not publish the Document anywhere else.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable

from . import _paths  # noqa: F401  (install flat core imports first)
from ._runtime_rehydration_projection import (
    _authority_fingerprint_matches,
    _business_root,
    _capture_business_projection,
    _capture_runtime_inventory,
    _declaration_authority_fingerprint,
    _install_declaration_authority,
    _issue_unpublished_document_for_test,
    _purge_runtime_overlays,
    _runtime_inventory_matches,
    _runtime_inventory_preserved_after_source,
    _runtime_sources_match_manifest,
    _validate_changed_canonical_rows,
    detach_restored_business_head,
    snapshot_registry_declarations,
)
from ._runtime_rehydration_state import (
    AUTHORITY_FENCE_PROTOCOL,
    AUTHORITY_WRITE_PROTOCOL,
    DetachedBusinessHead,
    ReadOnlyDeclarationSnapshot,
    RegistryDeclarationSnapshot,
    RuntimeAdmission,
    RuntimeAuthorityFenceReceipt,
    RuntimeAuthorityFenceRequest,
    RuntimeAuthorityReceipt,
    RuntimeAuthorityWrite,
    RuntimeReadyHead,
    _AUTHORITY_OUTCOMES,
    _CandidateRecord,
    _FENCE_OUTCOMES,
    _LEDGER,
    _MAX_SQLITE_INTEGER,
    _RuntimeInventory,
    _RuntimeRehydrationCandidate,
    _fail,
)

from document import DocumentMutationSession, ReadOnlySourceSpec  # noqa: E402
from entity import ZeError  # noqa: E402

# Keep the facade-qualified identity visible to repr/introspection and preserve
# the pre-split pickle lookup path.  These are the original objects, not wrapper
# classes/functions; the state ledger therefore still sees one exact identity.
_FACADE_REEXPORTS = (
    DetachedBusinessHead,
    ReadOnlyDeclarationSnapshot,
    RegistryDeclarationSnapshot,
    RuntimeAdmission,
    RuntimeAuthorityFenceReceipt,
    RuntimeAuthorityFenceRequest,
    RuntimeAuthorityReceipt,
    RuntimeAuthorityWrite,
    RuntimeReadyHead,
    detach_restored_business_head,
    snapshot_registry_declarations,
)
for _facade_export in _FACADE_REEXPORTS:
    _facade_export.__module__ = __name__
del _facade_export


def _assert_same_thread(record: _CandidateRecord) -> None:
    if threading.get_ident() != record.owner_thread_id:
        _fail(
            "runtime_rehydration_forbidden",
            "runtime_rehydration_thread_mismatch",
            "runtime load must prepare, write, and promote on one thread",
        )


def _assert_candidate_unchanged(record: _CandidateRecord) -> None:
    _assert_same_thread(record)
    current = _capture_business_projection(
        record.document, record.project_id, record.generation,
    )
    if (
        current != record.after_snapshot
        or not _runtime_inventory_matches(record.document, record.inventory)
    ):
        _fail(
            "runtime_rehydration_candidate_changed",
            "runtime_rehydration_candidate_tampered",
            "runtime rehydration candidate changed before promotion",
        )


def _rollback_pre_durability(
    candidate: _RuntimeRehydrationCandidate,
    original_error: BaseException,
) -> None:
    try:
        _LEDGER.abort(candidate)
    except BaseException as rollback_error:
        raise ZeError(
            "mutation_rollback_failed",
            "runtime load failed and rollback also failed",
            context={
                "reason": "runtime_rehydration_rollback_failed",
                "original_error_type": type(original_error).__name__,
                "original_error_code": getattr(original_error, "code", None),
                "rollback_error_type": type(rollback_error).__name__,
                "rollback_error_code": getattr(rollback_error, "code", None),
            },
        ) from original_error


def _prepare_runtime_candidate(
    detached: DetachedBusinessHead,
    declarations: RegistryDeclarationSnapshot,
    *,
    read_only_loader_resolver: (
        Callable[[ReadOnlyDeclarationSnapshot], Callable[[], Iterable[dict[str, Any]]]]
        | None
    ),
) -> _RuntimeRehydrationCandidate:
    declaration = _LEDGER.declaration(declarations)
    detached_record, lease_token = _LEDGER.consume_and_reserve(detached)
    document = detached_record.document
    session: DocumentMutationSession | None = None
    try:
        session = document.begin_mutation_session(exclusive=True)
        initial_business = _capture_business_projection(
            document, detached_record.project_id, detached_record.generation,
        )
        if initial_business != detached_record.baseline_snapshot:
            _fail(
                "runtime_head_invalid",
                "runtime_detached_head_changed",
                "detached business head changed before runtime load",
            )
        initial_updated_at = document.updated_at
        initial_created_at = document.created_at
        initial_document_id = document.id
        initial_version = document.version

        _purge_runtime_overlays(document)
        document._updated_at = initial_updated_at
        _install_declaration_authority(
            document, declaration, validate_read_only=False,
        )

        for migration in declaration.migrations:
            before_business = _capture_business_projection(
                document, detached_record.project_id, detached_record.generation,
            )
            authority = _declaration_authority_fingerprint(document)
            claimed = migration(document)
            if type(claimed) is not bool:
                _fail(
                    "runtime_migration_invalid",
                    "runtime_migration_result_not_bool",
                    "runtime migration must return an exact bool",
                )
            if not _authority_fingerprint_matches(document, authority):
                _fail(
                    "runtime_migration_invalid",
                    "runtime_migration_authority_mutation",
                    "runtime migration may not replace declaration authority",
                )
            after_business = _capture_business_projection(
                document, detached_record.project_id, detached_record.generation,
            )
            if claimed is not (before_business != after_business):
                _fail(
                    "runtime_migration_invalid",
                    "runtime_migration_claim_mismatch",
                    "runtime migration claim does not match its actual business delta",
                )

        _install_declaration_authority(
            document, declaration, validate_read_only=False,
        )
        proposed_business = _capture_business_projection(
            document, detached_record.project_id, detached_record.generation,
        )
        _validate_changed_canonical_rows(document, initial_business, proposed_business)
        # Migrations replay historical business facts outside ordinary command
        # allowlists, but their final graph must still satisfy the deployment's
        # closed-world cardinality contract before any version or authority CAS
        # is allocated.
        document._validate_mutation_policy_cardinality()
        proposed_business = _capture_business_projection(
            document, detached_record.project_id, detached_record.generation,
        )

        if document.id != initial_document_id or document.created_at != initial_created_at:
            _fail(
                "runtime_migration_invalid",
                "runtime_migration_document_identity_mutation",
                "runtime migration may not replace Document identity or creation time",
            )
        if document.version != initial_version:
            _fail(
                "runtime_migration_invalid",
                "runtime_document_version_authority_mutation",
                "runtime coordinator exclusively owns Document version allocation",
            )

        canonical_changed = proposed_business != initial_business
        if canonical_changed:
            if initial_version >= _MAX_SQLITE_INTEGER:
                _fail(
                    "runtime_rehydration_failed",
                    "runtime_document_version_exhausted",
                    "Document version cannot be advanced",
                )
            document._v = initial_version + 1
            document._updated_at = max(
                float(initial_updated_at), float(document.updated_at), time.time(),
            )
        else:
            document._v = initial_version
            document._updated_at = initial_updated_at

        final_business = _capture_business_projection(
            document, detached_record.project_id, detached_record.generation,
        )
        if canonical_changed is not (final_business != initial_business):
            _fail(
                "runtime_rehydration_failed",
                "runtime_canonical_delta_inconsistent",
                "runtime canonical delta classification is inconsistent",
            )
        if canonical_changed and final_business.document.document_version != initial_version + 1:
            _fail(
                "runtime_rehydration_failed",
                "runtime_document_version_transition_invalid",
                "changed runtime head must advance exactly one Document version",
            )
        if not canonical_changed and final_business.document.document_version != initial_version:
            _fail(
                "runtime_rehydration_failed",
                "runtime_document_version_transition_invalid",
                "no-op runtime head must preserve Document version",
            )
        inventory = _capture_runtime_inventory(document)
        candidate_record = _CandidateRecord(
            project_id=detached_record.project_id,
            generation=detached_record.generation,
            document=document,
            origin=detached_record.origin,
            declarations=declarations,
            declaration=declaration,
            session=session,
            lease_token=lease_token,
            owner_thread_id=threading.get_ident(),
            before_snapshot=initial_business,
            after_snapshot=final_business,
            before_business_root=_business_root(initial_business),
            after_business_root=_business_root(final_business),
            before_document_version=initial_version,
            after_document_version=final_business.document.document_version,
            canonical_changed=canonical_changed,
            inventory=inventory,
            read_only_loader_resolver=read_only_loader_resolver,
        )
        return _LEDGER.issue_candidate(candidate_record)
    except BaseException as original_error:
        if session is not None and session.active:
            try:
                session.rollback()
            except BaseException as rollback_error:
                # No candidate exists yet, so quarantine the Document lease
                # directly and preserve both failure classifications.
                _LEDGER.quarantine_unissued_lease(document, lease_token)
                raise ZeError(
                    "mutation_rollback_failed",
                    "runtime preparation failed and rollback also failed",
                    context={
                        "reason": "runtime_rehydration_rollback_failed",
                        "original_error_type": type(original_error).__name__,
                        "original_error_code": getattr(original_error, "code", None),
                        "rollback_error_type": type(rollback_error).__name__,
                        "rollback_error_code": getattr(rollback_error, "code", None),
                    },
                ) from original_error
        _LEDGER.release_unpublished_lease(document, lease_token)
        raise


def _materialize_runtime_overlays(record: _CandidateRecord) -> _RuntimeInventory:
    resolver = record.read_only_loader_resolver
    if record.declaration.read_only_sources and not callable(resolver):
        _fail(
            "runtime_rehydration_failed",
            "runtime_read_only_loader_resolver_missing",
            "read-only declarations require an in-memory loader resolver",
        )
    resolved_loaders: list[Any] = []
    for raw_source in record.declaration.read_only_sources:
        declared_name, declared_entity_type, declared_loader_path = raw_source
        before_inventory = _capture_runtime_inventory(record.document)
        before_business = _capture_business_projection(
            record.document, record.project_id, record.generation,
        )
        source = ReadOnlyDeclarationSnapshot(*raw_source)
        loader = resolver(source)  # type: ignore[misc]
        if (
            source.name != declared_name
            or source.entity_type != declared_entity_type
            or source.loader_path != declared_loader_path
        ):
            _fail(
                "runtime_rehydration_failed",
                "runtime_read_only_declaration_tampered",
                "read-only resolver changed its declaration view",
            )
        if (
            _capture_business_projection(
                record.document, record.project_id, record.generation,
            )
            != before_business
            or not _runtime_inventory_matches(record.document, before_inventory)
        ):
            _fail(
                "runtime_rehydration_failed",
                "runtime_read_only_resolver_mutated_document",
                "read-only resolver may only return a loader",
            )
        if not callable(loader):
            _fail(
                "runtime_rehydration_failed",
                "runtime_read_only_loader_invalid",
                "read-only loader resolver must return a callable",
            )
        record.document.register_read_only_source(ReadOnlySourceSpec(
            name=declared_name,
            entity_type=declared_entity_type,
            loader=loader,
            schema={},
        ))
        if (
            _capture_business_projection(
                record.document, record.project_id, record.generation,
            )
            != before_business
            or not _runtime_inventory_preserved_after_source(
                record.document,
                before_inventory,
                source_name=declared_name,
                source_entity_type=declared_entity_type,
                loader=loader,
            )
        ):
            _fail(
                "runtime_rehydration_failed",
                "runtime_read_only_loader_mutated_existing_state",
                "read-only loader changed state outside its declared source append",
            )
        resolved_loaders.append(loader)
    if not _runtime_sources_match_manifest(
        record.document, record.declaration, tuple(resolved_loaders),
    ):
        _fail(
            "runtime_rehydration_failed",
            "runtime_read_only_manifest_mismatch",
            "materialized read-only sources do not match the declaration manifest",
        )
    _install_declaration_authority(
        record.document, record.declaration, validate_read_only=True,
    )
    canonical = _capture_business_projection(
        record.document, record.project_id, record.generation,
    )
    if canonical != record.after_snapshot:
        _fail(
            "runtime_rehydration_failed",
            "runtime_overlay_changed_canonical_state",
            "runtime overlay materialization changed canonical business state",
        )
    return _capture_runtime_inventory(record.document)


def _finish_ready(
    candidate: _RuntimeRehydrationCandidate,
    *,
    authority_proof: str,
    durable: bool,
    authority_fence: Callable[
        [RuntimeAuthorityFenceRequest], RuntimeAuthorityFenceReceipt
    ],
) -> RuntimeAdmission:
    states = ("durable_committed",) if durable else ("pending",)
    record: _CandidateRecord | None = None
    try:
        record = _LEDGER.candidate(candidate, states=states)
        _assert_candidate_unchanged(record)
        inventory = _materialize_runtime_overlays(record)
        # Final exact checks immediately precede the session close.  Production
        # still must hold the external generation/load lease around this call.
        current = _capture_business_projection(
            record.document, record.project_id, record.generation,
        )
        if current != record.after_snapshot or not _runtime_inventory_matches(
            record.document, inventory,
        ):
            _fail(
                "runtime_rehydration_candidate_changed",
                "runtime_rehydration_candidate_tampered",
                "runtime head changed during overlay materialization",
            )
        authority_revision = _fence_runtime_authority(record, authority_fence)
        # The fence callback is an external trusted port.  Recheck the exact
        # in-memory head after it returns and immediately before publication.
        current = _capture_business_projection(
            record.document, record.project_id, record.generation,
        )
        if current != record.after_snapshot or not _runtime_inventory_matches(
            record.document, inventory,
        ):
            _fail(
                "runtime_rehydration_candidate_changed",
                "runtime_rehydration_candidate_tampered",
                "runtime head changed during final authority fencing",
            )
        record.session.commit()
        ready = _LEDGER.issue_ready(
            candidate,
            authority_proof=authority_proof,
            authority_revision=authority_revision,
            inventory=inventory,
        )
        return _consume_runtime_admission(ready)
    except BaseException:
        if durable or (record is not None and not record.session.active):
            # Durable canonical state must never be rolled back.  The unpublished
            # Document is quarantined and must be discarded/reloaded.  A no-op
            # path whose session already closed is also no longer rollbackable.
            _LEDGER.quarantine(
                candidate,
                allowed=("pending", "committing", "durable_committed", "ready"),
            )
            if record is not None:
                _close_quarantined_session(record)
        raise


def _promote_noop(
    candidate: _RuntimeRehydrationCandidate,
    authority_fence: Callable[
        [RuntimeAuthorityFenceRequest], RuntimeAuthorityFenceReceipt
    ],
) -> RuntimeAdmission:
    record = _LEDGER.candidate(candidate)
    if record.canonical_changed:
        _fail(
            "runtime_authority_commit_required",
            "runtime_canonical_delta_requires_commit",
            "changed canonical state requires an authority CAS",
        )
    return _finish_ready(
        candidate,
        authority_proof="canonical_noop",
        durable=False,
        authority_fence=authority_fence,
    )


def _receipt_matches(
    receipt: RuntimeAuthorityReceipt,
    request: RuntimeAuthorityWrite,
) -> bool:
    try:
        if type(receipt) is not RuntimeAuthorityReceipt:
            return False
        outcome = receipt.outcome
        protocol = receipt.protocol
        project_id = receipt.project_id
        generation = receipt.generation
        before_root = receipt.before_business_root
        before_version = receipt.before_document_version
        after_root = receipt.after_business_root
        after_version = receipt.after_document_version
        transaction_id = receipt.transaction_id
        if (
            type(outcome) is not str
            or outcome not in _AUTHORITY_OUTCOMES
            or type(protocol) is not str
            or type(project_id) is not str
            or type(generation) is not int
            or type(before_root) is not bytes
            or len(before_root) != 32
            or type(before_version) is not int
            or type(after_root) is not bytes
            or len(after_root) != 32
            or type(after_version) is not int
            or type(transaction_id) is not str
        ):
            return False
        transaction_id.encode("utf-8", "strict")
        if len(transaction_id) > 256 or (outcome == "committed" and not transaction_id):
            return False
        return (
            receipt.request_identity is request.request_identity
            and protocol == request.protocol
            and project_id == request.project_id
            and generation == request.generation
            and before_root == request.before_business_root
            and before_version == request.before_document_version
            and after_root == request.after_business_root
            and after_version == request.after_document_version
        )
    except BaseException:
        # Receipt validation happens after the writer may have committed.  No
        # malformed carrier may escape this predicate and leave COMMITTING live.
        return False


def _close_quarantined_session(record: _CandidateRecord) -> None:
    if record.session.active and threading.get_ident() == record.owner_thread_id:
        try:
            record.session.commit()
        except BaseException:
            pass


def _fence_receipt_valid(
    receipt: RuntimeAuthorityFenceReceipt,
    request: RuntimeAuthorityFenceRequest,
) -> bool:
    try:
        if type(receipt) is not RuntimeAuthorityFenceReceipt:
            return False
        if (
            type(receipt.outcome) is not str
            or receipt.outcome not in _FENCE_OUTCOMES
            or type(receipt.protocol) is not str
            or type(receipt.project_id) is not str
            or type(receipt.generation) is not int
            or type(receipt.business_root) is not bytes
            or len(receipt.business_root) != 32
            or type(receipt.document_version) is not int
            or receipt.document_version < 1
            or type(receipt.declaration_digest) is not bytes
            or len(receipt.declaration_digest) != 32
            or type(receipt.runtime_generation) is not str
            or type(receipt.authority_revision) is not str
        ):
            return False
        receipt.runtime_generation.encode("utf-8", "strict")
        receipt.authority_revision.encode("utf-8", "strict")
        if (
            not receipt.runtime_generation
            or len(receipt.runtime_generation) > 256
            or not receipt.authority_revision
            or len(receipt.authority_revision) > 256
        ):
            return False
        if (
            receipt.request_identity is not request.request_identity
            or receipt.protocol != request.protocol
            or receipt.project_id != request.project_id
            or receipt.generation != request.generation
            or receipt.declaration_digest != request.declaration_digest
            or receipt.runtime_generation != request.runtime_generation
        ):
            return False
        if receipt.outcome == "current":
            return (
                receipt.business_root == request.business_root
                and receipt.document_version == request.document_version
            )
        if receipt.outcome == "advanced":
            return (
                receipt.business_root != request.business_root
                or receipt.document_version != request.document_version
            )
        return True
    except BaseException:
        return False


def _fence_runtime_authority(
    record: _CandidateRecord,
    authority_fence: Callable[
        [RuntimeAuthorityFenceRequest], RuntimeAuthorityFenceReceipt
    ],
) -> str:
    request = RuntimeAuthorityFenceRequest(
        request_identity=object(),
        protocol=AUTHORITY_FENCE_PROTOCOL,
        project_id=record.project_id,
        generation=record.generation,
        business_root=record.after_business_root,
        document_version=record.after_document_version,
        declaration_digest=record.declaration.digest,
        runtime_generation=record.declaration.runtime_generation,
    )
    try:
        receipt = authority_fence(request)
    except BaseException as fence_error:
        raise ZeError(
            "runtime_authority_fence_unknown",
            "final runtime authority fence failed",
            context={
                "reason": "runtime_authority_fence_unclassified_failure",
                "error_type": type(fence_error).__name__,
                "error_code": getattr(fence_error, "code", None),
            },
        ) from fence_error
    if not _fence_receipt_valid(receipt, request):
        _fail(
            "runtime_authority_fence_unknown",
            "runtime_authority_fence_receipt_invalid",
            "final runtime authority fence returned an invalid receipt",
        )
    if receipt.outcome == "advanced":
        _fail(
            "runtime_authority_head_advanced",
            "runtime_authority_head_changed_before_publication",
            "authority advanced while the runtime head was loading",
        )
    if receipt.outcome == "unknown":
        _fail(
            "runtime_authority_fence_unknown",
            "runtime_authority_fence_indeterminate",
            "final runtime authority fence is indeterminate",
        )
    return receipt.authority_revision


def _commit_changed_candidate(
    candidate: _RuntimeRehydrationCandidate,
    writer: Callable[[RuntimeAuthorityWrite], RuntimeAuthorityReceipt],
    authority_fence: Callable[
        [RuntimeAuthorityFenceRequest], RuntimeAuthorityFenceReceipt
    ],
) -> RuntimeAdmission:
    record = _LEDGER.candidate(candidate)
    _assert_candidate_unchanged(record)
    if not record.canonical_changed:
        _fail(
            "runtime_rehydration_forbidden",
            "runtime_authority_commit_not_required",
            "unchanged canonical state must use the no-op transition",
        )
    if not callable(writer):
        _fail(
            "runtime_authority_commit_required",
            "runtime_authority_writer_missing",
            "changed canonical state requires a synchronous authority writer",
        )
    request = RuntimeAuthorityWrite(
        request_identity=object(),
        protocol=AUTHORITY_WRITE_PROTOCOL,
        project_id=record.project_id,
        generation=record.generation,
        before_business_root=record.before_business_root,
        before_document_version=record.before_document_version,
        after_business_root=record.after_business_root,
        after_document_version=record.after_document_version,
        business_snapshot=record.after_snapshot,
    )
    _LEDGER.mark_committing(candidate)
    try:
        receipt = writer(request)
    except BaseException as writer_error:
        _LEDGER.quarantine(candidate, allowed=("committing",))
        _close_quarantined_session(record)
        raise ZeError(
            "runtime_authority_outcome_unknown",
            "runtime authority writer failed without a typed durability outcome",
            context={
                "reason": "runtime_authority_writer_unclassified_failure",
                "error_type": type(writer_error).__name__,
                "error_code": getattr(writer_error, "code", None),
            },
        ) from writer_error
    if not _receipt_matches(receipt, request):
        _LEDGER.quarantine(candidate, allowed=("committing",))
        _close_quarantined_session(record)
        _fail(
            "runtime_authority_outcome_unknown",
            "runtime_authority_receipt_invalid",
            "runtime authority returned an invalid or mismatched receipt",
        )
    if receipt.outcome == "unknown":
        _LEDGER.quarantine(candidate, allowed=("committing",))
        _close_quarantined_session(record)
        _fail(
            "runtime_authority_outcome_unknown",
            "runtime_authority_commit_indeterminate",
            "runtime authority commit outcome is indeterminate",
        )
    if receipt.outcome == "not_durable":
        _LEDGER.restore_pending(candidate)
        error = ZeError(
            "runtime_authority_not_durable",
            "runtime authority proved that no business rows were committed",
            context={"reason": "runtime_authority_write_aborted"},
        )
        _rollback_pre_durability(candidate, error)
        raise error

    try:
        _LEDGER.mark_durable(candidate)
    except BaseException:
        _LEDGER.quarantine(
            candidate, allowed=("committing", "durable_committed"),
        )
        _close_quarantined_session(record)
        raise
    return _finish_ready(
        candidate,
        authority_proof="authority_commit:" + receipt.transaction_id,
        durable=True,
        authority_fence=authority_fence,
    )


def rehydrate_runtime(
    detached: DetachedBusinessHead,
    declarations: RegistryDeclarationSnapshot,
    *,
    authority_fence: Callable[
        [RuntimeAuthorityFenceRequest], RuntimeAuthorityFenceReceipt
    ],
    authority_writer: (
        Callable[[RuntimeAuthorityWrite], RuntimeAuthorityReceipt] | None
    ) = None,
    read_only_loader_resolver: (
        Callable[[ReadOnlyDeclarationSnapshot], Callable[[], Iterable[dict[str, Any]]]]
        | None
    ) = None,
) -> RuntimeAdmission:
    """Synchronously produce one ready head or close/quarantine every path.

    The caller must hold the project generation/load lease and exclusive
    ownership of the unpublished Document for the entire call.  Migration and
    loader callbacks are trusted synchronous ports: they must be deterministic,
    bounded, idempotent, and free of external side effects.
    """
    if not callable(authority_fence):
        _fail(
            "runtime_authority_fence_required",
            "runtime_authority_fence_missing",
            "runtime publication requires a fresh authority fence",
        )
    candidate = _prepare_runtime_candidate(
        detached,
        declarations,
        read_only_loader_resolver=read_only_loader_resolver,
    )
    try:
        record = _LEDGER.candidate(candidate)
        if record.canonical_changed:
            if authority_writer is None:
                _fail(
                    "runtime_authority_commit_required",
                    "runtime_authority_writer_missing",
                    "changed canonical state requires a synchronous authority writer",
                )
            return _commit_changed_candidate(
                candidate, authority_writer, authority_fence,
            )
        return _promote_noop(candidate, authority_fence)
    except BaseException as original_error:
        pending = False
        try:
            _LEDGER.candidate(candidate)
            pending = True
        except ZeError:
            # Durable/unknown failures are already terminal and must never roll
            # the in-memory head behind possible storage authority.
            pending = False
        if pending:
            _rollback_pre_durability(candidate, original_error)
        raise


def _consume_runtime_admission(value: Any) -> RuntimeAdmission:
    """Consume a ready handle into one manifest-bound future Context input."""
    record = _LEDGER.ready(value, require_issued=True)
    valid = False
    try:
        current = _capture_business_projection(
            record.document, record.project_id, record.generation,
        )
        valid = (
            current == record.snapshot
            and record.document.version == record.document_version
            and _business_root(current) == record.business_root
            and _runtime_inventory_matches(record.document, record.inventory)
        )
    except BaseException:
        valid = False
    if not valid:
        _LEDGER.quarantine_ready(value)
        _fail(
            "runtime_rehydration_forbidden",
            "runtime_ready_head_changed_before_admission",
            "RuntimeReadyHead changed before Context admission",
        )
    return _LEDGER.admit(value)


__all__ = [
    "AUTHORITY_FENCE_PROTOCOL",
    "AUTHORITY_WRITE_PROTOCOL",
    "DetachedBusinessHead",
    "ReadOnlyDeclarationSnapshot",
    "RegistryDeclarationSnapshot",
    "RuntimeAdmission",
    "RuntimeAuthorityFenceReceipt",
    "RuntimeAuthorityFenceRequest",
    "RuntimeAuthorityReceipt",
    "RuntimeAuthorityWrite",
    "detach_restored_business_head",
    "rehydrate_runtime",
    "snapshot_registry_declarations",
]
