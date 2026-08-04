"""Pure issuer, verifier, and fresh-evidence classifier for audit certificates.

The issuer accepts typed observations only and turns them into an immutable
expected specialised certificate plus its exact generic receipt parent.  It
does not access SQLite, a filesystem, a clock, a callback, or a signer.  A
future writer must persist ``plan.evidence`` and then perform a fresh read;
activation may consume only a classifier result of ``EXACT``.  The classifier
accepts only a private durable-reader stamped fresh wrapper, so in-memory plan
evidence cannot itself stand in for that read.  This is a trusted-process
Python capability boundary, not a cryptographic proof against hostile code.

Digest closure detects accidental or unauthorised writes under the trusted
writer model.  It is not a signature and cannot prove facts against an attacker
who can rewrite the database and every digest offline.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, NamedTuple, Optional

from business_codec import BusinessCodecError, encode_payload_bounded
from repository_audit_contract import (
    EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256,
    MAX_SQLITE_INTEGER,
    MAX_REPOSITORY_AUDIT_MANIFEST_BYTES,
    MAX_REPOSITORY_AUDIT_MANIFEST_NODES,
    MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS,
    MAX_REPOSITORY_AUDIT_MEMBERSHIP_MANIFEST_BYTES,
    REPOSITORY_AUDIT_CERTIFICATE_STATUS,
    REPOSITORY_AUDIT_MANIFEST_SCHEMA,
    REPOSITORY_AUDIT_PLAN_STATUS,
    REPOSITORY_AUDIT_PROTOCOL,
    REPOSITORY_AUDIT_TRANSACTION_KIND,
    REPOSITORY_AUDIT_TRANSACTION_PREFIX,
    RepositoryAuditCertificateLedgerHead,
    RepositoryAuditCertificateEvidence,
    RepositoryAuditCertificateLedgerEntry,
    RepositoryAuditClassification,
    RepositoryAuditContractError,
    RepositoryAuditCoverage,
    RepositoryAuditFindings,
    RepositoryAuditFreshObservation,
    RepositoryAuditFreshMembershipObservation,
    RepositoryAuditGenericReceiptEvidence,
    RepositoryAuditObservedInput,
    RepositoryAuditMembershipObservation,
    RepositoryAuditPersistedObservation,
    RepositoryAuditPersistedReadStatus,
    RepositoryAuditPurpose,
    RepositoryAuditReconcileStatus,
    RepositoryAuditRuntimeBinding,
    RepositoryAuditSchemaObservation,
    RepositoryFreshAuditObservation,
    _unwrap_repository_audit_fresh_observation,
    _unwrap_repository_audit_fresh_membership_observation,
    _unwrap_repository_fresh_audit_observation,
)


REPOSITORY_AUDIT_MANIFEST_HASH_DOMAIN = (
    b"ze.repository-audit.certificate-manifest-digest.v1"
)
REPOSITORY_AUDIT_CERTIFICATE_TAIL_HASH_DOMAIN = (
    b"ze.repository-audit.certificate-tail.v1"
)
REPOSITORY_AUDIT_FINDINGS_HASH_DOMAIN = b"ze.repository-audit.findings.v1"

_PLAN_ISSUER = object()


class RepositoryAuditIntegrityError(ValueError):
    """Stable, payload-free certificate integrity failure."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or not code:
            raise TypeError("code must be a non-empty exact string")
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    raise RepositoryAuditIntegrityError(code)


class _PreparedRepositoryAudit(NamedTuple):
    observed: RepositoryAuditObservedInput
    previous_certificate: Optional[RepositoryAuditCertificateEvidence]
    evidence: RepositoryAuditCertificateEvidence
    before_head: RepositoryAuditCertificateLedgerHead
    after_head: RepositoryAuditCertificateLedgerHead


class RepositoryAuditCertificatePlan:
    """Issuer-stamped expected value; never evidence that persistence happened."""

    __slots__ = (
        "observed",
        "previous_certificate",
        "before_head",
        "after_head",
        "evidence",
        "protocol",
        "status",
        "_issuer",
        "_fresh_observed",
    )

    def __init__(
        self,
        prepared: _PreparedRepositoryAudit,
        *,
        fresh_observed: RepositoryFreshAuditObservation,
        _issuer: Any,
    ) -> None:
        if _issuer is not _PLAN_ISSUER:
            _fail("repository_audit_plan_issuer_invalid")
        object.__setattr__(self, "observed", prepared.observed)
        object.__setattr__(
            self,
            "previous_certificate",
            prepared.previous_certificate,
        )
        object.__setattr__(self, "evidence", prepared.evidence)
        object.__setattr__(self, "before_head", prepared.before_head)
        object.__setattr__(self, "after_head", prepared.after_head)
        object.__setattr__(self, "protocol", REPOSITORY_AUDIT_PROTOCOL)
        object.__setattr__(self, "status", REPOSITORY_AUDIT_PLAN_STATUS)
        object.__setattr__(self, "_fresh_observed", fresh_observed)
        object.__setattr__(self, "_issuer", _issuer)

    def __setattr__(self, name: str, value: Any) -> None:
        del name, value
        raise AttributeError("RepositoryAuditCertificatePlan is immutable")


