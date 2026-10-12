#!/usr/bin/env python3
"""命令行构建 + 自动判定（工程名 / 路径 / 阈值取自 tools/project.cfg）。

工程配置: tools/project.cfg（缺失或字段缺失即报错，**不静默兜底**）

用法:
    python tools/build.py                # 全量重建 -r（默认，最可靠）
    python tools/build.py --incremental  # 增量构建 -b（快，但可能"什么都没做"）
    python tools/build.py --measure      # 测量模式：一次全量重建，只测动作数

PASS 判据（四条须同时满足，均为实测标定所得，见 ~/embedded-ai-journey/AUTOLAB_TESTS.md）:
    1. 退出码 == 0                          —— 0=成功 2=编译错误 15=工程不可读
    2. 日志匹配 "0 Error(s), 0 Warning(s)"
    3. 日志中 compiling/assembling/linking 行数 >= 阈值 —— 防止增量构建"假 0"
       （阈值为 tools/project.cfg 的 min_compile_lines，换工程必须重新标定；
        日志来源**只取一份**：uVision 的 stdout 与 -o 内容相同，相加会双重计数）
    4. 产物 .axf 时间戳晚于本次构建启动时刻              —— 防止"没产出新固件"

退出码: PASS -> 0, FAIL -> 1   （可直接被 CI / Agent 调用）
日志归档: tools/build_logs/build_<YYYYmmdd_HHMMSS>_<PASS|FAIL>.log

测量模式（--measure）
    给"阈值还没标定的新工程"用的**唯一**入口：此刻 project.cfg 里没有
    min_compile_lines，正常路径会因为"配置缺字段"直接报错退出 1 —— 有意如此。

    ⚠️ 测量模式**只**放宽判据 3 的阈值这一条，其余一条都不放宽：
      退出码、0 Error/0 Warning、AXF 时间戳仍全部照常判定。
      因此"构建失败"永远不可能被测量模式判成成功。
    它把结果作为**一行 ASCII JSON** 打到 stdout（前缀见 MEASURE_PREFIX），
    供 `autolab calibrate` 读取；`autolab calibrate` 再据此决定写不写阈值。
"""

import json
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

# 测量模式下结果行的前缀。`autolab calibrate` 按它认这一行 ——
# 不许在别的地方（例如普通构建的输出里）打同前缀，否则标定会读到脏证据。
MEASURE_PREFIX = "AUTOLAB_MEASURE="

# 判据 3 在测量模式下的行名。单独提出来，好让调用方按名字把它标成"不判"而不是"PASS"。
MEASURE_CHECK_NAME = "编译动作（测量模式：本条不设阈值）"


class Paths(NamedTuple):
    """一个工程的构建路径 —— 全部来自 tools/project.cfg。"""
    mdk: Path               # uVision 的工作目录（uvprojx 所在目录）
    uvprojx_name: str       # 传给 uVision 的工程文件名
    axf: Path               # 构建产物；判据 4 读它的时间戳
    tmp_log: Path           # uVision -o 的兜底日志（被 .gitignore 忽略）
    min_compile_lines: int  # 判据 3 的阈值；测量模式下尚未标定 -> None


def _cfg():
    """加载工程配置。

    ⚠️ 必须在**函数里**调用，不能在 import 期执行：preflight.py 会 `import build`
       取 UVISION；若 import 期就要求 project.cfg，"新工程还没建配置"会表现为
       "preflight 自己崩了"，把人带偏。
    """
    return project_config.load(TOOLS_DIR)


def resolve_paths(cfg, require_threshold: bool = True) -> Paths:
    """把工程配置解析成构建路径（纯计算，不碰文件系统）。

    require_threshold=False 是**测量模式专用**：新工程第一次构建时阈值本来就还没标定，
    否则连"测一次动作数"都跑不了，永远标不出来。
    ⚠️ 它只放宽"阈值必须存在"这一条 —— 路径项一个都不放宽；而且放宽的结果是
       `min_compile_lines=None`，evaluate() 见到 None **不会**判通过（见该函数）。
    """
    uvprojx = cfg.path("build", "uvprojx")
    if require_threshold:
        thr = cfg.int("build", "min_compile_lines")      # 缺失即抛 ConfigKeyMissing
    elif cfg.has("build", "min_compile_lines"):
        thr = cfg.int("build", "min_compile_lines")      # 有就照常校验类型
    else:
        thr = None
    return Paths(
        mdk=uvprojx.parent,
        uvprojx_name=uvprojx.name,
        axf=cfg.path("build", "axf"),
        tmp_log=uvprojx.parent / "_last_build.log",
        min_compile_lines=thr,
    )


def pick_log_source(stdout_text: str, tmp_log: Path, t0: float) -> str:
    """从 stdout 与 `-o` 日志里挑出**唯一**的权威日志来源。

    ⚠️ 为什么必须只取一份（2026-10-12 缺陷修复）
        uVision 把**同一份**构建日志**同时**写到 stdout 和 `-o` 文件。旧版
        run_build() 把两者**相加**，于是每个编译动作被统计两次 —— 判据 3 拿到
        2× 真实动作数（实测 48 = 24×2），阈值等于被绕过一半。
        因此这里**绝不**把两份内容拼接。

    选择规则
        1. 优先本次构建的 `-o` 日志（它才带 `-o` 指定的完整内容）；
        2. 仅当它 不存在 / 为空 / 时间戳早于 t0（是上一次构建的残留）时，
           才回退到 stdout。回退是**降级**，但不静默 —— 打一条 [WARN] 说明原因。

    安全性
        回退只会换来源、不会相加，故任何一种情形下动作数都不会翻倍；
        且实测 uVision 的 stdout 与 `-o` 内容一致，回退不影响判据结果。
    """
    if tmp_log.exists():
        log_text = tmp_log.read_text(encoding="gbk", errors="replace")
        if not log_text.strip():
            reason = "uVision -o 日志为空"
        elif tmp_log.stat().st_mtime < t0:
            reason = "uVision -o 日志早于本次构建（疑似上一次的残留）"
        else:
            return log_text                     # 本次构建的权威日志
    else:
        reason = "uVision -o 日志不存在"

    print(f"[WARN] 构建日志回退到 stdout：{reason}", file=sys.stderr)
    return stdout_text


