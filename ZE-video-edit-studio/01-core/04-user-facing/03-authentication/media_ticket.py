"""Short-lived, principal-bound capabilities for browser-native media loads.

Session tokens are account-wide credentials and must never be copied into an
``<img>``/``<video>`` URL.  A media ticket is deliberately much narrower: it is
bound to one opaque principal fingerprint, tool, project, topology generation
and asset checksum, and expires quickly.

The signer derives a domain-separated key from the existing durable credential
master key.  Consequently all workers configured for the same ZE auth domain
share tickets without introducing another rotation boundary.  Missing,
malformed or permission-unsafe key material fails closed in ``credentials``;
there is intentionally no per-process random fallback.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from credentials import _load_master_key  # domain-separated below; never exposed
from entity import ZeError


MEDIA_TICKET_VERSION = 1
DEFAULT_MEDIA_TICKET_TTL_SEC = 120
MAX_MEDIA_TICKET_TTL_SEC = 300
_CLOCK_SKEW_SEC = 5
_MAX_TICKET_LENGTH = 4096
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SUBJECT_RE = re.compile(r"[0-9a-f]{64}\Z")
_PAYLOAD_KEYS = frozenset(("v", "iss", "sub", "pid", "gen", "sha", "iat", "exp"))


@dataclass(frozen=True)
class MediaTicketClaims:
    issuer: str
    subject: str
    project_id: str
    generation: int
    sha256: str
    issued_at: int
    expires_at: int


def _invalid() -> ZeError:
    # One public error prevents format/expiry/signature oracles from becoming a
    # useful distinction.  Operators can diagnose stable-key problems at boot.
    return ZeError("media_ticket_invalid", "media ticket is invalid or expired")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or any(char.isspace() for char in value):
        raise ValueError("invalid base64url")
    padded = value + "=" * (-len(value) % 4)
    decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    # Reject alternate encodings that change only unused padding bits.  Without
    # this canonicality check, mutating a ticket's last character can sometimes
    # decode to the exact same signature bytes and appear to survive tampering.
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class MediaTicketSigner:
    """Issue and verify one deployment's opaque, asset-scoped media tickets."""

    def __init__(
        self,
        *,
        tool_id: str,
        key_path: str | Path | None = None,
        master_key: bytes | None = None,
        ttl_sec: int = DEFAULT_MEDIA_TICKET_TTL_SEC,
    ) -> None:
        issuer = str(tool_id or "").strip()
        if not issuer or len(issuer) > 256 or any(c in issuer for c in "\x00\r\n"):
            raise ZeError("validation_failed", "invalid media ticket issuer")
        if (
            not isinstance(ttl_sec, int) or isinstance(ttl_sec, bool)
            or ttl_sec <= 0 or ttl_sec > MAX_MEDIA_TICKET_TTL_SEC
        ):
            raise ZeError("validation_failed", "invalid media ticket ttl")
        if master_key is None:
            master = _load_master_key(Path(key_path) if key_path is not None else None)
        else:
            master = bytes(master_key)
            if len(master) < 32:
                raise ZeError("validation_failed", "media ticket master key is too short")
        self._issuer = issuer
        self._ttl_sec = ttl_sec
        self._key = hmac.new(
            master[:32],
            b"ze.media-ticket.signing.v1\0" + issuer.encode("utf-8"),
            hashlib.sha256,
        ).digest()

    @property
    def ttl_sec(self) -> int:
        return self._ttl_sec

    def subject_for(self, principal_id: str) -> str:
        principal = str(principal_id or "")
        if not principal or len(principal) > 512 or any(c in principal for c in "\x00\r\n"):
            raise ZeError("validation_failed", "invalid media ticket principal")
        return hmac.new(
            self._key,
            b"principal.v1\0" + principal.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def expires_at(self, *, now: float | None = None) -> int:
        """Return the authoritative expiry for one mint batch.

        A batch may legitimately contain only missing assets.  It still needs a
        short negative-cache lifetime so clients do not turn absence into a hot
        retry loop.  Callers can pass the same ``now`` to :meth:`issue` so every
        positive and negative result in the batch shares one refresh boundary.
        """
        current = time.time() if now is None else now
        if (
            not isinstance(current, (int, float)) or isinstance(current, bool)
            or not math.isfinite(current)
        ):
            raise ZeError("validation_failed", "invalid media ticket clock")
        return int(current) + self._ttl_sec

    @staticmethod
    def _validate_binding(project_id: str, generation: int, sha256: str) -> None:
        if (
            not isinstance(project_id, str) or not project_id
            or len(project_id) > 256 or any(c in project_id for c in "\x00\r\n")
        ):
            raise ZeError("validation_failed", "invalid media ticket project")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
            raise ZeError("validation_failed", "invalid media ticket generation")
        if not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256):
            raise ZeError("validation_failed", "invalid media ticket asset checksum")

    def issue(
        self,
        *,
        principal_id: str,
        project_id: str,
        generation: int,
        sha256: str,
        now: float | None = None,
    ) -> tuple[str, int]:
        self._validate_binding(project_id, generation, sha256)
        current = time.time() if now is None else now
        expires_at = self.expires_at(now=current)
        issued_at = expires_at - self._ttl_sec
        payload = {
            "v": MEDIA_TICKET_VERSION,
            "iss": self._issuer,
            "sub": self.subject_for(principal_id),
            "pid": project_id,
            "gen": generation,
            "sha": sha256,
            "iat": issued_at,
            "exp": expires_at,
        }
        body = _canonical_payload(payload)
        signature = hmac.new(self._key, body, hashlib.sha256).digest()
        return f"mt1.{_b64encode(body)}.{_b64encode(signature)}", expires_at

    def verify(
        self,
        ticket: str,
        *,
        project_id: str,
        generation: int,
        sha256: str,
        now: float | None = None,
    ) -> MediaTicketClaims:
        self._validate_binding(project_id, generation, sha256)
        try:
            if not isinstance(ticket, str) or len(ticket) > _MAX_TICKET_LENGTH:
                raise ValueError("invalid ticket length")
            prefix, body64, signature64 = ticket.split(".")
            if prefix != "mt1":
                raise ValueError("invalid ticket prefix")
            body = _b64decode(body64)
            signature = _b64decode(signature64)
            expected = hmac.new(self._key, body, hashlib.sha256).digest()
            if len(signature) != len(expected) or not hmac.compare_digest(signature, expected):
                raise ValueError("invalid signature")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict) or frozenset(payload) != _PAYLOAD_KEYS:
                raise ValueError("invalid payload shape")
            if _canonical_payload(payload) != body:
                raise ValueError("non-canonical payload")
            if payload.get("v") != MEDIA_TICKET_VERSION or payload.get("iss") != self._issuer:
                raise ValueError("wrong ticket domain")
            subject = payload.get("sub")
            if not isinstance(subject, str) or not _SUBJECT_RE.fullmatch(subject):
                raise ValueError("invalid subject")
            if (
                payload.get("pid") != project_id
                or payload.get("gen") != generation
                or payload.get("sha") != sha256
            ):
                raise ValueError("ticket binding mismatch")
            issued_at = payload.get("iat")
            expires_at = payload.get("exp")
            if (
                not isinstance(issued_at, int) or isinstance(issued_at, bool)
                or not isinstance(expires_at, int) or isinstance(expires_at, bool)
                or expires_at <= issued_at
                or expires_at - issued_at > MAX_MEDIA_TICKET_TTL_SEC
            ):
                raise ValueError("invalid lifetime")
            current_raw = time.time() if now is None else now
            if (
                not isinstance(current_raw, (int, float)) or isinstance(current_raw, bool)
                or not math.isfinite(current_raw)
            ):
                raise ValueError("invalid clock")
            current = int(current_raw)
            if issued_at > current + _CLOCK_SKEW_SEC or expires_at <= current:
                raise ValueError("expired ticket")
            return MediaTicketClaims(
                issuer=self._issuer,
                subject=subject,
                project_id=project_id,
                generation=generation,
                sha256=sha256,
                issued_at=issued_at,
                expires_at=expires_at,
            )
        except ZeError:
            raise
        except Exception as exc:  # all public verification failures are identical
            raise _invalid() from exc


__all__ = [
    "DEFAULT_MEDIA_TICKET_TTL_SEC",
    "MAX_MEDIA_TICKET_TTL_SEC",
    "MEDIA_TICKET_VERSION",
    "MediaTicketClaims",
    "MediaTicketSigner",
]