def _frame(hasher: Any, value: bytes) -> None:
    hasher.update(len(value).to_bytes(8, "big"))
    hasher.update(value)


def _manifest_digest(value: bytes) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, REPOSITORY_AUDIT_MANIFEST_HASH_DOMAIN)
    _frame(hasher, value)
    return hasher.digest()


def _certificate_tail_digest(
    sequence: int,
    previous_tail: bytes,
    manifest_sha256: bytes,
) -> bytes:
    hasher = hashlib.sha256()
    _frame(hasher, REPOSITORY_AUDIT_CERTIFICATE_TAIL_HASH_DOMAIN)
    _frame(hasher, sequence.to_bytes(8, "big"))
    _frame(hasher, previous_tail)
    _frame(hasher, manifest_sha256)
    return hasher.digest()


def _canonical_runtime(value: RepositoryAuditRuntimeBinding) -> RepositoryAuditRuntimeBinding:
    return RepositoryAuditRuntimeBinding(
        authority=value.authority,
        authority_state=value.authority_state,
        repository_epoch=value.repository_epoch,
        cutover_id=value.cutover_id,
        source_admission_revision=value.source_admission_revision,
        repository_revision=value.repository_revision,
        state_revision=value.state_revision,
        runtime_generation=value.runtime_generation,
        code_digest=value.code_digest,
        schema_version=value.schema_version,
        updated_at=value.updated_at,
    )


def _canonical_schema(
    value: RepositoryAuditSchemaObservation,
) -> RepositoryAuditSchemaObservation:
    return RepositoryAuditSchemaObservation(
        user_version=value.user_version,
        schema_fingerprint=value.schema_fingerprint,
        migration_ledger_count=value.migration_ledger_count,
        migration_ledger_sha256=value.migration_ledger_sha256,
    )


def _canonical_coverage(value: RepositoryAuditCoverage) -> RepositoryAuditCoverage:
    return RepositoryAuditCoverage(
        covered_business_revision=value.covered_business_revision,
        worker_ledger_sequence=value.worker_ledger_sequence,
        worker_ledger_tail_sha256=value.worker_ledger_tail_sha256,
        receipt_namespace_count=value.receipt_namespace_count,
        receipt_namespace_sha256=value.receipt_namespace_sha256,
        cutover_install_manifest_sha256=value.cutover_install_manifest_sha256,
    )


def _canonical_findings(value: RepositoryAuditFindings) -> RepositoryAuditFindings:
    return RepositoryAuditFindings(
        page_integrity=value.page_integrity,
        foreign_key_violation_count=value.foreign_key_violation_count,
        namespace_violation_count=value.namespace_violation_count,
        started_at=value.started_at,
        completed_at=value.completed_at,
    )


def _canonical_observed(value: Any) -> RepositoryAuditObservedInput:
    if type(value) is not RepositoryAuditObservedInput:
        _fail("repository_audit_observation_type_invalid")
    try:
        rebuilt = RepositoryAuditObservedInput(
            certificate_id=value.certificate_id,
            purpose=value.purpose,
            runtime=_canonical_runtime(value.runtime),
            schema=_canonical_schema(value.schema),
            coverage=_canonical_coverage(value.coverage),
            findings=_canonical_findings(value.findings),
        )
    except (AttributeError, RepositoryAuditContractError, TypeError):
        _fail("repository_audit_observation_changed")
    if rebuilt != value:
        _fail("repository_audit_observation_changed")
    return rebuilt


def _canonical_receipt(
    value: RepositoryAuditGenericReceiptEvidence,
) -> RepositoryAuditGenericReceiptEvidence:
    return RepositoryAuditGenericReceiptEvidence(
        transaction_id=value.transaction_id,
        kind=value.kind,
        committed_at=value.committed_at,
    )


def _canonical_head(value: Any) -> RepositoryAuditCertificateLedgerHead:
    if type(value) is not RepositoryAuditCertificateLedgerHead:
        _fail("repository_audit_certificate_head_type_invalid")
    try:
        rebuilt = RepositoryAuditCertificateLedgerHead(
            sequence=value.sequence,
            certificate_id=value.certificate_id,
            manifest_sha256=value.manifest_sha256,
            certificate_tail_sha256=value.certificate_tail_sha256,
        )
    except (AttributeError, RepositoryAuditContractError, TypeError):
        _fail("repository_audit_certificate_head_changed")
    if rebuilt != value:
        _fail("repository_audit_certificate_head_changed")
    return rebuilt