def run_build(rebuild: bool, paths: Paths):
    """执行构建。返回 (退出码, 日志文本, 启动时刻)。"""
    flag = "-r" if rebuild else "-b"
    tmp_log = paths.tmp_log                   # uVision -o 写入的本次构建日志

    t0 = time.time()                          # 判据 4 的时间基准，必须在构建前取
    proc = subprocess.run(
        [str(UVISION), flag, paths.uvprojx_name, "-j0", "-o", str(tmp_log)],
        cwd=str(paths.mdk),
        capture_output=True,
        timeout=BUILD_TIMEOUT,      # 审计发现原先没有 timeout：编译器挂死会拖死整条流水线
    )

    # 工具链输出为 GBK；errors="replace" 保证绝不因编码崩掉
    stdout_text = proc.stdout.decode("gbk", errors="replace")
    # 日志来源**只取一份**（见 pick_log_source），绝不 stdout + 日志 相加
    return proc.returncode, pick_log_source(stdout_text, tmp_log, t0), t0


ACTION_RE = re.compile(r"^(?:compiling|assembling|linking)", re.MULTILINE)


def count_actions(text: str) -> int:
    """数编译动作条数。

    ⚠️ 调用方必须只传**一份**日志 —— uVision 把同一份日志同时写到 stdout 与 `-o`，
       把两者相加会让计数翻倍（2026-10-12 修掉的真实缺陷，见 pick_log_source）。
    """
    return len(ACTION_RE.findall(text))


def evaluate(exit_code: int, text: str, t0: float, paths: Paths, measure: bool = False):
    """四条判据逐条检查，返回 (是否全部通过, [(判据名, 是否通过, 实测细节)])。

    measure=True（测量模式）
        判据 3 换成一行"不设阈值"的**信息行**（ok=True，但名字不同，调用方据此
        把它显示成"不判"而不是"PASS"）。判据 1/2/4 **一条都不放宽** ——
        测量模式的作用是"让还没标定阈值的工程能测出动作数"，不是"让坏构建过关"。
        计数本身由 calibrate 负责检查（必须 >0 且两次相等）。
    """
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
    n = count_actions(text)
    if measure:
        checks.append((MEASURE_CHECK_NAME, True, f"实际 {n} 条"))
    elif paths.min_compile_lines is None:
        # 阈值没标定却又走普通路径 —— 这是调用方的错，**不能**当成通过放过去。
        checks.append(("编译动作 >= 阈值", False,
                       "min_compile_lines 未标定 —— 拒绝判为通过"))
    else:
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


def measure_payload(exit_code: int, text: str, t0: float, paths: Paths,
                    passed: bool, log_path: Path) -> dict:
    """测量模式的结果行内容。只放**实测**到的东西，不放推断值。

    `ok` 直接就是判据 1/2/4 的合取 —— calibrate 只认它，不重算。
    """
    m = re.search(r"(\d+) Error\(s\), (\d+) Warning\(s\)", text)
    return {
        "actions": count_actions(text),
        "threshold_judged": False,
        "errors": int(m.group(1)) if m else None,
        "warnings": int(m.group(2)) if m else None,
        "uvision_exit_code": exit_code,
        "axf": str(paths.axf),
        "axf_updated": paths.axf.is_file() and paths.axf.stat().st_mtime > t0,
        "log": str(log_path),
        "passed": bool(passed),
    }


def main() -> int:
    if not UVISION.exists():
        print(f"[FAIL] 找不到构建工具: {UVISION}")
        return 1

    measure = "--measure" in sys.argv
    try:
        paths = resolve_paths(_cfg(), require_threshold=not measure)
    except project_config.ConfigError as e:
        print(f"[FAIL] 工程配置不可用：\n{e}")
        return 1

    rebuild = "--incremental" not in sys.argv
    print(f"=== Build: {'全量重建 (-r)' if rebuild else '增量构建 (-b)'}"
          f"{' [测量模式]' if measure else ''} ===")
    print(f"    工程: {paths.uvprojx_name}   (cwd={paths.mdk})")
    if measure:
        print("    测量模式：只测动作数，判据 3 的阈值**不判**；"
              "判据 1/2/4 照常判。")

    exit_code, text, t0 = run_build(rebuild, paths)
    passed, checks = evaluate(exit_code, text, t0, paths, measure=measure)
    log_path = archive_log(text, passed)

    for name, ok, detail in checks:
        if measure and name == MEASURE_CHECK_NAME:
            tag = "N/A "                       # 不判 ≠ 通过
        else:
            tag = "PASS" if ok else "FAIL"
        print(f"  [{tag}] {name}   ({detail})")

    # 结果行**只**在测量模式下打，且必须是 ASCII（Windows 重定向到管道时是 GBK）。
    if measure:
        print(MEASURE_PREFIX + json.dumps(
            measure_payload(exit_code, text, t0, paths, passed, log_path),
            ensure_ascii=True, sort_keys=True))

    print(f"\n结果: {'PASS' if passed else 'FAIL'}")
    print(f"日志: {log_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
