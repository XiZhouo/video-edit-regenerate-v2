"""Candidate lifecycle — pure logic (adopt / stale / auto-adopt).

Generalises gen-timeline's clip_candidate + editor.candidate stale machinery to
the unified ``candidate`` (scope=region|clip|span). Pure functions over entity
dicts; the hosting skill's commands wrap these into Operations.

Invariants preserved (the ``c6`` candidate-not-overwrite discipline):
- A candidate is written by the machine and adopted by a human.
- A ``stale`` candidate (target edited after the job started) cannot be adopted.
- Only the first candidate on an EMPTY target may auto-adopt; regeneration onto
  an already-adopted target never auto-adopts.
"""

from __future__ import annotations

from typing import Any, Mapping


def can_adopt(candidate: Mapping[str, Any]) -> tuple[bool, str]:
    """Return (ok, reason). Blocks stale candidates and ones with no video."""
    if candidate.get("stale"):
        return False, "候选已过期（目标区间在生成后被改动），不能采纳；请重新生成"
    video_ref = candidate.get("videoRef")
    has_video = bool(video_ref) and (
        isinstance(video_ref, str) or (isinstance(video_ref, Mapping) and video_ref.get("sha256"))
    )
    if not has_video:
        return False, "候选没有可用视频，不能采纳"
    return True, ""


def should_auto_adopt(*, target_has_video: bool, is_regeneration: bool,
                      auto_adopt_enabled: bool = True) -> bool:
    """First successful result on an empty target auto-adopts (confirmation #11).

    Never on regeneration, never on a target that already has adopted video, and
    only when the feature is enabled (可关闭). Auto-adopted result is still a
    candidate — revertible — so judgement is preserved, only the click is saved.
    """
    return bool(auto_adopt_enabled) and not target_has_video and not is_regeneration


def mark_stale_for_target(candidates: list[Mapping[str, Any]], *,
                          target_clip_id: str | None = None,
                          target_span: tuple[float, float] | None = None) -> list[str]:
    """Return ids of non-stale candidates whose target was invalidated by an edit.

    A human edit to a clip/span invalidates any not-yet-adopted candidate bound
    to it (its anchors/boundaries no longer match). The caller flips ``stale`` on
    the returned ids via an Operation.
    """
    invalidated: list[str] = []
    for cand in candidates:
        if cand.get("stale") or cand.get("adopted"):
            continue
        if target_clip_id is not None and cand.get("targetClipId") == target_clip_id:
            invalidated.append(cand["id"])
        elif target_span is not None and _spans_overlap(cand.get("targetSpan"), target_span):
            invalidated.append(cand["id"])
    return invalidated


def _spans_overlap(a: Any, b: tuple[float, float]) -> bool:
    if not (isinstance(a, (list, tuple)) and len(a) == 2):
        return False
    return not (float(a[1]) <= float(b[0]) or float(a[0]) >= float(b[1]))