def _head_for_certificate(
    value: Optional[RepositoryAuditCertificateEvidence],
) -> RepositoryAuditCertificateLedgerHead:
    if value is None:
        return RepositoryAuditCertificateLedgerHead(sequence=0)
    return RepositoryAuditCertificateLedgerHead(
        sequence=value.sequence,
        certificate_id=value.certificate_id,
        manifest_sha256=value.manifest_sha256,
        certificate_tail_sha256=value.certificate_tail_sha256,
    )


def _canonical_ledger_entry(
    value: Any,
) -> RepositoryAuditCertificateLedgerEntry:
    if type(value) is not RepositoryAuditCertificateLedgerEntry:
        _fail("repository_audit_ledger_entry_type_invalid")
    try:
        rebuilt = RepositoryAuditCertificateLedgerEntry(
            receipt=(
                None
                if value.receipt is None
                else _canonical_receipt(value.receipt)
            ),
            certificate=value.certificate,
        )
    except (AttributeError, RepositoryAuditContractError, TypeError):
        _fail("repository_audit_ledger_entry_changed")
    if rebuilt != value:
        _fail("repository_audit_ledger_entry_changed")
    return rebuilt


def _runtime_manifest(value: RepositoryAuditRuntimeBinding) -> dict:
    return {
        "authority": value.authority,
        "authority_state": value.authority_state.value,
        "repository_epoch": value.repository_epoch,
        "cutover_id": value.cutover_id,
        "source_admission_revision": value.source_admission_revision,
        "repository_revision": value.repository_revision,
        "state_revision": value.state_revision,
        "runtime_generation": value.runtime_generation,
        "code_digest": value.code_digest.hex(),
        "schema_version": value.schema_version,
        "updated_at": value.updated_at,
    }


def _schema_manifest(value: RepositoryAuditSchemaObservation) -> dict:
    return {
        "user_version": value.user_version,
        "schema_fingerprint": value.schema_fingerprint,
        "migration_ledger_count": value.migration_ledger_count,
        "migration_ledger_sha256": value.migration_ledger_sha256.hex(),
    }


def _coverage_manifest(value: RepositoryAuditCoverage) -> dict:
    return {
        "covered_business_revision": value.covered_business_revision,
        "worker_ledger_sequence": value.worker_ledger_sequence,
        "worker_ledger_tail_sha256": value.worker_ledger_tail_sha256.hex(),
        "receipt_namespace_count": value.receipt_namespace_count,
        "receipt_namespace_sha256": value.receipt_namespace_sha256.hex(),
        "cutover_install_manifest_sha256": (
            None
            if value.cutover_install_manifest_sha256 is None
            else value.cutover_install_manifest_sha256.hex()
        ),
    }


def _findings_manifest(value: RepositoryAuditFindings) -> dict:
    return {
        "page_integrity": value.page_integrity,
        "foreign_key_violation_count": value.foreign_key_violation_count,
        "namespace_violation_count": value.namespace_violation_count,
        "started_at": value.started_at,
        "completed_at": value.completed_at,
    }


def _findings_digest(value: RepositoryAuditFindings) -> bytes:
    """Return the unique domain-separated schema projection of findings.

    The certificate manifest already contains the full findings object.  The
    SQLite v8 audit table additionally stores ``findings_sha256`` as a compact
    projection, so give that column one protocol-owned derivation instead of
    letting a writer choose an unrelated digest.
    """

    if type(value) is not RepositoryAuditFindings:
        _fail("repository_audit_findings_type_invalid")
    try:
        canonical = encode_payload_bounded(
            _findings_manifest(_canonical_findings(value)),
            MAX_REPOSITORY_AUDIT_MANIFEST_BYTES,
            MAX_REPOSITORY_AUDIT_MANIFEST_NODES,
        )
    except (BusinessCodecError, RepositoryAuditContractError, TypeError):
        _fail("repository_audit_findings_encoding_failed")
    hasher = hashlib.sha256()
    _frame(hasher, REPOSITORY_AUDIT_FINDINGS_HASH_DOMAIN)
    _frame(hasher, canonical)
    return hasher.digest()


