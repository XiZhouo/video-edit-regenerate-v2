"""Pure wire identity for one SQLite business bootstrap baseline.

The cutover coordinator is added later.  Keeping this tiny descriptor contract
separate now prevents a baseline receipt from being hashed in the ordinary
change-commit domain and gives the future issuer and the rooted reader one
exact source of truth.
"""

from __future__ import annotations

import hashlib


BUSINESS_BASELINE_MANIFEST_SCHEMA = "ze.business-baseline.v1"
BUSINESS_BASELINE_MANIFEST_PROTOCOL = "ze.business-baseline.v1"
BUSINESS_BASELINE_MANIFEST_HASH_DOMAIN = (
    b"ze.business-baseline.manifest-digest.v1"
)


def digest_business_baseline_manifest(payload: bytes) -> bytes:
    """Return the framed, domain-separated digest of exact manifest bytes."""

    if type(payload) is not bytes or not payload:
        raise TypeError("payload must be non-empty exact bytes")
    hasher = hashlib.sha256()
    for value in (BUSINESS_BASELINE_MANIFEST_HASH_DOMAIN, payload):
        hasher.update(len(value).to_bytes(8, "big"))
        hasher.update(value)
    return hasher.digest()


__all__ = (
    "BUSINESS_BASELINE_MANIFEST_HASH_DOMAIN",
    "BUSINESS_BASELINE_MANIFEST_PROTOCOL",
    "BUSINESS_BASELINE_MANIFEST_SCHEMA",
    "digest_business_baseline_manifest",
)
