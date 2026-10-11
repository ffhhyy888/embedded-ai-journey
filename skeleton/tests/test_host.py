"""Stage 6 —— 宿主机单元测试驱动。

与另外两类的分工：
    test_static.py  —— 检查**构建产物**的结构（符号/预算/向量表），不执行代码
    test_host.py    —— 在 PC 上**执行固件里的真实代码**（同一份 ringbuf.h），毫秒级
    test_device.py  —— 上真机，验证串口/SDIO 等**仿真覆盖不到**的行为

工具链：`python -m ziglang cc`（pip 装的宿主机 C 编译器，不依赖板子/Keil）

前提：本工程必须提供 `Core/Inc/ringbuf.h`（由工程侧维护，固件与 PC 测试共用同一份）。
      该文件缺失时本用例**显式 SKIP** 并说明原因 —— 骨架仓库本身不含它，
      往骨架里塞一份会制造"测试副本"，与上面"不给测试留副本"的原则冲突。
      文件存在时行为不变：正常编译执行，**真实编译错误照常 FAIL，不吞异常**。
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

# C 用例 include 的头文件 —— 它属于**具体工程**（固件与 PC 测试共用同一份），
# 骨架仓库本身不含。缺失时跳过，不伪造一份放进来。
RINGBUF_H = os.path.join(INC, "ringbuf.h")


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


pytestmark = [
    pytest.mark.nohardware,        # 分层：属于 CI 层1（不碰硬件）
    pytest.mark.skipif(
        subprocess.run(ZIG + ["version"], capture_output=True, timeout=60).returncode != 0,
        reason="宿主机 C 编译器不可用（pip install ziglang）",
    ),
    # 只在**前提缺失**时跳过（文件不存在），不是放宽判定：
    # 文件一旦存在就照常编译执行，编译/断言失败依旧是 FAIL。
    pytest.mark.skipif(
        not os.path.isfile(RINGBUF_H),
        reason=f"本工程没有 {os.path.relpath(RINGBUF_H, ROOT)}"
               f"（该头文件由具体工程提供，骨架仓库本身不含）—— 跳过宿主机单测",
    ),
]


def test_ringbuf_on_host():
    """环形缓冲：空/满/FIFO/回绕/边收边发 —— 跑的是固件用的那份 ringbuf.h。"""
    rc, out = build_and_run("test_ringbuf.c")
    assert rc == 0, "宿主机单元测试失败:\n" + out
    assert "ALL OK" in out
