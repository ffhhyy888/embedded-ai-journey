"""Stage 4 回归套件 —— 真机部分：串口链路。

运行:  python -m pytest tests/ -v
前置:  - 固件已烧录（须含 ESC 软复位命令）
       - USB-TTL 接好；可用环境变量 AUTOLAB_PORT 覆盖端口（默认 COM9）
证据:  每次调用工具都会把原始输出落盘到 tests/artifacts/（失败后可直接翻看）

设计说明:
    测试把 tools/serial_tool.py 当**黑盒**调用，断言它的「退出码 + 结构化 JSON」。
    退出码约定：0=PASS(被测通过) / 1=FAIL(被测不通过) / 2=工具或连接错误。
    正常路径与异常路径都要覆盖 —— 只见过 PASS 的判据不算判据。
"""
import json
import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "serial_tool.py")
ARTIFACTS = os.path.join(ROOT, "tests", "artifacts")
PORT = os.environ.get("AUTOLAB_PORT", "COM9")

# 这些用例要真机：CI 层1 用 -m "not hardware" 跳过它们
pytestmark = pytest.mark.hardware


def run_tool(*args, timeout=60):
    """黑盒调用串口工具；返回 (退出码, 结构化结果) 并把原始输出落盘取证。"""
    os.makedirs(ARTIFACTS, exist_ok=True)
    label = "_".join(str(a) for a in args if not str(a).startswith("-"))[:40] or "run"
    label = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in label)
    stamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    log = os.path.join(ARTIFACTS, f"{stamp}_{label}.txt")

    argv = [sys.executable, TOOL] + [str(a) for a in args]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")   # 与 hil.py 一致：防 GBK 乱码
    p = subprocess.run(argv, capture_output=True, cwd=ROOT, timeout=timeout, env=env)
    stdout = p.stdout.decode("utf-8", "replace")
    stderr = p.stderr.decode("utf-8", "replace")

    with open(log, "w", encoding="utf-8") as f:
        f.write("$ " + " ".join(argv) + "\n")
        f.write(f"exit={p.returncode}\n--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}\n")

    lines = [l for l in stdout.splitlines() if l.strip().startswith("{")]
    return p.returncode, (json.loads(lines[-1]) if lines else {})


# ------------------------------------------------------------------ 正常路径

def test_soft_reset_smoke():
    """软复位后必须收到启动标记 USh（U=时钟/UART、S=SDIO 初始化、h=进入主循环）。

    这是**无需人工按键**的冒烟测试 —— 复位由串口 ESC 命令触发。
    """
    rc, data = run_tool("--port", PORT, "--reset", "--expect", "USh", "--timeout", 8)
    assert rc == 0, f"软复位冒烟失败（退出码 {rc}）: {data}"
    assert data.get("result") == "PASS"


def test_echo_roundtrip():
    """收发闭环：发出去什么，必须原样回来。"""
    payload = "hello 123"
    rc, data = run_tool("--port", PORT, "--echo", payload, "--timeout", 5)
    assert rc == 0, f"回显失败（退出码 {rc}）: {data}"
    assert data["recv"]["ascii"] == payload


# ------------------------------------------------------------------ 边界路径

def test_echo_single_byte():
    """边界：单字节也要能回（最容易暴露接收超时/首字节处理问题）。"""
    rc, data = run_tool("--port", PORT, "--echo", "Z", "--timeout", 5)
    assert rc == 0, f"单字节回显失败（退出码 {rc}）: {data}"


def test_echo_long_payload():
    """边界：128 字节长载荷必须逐字节一致（考察连续收发与缓冲）。

    历史：2026-10-10 本用例曾失败 —— 阻塞轮询回显丢字节（128 发只收 117），
    当时用 xfail(strict=True) 如实标记；同日改用「中断接收 + 环形缓冲」修复，
    标记已删除。此用例现在必须**真通过**。
    """
    payload = ("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" * 4)[:128]
    rc, data = run_tool("--port", PORT, "--echo", payload, "--timeout", 10)
    assert rc == 0, f"长载荷回显失败（退出码 {rc}）: {data}"
    assert data["n_recv"] == data["n_sent"] == len(payload)


def test_echo_4k_burst():
    """边界：4 KB 连续突发也必须逐字节一致。

    历史（都是 2026-10-10 这一天里踩到的）：
      1. 阻塞轮询回显 -> 128 字节就丢 11 个；
      2. 只加环形缓冲 -> 128~2048 字节好了，但 4096 起又丢（丢 123），
         因为发送端仍在等 TC，持续积压把缓冲填满；
      3. 发送改为只等 TXE（非阻塞）后 -> 4 KB / 16 KB / 64 KB 全部逐字节一致。
    本用例锁住这个边界，防止以后回退。
    """
    payload = ("The quick brown fox jumps over the lazy dog. 0123456789 " * 100)[:4096]
    rc, data = run_tool("--port", PORT, "--echo", payload, "--timeout", 20)
    assert rc == 0, f"4KB 突发回显失败（退出码 {rc}）: {data}"
    assert data["n_recv"] == data["n_sent"] == len(payload)


# --------------------------------------------------- 异常路径（判据双向标定）

def test_wrong_expect_must_fail():
    """异常：期望一个不可能出现的标记 -> 必须 FAIL 且退出码 **1**（被测不通过）。"""
    rc, data = run_tool("--port", PORT, "--reset", "--expect", "ZZ",
                        "--timeout", 3, "--retries", 0)
    assert rc == 1, f"退出码应为 1（被测失败），实际 {rc}: {data}"
    assert data.get("result") == "FAIL"


def test_bad_port_must_exit_2():
    """异常：端口不存在 -> 必须是退出码 **2**（工具/连接错误，与"被测失败"区分开）。"""
    rc, data = run_tool("--port", "COM99", "--monitor", 1, timeout=30)
    assert rc == 2, f"退出码应为 2（工具/连接错误），实际 {rc}: {data}"