def _build_manifest(
    observed: RepositoryAuditObservedInput,
    *,
    sequence: int,
    receipt: RepositoryAuditGenericReceiptEvidence,
    previous_certificate_id: Optional[str],
    previous_manifest_sha256: Optional[bytes],
    previous_certificate_tail_sha256: Optional[bytes],
) -> bytes:
    predecessor = None
    if previous_certificate_id is not None:
        predecessor = {
            "certificate_id": previous_certificate_id,
            "sequence": sequence - 1,
            "manifest_sha256": previous_manifest_sha256.hex(),
            "certificate_tail_sha256": previous_certificate_tail_sha256.hex(),
        }
    manifest = {
        "schema": REPOSITORY_AUDIT_MANIFEST_SCHEMA,
        "protocol": REPOSITORY_AUDIT_PROTOCOL,
        "status": REPOSITORY_AUDIT_CERTIFICATE_STATUS,
        "certificate": {
            "certificate_id": observed.certificate_id,
            "sequence": sequence,
            "purpose": observed.purpose.value,
        },
        # This block is the expected parent relation: the specialised row and
        # the generic transaction receipt share one exact id/kind/time tuple.
        "receipt_parent": {
            "ledger": "transaction_receipts",
            "transaction_id": receipt.transaction_id,
            "kind": receipt.kind,
            "committed_at": receipt.committed_at,
            "specialised_protocol": REPOSITORY_AUDIT_PROTOCOL,
            "specialised_certificate_id": observed.certificate_id,
        },
        "runtime": _runtime_manifest(observed.runtime),
        "schema_identity": _schema_manifest(observed.schema),
        "coverage": _coverage_manifest(observed.coverage),
        "findings": _findings_manifest(observed.findings),
        "predecessor": predecessor,
    }
    try:
        return encode_payload_bounded(
            manifest,
            MAX_REPOSITORY_AUDIT_MANIFEST_BYTES,
            MAX_REPOSITORY_AUDIT_MANIFEST_NODES,
        )
    except BusinessCodecError:
        _fail("repository_audit_manifest_encoding_failed")


def _require_predecessor(
    observed: RepositoryAuditObservedInput,
    previous: RepositoryAuditCertificateEvidence,
) -> None:
    current_runtime = observed.runtime
    previous_runtime = previous.runtime
    if previous.sequence >= MAX_SQLITE_INTEGER:
        _fail("repository_audit_predecessor_sequence_exhausted")
    if (
        observed.certificate_id == previous.certificate_id
        or current_runtime.authority != previous_runtime.authority
        or current_runtime.repository_epoch != previous_runtime.repository_epoch
        or current_runtime.cutover_id != previous_runtime.cutover_id
        or current_runtime.source_admission_revision
        != previous_runtime.source_admission_revision
        or current_runtime.runtime_generation != previous_runtime.runtime_generation
        or not hmac.compare_digest(
            current_runtime.code_digest,
            previous_runtime.code_digest,
        )
        or current_runtime.schema_version != previous_runtime.schema_version
    ):
        _fail("repository_audit_predecessor_identity_mismatch")
    if (
        current_runtime.repository_revision < previous_runtime.repository_revision
        or current_runtime.state_revision < previous_runtime.state_revision
        or current_runtime.updated_at < previous_runtime.updated_at
        or observed.findings.started_at < previous.findings.completed_at
        or observed.coverage.covered_business_revision
        < previous.coverage.covered_business_revision
        or observed.coverage.worker_ledger_sequence
        < previous.coverage.worker_ledger_sequence
        # The predecessor's generic audit receipt was inserted after the
        # predecessor coverage snapshot.  Every successor audit must therefore
        # cover at least that one additional parent receipt.
        or observed.coverage.receipt_namespace_count
        <= previous.coverage.receipt_namespace_count
        or observed.schema.user_version < previous.schema.user_version
    ):
        _fail("repository_audit_predecessor_high_water_invalid")
    if (
        observed.coverage.worker_ledger_sequence
        == previous.coverage.worker_ledger_sequence
        and not hmac.compare_digest(
            observed.coverage.worker_ledger_tail_sha256,
            previous.coverage.worker_ledger_tail_sha256,
        )
    ):
        _fail("repository_audit_predecessor_worker_tail_invalid")
    if (
        observed.coverage.worker_ledger_sequence
        > previous.coverage.worker_ledger_sequence
        and hmac.compare_digest(
            observed.coverage.worker_ledger_tail_sha256,
            previous.coverage.worker_ledger_tail_sha256,
        )
    ):
        _fail("repository_audit_predecessor_worker_tail_invalid")
    if hmac.compare_digest(
        observed.coverage.receipt_namespace_sha256,
        previous.coverage.receipt_namespace_sha256,
    ):
        _fail("repository_audit_predecessor_receipt_digest_invalid")
    if observed.schema.user_version == previous.schema.user_version and (
        observed.schema.schema_fingerprint != previous.schema.schema_fingerprint
        or observed.schema.migration_ledger_count
        != previous.schema.migration_ledger_count
        or not hmac.compare_digest(
            observed.schema.migration_ledger_sha256,
            previous.schema.migration_ledger_sha256,
        )
    ):
        _fail("repository_audit_predecessor_schema_invalid")
    previous_install = previous.coverage.cutover_install_manifest_sha256
    current_install = observed.coverage.cutover_install_manifest_sha256
    if previous_install is not None and (
        current_install is None
        or not hmac.compare_digest(current_install, previous_install)
    ):
        _fail("repository_audit_predecessor_install_binding_invalid")


