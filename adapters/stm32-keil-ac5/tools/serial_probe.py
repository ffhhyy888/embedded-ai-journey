#!/usr/bin/env python3
"""串口探针 —— 找出 MCU 实际使用的波特率。

背景（2026-10-10 诊断）：
    固件复位后发 U(0x55) S(0x53) h(0x68)，随后 while(1) 每 1 秒发一个 '3'(0x33)。
    PC 按 115200 读取时得到 `1c 1c fc`，仿真反推真实波特率约为 115200/3.1 ≈ 37k。
    本脚本扫一遍候选波特率，看哪一个能读出干净数据。

用法:
    python tools/serial_probe.py            # 默认 COM9
    python tools/serial_probe.py COM7
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM9"

# 候选：先标准值，再覆盖仿真反推的区间 [35446, 40635]
BAUDS = [115200, 57600, 46080, 40635, 40000, 38400, 36864, 36000, 35446, 28800, 19200, 9600]

SECONDS_PER_BAUD = 3.0


def listen(baud, seconds):
    """在指定波特率下监听 seconds 秒，返回收到的所有字节。"""
    try:
        s = serial.Serial(PORT, baud, timeout=0.25)
    except Exception as e:
        return None, str(e)
    try:
        s.reset_input_buffer()
        t0 = time.time()
        buf = b""
        while time.time() - t0 < seconds:
            buf += s.read(256)
    finally:
        s.close()
    return buf, None


def describe(buf):
    if not buf:
        return "无数据"
    hexs = buf[:40].hex(" ")
    txt = "".join(chr(c) if 32 <= c < 127 else "." for c in buf[:40])
    return f"{len(buf):>4} 字节 | {hexs} | {txt}"


def judge(buf):
    if not buf:
        return ""
    if b"\x55\x53\x68" in buf:
        return "   <<< 命中 U S h（复位启动序列）"
    if buf.count(b"\x33") >= 2 or (len(buf) > 3 and len(set(buf)) == 1):
        return "   <<< 规律重复，很可能是真实波特率"
    return ""


def main():
    # 先确认端口能打开
    try:
        t = serial.Serial(PORT)
        t.close()
    except Exception as e:
        print(f"打不开 {PORT}: {e}")
        return 1

    print(f"=== 探针: {PORT} ===")
    print("固件应在持续发送。每个波特率听 "
          f"{SECONDS_PER_BAUD:g} 秒。\n")

    winners = []
    for b in BAUDS:
        buf, err = listen(b, SECONDS_PER_BAUD)
        if err:
            print(f"  [{b:>6}] 打开失败: {err}")
            continue
        line = describe(buf)
        mark = judge(buf)
        print(f"  [{b:>6}] {line}{mark}")
        if mark:
            winners.append(b)

    print()
    if winners:
        print(f"结论: 以下波特率读到了可信数据 -> {winners}")
        print("      真实波特率取其中最大值/最靠近仿真的一个来复测。")
    else:
        print("结论: 所有候选波特率都读不到规律数据。")
        print("      下一步应做 USB-TTL 自环（把模块的 TX 和 RX 短接）验证 PC 侧。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
