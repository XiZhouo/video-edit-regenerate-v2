from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))

from guide_motion import resolve_guide_geometry  # noqa: E402


BASE = {"x": 9, "y": 8, "w": 7, "h": 6}
FRAMES = [
    {"time": 0, "bboxX": 0, "bboxY": 10, "bboxW": 100, "bboxH": 50},
    {"time": 2, "bboxX": 200, "bboxY": 110, "bboxW": 140, "bboxH": 70},
]


def test_no_keyframes_keeps_original_geometry():
    assert resolve_guide_geometry(BASE, [], 1) == BASE


def test_geometry_interpolates_linearly_between_frames():
    assert resolve_guide_geometry(BASE, FRAMES, 1) == {
        "x": 100, "y": 60, "w": 120, "h": 60,
    }


def test_geometry_holds_before_first_and_after_last_frame():
    assert resolve_guide_geometry(BASE, FRAMES, -1) == {
        "x": 0, "y": 10, "w": 100, "h": 50,
    }
    assert resolve_guide_geometry(BASE, FRAMES, 4) == {
        "x": 200, "y": 110, "w": 140, "h": 70,
    }