def _rebuild_evidence(value: Any) -> RepositoryAuditCertificateEvidence:
    if type(value) is not RepositoryAuditCertificateEvidence:
        _fail("repository_audit_evidence_type_invalid")
    try:
        observed = RepositoryAuditObservedInput(
            certificate_id=value.certificate_id,
            purpose=value.purpose,
            runtime=_canonical_runtime(value.runtime),
            schema=_canonical_schema(value.schema),
            coverage=_canonical_coverage(value.coverage),
            findings=_canonical_findings(value.findings),
        )
        receipt = _canonical_receipt(value.receipt_parent)
        manifest_bytes = _build_manifest(
            observed,
            sequence=value.sequence,
            receipt=receipt,
            previous_certificate_id=value.previous_certificate_id,
            previous_manifest_sha256=value.previous_manifest_sha256,
            previous_certificate_tail_sha256=value.previous_certificate_tail_sha256,
        )
        manifest_sha256 = _manifest_digest(manifest_bytes)
        previous_tail = (
            EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
            if value.previous_certificate_tail_sha256 is None
            else value.previous_certificate_tail_sha256
        )
        certificate_tail = _certificate_tail_digest(
            value.sequence,
            previous_tail,
            manifest_sha256,
        )
        rebuilt = RepositoryAuditCertificateEvidence(
            certificate_id=observed.certificate_id,
            purpose=observed.purpose,
            sequence=value.sequence,
            runtime=observed.runtime,
            schema=observed.schema,
            coverage=observed.coverage,
            findings=observed.findings,
            receipt_parent=receipt,
            previous_certificate_id=value.previous_certificate_id,
            previous_manifest_sha256=value.previous_manifest_sha256,
            previous_certificate_tail_sha256=value.previous_certificate_tail_sha256,
            manifest_bytes=manifest_bytes,
            manifest_sha256=manifest_sha256,
            certificate_tail_sha256=certificate_tail,
            protocol=REPOSITORY_AUDIT_PROTOCOL,
            status=REPOSITORY_AUDIT_CERTIFICATE_STATUS,
        )
    except (
        AttributeError,
        OverflowError,
        RepositoryAuditContractError,
        TypeError,
    ):
        _fail("repository_audit_evidence_changed")
    if (
        rebuilt != value
        or not hmac.compare_digest(rebuilt.manifest_bytes, value.manifest_bytes)
        or not hmac.compare_digest(rebuilt.manifest_sha256, value.manifest_sha256)
        or not hmac.compare_digest(
            rebuilt.certificate_tail_sha256,
            value.certificate_tail_sha256,
        )
    ):
        _fail("repository_audit_evidence_changed")
    return rebuilt


def _prepare_repository_audit_values(
    observed: Any,
    previous_certificate: Any = None,
) -> _PreparedRepositoryAudit:
    canonical_observed = _canonical_observed(observed)
    canonical_previous = None
    if previous_certificate is not None:
        canonical_previous = _rebuild_evidence(previous_certificate)
        _require_predecessor(canonical_observed, canonical_previous)

    sequence = 1 if canonical_previous is None else canonical_previous.sequence + 1
    previous_certificate_id = (
        None if canonical_previous is None else canonical_previous.certificate_id
    )
    previous_manifest_sha256 = (
        None if canonical_previous is None else canonical_previous.manifest_sha256
    )
    previous_tail_sha256 = (
        None
        if canonical_previous is None
        else canonical_previous.certificate_tail_sha256
    )
    receipt = RepositoryAuditGenericReceiptEvidence(
        transaction_id=(
            REPOSITORY_AUDIT_TRANSACTION_PREFIX + canonical_observed.certificate_id
        ),
        kind=REPOSITORY_AUDIT_TRANSACTION_KIND,
        committed_at=canonical_observed.findings.completed_at,
    )
    manifest_bytes = _build_manifest(
        canonical_observed,
        sequence=sequence,
        receipt=receipt,
        previous_certificate_id=previous_certificate_id,
        previous_manifest_sha256=previous_manifest_sha256,
        previous_certificate_tail_sha256=previous_tail_sha256,
    )
    manifest_sha256 = _manifest_digest(manifest_bytes)
    certificate_tail = _certificate_tail_digest(
        sequence,
        (
            EMPTY_REPOSITORY_AUDIT_CERTIFICATE_TAIL_SHA256
            if previous_tail_sha256 is None
            else previous_tail_sha256
        ),
        manifest_sha256,
    )
    evidence = RepositoryAuditCertificateEvidence(
        certificate_id=canonical_observed.certificate_id,
        purpose=canonical_observed.purpose,
        sequence=sequence,
        runtime=canonical_observed.runtime,
        schema=canonical_observed.schema,
        coverage=canonical_observed.coverage,
        findings=canonical_observed.findings,
        receipt_parent=receipt,
        previous_certificate_id=previous_certificate_id,
        previous_manifest_sha256=previous_manifest_sha256,
        previous_certificate_tail_sha256=previous_tail_sha256,
        manifest_bytes=manifest_bytes,
        manifest_sha256=manifest_sha256,
        certificate_tail_sha256=certificate_tail,
    )
    before_head = _head_for_certificate(canonical_previous)
    after_head = _head_for_certificate(evidence)
    return _PreparedRepositoryAudit(
        canonical_observed,
        canonical_previous,
        evidence,
        before_head,
        after_head,
    )


