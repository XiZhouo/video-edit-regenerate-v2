"""钉子表:域层的落位算式 == 前端的落位算式。

界面上画的变换手柄必须落在画面真正占的那块地方,而"真正占哪块"是渲染时 ffmpeg 算的。
两边算错一点,人拖的框和出来的画面就对不上 —— 这种错只有导出之后才看得见。

所以两侧各盯同一张表:
  · 这条测试:域层 `clip_geometry.canvas_rect_of` 算出来 == 表(改域层不改表 → 红)
  · `renderers/clipGeometry.test.ts`:前端 `canvasRectOf` 算出来 == 同一张表

表本身由这条测试生成(``--write``),日常只校验。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "02-mod" / "05-code"))

from _domain import clip_geometry  # noqa: E402

FIXTURE = Path(__file__).parent / "renderers" / "__fixtures__" / "clip_geometry_cases.json"


def _cases() -> list[dict]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def test_fixture_exists_and_covers_every_fit_mode():
    """表不许退化成只测默认那一种 —— 四种适配方式都得在。"""
    cases = _cases()
    assert len(cases) >= 10
    modes = {str(c["clip"].get("fitMode") or "contain") for c in cases}
    for want in ("contain", "cover", "stretch", "none"):
        assert want in modes, f"钉子表没有覆盖 fitMode={want}"


@pytest.mark.parametrize("case", _cases(), ids=lambda c: c["name"])
def test_domain_matches_fixture(case):
    got = clip_geometry.canvas_rect_of(
        case["clip"], width=case["canvas"][0], height=case["canvas"][1],
        src_w=case["src"][0], src_h=case["src"][1])
    for key, want in case["rect"].items():
        assert round(got[key], 4) == pytest.approx(want, abs=1e-4), (
            f"{case['name']} 的 {key}:域层算出 {got[key]},表里是 {want} —— "
            f"改了算式就得同时更新表和前端 clipGeometry.ts")
