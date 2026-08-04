"""Pure wire and resource constants for the legacy-import bridge.

This module deliberately owns no carrier ledger, filesystem scan, SQLite
connection, migration execution, or authority decision.  The process-local
preparation seam consumes these constants now; the future durable SQLite
adapter must consume the same definitions so their protocol names and resource
ceilings cannot drift independently.
"""

from __future__ import annotations


LEGACY_PAYLOAD_PROTOCOL = "ze.legacy-import.payload.v1"
LEGACY_MANIFEST_SCHEMA = "ze.legacy-import.manifest.v1"
LEGACY_BATCH_PROTOCOL = "ze.legacy-import.batch.v1"
LEGACY_IMPORT_PLAN_PROTOCOL = "ze.legacy-import.verified-plan.v1"
PREPARED_PAYLOAD_STATUS = "prepared_verified_payload"

SQLITE_IMPORT_RECEIPT_PROTOCOL = "ze.legacy-import.sqlite-receipt.v1"
SQLITE_IMPORT_TRANSACTION_PREFIX = "legacy-import:"
SQLITE_IMPORT_TRANSACTION_KIND = "legacy_import_v1"
SQLITE_IMPORT_TERMINAL_STATUS = "imported"
SQLITE_IMPORT_STATE_KEY = "legacy_import_state"
SQLITE_IMPORT_STATE_TERMINAL_SHADOW = "terminal_shadow_v1"
SQLITE_IMPORT_RECONCILE_PROTOCOL = "ze.legacy-import.sqlite-reconcile.v1"

MAX_LEGACY_PROJECTS = 10_000
MAX_LEGACY_ENTITIES = 1_000_000
MAX_LEGACY_ROW_SNAPSHOT_BYTES = 1024 * 1024 * 1024
MAX_LEGACY_MANIFEST_BYTES = 64 * 1024 * 1024
# Closed manifest root: about 23 value/key nodes; each ten-field project
# descriptor: 21.  Ten thousand descriptors therefore remain below 250,000.
MAX_LEGACY_MANIFEST_NODES = 250_000


__all__ = [
    "LEGACY_BATCH_PROTOCOL",
    "LEGACY_IMPORT_PLAN_PROTOCOL",
    "LEGACY_MANIFEST_SCHEMA",
    "LEGACY_PAYLOAD_PROTOCOL",
    "MAX_LEGACY_ENTITIES",
    "MAX_LEGACY_MANIFEST_BYTES",
    "MAX_LEGACY_MANIFEST_NODES",
    "MAX_LEGACY_PROJECTS",
    "MAX_LEGACY_ROW_SNAPSHOT_BYTES",
    "PREPARED_PAYLOAD_STATUS",
    "SQLITE_IMPORT_RECEIPT_PROTOCOL",
    "SQLITE_IMPORT_RECONCILE_PROTOCOL",
    "SQLITE_IMPORT_STATE_KEY",
    "SQLITE_IMPORT_STATE_TERMINAL_SHADOW",
    "SQLITE_IMPORT_TERMINAL_STATUS",
    "SQLITE_IMPORT_TRANSACTION_KIND",
    "SQLITE_IMPORT_TRANSACTION_PREFIX",
]
