#!/usr/bin/env python3
"""HIL 主控 —— 一条命令跑完整轮次：构建 -> 烧录 -> 真机判定 -> 失败自动取证 -> 出报告。

为什么需要它
    零件早就齐了：build.py(构建)、uVision.com -f(烧录)、serial_tool.py(激励+观测)、
    debug_probe.py(运行态现场)、pytest(判定)。但**是分散的**，靠人一条条敲 ——
    人一累就会漏步骤、漏证据。本脚本把它们编排成**可重复的轮次**，
    失败时自动取证，并把证据整理成**人能直接读的报告**。

一轮做什么
    1. 构建    tools/build.py             （四判据）
    2. 烧录    uVision.com -f             （Erase Sectors + Program + Verify + Reset&Run）
    3. 判定    pytest tests/test_device.py（真机用例，复用已验证的判据）
    4. 失败取证（任一步失败都触发）
    5. 生成报告 runs/<时间戳>_FAIL/report.md（证据 -> 判断依据 -> 下一步建议）

【停止条件 —— 刻意写死在这里】
    - **不做无界重试**：--attempts 默认 1，硬上限 3；一旦某轮 PASS 立即停止
    - **绝不自动修改代码**：失败就停下来出报告，**改不改由人决定**
      （自动化最危险的形式，就是"失败了自己乱改再试"）
    - 每步都有 timeout；任何一步失败都不继续往下走

用法
    python tools/hil.py                 # 一轮
    python tools/hil.py --skip-build
    python tools/hil.py --attempts 2    # 最多两轮（用于对付偶发的硬件掉线）
退出码：0 = PASS；1 = FAIL（证据与报告已落盘）
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
AXF = os.path.join(ROOT, "MDK-ARM", "SDIO_test", "SDIO_test.axf")
FLASH_LOG = os.path.join(ROOT, "MDK-ARM", "flash.log")
RUNS = os.path.join(ROOT, "runs")
UVISION = os.environ.get("AUTOLAB_UVISION", r"F:\keil\UV4\uVision.com")

MAX_ATTEMPTS_HARD = 3      # 硬上限：任何情况下都不允许超过


def run(argv, timeout=600, cwd=ROOT):
    # 子进程的输出按 UTF-8 编码 —— 否则 Windows 控制台默认用 GBK，
    # 而我们按 UTF-8 解码，中文会变乱码（曾导致证据比对失败）
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run(argv, capture_output=True, cwd=cwd, timeout=timeout, env=env)
    return (p.returncode,
            p.stdout.decode("utf-8", "replace"),
            p.stderr.decode("utf-8", "replace"))


def firmware_fingerprint():
    """证据里必须能唯一对上"当时烧的是哪一版"。"""
    fp = {}
    if os.path.exists(AXF):
        fp["axf_sha256"] = hashlib.sha256(open(AXF, "rb").read()).hexdigest()[:16]
        fp["axf_mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                        time.localtime(os.path.getmtime(AXF)))
    rc, out, _ = run(["git", "rev-parse", "--short", "HEAD"])
    fp["git_head"] = out.strip() if rc == 0 else "?"
    rc, out, _ = run(["git", "status", "--porcelain"])
    fp["git_dirty"] = bool(out.strip())
    if fp["git_dirty"]:
        # 审计发现：只记 dirty=true/false 时，脏工作树的固件**无法复现**。
        # 至少把"改了哪些文件"和 diff 的指纹留下。
        rc2, stat, _ = run(["git", "diff", "--stat"])
        rc3, diff, _ = run(["git", "diff"])
        fp["dirty_files"] = [l for l in stat.strip().splitlines() if l.strip()][:20]
        fp["dirty_diff_sha256"] = hashlib.sha256(diff.encode("utf-8")).hexdigest()[:16]
    return fp


# ---------------------------------------------------------------- 失败取证

def collect_evidence(ev, notes):
    os.makedirs(ev, exist_ok=True)
    with open(os.path.join(ev, "fingerprint.json"), "w", encoding="utf-8") as f:
        json.dump(firmware_fingerprint(), f, ensure_ascii=False, indent=2)

    for argv, name, tmo in (
        # --reset 是最有诊断力的一项：它主动激励并期待启动标记，
        # 而 --monitor 对"空闲时本来就静默"的固件只能收到 0 字节，说明不了问题
        (["--reset", "--timeout", "6"], "serial_reset.txt", 90),
        (["--monitor", "2"], "serial_monitor.txt", 60),
        (["--snapshot"], "debug_snapshot.txt", 180),
        (["--fault"], "debug_fault.txt", 180),
    ):
        tool = "serial_tool.py" if name.startswith("serial") else "debug_probe.py"
        rc, out, err = run([PY, os.path.join(ROOT, "tools", tool)] + argv, timeout=tmo)
        with open(os.path.join(ev, name), "w", encoding="utf-8") as f:
            f.write(f"exit={rc}\n{out}\n{err}\n")

    if os.path.exists(FLASH_LOG):
        shutil.copy(FLASH_LOG, os.path.join(ev, "flash.log"))
    notes.append(f"取证目录: {os.path.relpath(ev, ROOT)}")


# ---------------------------------------------------------------- 证据 -> 报告

def _last_json(text, must_have=None):
    """工具输出里混了 stdout/stderr，取"含指定关键字"的最后一行 JSON。"""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and (must_have is None or must_have in line):
            try:
                return json.loads(line)
            except Exception:
                pass
    return None


def analyze(ev):
    """把原始证据整理成「判断依据」。**只做证据归纳，不下"一定是XX"的结论。**"""
    findings = []

    # 主判据：软复位后能不能收到启动标记（主动激励，最有诊断力）
    raw = open(os.path.join(ev, "serial_reset.txt"), encoding="utf-8").read()
    rst = _last_json(raw, '"mode": "reset"')
    if rst:
        if rst.get("result") == "PASS":
            findings.append("串口：软复位后**正常收到启动标记 USh** -> 固件在跑、串口链路通")
        else:
            got = (rst.get("hex") or "").replace(" ", "")
            findings.append("串口：软复位后**没收到启动标记 USh** -> 问题在"
                            "「串口链路 / 波特率 / 启动阶段」，不是纯软件逻辑")
            if got:
                uniq = len(set(got[i:i + 2] for i in range(0, min(len(got), 64), 2)))
                nbytes = len(got) // 2
                if uniq <= 2 and nbytes > 1000:
                    hint = "（大量恒定字节 -> 像故障处理程序在刷标记）"
                elif uniq <= 2:
                    hint = "（字节数少、取值单一 -> 更像波特率错位后解出的残片）"
                else:
                    hint = "（取值杂乱 -> 像波特率/时序不匹配）"
                findings.append(f"  · 实际收到 {nbytes} 字节，不同取值数={uniq}{hint}")
                findings.append(f"  · 原始: {got[:60]}  {rst.get('ascii', '')[:30]}")
    else:
        findings.append("串口：探测未产出结构化结果（工具/连接错误），见 serial_reset.txt")

    raw = open(os.path.join(ev, "debug_fault.txt"), encoding="utf-8").read()
    flt = _last_json(raw, '"current_exception"')
    if flt:
        exc = flt.get("current_exception")
        if exc and exc != "Thread(非异常)":
            findings.append(
                f"调试器：CPU 正处在 **{exc}** 异常处理中"
                + (f"；出错指令 = {flt.get('faulting_function')}+{flt.get('faulting_offset')}"
                   if flt.get("faulting_pc") else "")
                + (f"；CFSR={flt.get('fault_regs', {}).get('CFSR')}" if flt.get("fault_regs") else ""))
        else:
            findings.append("调试器：**不在异常处理中**（IPSR=0）-> 固件没崩，是行为不对")

    raw = open(os.path.join(ev, "debug_snapshot.txt"), encoding="utf-8").read()
    snap = _last_json(raw, '"pc"')
    if snap:
        findings.append(f"调试器：PC 落在 **{snap.get('pc_function')}{snap.get('pc_offset')}**"
                        " —— 固件没崩、在正常跑（细节见原始证据）")

    return findings


def write_report(path, steps, notes, ev, findings):
    lines = ["# HIL 轮次报告", ""]
    lines += [f"- 结果：**FAIL**", f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"]
    fp = firmware_fingerprint()
    lines += [f"- 固件指纹：`{fp.get('axf_sha256')}`（mtime {fp.get('axf_mtime')}）",
              f"- 代码版本：`{fp.get('git_head')}`"
              + ("（**有未提交改动**）" if fp.get("git_dirty") else "（干净）"), ""]

    lines += ["## 各步骤结果", "", "| 步骤 | 结果 | 说明 |", "|---|---|---|"]
    for name, ok, detail in steps:
        lines.append(f"| {name} | {'✅ PASS' if ok else '❌ FAIL'} | {detail} |")
    lines.append("")

    lines += ["## 判断依据（工具自动归纳，**不是结论**）", ""]
    lines += [f"- {f}" for f in findings] or ["- （未能从证据中归纳出条目，请直接看原始证据）"]
    lines.append("")

    lines += ["## 原始证据", ""]
    for f in sorted(os.listdir(ev)):
        lines.append(f"- `evidence/{f}`")
    if os.path.exists(os.path.join(os.path.dirname(path), "pytest_output.txt")):
        lines.append("- `pytest_output.txt`")
    if os.path.exists(os.path.join(os.path.dirname(path), "build_output.txt")):
        lines.append("- `build_output.txt`")
    lines.append("")

    lines += ["## 下一步（由**人**决定）", "",
              "本脚本**不会自动修改代码**。建议顺序：",
              "1. 先看「先失败的那一步」——如果 build 就挂了，后面都不用看",
              "2. 对照 `evidence/` 里的原始字节 / 寄存器 / 故障现场，形成假设",
              "3. **先假设、再最小改动**，改完重跑一轮 `python tools/hil.py`",
              "4. 修好后删掉/更新对应的测试标记（若有 xfail）", ""]

    lines += ["## 本报告**不能**证明的", "",
              "- 只能证明「这一版固件、在这一台板子上、这一次运行」的表现",
              "- 不覆盖：长时稳定性、温度/电压边界、其它板子/其它 SD 卡、EMC", ""]

    open(path, "w", encoding="utf-8").write("\n".join(lines))


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="HIL 主控：构建->烧录->判定->失败取证->报告")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-flash", action="store_true")
    ap.add_argument("--attempts", type=int, default=1,
                    help=f"最多几轮（默认 1，硬上限 {MAX_ATTEMPTS_HARD}）；"
                         "用于对付偶发的硬件掉线，不是「重试直到成功」")
    a = ap.parse_args()
    attempts = max(1, min(a.attempts, MAX_ATTEMPTS_HARD))

    for attempt in range(1, attempts + 1):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        steps, notes = [], []

        def finish(ok):
            rundir = os.path.join(RUNS, f"{stamp}_{'PASS' if ok else 'FAIL'}")
            os.makedirs(rundir, exist_ok=True)
            if not ok:
                ev = os.path.join(rundir, "evidence")
                if not os.path.isdir(ev):
                    collect_evidence(ev, notes)
                try:
                    write_report(os.path.join(rundir, "report.md"),
                                 steps, notes, ev, analyze(ev))
                    notes.append("报告: report.md")
                except Exception as e:            # 报告失败不能吃掉主结论
                    notes.append(f"报告生成失败: {e}")
            result = {"result": "PASS" if ok else "FAIL", "run": os.path.basename(rundir),
                      "attempt": attempt,
                      "steps": [{"step": n, "ok": o, "detail": d} for n, o, d in steps],
                      "notes": notes, "fingerprint": firmware_fingerprint()}
            with open(os.path.join(rundir, "result.json"), "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(json.dumps({"result": result["result"], "run": result["run"],
                              "steps": result["steps"], "notes": notes},
                             ensure_ascii=False, indent=2))
            print(f"产物目录: {os.path.relpath(rundir, ROOT)}")
            return 0 if ok else 1

        # 1) 构建
        if not a.skip_build:
            rc, out, err = run([PY, os.path.join(ROOT, "tools", "build.py")])
            ok = rc == 0
            steps.append(("build", ok, "四判据 PASS" if ok else "构建失败（见证据）"))
            if not ok:
                d = os.path.join(RUNS, f"{stamp}_FAIL")
                os.makedirs(d, exist_ok=True)
                open(os.path.join(d, "build_output.txt"), "w", encoding="utf-8").write(out + err)
                return finish(False)
        else:
            steps.append(("build", True, "skipped"))

        # 2) 烧录
        if not a.skip_flash:
            rc, out, err = run([UVISION, "-f", "SDIO_test.uvprojx", "-j0", "-o", "flash.log"],
                               cwd=os.path.join(ROOT, "MDK-ARM"), timeout=180)
            log = (open(FLASH_LOG, encoding="utf-8", errors="replace").read()
                   if os.path.exists(FLASH_LOG) else "")
            ok = (rc == 0) and ("Verify OK" in log)
            steps.append(("flash", ok, "Verify OK" if ok else f"退出码={rc}；{log.strip()[:160]}"))
            if not ok:
                return finish(False)
            time.sleep(0.5)
        else:
            steps.append(("flash", True, "skipped"))

        # 3) 真机判定
        rc, out, err = run([PY, "-m", "pytest", "tests/test_device.py", "-q"], timeout=900)
        ok = rc == 0
        tail = [l for l in out.strip().splitlines() if l.strip()][-1:] or [""]
        steps.append(("device-tests", ok, tail[0][:120]))
        if not ok:
            d = os.path.join(RUNS, f"{stamp}_FAIL")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "pytest_output.txt"), "w", encoding="utf-8").write(out + "\n" + err)
            notes.append("真机用例失败；已自动采集现场并生成报告")
            return finish(False)

        return finish(True)

    return 1


if __name__ == "__main__":
    sys.exit(main())
