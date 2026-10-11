"""Stage 6 —— 宿主机单元测试驱动。

与另外两类的分工：
    test_static.py  —— 检查**构建产物**的结构（符号/预算/向量表），不执行代码
    test_host.py    —— 在 PC 上**执行固件里的真实代码**（同一份 ringbuf.h），毫秒级
    test_device.py  —— 上真机，验证串口/SDIO 等**仿真覆盖不到**的行为

工具链：`python -m ziglang cc`（pip 装的宿主机 C 编译器，不依赖板子/Keil）
"""
import os
import subprocess
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INC = os.path.join(ROOT, "Core", "Inc")
HOST = os.path.join(ROOT, "tests", "host")
ZIG = [sys.executable, "-m", "ziglang"]


def build_and_run(src_name):
    src = os.path.join(HOST, src_name)
    with tempfile.TemporaryDirectory() as td:
        exe = os.path.join(td, os.path.splitext(src_name)[0] + ".exe")
        cc = subprocess.run(ZIG + ["cc", "-Wall", "-I", INC, src, "-o", exe],
                            capture_output=True, timeout=300)
        assert cc.returncode == 0, ("编译失败:\n"
                                    + cc.stderr.decode("utf-8", "replace")[-3000:])
        run = subprocess.run([exe], capture_output=True, timeout=60)
        out = run.stdout.decode("utf-8", "replace") + run.stderr.decode("utf-8", "replace")
        return run.returncode, out


pytestmark = pytest.mark.skipif(
    subprocess.run(ZIG + ["version"], capture_output=True, timeout=60).returncode != 0,
    reason="宿主机 C 编译器不可用（pip install ziglang）",
)


def test_ringbuf_on_host():
    """环形缓冲：空/满/FIFO/回绕/边收边发 —— 跑的是固件用的那份 ringbuf.h。"""
    rc, out = build_and_run("test_ringbuf.c")
    assert rc == 0, "宿主机单元测试失败:\n" + out
    assert "ALL OK" in out
