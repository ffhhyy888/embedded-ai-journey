"""Stage 4 回归套件 —— 无硬件部分：构建链路。

运行:  python -m pytest tests/ -v
"""
import os
import subprocess

import pytest
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_build_passes():
    """四判据必须全 PASS：
    退出码 0 / 0 Error 0 Warning / 编译动作数 >= 项目配置的 min_compile_lines /
    .axf 时间戳更新。
    """
    p = subprocess.run(
        [sys.executable, os.path.join(ROOT, "tools", "build.py")],
        capture_output=True, cwd=ROOT, timeout=900,
    )
    out = p.stdout.decode("utf-8", "replace")
    assert p.returncode == 0, "构建未通过：\n" + out[-3000:]
    assert "PASS" in out.upper()


pytestmark = pytest.mark.nohardware
