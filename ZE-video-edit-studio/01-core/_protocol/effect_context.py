"""Execution-phase context shared by mutation and external-effect ports.

The ContextVar is deliberately a protocol primitive rather than a persistence
implementation detail: low-level Asset/notify/file adapters need to reject a
write while a Command producer is still provisional without importing the
CommitCoordinator layer that owns the phase transition.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from typing import Iterator


_ENTITY_DIR = Path(__file__).resolve().parents[1] / "01-data-structure" / "02-entity"
if str(_ENTITY_DIR) not in sys.path:
    sys.path.insert(0, str(_ENTITY_DIR))

from entity import ZeError  # noqa: E402


class EffectPhase(str, Enum):
    """Closed execution phases relevant to external side-effect ports."""

    IDLE = "idle"
    PRODUCER = "producer"
    AFTER_COMMIT = "after_commit"


_EFFECT_PHASE: ContextVar[EffectPhase] = ContextVar(
    "ze_effect_phase",
    default=EffectPhase.IDLE,
)


def current_effect_phase() -> EffectPhase:
    """Return the phase in the current thread/task context."""
    return _EFFECT_PHASE.get()


def in_producer_phase() -> bool:
    return current_effect_phase() is EffectPhase.PRODUCER


@contextmanager
def effect_phase(phase: EffectPhase | str) -> Iterator[None]:
    """Temporarily enter a phase and restore the exact outer context."""
    try:
        normalized = phase if isinstance(phase, EffectPhase) else EffectPhase(phase)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported effect phase {phase!r}") from exc
    token = _EFFECT_PHASE.set(normalized)
    try:
        yield
    finally:
        _EFFECT_PHASE.reset(token)


def producer_effect_phase() -> Iterator[None]:
    """Context manager used only around CommitCoordinator's producer call."""
    return effect_phase(EffectPhase.PRODUCER)


def after_commit_effect_phase() -> Iterator[None]:
    """Context manager for an accepted commit's ordered ephemeral actions."""
    return effect_phase(EffectPhase.AFTER_COMMIT)


def assert_external_effect_allowed(effect_name: str) -> None:
    """Fail closed when a provisional producer calls an effectful port.

    This guards framework-owned ports; it cannot intercept arbitrary Python I/O.
    Static producer-effect gates complement it by finding direct/helper calls in
    command handlers. ``idle`` and ``after_commit`` are allowed because uploads,
    boot repair and committed actions legitimately write outside a producer.
    """
    name = str(effect_name or "").strip()
    if not name:
        raise ValueError("effect_name must not be empty")
    if in_producer_phase():
        raise ZeError(
            "producer_effect_forbidden",
            f"external effect {name!r} is forbidden while a mutation producer is provisional",
            context={"effect": name, "phase": EffectPhase.PRODUCER.value},
        )


__all__ = [
    "EffectPhase",
    "after_commit_effect_phase",
    "assert_external_effect_allowed",
    "current_effect_phase",
    "effect_phase",
    "in_producer_phase",
    "producer_effect_phase",
]
