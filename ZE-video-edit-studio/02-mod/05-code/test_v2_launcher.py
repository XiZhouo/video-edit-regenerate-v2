"""V2 本地启动器必须把视频服务端点注入运行时。

这不是对 shell 文本做 grep：用一个假的最终 ``bash start.sh`` 接住启动器导出的
真实环境，确保用户从 ``bash v2/run-v2.sh`` 启动时能够进入视频生成链路。
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


def test_v2_launcher_configures_default_video_service(tmp_path: Path) -> None:
    app_root = Path(__file__).resolve().parents[2]
    launcher = app_root.parent / "run-v2.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_bash = fake_bin / "bash"
    fake_bash.write_text("#!/bin/sh\nenv\n", encoding="utf-8")
    fake_bash.chmod(0o755)

    env = dict(os.environ)
    env.pop("ZE_VIDEOGEN_BASE_URL", None)
    env.pop("VIDEOGEN_API_BASE_URL", None)
    env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
    env["ZE_DATA_DIR"] = str(tmp_path / "data")
    env["ZE_SYSTEM_AUTH_DIR"] = str(tmp_path / "auth")

    proc = subprocess.run(
        ["/bin/bash", str(launcher)],
        cwd=launcher.parent,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert proc.returncode == 0, proc.stderr
    exported = dict(
        line.split("=", 1)
        for line in proc.stdout.splitlines()
        if "=" in line
    )
    assert exported["ZE_VIDEOGEN_BASE_URL"] == (
        "http://videogen.tools.hupogames.com"
    )
