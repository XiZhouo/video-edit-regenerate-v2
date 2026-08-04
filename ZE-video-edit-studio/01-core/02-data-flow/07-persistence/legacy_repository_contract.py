"""Pure constants for the v5 legacy-repository proof substrate.

The proof closes one durable repository epoch/revision over both live and
deleted project identities and binds it to the already-issued v4 historical
catalog seal.  It deliberately remains ``authority=legacy`` and can never
represent the production authority flip.  A later coordinator/schema must
perform that separately with an exact cross-store CAS.

This module owns no filesystem, SQLite, SDK, clock, writer admission, or
authority-transition capability.
"""

from __future__ import annotations

from legacy_catalog_contract import (
    MAX_LEGACY_CATALOG_MANIFEST_BYTES,
    MAX_LEGACY_CATALOG_PROJECTS,
    MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES,
    MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES,
    MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES,
)


LEGACY_REPOSITORY_MANIFEST_SCHEMA = "ze.legacy-repository.manifest.v1"
LEGACY_REPOSITORY_PROOF_PROTOCOL = "ze.legacy-repository.sqlite-proof.v1"
LEGACY_REPOSITORY_RECEIPT_PROTOCOL = "ze.legacy-repository.sqlite-receipt.v1"

# The generic v1-v4 identity grammar requires an alphanumeric first byte.
# These private identities therefore cannot be preoccupied by any healthy
# generic receipt issued before the v5 migration exists.
LEGACY_REPOSITORY_TRANSACTION_PREFIX = "_legacy-repository-seal:"
LEGACY_REPOSITORY_TRANSACTION_KIND = "_legacy_repository_seal_v1"

LEGACY_REPOSITORY_STATUS = "cutover_prepared"
LEGACY_REPOSITORY_SELECTION = "complete_repository_state"
LEGACY_REPOSITORY_AUTHORITY = "legacy"
LEGACY_REPOSITORY_AUTHORITY_STATE = "cutover_prepared"
LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION = 5
LEGACY_REPOSITORY_CUTOVER_READY = 0

LEGACY_REPOSITORY_MEMBER_LIVE = "live"
LEGACY_REPOSITORY_MEMBER_DELETED = "deleted"
LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID = "legacy-adopt"

MAX_LEGACY_REPOSITORY_PROJECTS = MAX_LEGACY_CATALOG_PROJECTS
MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES = (
    MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES
)
MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES = MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES
MAX_LEGACY_REPOSITORY_TARGET_SNAPSHOT_BYTES = MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES
MAX_LEGACY_REPOSITORY_MANIFEST_BYTES = MAX_LEGACY_CATALOG_MANIFEST_BYTES
MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES = 256

# The repository manifest root contains 26 keys, 25 scalar values and one
# members-list value: 53 serialized nodes including the root object/list.
# Every member is one object with 15 keys and 15 scalar/null values: 31 nodes.
# Keep this formula adjacent to the manifest shape; a field addition must
# update both the constants and the 10k boundary test rather than borrowing an
# unrelated importer quota that is too small for this richer descriptor.
LEGACY_REPOSITORY_MANIFEST_ROOT_NODES = 53
LEGACY_REPOSITORY_MANIFEST_MEMBER_NODES = 31
MAX_LEGACY_REPOSITORY_MANIFEST_NODES = (
    LEGACY_REPOSITORY_MANIFEST_ROOT_NODES
    + LEGACY_REPOSITORY_MANIFEST_MEMBER_NODES * MAX_LEGACY_REPOSITORY_PROJECTS
)
assert MAX_LEGACY_REPOSITORY_MANIFEST_NODES == 310_053
assert (
    LEGACY_REPOSITORY_MANIFEST_ROOT_NODES
    + LEGACY_REPOSITORY_MANIFEST_MEMBER_NODES * MAX_LEGACY_REPOSITORY_PROJECTS
    == MAX_LEGACY_REPOSITORY_MANIFEST_NODES
)


__all__ = [
    "LEGACY_REPOSITORY_AUTHORITY",
    "LEGACY_REPOSITORY_AUTHORITY_STATE",
    "LEGACY_REPOSITORY_CUTOVER_READY",
    "LEGACY_REPOSITORY_LEGACY_ADOPT_OPERATION_ID",
    "LEGACY_REPOSITORY_MANIFEST_SCHEMA",
    "LEGACY_REPOSITORY_MANIFEST_MEMBER_NODES",
    "LEGACY_REPOSITORY_MANIFEST_ROOT_NODES",
    "LEGACY_REPOSITORY_MEMBER_DELETED",
    "LEGACY_REPOSITORY_MEMBER_LIVE",
    "LEGACY_REPOSITORY_PROOF_PROTOCOL",
    "LEGACY_REPOSITORY_RECEIPT_PROTOCOL",
    "LEGACY_REPOSITORY_SELECTION",
    "LEGACY_REPOSITORY_STATUS",
    "LEGACY_REPOSITORY_TARGET_SCHEMA_VERSION",
    "LEGACY_REPOSITORY_TRANSACTION_KIND",
    "LEGACY_REPOSITORY_TRANSACTION_PREFIX",
    "MAX_LEGACY_REPOSITORY_MANIFEST_BYTES",
    "MAX_LEGACY_REPOSITORY_MANIFEST_NODES",
    "MAX_LEGACY_REPOSITORY_PROJECTS",
    "MAX_LEGACY_REPOSITORY_PROJECT_SOURCE_STATE_BYTES",
    "MAX_LEGACY_REPOSITORY_RUNTIME_GENERATION_BYTES",
    "MAX_LEGACY_REPOSITORY_SOURCE_STATE_BYTES",
    "MAX_LEGACY_REPOSITORY_TARGET_SNAPSHOT_BYTES",
]
