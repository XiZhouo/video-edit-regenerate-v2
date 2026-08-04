"""Pure timeline math — no framework, no network, no filesystem.

Ported and generalised from ZE-video-generation-timeline's ``clip_math.py``.
Only the genuinely generic timeline functions live here (the report flagged
``clamp_seedance_duration`` as a provider concern, so it moved to
``videogen_client``). ``dict``/``number`` in, ``dict``/``number`` out.
"""

from __future__ import annotations

from typing import Any

MAX_TIMELINE_TRACKS = 64  # tracks are a bounded 0..63 integer field, not entities


def is_timeline_track(value: Any) -> bool:
    """True iff ``value`` is an int in ``0..MAX_TIMELINE_TRACKS-1`` (bool rejected)."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < MAX_TIMELINE_TRACKS


def require_timeline_track(value: Any) -> int:
    if not is_timeline_track(value):
        raise ValueError(f"track must be an int in 0..{MAX_TIMELINE_TRACKS - 1}, got {value!r}")
    return value


def clamp_mark_range(mark_in: float | None, mark_out: float | None,
                     duration: float) -> tuple[float | None, float | None]:
    """Clamp a mark in/out pair to ``[0, duration]``, sorted so in <= out.

    A single end (the other ``None``) is allowed and passes through clamped.
    """
    def _clamp(v: float | None) -> float | None:
        if v is None:
            return None
        return max(0.0, min(float(v), float(duration)))

    lo, hi = _clamp(mark_in), _clamp(mark_out)
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return lo, hi


def track_end(clips: list[dict], track: int) -> float:
    """Right edge (timeline end) of the last clip on ``track`` (empty → 0.0).

    Null/type-safe throughout (round2 swallows None and junk) — a clip with a
    missing start/duration must not crash timeline math.
    """
    end = 0.0
    for clip in clips:
        if clip.get("trackId") == track or clip.get("track") == track:
            tl = clip.get("timeline") or {}
            clip_end = tl.get("end")
            if clip_end is None:
                clip_end = round2(clip.get("start")) + round2(clip.get("duration"))
            end = max(end, round2(clip_end))
    return round2(end)


def round2(value: Any) -> float:
    """Round to 2 decimal-place seconds; bad input → 0.0."""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def interval_union(intervals: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Union bounding interval over ``intervals`` (min t0, max t1); [] → None.

    The temporal analog of S2D's region-bbox union: participating annotation
    intervals collapse to one send interval.
    """
    if not intervals:
        return None
    t0 = min(float(a) for a, _ in intervals)
    t1 = max(float(b) for _, b in intervals)
    return (round2(t0), round2(t1))


def pad_interval(interval: tuple[float, float], padding_sec: float,
                 total_duration: float) -> tuple[float, float]:
    """Expand an interval by ``padding_sec`` each side, clamped to [0, total].

    A degenerate single-frame interval [ta, ta] is stretched open by padding so
    a real send segment exists (edit-studio interval discipline).
    """
    t0, t1 = interval
    t0 = max(0.0, float(t0) - float(padding_sec))
    t1 = min(float(total_duration), float(t1) + float(padding_sec))
    if t1 < t0:
        t1 = t0
    return (round2(t0), round2(t1))


def span_anchor_frames(span: tuple[float, float], total_duration: float,
                       fps: float) -> dict[str, float | None]:
    """Outer-edge anchor frames for a span regeneration.

    The regenerated span must re-join the untouched footage on either side, so
    the anchors are the nearest ORIGINAL frames *outside* the span (span-1 frame
    before, span+1 frame after) — never the span's own first/last frames, which
    get remade. Returns ``{head, tail}`` in seconds, ``None`` at the boundaries.
    """
    t0, t1 = float(span[0]), float(span[1])
    frame = 1.0 / max(fps, 1)
    head = round2(t0 - frame) if t0 - frame >= 0 else None
    tail = round2(t1 + frame) if t1 + frame <= total_duration else None
    return {"head": head, "tail": tail}


def safe_export_filename(name: str, *, fallback: str = "video", ext: str = ".mp4") -> str:
    """Strip directory components, reject '.'/'..'/empty, force ``ext``."""
    base = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if base in ("", ".", ".."):
        base = fallback
    if not base.lower().endswith(ext):
        stem = base[: -len("." + base.rsplit(".", 1)[-1])] if "." in base else base
        base = (stem or fallback) + ext
    return base
