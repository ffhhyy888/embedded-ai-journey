#!/usr/bin/env python3
"""串口工具 —— Stage 3 交付物。

能力契约：**连接 / 发送命令 / 接收 / 解析 / timeout / 有限重试 / 结构化结果**
退出码：0 = PASS，1 = FAIL，2 = 参数或连接错误（PASS/FAIL 都是"判据"，2 是"工具自己坏了"）

配置与优先级（端口 / 波特率 / 启动标记 / 复位命令）
    CLI 参数  >  环境变量  >  tools/project.cfg  >  内置默认
    · 端口支持环境变量 AUTOLAB_PORT；其余项不设环境变量，保持面最小
    · 本工具可以**脱离工程单独使用**（临时排查一块陌生板子）：
      没有 project.cfg 时退回内置默认，并在 stderr 打 [WARN] 逐项说明降级了什么
      —— **降级允许，静默不允许**

固件侧约定（具体标记由 project.cfg 的 boot_marker 决定，默认 USh）：
    复位后依次发出启动标记，随后进入回显循环（收到什么原样发回）。
    例（SDIO_test）：'U'=时钟+UART 就绪、'S'=SDIO 初始化通过、'h'=已进入主循环。

用法:
    python tools/serial_tool.py --smoke              # 按开发板复位键，等待启动标记 USh
    python tools/serial_tool.py --echo "hello 123"   # 发送并校验回显
    python tools/serial_tool.py --monitor 3          # 只监听 3 秒，打印原始内容
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import NamedTuple

import serial

import project_config   # 与本文件同目录（拷进工程后同样在 tools/ 下）

TOOLS_DIR = Path(__file__).resolve().parent

# ---- 内置默认 ----
# 只在「CLI 没给、环境变量没给、project.cfg 也没有」时才用到；一旦用到就会打 WARN。
DEFAULT_PORT = "COM9"
DEFAULT_BAUD = 115200
DEFAULT_BOOT_MARKER = "USh"
# 与固件约定：收到该字节即软复位（1b = ESC）
DEFAULT_RESET_HEX = "1b"


class Settings(NamedTuple):
    """最终生效的串口设置。"""
    port: str
    baud: int
    expect: str
    reset_cmd: bytes


def _pick(cli, env_name, cfg, section, key, default, notes):
    """单值解析：CLI > 环境变量 > project.cfg > 内置默认。

    取值一律走 project_config.resolve()（优先级规则的**单一来源**）；
    这里额外判断"是不是真的落到了内置默认"，只为决定要不要提醒用户 ——
    降级允许，静默不允许。
    """
    cfg_value = cfg.get(section, key, None) if cfg is not None else None
    chosen = project_config.resolve(cli, env_name, cfg_value, default)

    from_cli = cli not in (None, "")
    from_env = bool(env_name) and os.environ.get(env_name) not in (None, "")
    if not from_cli and not from_env and cfg_value in (None, ""):
        notes.append(f"{section}.{key} 未取自工程配置 -> 内置默认 {default!r}")
    return chosen


def load_cfg_or_none():
    """读工程配置；**文件不存在时返回 None，而不是抛异常**。

    本工具是诊断工具，允许在还没配好 project.cfg 的目录里临时使用。
    降级事实由调用方打 WARN —— 不静默。
    """
    try:
        return project_config.load(TOOLS_DIR)
    except project_config.ConfigMissing:
        return None


def resolve_settings(cli_port, cli_baud, cli_expect, cfg):
    """合成最终串口设置，返回 (Settings, notes)。

    纯函数（只读 cfg，不碰文件系统/串口），便于在没有硬件的机器上直接验证。
    notes 里是"哪些取值退回了内置默认"，调用方必须把它打给用户看。

    reset_cmd_hex 非法时抛 ValueError（调用方报 ERROR 退出 2）。
    """
    notes = []
    if cfg is None:
        notes.append("没有 tools/project.cfg —— 本工具将使用内置默认"
                     "（若本工程应当有配置，请确认它在 tools/ 下）")

    port = _pick(cli_port, "AUTOLAB_PORT", cfg, "serial", "port", DEFAULT_PORT, notes)
    baud = int(_pick(cli_baud, None, cfg, "serial", "baud", DEFAULT_BAUD, notes))
    expect = _pick(cli_expect, None, cfg, "serial", "boot_marker",
                   DEFAULT_BOOT_MARKER, notes)

    reset_hex = _pick(None, None, cfg, "serial", "reset_cmd_hex",
                      DEFAULT_RESET_HEX, notes)
    try:
        reset_cmd = bytes.fromhex(reset_hex)
    except ValueError:
        raise ValueError(
            f"[serial] reset_cmd_hex 不是合法的十六进制：{reset_hex!r}"
            f"（应为偶数个十六进制字符，例如 1b）") from None

    return Settings(port=port, baud=baud, expect=expect, reset_cmd=reset_cmd), notes


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


def cmd_reset(s, expect, timeout, retries, reset_cmd):
    """发复位命令让设备软复位，再等启动标记 —— **全程无需人工按键**，可被 CI/pytest 调用。"""
    want = expect.encode()
    for attempt in range(1, retries + 2):
        s.reset_input_buffer()
        s.write(reset_cmd)
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
    ap.add_argument("--port", default=None,
                    help=f"串口端口（默认：AUTOLAB_PORT / project.cfg / {DEFAULT_PORT}）")
    ap.add_argument("--baud", type=int, default=None,
                    help=f"波特率（默认：project.cfg / {DEFAULT_BAUD}）")
    ap.add_argument("--timeout", type=float, default=5.0, help="单次等待秒数")
    ap.add_argument("--retries", type=int, default=1, help="失败后的重试次数(有限)")
    ap.add_argument("--smoke", action="store_true", help="等待复位后的启动标记(需人工按复位键)")
    ap.add_argument("--reset", action="store_true", help="发复位命令让设备软复位并等待启动标记(无需人工)")
    ap.add_argument("--expect", default=None,
                    help=f"--smoke/--reset 期望的标记序列"
                         f"（默认：project.cfg / {DEFAULT_BOOT_MARKER}）")
    ap.add_argument("--echo", metavar="TEXT", help="发送 TEXT 并校验回显")
    ap.add_argument("--monitor", type=float, metavar="SEC", help="只监听指定秒数")
    a = ap.parse_args()

    if not (a.smoke or a.reset or a.echo or a.monitor):
        ap.print_help()
        return 2

    try:
        st, notes = resolve_settings(a.port, a.baud, a.expect, load_cfg_or_none())
    except ValueError as e:
        emit("FAIL", mode="config", error=str(e))
        return 2

    if notes:
        print("[WARN] 以下取值未取自工程配置，已退回内置默认：", file=sys.stderr)
        for n in notes:
            print(f"       - {n}", file=sys.stderr)

    try:
        s = serial.Serial(st.port, st.baud, timeout=min(a.timeout, 0.5))
        s.reset_input_buffer()
        s.reset_output_buffer()
    except Exception as e:
        emit("FAIL", mode="connect", port=st.port, baud=st.baud, error=str(e))
        return 2

    try:
        if a.smoke:
            return cmd_smoke(s, st.expect, a.timeout, a.retries)
        if a.reset:
            return cmd_reset(s, st.expect, a.timeout, a.retries, st.reset_cmd)
        if a.echo:
            return cmd_echo(s, a.echo, a.timeout, a.retries)
        return cmd_monitor(s, a.monitor)
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
