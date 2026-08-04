"""idempotency.py — request-level replay ledger for ZE runtime writes.

The store is intentionally separate from OpLog: OpLog audits facts after a
command executes, while this ledger decides whether a user request should run
or replay a previous response.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import _paths  # noqa: F401  (side-effect: leaf path injection)
from entity import ZeError  # noqa: E402
from effect_context import assert_external_effect_allowed  # noqa: E402


_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,200}\Z")
_SCHEMA_VERSION = 1


def canonical_request_hash(payload: Any) -> str:
    """Stable sha256 for JSON-like request payloads."""
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class IdempotencyStore:
    """Small JSON-backed ledger keyed by (scope, idempotency key).

    The current runtime is single-process, so an in-process RLock only prevents
    duplicate owners inside one process. Records are written atomically so
    successful replays survive restarts. If an owner disappears while a record
    is ``in_progress``, expiry changes it to ``outcome_unknown`` and blocks
    replay; TTL is never treated as proof that the business mutation did not
    commit. W06 replaces this file ledger with the business transaction.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        in_progress_ttl_sec: int = 1800,
        retention_sec: int = 7 * 86400,
        max_records: int = 10000,
        retain_failed_as_unknown: bool = False,
    ) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._in_progress_ttl_sec = max(30, int(in_progress_ttl_sec))
        self._retention_sec = max(self._in_progress_ttl_sec, int(retention_sec))
        self._max_records = max(1, int(max_records))
        self._retain_failed_as_unknown = bool(retain_failed_as_unknown)

    @property
    def path(self) -> Path:
        return self._path

    def run(
        self,
        *,
        scope: str,
        key: str | None,
        request_payload: Any,
        action: Callable[[], dict[str, Any]],
        outcome_unknown_codes: tuple[str, ...] = (),
        outcome_unknown_classifier: Callable[[BaseException], bool] | None = None,
    ) -> dict[str, Any]:
        """Run action once for (scope,key), replaying only a proven success.

        Missing keys retain the legacy direct-call behavior. A stale owner,
        an EffectLedger exception, or a typed classifier match creates an
        outcome-unknown repair tombstone, because automatically running the
        action again could duplicate a committed mutation or external send.
        """
        key = (key or "").strip()
        if not key:
            return action()
        if not _KEY_RE.fullmatch(key):
            raise ZeError(
                "idempotency_key_invalid",
                "Idempotency-Key 只能包含字母数字、点、下划线、冒号、短横线,长度 1-200",
                context={"key": key[:32]},
            )

        request_hash = canonical_request_hash(request_payload)
        record_id = self._record_id(scope, key)
        now = time.time()

        with self._lock:
            state = self._load_unlocked()
            records = state.setdefault("records", {})
            pruned = self._prune_unlocked(records, now)
            rec = records.get(record_id)
            if rec:
                if rec.get("request_hash") != request_hash:
                    raise ZeError(
                        "idempotency_key_reused",
                        "同一个 Idempotency-Key 不能用于不同请求体",
                        context={"scope": scope, "key": key},
                    )
                status = rec.get("status")
                if status == "succeeded":
                    if pruned:
                        self._save_unlocked(state)
                    return copy.deepcopy(rec.get("response") or {})
                if status == "outcome_unknown":
                    if pruned:
                        self._save_unlocked(state)
                    raise ZeError(
                        "idempotency_outcome_unknown",
                        "同一个 Idempotency-Key 的既有请求结果无法确认,禁止自动重放",
                        context={"scope": scope, "key": key, "repair_required": True},
                    )
                if status == "in_progress":
                    if pruned:
                        self._save_unlocked(state)
                    raise ZeError(
                        "idempotency_in_progress",
                        "同一个 Idempotency-Key 对应的请求仍在执行中",
                        context={"scope": scope, "key": key},
                    )

            records[record_id] = {
                "schema_version": _SCHEMA_VERSION,
                "scope": scope,
                "key": key,
                "request_hash": request_hash,
                "status": "in_progress",
                "created_at": now,
                "updated_at": now,
            }
            self._save_unlocked(state)

            try:
                response = action()
            except Exception as exc:
                state = self._load_unlocked()
                records = state.setdefault("records", {})
                error_code = str(getattr(exc, "code", "") or "effect_outcome_unknown")
                classified_unknown = False
                if outcome_unknown_classifier is not None:
                    try:
                        classified_unknown = (
                            outcome_unknown_classifier(exc) is True
                        )
                    except BaseException:
                        # A broken classifier must never re-authorize replay of
                        # an action whose durability is currently unclassified.
                        classified_unknown = True
                retain_as_unknown = (
                    self._retain_failed_as_unknown
                    or error_code in outcome_unknown_codes
                    or classified_unknown
                )
                if retain_as_unknown:
                    rec = records.setdefault(record_id, {})
                    rec.update({
                        "schema_version": _SCHEMA_VERSION,
                        "scope": scope,
                        "key": key,
                        "request_hash": request_hash,
                        "status": "outcome_unknown",
                        "error_code": error_code,
                        "error_type": type(exc).__name__,
                        "updated_at": time.time(),
                    })
                else:
                    records.pop(record_id, None)
                self._save_unlocked(state)
                raise

            state = self._load_unlocked()
            rec = state.setdefault("records", {}).setdefault(record_id, {})
            rec.update({
                "schema_version": _SCHEMA_VERSION,
                "scope": scope,
                "key": key,
                "request_hash": request_hash,
                "status": "succeeded",
                "response": copy.deepcopy(response),
                "updated_at": time.time(),
            })
            self._prune_unlocked(state.setdefault("records", {}), time.time())
            self._save_unlocked(state)
            return response

    def _record_id(self, scope: str, key: str) -> str:
        return hashlib.sha256(
            json.dumps([scope, key], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    def _prune_unlocked(self, records: dict[str, Any], now: float) -> bool:
        changed = False
        for rid, rec in list(records.items()):
            updated_at = float(rec.get("updated_at") or rec.get("created_at") or 0)
            status = rec.get("status")
            if status == "in_progress":
                if now - updated_at >= self._in_progress_ttl_sec:
                    # A crashed owner may have committed the business mutation or
                    # external effect before writing its response.  TTL expiry is
                    # therefore a repair signal, never permission to replay.
                    rec["status"] = "outcome_unknown"
                    rec["error_code"] = "idempotency_owner_lost"
                    rec["error_type"] = "OwnerLost"
                    rec["outcome_unknown_at"] = now
                    changed = True
                continue
            if status == "outcome_unknown":
                # Unknown outcomes are safety tombstones.  W06 moves them to the
                # transactional request ledger with an explicit repair workflow;
                # silently pruning one would re-authorize a duplicate mutation.
                continue
            if now - updated_at >= self._retention_sec:
                records.pop(rid, None)
                changed = True
        if len(records) <= self._max_records:
            return changed
        ordered = sorted(
            records.items(),
            key=lambda item: float(item[1].get("updated_at") or item[1].get("created_at") or 0),
        )
        fresh_in_progress_cutoff = now - self._in_progress_ttl_sec
        for rid, rec in ordered:
            if len(records) <= self._max_records:
                break
            updated_at = float(rec.get("updated_at") or rec.get("created_at") or 0)
            if rec.get("status") == "outcome_unknown":
                continue
            if rec.get("status") == "in_progress" and updated_at > fresh_in_progress_cutoff:
                continue
            records.pop(rid, None)
            changed = True
        return changed

    def _load_unlocked(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": _SCHEMA_VERSION, "records": {}}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ZeError(
                "idempotency_store_corrupt",
                f"幂等账本损坏: {self._path}",
                context={"path": str(self._path), "error": str(e)},
            ) from e
        if not isinstance(raw, dict):
            raise ZeError("idempotency_store_corrupt", f"幂等账本不是对象: {self._path}",
                          context={"path": str(self._path)})
        raw.setdefault("schema_version", _SCHEMA_VERSION)
        raw.setdefault("records", {})
        return raw

    def _save_unlocked(self, state: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)


class EffectLedger:
    """Local ledger for explicit external side-effect idempotency keys."""

    def __init__(self, path: str | Path, **kwargs: Any) -> None:
        # External effects can succeed remotely and still lose their response.
        # On any exception retain a repair tombstone instead of authorizing an
        # automatic second send.
        kwargs.setdefault("retain_failed_as_unknown", True)
        self._store = IdempotencyStore(path, **kwargs)

    @property
    def path(self) -> Path:
        return self._store.path

    def run(
        self,
        *,
        scope: str,
        key: str | None,
        effect_payload: Any,
        action: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        assert_external_effect_allowed(f"effect_ledger:{scope}")
        return self._store.run(
            scope=f"effect:{scope}",
            key=key,
            request_payload=effect_payload,
            action=action,
        )
