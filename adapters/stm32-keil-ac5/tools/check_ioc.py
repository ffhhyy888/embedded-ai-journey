#!/usr/bin/env python3
"""CubeMX 配置一致性检查 —— `.ioc` ↔ 生成代码。

为什么需要它（本工程真实踩过两次）
    `.ioc` 是 CubeMX 的配置源，代码是从它生成的。手工改过代码之后两者很容易脱节；
    一旦有人打开 CubeMX 点"生成代码"，**手改的修复会被悄悄冲掉**：
      · 2026-10-10 时钟修复改了代码（HSE 25->8MHz、PLLM 25->8），.ioc 仍写着 25MHz
        -> 重新生成会把 Stage 3 花几小时定位的时钟问题原样带回来
      · sdio.c 的 Init.BusWide 被 CubeMX 从 1B 改回 4B（同类陷阱）

检查什么
    · RCC.HSE_VALUE               ↔ stm32f4xx_hal_conf.h 的 HSE_VALUE 宏
    · RCC.PLLM / PLLN / PLLQ      ↔ main.c 里 SystemClock_Config 的赋值
    · RCC.APB2CLKDivider          ↔ main.c 的 APB2CLKDivider
    · .ioc 自洽：由 HSE/PLLM*PLLN/PLLP 推出的 SYSCLK 应等于它自己记录的 SYSCLKFreq_VALUE

被检查的 .ioc 从哪来
    tools/project.cfg 的 [ioc] file —— 换工程不需要改本文件。
    ⚠️ 这里以前写死 "SDIO_test.ioc"，后果不是"报个清楚的错"，而是**换工程后
       本检查静默失效**：找不到 SDIO_test.ioc -> FileNotFoundError -> 退出码非 0
       -> 被调用方当成"有漂移"。也就是说对除 SDIO_test 以外的任何工程，
       这道防护只产生噪声、不产生保护。

退出码：0 = 一致；1 = 有漂移（列出具体项）；2 = 配置/环境错误（无法确定 .ioc 路径）
"""
import os
import re
import sys

import project_config   # 与本文件同目录（拷进工程后同样在 tools/ 下）

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
HAL_CONF = os.path.join(ROOT, "Core", "Inc", "stm32f4xx_hal_conf.h")
MAIN_C = os.path.join(ROOT, "Core", "Src", "main.c")


def _cfg():
    """加载工程配置。

    ⚠️ 必须在**函数里**调用，不能在 import 期执行 —— 与 build.py 同一约定。
    """
    return project_config.load(TOOLS_DIR)


def ioc_path():
    """本工程 .ioc 的绝对路径 —— 来自 tools/project.cfg 的 [ioc] file。

    只读配置，不在这里判断 [ioc] enabled：启用与否的判定在调用方
    （tests/test_ioc.py）—— 那才是它既有语义所在，这里不重复也不改变它。
    字段缺失（cfg 没有 [ioc] file）即报错，不静默兜底到任何固定文件名。
    """
    return str(_cfg().path("ioc", "file"))


def parse_ioc(path):
    kv = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv


def parse_code():
    hal = open(HAL_CONF, encoding="utf-8", errors="replace").read()
    m = re.search(r"#define\s+HSE_VALUE\s+(\d+)U?", hal)
    code = {"HSE_VALUE": int(m.group(1)) if m else None}

    main = open(MAIN_C, encoding="utf-8", errors="replace").read()
    for key, pat in (("PLLM", r"PLL\.PLLM\s*=\s*(\d+)"),
                     ("PLLN", r"PLL\.PLLN\s*=\s*(\d+)"),
                     ("PLLQ", r"PLL\.PLLQ\s*=\s*(\d+)"),
                     ("APB2DIV", r"APB2CLKDivider\s*=\s*RCC_HCLK_DIV(\d+)"),
                     ("PLLP", r"PLL\.PLLP\s*=\s*RCC_PLLP_DIV(\d+)")):
        m = re.search(pat, main)
        code[key] = int(m.group(1)) if m else None
    return code


def main():
    try:
        ioc_file = ioc_path()
    except (project_config.ConfigError, OSError) as e:
        # 配置读不到 = 工具自己坏了（退出码 2），**不是**"有漂移"（1）。
        # 绝不静默通过：宁可报清楚，也不让这道防护悄悄消失。
        print(f"[FAIL] 无法确定 .ioc 路径：{e}")
        return 2

    try:
        ioc = parse_ioc(ioc_file)
    except OSError as e:
        print(f"[FAIL] 读不到 {ioc_file}：{e}")
        return 2

    code = parse_code()
    bad = []

    # 明示"检查的是哪一个 .ioc" —— 换工程后一眼能看出它读的是配置而不是固定文件名
    print(f"  检查 .ioc = {ioc_file}")

    def cmp(name, a, b):
        if a != b:
            bad.append(f"{name}: .ioc={a}  代码={b}")

    cmp("HSE_VALUE", int(ioc.get("RCC.HSE_VALUE", 0)), code["HSE_VALUE"])
    cmp("PLLM", int(ioc.get("RCC.PLLM", 0)), code["PLLM"])
    cmp("PLLN", int(ioc.get("RCC.PLLN", 0)), code["PLLN"])
    cmp("PLLQ", int(ioc.get("RCC.PLLQ", 0)), code["PLLQ"])
    cmp("APB2 分频", int(ioc.get("RCC.APB2CLKDivider", "X").replace("RCC_HCLK_DIV", "")),
        code["APB2DIV"])

    # .ioc 自身自洽：按它自己的参数算 SYSCLK，看是否等于它记录的 SYSCLKFreq_VALUE
    try:
        hse = int(ioc["RCC.HSE_VALUE"]); pllm = int(ioc["RCC.PLLM"])
        plln = int(ioc["RCC.PLLN"]); pllq = int(ioc["RCC.PLLQ"])
        pllp = code["PLLP"]
        sysclk = hse // pllm * plln // pllp
        recorded = int(ioc["RCC.SYSCLKFreq_VALUE"])
        if sysclk != recorded:
            bad.append(f".ioc 自身不自洽: 由 HSE/PLLM/PLLN/PLLP 算出 SYSCLK={sysclk}"
                       f"，但它记录的是 {recorded}")
        # 顺带看一眼 UART 用的 APB2（BaudRate 就是按它算的）
        apb2 = recorded // 2
        print(f"  由 .ioc 推出的 SYSCLK = {sysclk/1e6:.1f} MHz, APB2 = {apb2/1e6:.1f} MHz")
    except Exception as e:
        bad.append(f"无法自洽校验: {e}")

    if bad:
        print("[FAIL] .ioc 与代码不一致（CubeMX 重新生成会冲掉这些修复）：")
        for b in bad:
            print("   -", b)
        return 1
    print("[PASS] .ioc 与代码一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