def _prepare_repository_audit(
    fresh_observed: Any,
) -> _PreparedRepositoryAudit:
    try:
        observed, observed_head, previous = (
            _unwrap_repository_fresh_audit_observation(fresh_observed)
        )
    except (RepositoryAuditContractError, TypeError):
        _fail("repository_fresh_audit_observation_invalid")
    canonical_head = _canonical_head(observed_head)
    prepared = _prepare_repository_audit_values(observed, previous)
    if canonical_head != prepared.before_head:
        _fail("repository_audit_certificate_head_predecessor_mismatch")
    return prepared


def _issue_repository_audit_certificate_plan(
    fresh_observed: RepositoryFreshAuditObservation,
) -> RepositoryAuditCertificatePlan:
    """Issue only from an independently stamped full same-snapshot audit.

    The future writer must CAS ``plan.before_head`` to ``plan.after_head`` in
    the same transaction that inserts the generic receipt and certificate.
    """

    prepared = _prepare_repository_audit(fresh_observed)
    return RepositoryAuditCertificatePlan(
        prepared,
        fresh_observed=fresh_observed,
        _issuer=_PLAN_ISSUER,
    )


def _require_repository_audit_certificate_evidence(
    value: Any,
) -> RepositoryAuditCertificateEvidence:
    """Rebuild one loaded specialised certificate before it may be trusted."""

    return _rebuild_evidence(value)


def _require_repository_audit_certificate_plan(
    value: Any,
) -> RepositoryAuditCertificatePlan:
    if (
        type(value) is not RepositoryAuditCertificatePlan
        or getattr(value, "_issuer", None) is not _PLAN_ISSUER
        or getattr(value, "protocol", None) != REPOSITORY_AUDIT_PROTOCOL
        or getattr(value, "status", None) != REPOSITORY_AUDIT_PLAN_STATUS
    ):
        _fail("repository_audit_plan_invalid")
    prepared = _prepare_repository_audit(value._fresh_observed)
    if (
        prepared.observed != value.observed
        or prepared.previous_certificate != value.previous_certificate
        or prepared.before_head != value.before_head
        or prepared.after_head != value.after_head
        or prepared.evidence != value.evidence
        or not hmac.compare_digest(
            prepared.evidence.manifest_sha256,
            value.evidence.manifest_sha256,
        )
        or not hmac.compare_digest(
            prepared.evidence.certificate_tail_sha256,
            value.evidence.certificate_tail_sha256,
        )
    ):
        _fail("repository_audit_plan_changed")
    return value


def _classification(
    status: RepositoryAuditReconcileStatus,
    expected: RepositoryAuditCertificateEvidence,
    *,
    evidence: Optional[RepositoryAuditCertificateEvidence] = None,
    error_code: str = "",
) -> RepositoryAuditClassification:
    return RepositoryAuditClassification(
        status=status,
        certificate_id=expected.certificate_id,
        evidence=evidence,
        error_code=error_code,
    )


