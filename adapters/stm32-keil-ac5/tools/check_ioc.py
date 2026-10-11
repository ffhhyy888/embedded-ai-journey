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
退出码：0 = 一致；1 = 有漂移（列出具体项）
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOC = os.path.join(ROOT, "SDIO_test.ioc")
HAL_CONF = os.path.join(ROOT, "Core", "Inc", "stm32f4xx_hal_conf.h")
MAIN_C = os.path.join(ROOT, "Core", "Src", "main.c")


def parse_ioc():
    kv = {}
    for line in open(IOC, encoding="utf-8", errors="replace"):
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
    ioc = parse_ioc()
    code = parse_code()
    bad = []

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
