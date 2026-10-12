#!/usr/bin/env python3
"""调试探针 —— 用 pyocd(SWD) 取「运行态证据」。Stage 5 交付物。

为什么需要它
    printf / 串口只能看到程序「想让你看到」的东西。程序卡死、野指针、HardFault 时，
    串口也帮不上忙 —— 因为根本没人去执行那行打印。调试器不一样：它能直接读 CPU 现场，
    不管程序跑到哪、是不是已经死了。

能力
    --snapshot            暂停 -> 取现场（状态 + PC 所在函数 + 寄存器）-> 自动恢复运行
    --break SYM           在符号处下断点 -> 运行到它 -> 取现场 -> 自动恢复运行
    --read ADDR LEN       附带读一段内存（可反复给），十六进制 + ASCII
    --reset               先复位（配合 --break，用于 main 这种只在启动时经过的位置）

硬约束
    **任何路径结束时都必须把目标放回 RUNNING**。停在 halt 会让板子"假死"，
    下一个人会以为是固件挂了。实现方式：主流程结束/异常时都走 ensure_running()。

后端说明
    控制通道：`python -m pyocd cmd -t cortex_m --elf <axf>`
    符号表  ：`fromelf --text -s <axf>` 自己解析
             （pyocd 的 `where` 在本工程的 ARMCC DWARF 上会报错，绕开它）
"""
import argparse
import json
import os
import re
import subprocess
import sys

import project_config   # 与本文件同目录（拷进工程后同样在 tools/ 下）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
FROMELF = os.environ.get("AUTOLAB_FROMELF", r"F:\keil\ARM\ARMCC\bin\fromelf.exe")
TARGET = "cortex_m"


def _cfg():
    """加载工程配置。

    ⚠️ 必须在**函数里**调用，不能在 import 期执行 —— 与 build.py 同一约定
    （hil.py 会以子进程方式调用本文件，import 期读配置会把失败点带偏）。
    """
    return project_config.load(TOOLS_DIR)


def axf_path():
    """本工程镜像的绝对路径 —— 来自 tools/project.cfg 的 [build] axf。

    ⚠️ 这里以前写死 "MDK-ARM/SDIO_test/SDIO_test.axf"。换工程后探针会去读
       **另一个工程**的镜像（或报一个与本工程无关的"文件不存在"），
       而症状会表现为"符号表里查不到函数"，很难联想到是路径写死。
    字段缺失即报错，不静默兜底到任何固定路径。
    """
    return str(_cfg().path("build", "axf"))


# --------------------------------------------------------------- 故障诊断表
# Cortex-M 内核故障状态寄存器（SCB）
FAULT_REG = {"CFSR": 0xE000ED28, "HFSR": 0xE000ED2C,
             "MMFAR": 0xE000ED34, "BFAR": 0xE000ED38}

CFSR_BITS = [
    (0, "MMFSR.IACCVIOL  取指访问违规"), (1, "MMFSR.DACCVIOL  数据访问违规"),
    (3, "MMFSR.MUNSTKERR 异常返回时出栈失败"), (4, "MMFSR.MSTKERR  异常入栈失败"),
    (7, "MMFSR.MMARVALID MMFAR 中的地址有效"),
    (8, "BFSR.IBUSERR   取指总线错误"), (9, "BFSR.PRECISERR  精确数据总线错误"),
    (10, "BFSR.IMPRECISERR 非精确数据总线错误"), (11, "BFSR.UNSTKERR 出栈总线错误"),
    (12, "BFSR.STKERR    入栈总线错误"), (15, "BFSR.BFARVALID  BFAR 中的地址有效"),
    (16, "UFSR.UNDEFINSTR 未定义指令"), (17, "UFSR.INVSTATE  非法状态"),
    (18, "UFSR.INVPC     非法 PC"), (19, "UFSR.NOCP      无协处理器"),
    (24, "UFSR.UNALIGNED 非对齐访问"), (25, "UFSR.DIVBYZERO 除零"),
]
HFSR_BITS = [(1, "VECTTBL 向量表读取错误"), (30, "FORCED 由已禁用的异常升级而来"),
             (31, "DEBUGEVT 调试事件")]
IPSR_NAME = {0: "Thread(非异常)", 2: "NMI", 3: "HardFault",
             4: "MemManage", 5: "BusFault", 6: "UsageFault"}


# --------------------------------------------------------------- 符号表

