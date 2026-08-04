"""test_run_status — EntityRunStatus 多 stage 加性字段 + 字节兼容(P2a)。

红线:单 stage(默认值)的 to_dict() 键集必须与 P2 前完全一致 —— 否则既有
前端解析 / 测试快照 / SSE 字节回归。多 stage 字段只在非默认时出现。
"""
from __future__ import annotations

import pytest

from run_status import EntityRunStatus, StageStatus

# P2 前的固定键集(单 stage payload 必须正好这些键)
_LEGACY_KEYS = {
    "entity_type", "entity_id", "phase", "progress",
    "run_id", "stage_label", "error", "updated_at",
}


def test_single_stage_payload_byte_compat():
    s = EntityRunStatus(entity_type="t", entity_id="e", phase="running", progress=0.5)
    d = s.to_dict()
    assert set(d.keys()) == _LEGACY_KEYS          # 无新键泄漏
    assert "stages" not in d and "stage_index" not in d and "eta_sec" not in d
    assert "placeholder" not in d


def test_placeholder_is_explicit_true_only_and_default_stays_absent():
    status = EntityRunStatus(
        entity_type="t", entity_id="e", phase="pending", placeholder=True,
    )
    assert status.to_dict()["placeholder"] is True

    ordinary = EntityRunStatus(
        entity_type="t", entity_id="ordinary", phase="pending", placeholder=False,
    )
    assert "placeholder" not in ordinary.to_dict()


@pytest.mark.parametrize("invalid", [1, 0, "true", None])
def test_placeholder_requires_exact_bool(invalid):
    with pytest.raises(TypeError, match="exact bool"):
        EntityRunStatus(
            entity_type="t", entity_id="e", phase="pending", placeholder=invalid,
        )


def test_multi_stage_fields_appear_only_when_set():
    st = StageStatus(stage_id="gen", label="GEN", phase="running", progress=0.3, eta_sec=12.0)
    s = EntityRunStatus(
        entity_type="t", entity_id="e", phase="running",
        stages=(st,), stage_index=0, eta_sec=20.0,
    )
    d = s.to_dict()
    assert d["stage_index"] == 0
    assert d["eta_sec"] == 20.0
    assert d["stages"] == [{
        "stage_id": "gen", "label": "GEN", "phase": "running",
        "progress": 0.3, "eta_sec": 12.0, "throughput": None,
    }]


def test_stage_index_zero_is_emitted_not_omitted():
    # 0 是合法序号,不能被当默认省略(None 才省略)
    s = EntityRunStatus(entity_type="t", entity_id="e", phase="running", stage_index=0)
    assert s.to_dict()["stage_index"] == 0


def test_invalid_phase_rejected():
    with pytest.raises(ValueError):
        EntityRunStatus(entity_type="t", entity_id="e", phase="bogus")
    with pytest.raises(ValueError):
        StageStatus(stage_id="s", label="S", phase="bogus")
