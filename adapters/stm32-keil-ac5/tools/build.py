#!/usr/bin/env python3
"""命令行构建 + 自动判定（工程名 / 路径 / 阈值取自 tools/project.cfg）。

工程配置: tools/project.cfg（缺失或字段缺失即报错，**不静默兜底**）

用法:
    python tools/build.py                # 全量重建 -r（默认，最可靠）
    python tools/build.py --incremental  # 增量构建 -b（快，但可能"什么都没做"）

PASS 判据（四条须同时满足，均为实测标定所得，见 ~/embedded-ai-journey/AUTOLAB_TESTS.md）:
    1. 退出码 == 0                          —— 0=成功 2=编译错误 15=工程不可读
    2. 日志匹配 "0 Error(s), 0 Warning(s)"
    3. 日志中 compiling/assembling/linking 行数 >= 阈值 —— 防止增量构建"假 0"
       （阈值为 tools/project.cfg 的 min_compile_lines，换工程必须重新标定）
    4. 产物 .axf 时间戳晚于本次构建启动时刻              —— 防止"没产出新固件"

退出码: PASS -> 0, FAIL -> 1   （可直接被 CI / Agent 调用）
日志归档: tools/build_logs/build_<YYYYmmdd_HHMMSS>_<PASS|FAIL>.log
"""

import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

import project_config   # 与本文件同目录（拷进工程后同样在 tools/ 下）

# ------------------------------------------------- 路径常量（工程级 / 机器级分开）
# 工程级（工程名、工程文件、产物路径、阈值）来自 tools/project.cfg -> resolve_paths()；
# 机器级（Keil 装在哪、构建超时）换电脑才变 -> 环境变量 + 默认值。
TOOLS_DIR = Path(__file__).resolve().parent
LOG_DIR = TOOLS_DIR / "build_logs"

UVISION = Path(os.environ.get("AUTOLAB_UVISION", r"F:\keil\UV4\uVision.com"))
BUILD_TIMEOUT = int(os.environ.get("AUTOLAB_BUILD_TIMEOUT", "600"))  # 秒；防编译器挂死

# ⚠️ UVISION 必须留在**模块级**：preflight.py 靠 `import build` 读它做环境自检。
#    tests/test_build_paths.py 有断言锁住这条契约。


class Paths(NamedTuple):
    """一个工程的构建路径 —— 全部来自 tools/project.cfg。"""
    mdk: Path               # uVision 的工作目录（uvprojx 所在目录）
    uvprojx_name: str       # 传给 uVision 的工程文件名
    axf: Path               # 构建产物；判据 4 读它的时间戳
    tmp_log: Path           # uVision -o 的兜底日志（被 .gitignore 忽略）
    min_compile_lines: int  # 判据 3 的阈值


def _cfg():
    """加载工程配置。

    ⚠️ 必须在**函数里**调用，不能在 import 期执行：preflight.py 会 `import build`
       取 UVISION；若 import 期就要求 project.cfg，"新工程还没建配置"会表现为
       "preflight 自己崩了"，把人带偏。
    """
    return project_config.load(TOOLS_DIR)


def resolve_paths(cfg) -> Paths:
    """把工程配置解析成构建路径（纯计算，不碰文件系统）。"""
    uvprojx = cfg.path("build", "uvprojx")
    return Paths(
        mdk=uvprojx.parent,
        uvprojx_name=uvprojx.name,
        axf=cfg.path("build", "axf"),
        tmp_log=uvprojx.parent / "_last_build.log",
        min_compile_lines=cfg.int("build", "min_compile_lines"),
    )


def run_build(rebuild: bool, paths: Paths):
    """执行构建。返回 (退出码, 日志文本, 启动时刻)。"""
    flag = "-r" if rebuild else "-b"
    tmp_log = paths.tmp_log                   # 兜底副本，被 .gitignore 忽略

    t0 = time.time()                          # 判据 4 的时间基准，必须在构建前取
    proc = subprocess.run(
        [str(UVISION), flag, paths.uvprojx_name, "-j0", "-o", str(tmp_log)],
        cwd=str(paths.mdk),
        capture_output=True,
        timeout=BUILD_TIMEOUT,      # 审计发现原先没有 timeout：编译器挂死会拖死整条流水线
    )

    # 工具链输出为 GBK；errors="replace" 保证绝不因编码崩掉
    text = proc.stdout.decode("gbk", errors="replace")
    if tmp_log.exists():
        text += tmp_log.read_text(encoding="gbk", errors="replace")

    return proc.returncode, text, t0


def evaluate(exit_code: int, text: str, t0: float, paths: Paths):
    """四条判据逐条检查，返回 (是否全部通过, [(判据名, 是否通过, 实测细节)])。"""
    checks = []

    # 判据 1
    checks.append(("退出码 == 0", exit_code == 0, f"实际 {exit_code}"))

    # 判据 2
    m = re.search(r"(\d+) Error\(s\), (\d+) Warning\(s\)", text)
    if m:
        ok = m.group(1) == "0" and m.group(2) == "0"
        checks.append(("日志 0 Error / 0 Warning", ok, m.group(0)))
    else:
        checks.append(("日志 0 Error / 0 Warning", False, "未找到统计行"))

    # 判据 3 —— 直接针对 2026-10-09 的"增量假 0"事件
    n = len(re.findall(r"^(?:compiling|assembling|linking)", text, re.MULTILINE))
    checks.append((f"编译动作 >= {paths.min_compile_lines} 条",
                   n >= paths.min_compile_lines, f"实际 {n} 条"))

    # 判据 4 —— 直接针对 "Target not created."（失败时不产出固件）
    if paths.axf.exists():
        mtime = paths.axf.stat().st_mtime
        checks.append((
            "产物 .axf 时间戳已更新",
            mtime > t0,
            datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
        ))
    else:
        checks.append(("产物 .axf 时间戳已更新", False, "文件不存在 (Target not created?)"))

    return all(ok for _, ok, _ in checks), checks


def archive_log(text: str, passed: bool) -> Path:
    """按时间戳归档，杜绝 2026-10-10 那次"日志被覆盖"。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"build_{stamp}_{'PASS' if passed else 'FAIL'}.log"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    if not UVISION.exists():
        print(f"[FAIL] 找不到构建工具: {UVISION}")
        return 1

    try:
        paths = resolve_paths(_cfg())
    except project_config.ConfigError as e:
        print(f"[FAIL] 工程配置不可用：\n{e}")
        return 1

    rebuild = "--incremental" not in sys.argv
    print(f"=== Build: {'全量重建 (-r)' if rebuild else '增量构建 (-b)'} ===")
    print(f"    工程: {paths.uvprojx_name}   (cwd={paths.mdk})")

    exit_code, text, t0 = run_build(rebuild, paths)
    passed, checks = evaluate(exit_code, text, t0, paths)
    log_path = archive_log(text, passed)

    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}   ({detail})")

    print(f"\n结果: {'PASS' if passed else 'FAIL'}")
    print(f"日志: {log_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
