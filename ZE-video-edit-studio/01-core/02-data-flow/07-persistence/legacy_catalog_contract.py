"""Pure constants for one historical fenced legacy-catalog seal.

The v1 seal is deliberately evidence about a completed observation, not a
lease on the live legacy source.  In particular, its database representation
can never claim that the observation is still current or that authority may be
cut over.  A later coordinator must add a durable source fence/currentness
protocol before it can issue a cutover-ready proof.

This module owns no filesystem access, SQLite connection, carrier state,
catalog enumeration, authority decision, or clock.
"""

from __future__ import annotations

from legacy_import_contract import (
    MAX_LEGACY_MANIFEST_BYTES,
    MAX_LEGACY_PROJECTS,
    MAX_LEGACY_ROW_SNAPSHOT_BYTES,
)


LEGACY_CATALOG_MANIFEST_SCHEMA = "ze.legacy-catalog.manifest.v1"
LEGACY_CATALOG_SEAL_PROTOCOL = "ze.legacy-catalog.sqlite-seal.v1"
LEGACY_CATALOG_RECEIPT_PROTOCOL = "ze.legacy-catalog.sqlite-receipt.v1"
# Both identities deliberately begin with ``_``.  The v1-v3 generic kernel
# identity alphabet required an alphanumeric first byte, so no healthy older
# database can contain a generic receipt that pre-occupies this future private
# namespace before upgrading to the catalog schema.
LEGACY_CATALOG_TRANSACTION_PREFIX = "_legacy-catalog-seal:"
LEGACY_CATALOG_TRANSACTION_KIND = "_legacy_catalog_seal_v1"
LEGACY_CATALOG_STATUS = "historical_fenced_snapshot"
LEGACY_CATALOG_SELECTION = "complete_live_catalog"
LEGACY_CATALOG_TARGET_SCHEMA_VERSION = 4

# This marker is intentionally independent from ``legacy_import_state``.  A
# historical catalog seal is a stronger, repository-wide observation layered
# on top of an optional import batch; overwriting the import marker would make
# the empty-catalog and non-empty-catalog states ambiguous during recovery.
SQLITE_CATALOG_STATE_KEY = "legacy_catalog_state"
SQLITE_CATALOG_STATE_HISTORICAL_FENCED = "historical_fenced_snapshot_v1"

# This is a protocol constant, not a default that a caller may override.  The
# v1 rows do not prove that the external source fence remains installed, so no
# value other than zero is representable in the schema.
LEGACY_CATALOG_CUTOVER_READY = 0

MAX_LEGACY_CATALOG_PROJECTS = MAX_LEGACY_PROJECTS
MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES = 64 * 1024 * 1024
# Every scanner-issued source head is independently bounded at 64 MiB.  The
# catalog field is their aggregate, so reusing the 1 GiB normalized-row budget
# would reject a legal sparse catalog whose formatted source heads are larger
# than their deterministic snapshots.
MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES = (
    MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES * MAX_LEGACY_PROJECTS
)
MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES = MAX_LEGACY_ROW_SNAPSHOT_BYTES
MAX_LEGACY_CATALOG_MANIFEST_BYTES = MAX_LEGACY_MANIFEST_BYTES


__all__ = [
    "LEGACY_CATALOG_CUTOVER_READY",
    "LEGACY_CATALOG_MANIFEST_SCHEMA",
    "LEGACY_CATALOG_RECEIPT_PROTOCOL",
    "LEGACY_CATALOG_SEAL_PROTOCOL",
    "LEGACY_CATALOG_SELECTION",
    "LEGACY_CATALOG_STATUS",
    "LEGACY_CATALOG_TARGET_SCHEMA_VERSION",
    "LEGACY_CATALOG_TRANSACTION_KIND",
    "LEGACY_CATALOG_TRANSACTION_PREFIX",
    "MAX_LEGACY_CATALOG_MANIFEST_BYTES",
    "MAX_LEGACY_CATALOG_PROJECTS",
    "MAX_LEGACY_CATALOG_PROJECT_SOURCE_HEAD_BYTES",
    "MAX_LEGACY_CATALOG_SOURCE_HEAD_BYTES",
    "MAX_LEGACY_CATALOG_TARGET_SNAPSHOT_BYTES",
    "SQLITE_CATALOG_STATE_HISTORICAL_FENCED",
    "SQLITE_CATALOG_STATE_KEY",
]