def _classify_repository_audit_certificate(
    plan: RepositoryAuditCertificatePlan,
    fresh: RepositoryAuditFreshObservation,
) -> RepositoryAuditClassification:
    """Reconcile only the current-head CAS transaction.

    ``EXACT`` is activation-safe.  Once a successor advances the singleton
    head, retrying this older plan intentionally returns ``CONFLICT`` even if
    its historical certificate row remains valid; historical membership must
    be answered by a separate ledger-chain reader, not this idempotency path.
    """

    plan = _require_repository_audit_certificate_plan(plan)
    try:
        persisted = _unwrap_repository_audit_fresh_observation(fresh)
    except (RepositoryAuditContractError, TypeError):
        _fail("repository_audit_fresh_observation_invalid")
    try:
        rebuilt_persisted = RepositoryAuditPersistedObservation(
            read_status=persisted.read_status,
            receipt=(
                None
                if persisted.receipt is None
                else _canonical_receipt(persisted.receipt)
            ),
            certificate=persisted.certificate,
            certificate_head=(
                None
                if persisted.certificate_head is None
                else _canonical_head(persisted.certificate_head)
            ),
            error_code=persisted.error_code,
        )
    except (AttributeError, RepositoryAuditContractError, TypeError):
        _fail("repository_audit_persisted_observation_changed")
    if rebuilt_persisted != persisted:
        _fail("repository_audit_persisted_observation_changed")
    persisted = rebuilt_persisted
    expected = plan.evidence
    if persisted.read_status is RepositoryAuditPersistedReadStatus.UNKNOWN:
        return _classification(
            RepositoryAuditReconcileStatus.UNKNOWN,
            expected,
            error_code=persisted.error_code,
        )

    receipt = persisted.receipt
    certificate = persisted.certificate
    certificate_head = persisted.certificate_head
    if certificate_head is None:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_certificate_head_missing",
        )
    if receipt is None and certificate is None:
        if certificate_head != plan.before_head:
            return _classification(
                RepositoryAuditReconcileStatus.CONFLICT,
                expected,
                error_code="repository_audit_certificate_head_conflict",
            )
        return _classification(RepositoryAuditReconcileStatus.ABSENT, expected)
    if receipt is None:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_certificate_without_receipt",
        )
    if certificate is None:
        status = (
            RepositoryAuditReconcileStatus.CONFLICT
            if receipt.transaction_id == expected.transaction_id
            and receipt.kind != expected.receipt_kind
            else RepositoryAuditReconcileStatus.CORRUPT
        )
        return _classification(
            status,
            expected,
            error_code=(
                "repository_audit_receipt_identity_conflict"
                if status is RepositoryAuditReconcileStatus.CONFLICT
                else "repository_audit_receipt_without_certificate"
            ),
        )

    try:
        observed_certificate = _rebuild_evidence(certificate)
    except RepositoryAuditIntegrityError:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_certificate_corrupt",
        )
    if receipt != observed_certificate.receipt_parent:
        status = (
            RepositoryAuditReconcileStatus.CONFLICT
            if receipt.transaction_id == expected.transaction_id
            and receipt.kind != expected.receipt_kind
            else RepositoryAuditReconcileStatus.CORRUPT
        )
        return _classification(
            status,
            expected,
            error_code=(
                "repository_audit_receipt_identity_conflict"
                if status is RepositoryAuditReconcileStatus.CONFLICT
                else "repository_audit_receipt_certificate_mismatch"
            ),
        )
    if (
        observed_certificate.certificate_id != expected.certificate_id
        or observed_certificate.transaction_id != expected.transaction_id
    ):
        return _classification(
            RepositoryAuditReconcileStatus.CONFLICT,
            expected,
            error_code="repository_audit_certificate_identity_conflict",
        )
    if observed_certificate != expected:
        return _classification(
            RepositoryAuditReconcileStatus.CONFLICT,
            expected,
            error_code="repository_audit_certificate_content_conflict",
        )
    if receipt != expected.receipt_parent:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_receipt_parent_corrupt",
        )
    if certificate_head != plan.after_head:
        if certificate_head == plan.before_head:
            return _classification(
                RepositoryAuditReconcileStatus.CORRUPT,
                expected,
                error_code="repository_audit_certificate_head_not_advanced",
            )
        return _classification(
            RepositoryAuditReconcileStatus.CONFLICT,
            expected,
            error_code="repository_audit_certificate_head_conflict",
        )
    return _classification(
        RepositoryAuditReconcileStatus.EXACT,
        expected,
        evidence=observed_certificate,
    )


