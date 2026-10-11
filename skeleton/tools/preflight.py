#!/usr/bin/env python3
"""环境自检 —— 换电脑 / 换板子 / 换了 USB 口，先跑这一条。

为什么需要它
    这套闭环的脆弱点**不在代码里，在环境里**：Keil 装在哪、pip 依赖装没装、
    串口号变了没、调试器认不认得到。这些问题在别的工具里表现为
    "莫名其妙的失败"，最后往往被误判成"固件坏了"。
    本脚本把它们逐项查一遍，**对每一项失败都直接打印可复制的修复命令**。

用法
    python tools/preflight.py                # 只查不碰硬件
    python tools/preflight.py --with-hardware  # 额外探测串口/调试器（不复位、不烧录）
退出码：0 = 关键项全过；1 = 有致命项未过
"""
import argparse
import importlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

results = []          # (级别, 项目, 详情, 修复建议)


def add(level, name, detail, fix=""):
    results.append((level, name, detail, fix))


def check_python():
    v = sys.version_info
    if v >= (3, 10):
        add("OK", "Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        add("FAIL", "Python", f"{v.major}.{v.minor}（需要 >= 3.10）",
            "安装 Python 3.10+")


def check_pip_deps():
    for mod, ver_attr in (("serial", "VERSION"), ("pytest", "__version__"),
                          ("pyocd", "__version__"), ("ziglang", None)):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            add("FAIL", f"pip:{mod}", "未安装",
                "python -m pip install -r requirements.txt")
            continue
        ver = ""
        if mod == "ziglang":
            try:
                ver = subprocess.run([PY, "-m", "ziglang", "version"],
                                     capture_output=True, timeout=60
                                     ).stdout.decode("utf-8", "replace").strip()
            except Exception:
                ver = "?"
        else:
            ver = getattr(m, ver_attr, "?") if ver_attr else "?"
        add("OK", f"pip:{mod}", str(ver))


def check_toolchain():
    """构建/调试工具链的检查**依赖适配器** —— 通用骨架里没有它们。

    关键区分（不能混）：
      · 找不到适配器模块 `build`     -> SKIP（骨架本来就不带芯片适配器）
      · 适配器在、但它自己缺依赖     -> FAIL 并如实报错
      · 适配器自己抛异常             -> FAIL 并如实报错
    否则会把"适配器的真实故障"伪装成"没装适配器"，把人带偏。
    """
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    try:
        import build as build_mod                 # 读它解析出来的路径，避免两处不一致
    except ModuleNotFoundError as e:
        if e.name == "build":
            add("SKIP", "构建工具链", "未检测到适配器 tools/build.py",
                "通用骨架不绑定具体芯片；拷入 adapters/<芯片家族>/ 后此项才有意义")
            return
        add("FAIL", "构建工具链",
            f"适配器导入失败：缺模块 `{e.name}` —— 这是适配器自己的依赖没装，不是缺适配器",
            f"python -m pip install {e.name}   （或按适配器 README 安装）")
        return
    except Exception as e:
        add("FAIL", "构建工具链", f"适配器导入时抛异常：{type(e).__name__}: {e}",
            "这是适配器自身的 bug，请修 tools/build.py（不是环境问题）")
        return

    uv = str(build_mod.UVISION)
    if os.path.exists(uv):
        add("OK", "Keil uVision(.com)", uv)
    else:
        add("FAIL", "Keil uVision(.com)", f"找不到: {uv}",
            "装 Keil MDK；或设环境变量 AUTOLAB_UVISION 指向你的 uVision.com")

    fromelf = os.environ.get("AUTOLAB_FROMELF", r"F:\keil\ARM\ARMCC\bin\fromelf.exe")
    if os.path.exists(fromelf):
        add("OK", "fromelf", fromelf)
    else:
        add("FAIL", "fromelf", f"找不到: {fromelf}",
            "设环境变量 AUTOLAB_FROMELF 指向你的 fromelf.exe（在 Keil 的 ARMCC\\bin 下）")


def check_git():
    try:
        rc = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            capture_output=True, cwd=ROOT, timeout=30)
        ok = rc.returncode == 0 and b"true" in rc.stdout
    except Exception:
        ok = False
    if not ok:
        add("FAIL", "git 仓库", "不是 git 仓库", "确认工程根有 .git；或 git init")
        return
    hp = subprocess.run(["git", "config", "core.hooksPath"],
                        capture_output=True, cwd=ROOT, timeout=30
                        ).stdout.decode("utf-8", "replace").strip()
    if hp == "tools/hooks":
        add("OK", "git hook", "core.hooksPath = tools/hooks（提交前自动跑无板层）")
    else:
        add("WARN", "git hook", f"core.hooksPath = {hp or '(未设)'}",
            "git config core.hooksPath tools/hooks   # 启用提交前自动检查")


def check_hardware():
    port = os.environ.get("AUTOLAB_PORT", "COM9")
    try:
        import serial
        s = serial.Serial(port, 115200, timeout=1)
        s.close()
        add("OK", "串口", f"{port} 可打开")
    except Exception as e:
        add("FAIL", "串口", f"{port} 打不开: {e}",
            f"确认 USB-TTL 已插好；端口号可能变了 -> 设环境变量 AUTOLAB_PORT，"
            f"或查设备管理器。当前可见端口：{list_ports()}")
        return
    try:
        out = subprocess.run([PY, "-m", "pyocd", "list"], capture_output=True,
                             timeout=90).stdout.decode("utf-8", "replace")
        add("OK" if "STM32" in out or "STLink" in out else "WARN", "调试器",
            "认出探针" if "STM32" in out or "STLink" in out else "未认出探针",
            "" if "STM32" in out or "STLink" in out else "确认 ST-Link 插好")
    except Exception as e:
        add("WARN", "调试器", f"探测失败: {e}", "确认 ST-Link 插好")


def list_ports():
    try:
        from serial.tools import list_ports as lp
        return [p.device for p in lp.comports()]
    except Exception:
        return "?"


def main():
    ap = argparse.ArgumentParser(description="环境自检（换机/换板先跑这个）")
    ap.add_argument("--with-hardware", action="store_true",
                    help="额外探测串口与调试器（只打开端口/列探针，不复位、不烧录）")
    a = ap.parse_args()

    check_python()
    check_pip_deps()
    check_toolchain()
    check_git()
    if a.with_hardware:
        check_hardware()

    width = max(len(n) for _, n, _, _ in results) + 2
    print("=" * 72)
    print("环境自检")
    print("=" * 72)
    for level, name, detail, fix in results:
        mark = {"OK": "[ OK ]", "WARN": "[WARN]", "FAIL": "[MISS]", "SKIP": "[SKIP]"}[level]
        print(f"{mark} {name:<{width}} {detail}")
        if fix:
            print(f"       └─ 修复: {fix}")
    fails = [r for r in results if r[0] == "FAIL"]
    print("-" * 72)
    if fails:
        print(f"结论：{len(fails)} 项致命问题未解决 -> 上层工具会以各种奇怪的方式失败")
        return 1
    print("结论：关键项全部就绪")
    skipped = [r[1] for r in results if r[0] == "SKIP"]
    if skipped:
        print(f"（跳过 {len(skipped)} 项：{', '.join(skipped)}）")
    if not a.with_hardware:
        print("（硬件未探测；要查板子/串口/调试器，加 --with-hardware）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
