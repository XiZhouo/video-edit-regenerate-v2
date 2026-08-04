from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_all as verify  # noqa: E402


def _run_pytest_code(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(verify.ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_slow_gate_accepts_a_legitimate_empty_marker_set(tmp_path):
    (tmp_path / "test_plain.py").write_text(
        "def test_plain():\n    assert True\n",
        encoding="utf-8",
    )
    code = verify._pytest_code(
        (str(tmp_path),),
        marker="slow",
        allow_no_tests=True,
    )
    result = _run_pytest_code(code)
    assert result.returncode == 0, result.stdout + result.stderr


def test_non_slow_pytest_gate_still_rejects_an_empty_target(tmp_path):
    code = verify._pytest_code((str(tmp_path),))
    result = _run_pytest_code(code)
    assert result.returncode == int(pytest.ExitCode.NO_TESTS_COLLECTED)


def test_slow_gate_does_not_hide_a_real_failure(tmp_path):
    (tmp_path / "test_slow_failure.py").write_text(
        "import pytest\n\n@pytest.mark.slow\ndef test_fails():\n    assert False\n",
        encoding="utf-8",
    )
    code = verify._pytest_code(
        (str(tmp_path),),
        marker="slow",
        allow_no_tests=True,
    )
    result = _run_pytest_code(code)
    assert result.returncode == 1


def test_changed_python_leaf_selects_fast_and_slow(monkeypatch):
    monkeypatch.setattr(verify, "_changed_paths", lambda: ["scripts/verify_all.py"])
    skip, targets = verify._changed_selection()
    assert "pytest-fast" not in skip
    assert "pytest-slow" not in skip
    assert targets == ["scripts"]


def test_changed_web_only_skips_python_gates(monkeypatch):
    monkeypatch.setattr(verify, "_changed_paths", lambda: ["webapp/src/App.tsx"])
    skip, _targets = verify._changed_selection()
    assert "pytest-fast" in skip
    assert "pytest-slow" in skip
