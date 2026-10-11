#!/usr/bin/env python3
"""串口工具 —— Stage 3 交付物。

能力契约：**连接 / 发送命令 / 接收 / 解析 / timeout / 有限重试 / 结构化结果**
退出码：0 = PASS，1 = FAIL，2 = 参数或连接错误（PASS/FAIL 都是"判据"，2 是"工具自己坏了"）

固件侧约定（Core/Src/main.c）：
    复位后依次发出启动标记 'U' 'S' 'h'，随后进入回显循环（收到什么原样发回）。
    - 'U' = 时钟 + UART 已就绪
    - 'S' = SDIO 初始化通过（没等到 'S' 而是一串 'E' → SDIO 初始化失败）
    - 'h' = 已进入主循环

用法:
    python tools/serial_tool.py --smoke              # 按开发板复位键，等待启动标记 USh
    python tools/serial_tool.py --echo "hello 123"   # 发送并校验回显
    python tools/serial_tool.py --monitor 3          # 只监听 3 秒，打印原始内容
"""
import argparse
import json
import sys
import time

import serial

DEFAULT_PORT = "COM9"
DEFAULT_BAUD = 115200

# 与固件约定（Core/Src/main.c 的 [CMD] 分支）：收到 ESC 即软复位
RESET_CMD = b"\x1b"


def emit(result, **kv):
    """结构化结果：一行 JSON，人和机器都能判读。"""
    print(json.dumps({"result": result, **kv}, ensure_ascii=False))


def show(buf, n=80):
    b = buf[:n]
    return {
        "hex": b.hex(" "),
        "ascii": "".join(chr(c) if 32 <= c < 127 else "." for c in b),
    }


def cmd_smoke(s, expect, timeout, retries):
    """复位后必须依次收到启动标记。等不到就是 FAIL（含重试）。"""
    want = expect.encode()
    for attempt in range(1, retries + 2):
        s.reset_input_buffer()
        print(f"  >>> 请立刻按开发板复位键（第 {attempt} 次，等待 {timeout:g}s）", flush=True)
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            buf += s.read(64)
            if want in buf:
                emit("PASS", mode="smoke", expect=expect, attempts=attempt, **show(buf))
                return 0
        emit("FAIL", mode="smoke", expect=expect, attempts=attempt,
             hint="没等到启动标记：固件没在跑 / SDIO 初始化失败(会刷 'E') / 串口接线",
             **show(buf))
    return 1


def cmd_reset(s, expect, timeout, retries):
    """发 ESC 让设备软复位，再等启动标记 —— **全程无需人工按键**，可被 CI/pytest 调用。"""
    want = expect.encode()
    for attempt in range(1, retries + 2):
        s.reset_input_buffer()
        s.write(RESET_CMD)
        s.flush()
        deadline = time.time() + timeout
        buf = b""
        while time.time() < deadline:
            buf += s.read(64)
            if want in buf:
                emit("PASS", mode="reset", expect=expect, attempts=attempt, **show(buf))
                return 0
        emit("FAIL", mode="reset", expect=expect, attempts=attempt,
             hint="软复位后没等到启动标记：固件是否含 ESC 复位命令？SDIO 是否卡住(会刷 'E')？",
             **show(buf))
    return 1


def cmd_echo(s, payload, timeout, retries):
    """发什么收什么；逐次重试（有限次）。"""
    data = payload.encode()
    for attempt in range(1, retries + 2):
        s.reset_input_buffer()
        s.write(data)
        s.flush()
        deadline = time.time() + timeout
        got = b""
        while len(got) < len(data) and time.time() < deadline:
            got += s.read(len(data) - len(got))
        if got == data:
            emit("PASS", mode="echo", attempts=attempt,
                 n_sent=len(data), n_recv=len(got), sent=show(data, 32), recv=show(got, 32))
            return 0
        emit("FAIL", mode="echo", attempts=attempt,
             n_sent=len(data), n_recv=len(got), sent=show(data, 32), recv=show(got, 32),
             hint="回显不符：丢字节多为固件接收无缓冲(轮询溢出)；接线问题则整段收不到")
    return 1


def cmd_monitor(s, seconds):
    t0 = time.time()
    buf = b""
    while time.time() - t0 < seconds:
        buf += s.read(64)
    emit("PASS", mode="monitor", seconds=seconds, nbytes=len(buf), **show(buf))
    return 0


def main():
    ap = argparse.ArgumentParser(description="STM32 串口验证/回归工具")
    ap.add_argument("--port", default=DEFAULT_PORT)
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--timeout", type=float, default=5.0, help="单次等待秒数")
    ap.add_argument("--retries", type=int, default=1, help="失败后的重试次数(有限)")
    ap.add_argument("--smoke", action="store_true", help="等待复位后的启动标记(需人工按复位键)")
    ap.add_argument("--reset", action="store_true", help="发 ESC 软复位并等待启动标记(无需人工)")
    ap.add_argument("--expect", default="USh", help="--smoke/--reset 期望的标记序列")
    ap.add_argument("--echo", metavar="TEXT", help="发送 TEXT 并校验回显")
    ap.add_argument("--monitor", type=float, metavar="SEC", help="只监听指定秒数")
    a = ap.parse_args()

    if not (a.smoke or a.reset or a.echo or a.monitor):
        ap.print_help()
        return 2

    try:
        s = serial.Serial(a.port, a.baud, timeout=min(a.timeout, 0.5))
        s.reset_input_buffer()
        s.reset_output_buffer()
    except Exception as e:
        emit("FAIL", mode="connect", port=a.port, baud=a.baud, error=str(e))
        return 2

    try:
        if a.smoke:
            return cmd_smoke(s, a.expect, a.timeout, a.retries)
        if a.reset:
            return cmd_reset(s, a.expect, a.timeout, a.retries)
        if a.echo:
            return cmd_echo(s, a.echo, a.timeout, a.retries)
        return cmd_monitor(s, a.monitor)
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