def load_symtab(path=None):
    """从 fromelf 的符号表里取 Code 段符号，用于「地址 -> 函数」。

    path 省略时取当前工程的 [build] axf（来自 tools/project.cfg）。
    """
    out = subprocess.run([FROMELF, "--text", "-s", path or axf_path()],
                         capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    pat = re.compile(r"^\s*\d+\s+(\S+)\s+0x([0-9a-fA-F]{8})\s+\S+\s+\d+\s+(\S+)\s+\S+\s*(0x[0-9a-fA-F]+)?\s*$")
    syms = []
    for line in out.splitlines():
        m = pat.match(line)
        if m and m.group(3) == "Code":
            syms.append((int(m.group(2), 16) & ~1,
                         int(m.group(4) or "0", 16),
                         m.group(1)))
    return sorted(syms)


def resolve(addr, syms):
    """找出地址落在哪个函数里（取包含它的、地址最大的那个符号）。"""
    addr &= ~1
    hit = None
    for a, size, name in syms:
        if a <= addr < a + max(size, 2):
            hit = (a, size, name)
    return hit


# --------------------------------------------------------------- 调试后端

def pyocd(cmds, timeout=120):
    argv = [sys.executable, "-m", "pyocd", "cmd", "-t", TARGET, "--elf", axf_path()]
    for c in cmds:
        argv += ["-c", c]
    p = subprocess.run(argv, capture_output=True, cwd=ROOT, timeout=timeout)
    return (p.stdout + p.stderr).decode("utf-8", "replace")


def ensure_running():
    """兜底：无论前面发生了什么，都把目标放回运行态。"""
    try:
        from pyocd.core.helpers import ConnectHelper
        s = ConnectHelper.session_with_chosen_probe(target_override=TARGET)
        s.open()
        try:
            s.target.resume()
            return str(s.target.get_state())
        finally:
            s.close()
    except Exception as e:                       # 兜底逻辑本身失败也要说清楚
        return "ensure_running 失败: %s" % e


def parse_regs(text):
    """解析 pyocd `reg` 的输出。寄存器名有 r0..r12 / lr / pc / sp / xpsr 等，
    长度不一（r0 是 1 字母+数字，xpsr 是 4 字母），所以不能用固定长度正则。"""
    return {n: int(v, 16)
            for n, v in re.findall(r"\b([a-z][a-z0-9]{0,7})\s*:\s*0x([0-9a-fA-F]{1,8})\b", text)}


def emit(result, **kv):
    print(json.dumps({"result": result, **kv}, ensure_ascii=False))


def mem_words(text, addr, ln):
    """从 read32 的输出里取出 [addr, addr+ln) 内的 32bit 字。
    行格式： `20000700:  080029e0  00000000  |..).|`

    注意：pyocd `read32 ADDR LEN` 的 LEN 是**字节数**（须 4 对齐），不是字数。
    """
    words = []
    for line in text.splitlines():
        m = re.match(r"\s*([0-9a-fA-F]{8}):\s+(.*?)\s*\|", line)
        if not m:
            continue
        a = int(m.group(1), 16)
        if not (addr <= a < addr + ln):
            continue
        words += re.findall(r"\b([0-9a-fA-F]{8})\b", m.group(2))
    return words[: (ln + 3) // 4]


# --------------------------------------------------------------- 取证

def snapshot(reads, syms, note=""):
    """暂停 -> 取现场 -> 恢复。返回结构化证据。"""
    cmds = ["halt", "status", "reg"]
    for addr, ln in reads:
        cmds.append("read32 0x%08X %d" % (addr, (ln + 3) // 4 * 4))   # LEN 单位为字节
    cmds.append("exit")
    text = pyocd(cmds)

    regs = parse_regs(text)
    ev = {"note": note, "regs": {k: "0x%08X" % v for k, v in regs.items()}}

    pc = regs.get("pc")
    if pc is not None:
        hit = resolve(pc, syms)
        ev["pc"] = "0x%08X" % pc
        if hit:
            a, size, name = hit
            ev["pc_function"] = name
            ev["pc_offset"] = "+0x%X" % (pc - a)

    if reads:
        ev["memory"] = {"0x%08X" % addr: mem_words(text, addr, ln) for addr, ln in reads}

    ev["raw_tail"] = text.strip().splitlines()[-6:]
    return ev


def cmd_fault(syms):
    """故障现场取证：异常类型 + 故障状态寄存器 + **出错的那条 PC**。

    原理：Cortex-M 进入异常时，硬件会把 R0-R3/R12/LR/PC/xPSR 顺序压栈
    （SP+0x00=R0 ... SP+0x14=LR, **SP+0x18=出错的 PC**, SP+0x1C=xPSR）。
    所以只要读出异常帧，就能拿到"程序死在哪条指令"，再配合符号表还原成函数名。
    这是 printf 做不到的 —— 程序已经死了，没人去执行打印。
    """
    t1 = pyocd(["halt", "status", "reg"] +
               ["read32 0x%08X 4" % a for a in FAULT_REG.values()] + ["exit"])
    regs = parse_regs(t1)
    vals = {}
    for name, addr in FAULT_REG.items():
        w = mem_words(t1, addr, 4)
        vals[name] = int(w[0], 16) if w else None

    ipsr = regs.get("xpsr", 0) & 0x1FF
    ev = {
        "current_exception": IPSR_NAME.get(ipsr, "异常号 %d" % ipsr),
        "ipsr": ipsr,
        "regs": {k: "0x%08X" % v for k, v in regs.items()},
        "fault_regs": {k: ("0x%08X" % v if v is not None else None) for k, v in vals.items()},
        "cfsr_flags": [n for b, n in CFSR_BITS if (vals.get("CFSR") or 0) & (1 << b)],
        "hfsr_flags": [n for b, n in HFSR_BITS if (vals.get("HFSR") or 0) & (1 << b)],
        "raw_regs_tail": t1.strip().splitlines()[-9:],
    }

    if ipsr in (3, 4, 5, 6):
        sp = regs.get("sp")
        t2 = pyocd(["halt", "read32 0x%08X 32" % sp, "exit"])
        frame = mem_words(t2, sp, 32)
        ev["exception_frame"] = frame
        if len(frame) >= 7:
            pc = int(frame[6], 16)
            ev["faulting_pc"] = "0x%08X" % pc
            ev["stacked_lr"] = frame[5]
            ev["stacked_r0_r3"] = frame[0:4]
            hit = resolve(pc, syms)
            if hit:
                ev["faulting_function"] = hit[2]
                ev["faulting_offset"] = "+0x%X" % (pc - hit[0])
    else:
        ev["note"] = "当前不在异常处理里（IPSR=0）—— 没有故障现场可读"
    return 0, ev


def main():
    ap = argparse.ArgumentParser(description="pyocd 调试探针（Stage 5）")
    ap.add_argument("--snapshot", action="store_true", help="暂停取现场后自动恢复")
    ap.add_argument("--fault", action="store_true",
                    help="故障现场取证：异常类型 + 故障寄存器 + 出错的 PC->函数")
    ap.add_argument("--break", dest="bp", metavar="SYMBOL", help="在符号处下断点并运行到它")
    ap.add_argument("--wait", type=int, default=800, help="下断点后等待命中的毫秒数")
    ap.add_argument("--reset", action="store_true", help="先复位再运行（用于 main 这类启动位置）")
    ap.add_argument("--read", nargs=2, action="append", metavar=("ADDR", "LEN"),
                    default=[], help="附带读一段内存，可多次给，如 --read 0x20000000 16")
    a = ap.parse_args()
    reads = [(int(x, 0), int(y, 0)) for x, y in a.read]

    # ⚠️ 配置守卫必须在 try/finally **之前**：否则配置错误也会走到 finally 里的
    #    ensure_running() —— 那会**真的去连一次调试探针**。配都没配好就不该碰硬件。
    try:
        axf_path()
    except (project_config.ConfigError, OSError) as e:
        emit("FAIL", note="config", error=f"无法确定镜像路径：{e}",
             hint="检查 tools/project.cfg 的 [build] axf（换工程必须改这一项）")
        return 2

    syms = load_symtab()
    try:
        if a.fault:
            rc, ev = cmd_fault(syms)
            emit("PASS" if rc == 0 else "FAIL", **ev)
            return rc

        if a.snapshot:
            ev = snapshot(reads, syms, note="snapshot")
            emit("PASS", **ev)
            return 0

        if a.bp:
            cmds = []
            if a.reset:
                cmds.append("reset halt")
            cmds += ["break %s" % a.bp, "go", "sleep %d" % a.wait, "status"]
            cmds.append("exit")
            text = pyocd(cmds)

            bit = "break %s" % a.bp
            if "Halted" not in text:
                emit("FAIL", note="breakpoint", symbol=a.bp,
                     hint="等待内未命中：符号是否为可执行代码？是否根本没走到？",
                     raw_tail=text.strip().splitlines()[-6:])
                return 1
            # 命中后再取一次现场（此时已停住）
            ev = snapshot(reads, syms, note="breakpoint-hit")
            ev["symbol"] = a.bp
            emit("PASS", **ev)
            return 0

        ap.print_help()
        return 2
    finally:
        state = ensure_running()
        print(json.dumps({"target_state": state}, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
