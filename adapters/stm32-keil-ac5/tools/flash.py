#!/usr/bin/env python3
"""Keil 烧录适配器 —— uVision.com -f（Erase Sectors + Program + Verify + Reset&Run）。

由 skeleton/tools/hil.py 调用：把"烧录"这件**工具链专属**的事从通用编排里分出来，
骨架因此不必知道 Keil 的存在。

⚠️ 本工具会改变目标板上的程序。执行前必须获得人工授权
   （见 docs/engineering/TEST_AND_RELEASE_POLICY.md 第 4 节）。
   未授权时用 --dry-run：只打印将执行的命令，**不启动 uVision**。

退出码（沿用平台 0/1/2 约定）：
    0 = PASS   —— 烧录执行了，且日志出现 "Verify OK"
    1 = FAIL   —— 烧录执行了但未通过（被测对象的问题）
    2 = ERROR  —— 工具/配置/参数问题；**--dry-run 的"未执行"也是 2**
调用方应读结构化 JSON 的 result 字段（PASS / FAIL / NOT_RUN / ERROR）来区分 ——
**绝不用 0 表示"没跑"**（NOT RUN ≠ PASS）。

配置: tools/project.cfg 的 [build] uvprojx 与 [flash] log
机器级: AUTOLAB_UVISION（Keil 位置）、AUTOLAB_FLASH_TIMEOUT（超时秒数，默认 180）
"""

import argparse
import json
import os
import subprocess
import sys

import build              # 同目录；复用配置加载与路径解析，避免两处各解析一遍
import project_config

DEFAULT_FLASH_TIMEOUT = 180


def emit(result, **kv):
    """结构化结果：一行 JSON，人和机器都能判读。"""
    print(json.dumps({"result": result, **kv}, ensure_ascii=False))


def resolve_timeout(cli_value=None) -> int:
    """超时优先级：--timeout > AUTOLAB_FLASH_TIMEOUT > 180 秒。"""
    return int(project_config.resolve(cli_value, "AUTOLAB_FLASH_TIMEOUT", None,
                                      DEFAULT_FLASH_TIMEOUT))


def build_command(paths, log_path) -> list:
    """构造 uVision 烧录命令（纯函数，便于在没有板子/Keil 的环境里验证）。"""
    return [str(build.UVISION), "-f", paths.uvprojx_name, "-j0", "-o", str(log_path)]


def evaluate(returncode: int, log_text: str):
    """逐条判定，返回 (是否通过, [(判据名, 是否通过, 实测细节)])。

    刻意拆成两条而不是一个 and：退出码正常但日志没有 "Verify OK" 时，
    能直接看出是"烧了但没验过"，而不是笼统的一个 FAIL。
    """
    found = "Verify OK" in log_text
    detail = ("已出现" if found
              else ("日志为空或不存在" if not log_text else "未出现（未执行 Verify 或烧录失败）"))
    checks = [
        ("退出码 == 0", returncode == 0, f"实际 {returncode}"),
        ("日志出现 'Verify OK'", found, detail),
    ]
    return all(ok for _, ok, _ in checks), checks


def run_flash(paths, log_path, timeout):
    """真正执行烧录。返回 (退出码, 日志文本)。"""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")   # 统一子进程编码，防 GBK 乱码
    proc = subprocess.run(build_command(paths, log_path), cwd=str(paths.mdk),
                          capture_output=True, timeout=timeout, env=env)
    stdout = proc.stdout.decode("gbk", errors="replace")   # uVision 输出是 GBK
    log = log_path.read_text(encoding="gbk", errors="replace") if log_path.exists() else ""
    return proc.returncode, stdout + log


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Keil 烧录适配器（⚠️ 会改动目标板上的程序，需人工授权）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将执行的命令，不启动 uVision（返回 2 = 未执行）")
    ap.add_argument("--timeout", type=int, default=None, help="烧录超时秒数")
    a = ap.parse_args()

    try:
        cfg = build._cfg()
        paths = build.resolve_paths(cfg)
        log_path = cfg.path("flash", "log")
    except project_config.ConfigError as e:
        emit("ERROR", error=f"工程配置不可用：{e}")
        return 2

    if a.dry_run:
        emit("NOT_RUN", mode="dry-run", cwd=str(paths.mdk),
             argv=build_command(paths, log_path), timeout=resolve_timeout(a.timeout))
        print("⚠️ 未执行：--dry-run 不会启动 uVision，也不会改动目标板", file=sys.stderr)
        return 2

    if not build.UVISION.exists():
        emit("ERROR", error=f"找不到烧录工具: {build.UVISION}")
        return 2

    timeout = resolve_timeout(a.timeout)
    try:
        rc, text = run_flash(paths, log_path, timeout)
    except subprocess.TimeoutExpired:
        emit("ERROR", error=f"烧录超时（>{timeout}s）—— 按平台约定不自动重试")
        return 2

    passed, checks = evaluate(rc, text)
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}   ({detail})")
    emit("PASS" if passed else "FAIL", exit_code=rc, log=str(log_path))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