def _classify_repository_audit_certificate_membership(
    plan: RepositoryAuditCertificatePlan,
    fresh: RepositoryAuditFreshMembershipObservation,
) -> RepositoryAuditClassification:
    """Prove that the plan certificate is an ancestor of the current head.

    This is the independent historical-ledger path.  It never authorises a new
    certificate write and must not replace the current-head CAS classifier for
    ``ABSENT``/apply decisions.  Its sole success is an exact, rebuilt bound
    certificate plus a complete rebuilt successor chain ending at the freshly
    observed singleton head.
    """

    plan = _require_repository_audit_certificate_plan(plan)
    expected = plan.evidence
    try:
        membership = _unwrap_repository_audit_fresh_membership_observation(
            fresh
        )
    except (RepositoryAuditContractError, TypeError):
        _fail("repository_audit_fresh_membership_invalid")
    try:
        membership_manifest_bytes = sum(
            len(value.certificate.manifest_bytes)
            for value in membership.successor_chain
        ) + (
            0
            if membership.certificate is None
            else len(membership.certificate.manifest_bytes)
        )
        membership_budget_exceeded = (
            len(membership.successor_chain)
            > MAX_REPOSITORY_AUDIT_MEMBERSHIP_HOPS
            or membership_manifest_bytes
            > MAX_REPOSITORY_AUDIT_MEMBERSHIP_MANIFEST_BYTES
        )
    except (AttributeError, TypeError):
        _fail("repository_audit_membership_observation_changed")
    if membership_budget_exceeded:
        return _classification(
            RepositoryAuditReconcileStatus.UNKNOWN,
            expected,
            error_code="repository_audit_membership_budget_exceeded",
        )
    try:
        canonical = RepositoryAuditMembershipObservation(
            read_status=membership.read_status,
            receipt=(
                None
                if membership.receipt is None
                else _canonical_receipt(membership.receipt)
            ),
            certificate=membership.certificate,
            current_head=(
                None
                if membership.current_head is None
                else _canonical_head(membership.current_head)
            ),
            successor_chain=tuple(
                _canonical_ledger_entry(value)
                for value in membership.successor_chain
            ),
            error_code=membership.error_code,
        )
    except (AttributeError, RepositoryAuditContractError, TypeError):
        _fail("repository_audit_membership_observation_changed")
    if canonical != membership:
        _fail("repository_audit_membership_observation_changed")
    if canonical.read_status is RepositoryAuditPersistedReadStatus.UNKNOWN:
        return _classification(
            RepositoryAuditReconcileStatus.UNKNOWN,
            expected,
            error_code=canonical.error_code,
        )
    if canonical.current_head is None:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_membership_head_missing",
        )
    if canonical.receipt is None or canonical.certificate is None:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_membership_certificate_missing",
        )
    try:
        bound = _rebuild_evidence(canonical.certificate)
    except RepositoryAuditIntegrityError:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_membership_certificate_corrupt",
        )
    if canonical.receipt != bound.receipt_parent:
        return _classification(
            RepositoryAuditReconcileStatus.CORRUPT,
            expected,
            error_code="repository_audit_membership_receipt_mismatch",
        )
    if (
        bound.certificate_id != expected.certificate_id
        or bound.transaction_id != expected.transaction_id
    ):
        return _classification(
            RepositoryAuditReconcileStatus.CONFLICT,
            expected,
            error_code="repository_audit_membership_identity_conflict",
        )
    if bound != expected or canonical.receipt != expected.receipt_parent:
        return _classification(
            RepositoryAuditReconcileStatus.CONFLICT,
            expected,
            error_code="repository_audit_membership_content_conflict",
        )

    previous = bound
    for entry in canonical.successor_chain:
        try:
            successor = _rebuild_evidence(entry.certificate)
            if entry.receipt is None or entry.receipt != successor.receipt_parent:
                raise RepositoryAuditIntegrityError(
                    "repository_audit_membership_receipt_invalid"
                )
            successor_observed = RepositoryAuditObservedInput(
                certificate_id=successor.certificate_id,
                purpose=successor.purpose,
                runtime=successor.runtime,
                schema=successor.schema,
                coverage=successor.coverage,
                findings=successor.findings,
            )
            if (
                successor.sequence != previous.sequence + 1
                or successor.previous_certificate_id
                != previous.certificate_id
                or successor.previous_manifest_sha256
                != previous.manifest_sha256
                or successor.previous_certificate_tail_sha256
                != previous.certificate_tail_sha256
            ):
                raise RepositoryAuditIntegrityError(
                    "repository_audit_membership_link_invalid"
                )
            _require_predecessor(successor_observed, previous)
        except (
            RepositoryAuditContractError,
            RepositoryAuditIntegrityError,
            TypeError,
        ):
            return _classification(
                RepositoryAuditReconcileStatus.CORRUPT,
                expected,
                error_code="repository_audit_membership_chain_corrupt",
            )
        previous = successor

    if _head_for_certificate(previous) != canonical.current_head:
        return _classification(
            RepositoryAuditReconcileStatus.CONFLICT,
            expected,
            error_code="repository_audit_membership_head_not_descendant",
        )
    return _classification(
        RepositoryAuditReconcileStatus.EXACT,
        expected,
        evidence=bound,
    )


__all__ = ()
