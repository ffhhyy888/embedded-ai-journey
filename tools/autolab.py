# -*- coding: utf-8 -*-
"""autolab —— 把平台骨架 + 芯片家族适配器接进一个新工程的命令行工具。

本阶段只实现 `init` 一个子命令。

    python tools/autolab.py init --family stm32-keil-ac5 --target <目录> [选项]

⚠️ `calibrate`（阈值标定）与 `gen-ioc`（CubeMX 配置生成）**尚未实现** ——
   刻意不建空壳命令：一个跑不动的命令比没有命令更容易让人误以为已经可用。

退出码
------
    0  写入成功，且本阶段要求的初始化检查通过
    2  参数 / 源文件 / 模板 / 配置错误；**未对目标目录落盘**
    3  目标文件已存在且内容不同，且未加 --force；目标目录**零变化**
    4  文件已经写入，但初始化后的检查（L0 预检）未通过
    5  保留给后续阶段的阈值标定无法复现的情况；本阶段不使用

设计要点
--------
· 目标工程**要么完整落盘、要么一个字节都不动**：所有守卫都在落盘之前。
· `--dry-run` 对目标目录**零变化**（staging 放在系统临时目录）。
· 平台仓库**只读**：本工具没有任何写入平台的代码路径。
· 不初始化 Git、不设 remote、不碰任何仓库的暂存区与历史。
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import autolab_steps as S  # noqa: E402


def _init_stdout():
    """Windows 上 stdout 被重定向到文件/管道时编码是 GBK，而本工具的说明里有
    非 GBK 字符（✗ / ⚠️ / 中文引号）—— 不重设编码的话 print() 会抛
    UnicodeEncodeError，让工具因为"打不出日志"而崩掉（与它做的事无关）。
    平台 CI 模板里的内联脚本用的是同一套处理。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _p(msg=""):
    print(msg)


def _hr(title=""):
    _p("=" * 72)
    if title:
        _p(title)
        _p("=" * 72)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="autolab",
        description="Autolab 工具（本阶段只有 init）",
        epilog="示例：python tools/autolab.py init --family stm32-keil-ac5 --target F:/proj",
    )
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")

    init = sub.add_parser(
        "init",
        help="把一个已由 CubeMX 生成的工程接上 Autolab 工具链",
        description=(
            "把平台骨架（与芯片无关的部分）与指定芯片家族的适配器接进目标工程，"
            "并实例化 CLAUDE.md / .gitignore / tools/project.cfg / README.md / "
            "docs/HARDENING.md / .github/workflows/ci.yml。\n\n"
            "⚠️ 目标必须是**已经由 CubeMX 生成**的工程（含 .ioc、MDK-ARM/*.uvprojx、"
            "Core/、Drivers/）。本工具不生成工程本身。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    init.add_argument("--family", required=True,
                      help="芯片家族，v1 只支持 stm32-keil-ac5；其它值返回退出码 2")
    init.add_argument("--target", required=True,
                      help="目标工程目录（已由 CubeMX 生成）")
    init.add_argument("--project-name", default=None,
                      help="工程名；默认取目标目录名。必须与该工程 .ioc 里的 "
                           "ProjectManager.ProjectName 一致")
    init.add_argument("--mcu", default=None,
                      help="MCU 型号；不给则从工程的 Keil <Device> / .ioc Mcu.CPN 实测")
    init.add_argument("--board", default=None,
                      help="开发板型号；无法从工程推导，不给则生成物里写「未配置」")
    init.add_argument("--toolchain", default=None,
                      help="工具链描述；不给则从 .ioc 的 TargetToolchain 实测")
    init.add_argument("--dry-run", action="store_true",
                      help="只报告将要做什么，**目标目录零变化**")
    init.add_argument("--force", action="store_true",
                      help="允许覆盖已存在且内容不同的文件（覆盖前先备份到 "
                           ".autolab-init/backup-<时间戳>/ 并逐字节校验）")
    init.add_argument("--no-run-checks", action="store_true",
                      help="落盘后不跑 L0 预检（默认会跑 `python tools/preflight.py`）")
    init.add_argument("--platform", default=str(S.PLATFORM_ROOT_DEFAULT),
                      help="平台仓库路径；默认取本脚本所在的平台仓库")
    return ap


# ============================================================================
# 输出
# ============================================================================
def print_plan(plan, collisions, opts):
    _hr("autolab init —— %s" % plan.project_name)
    _p("目标工程   : %s" % plan.target)
    _p("芯片家族   : %s（%s）" % (plan.family.key, plan.family.title))
    _p("平台仓库   : %s" % plan.platform_root)
    _p("平台 HEAD  : %s（≥ %s，已用 git 祖先关系校验）"
       % (plan.platform_head, S.MIN_PLATFORM_COMMIT))
    _p("")
    _p("探测到的工程事实：")
    _p("  .ioc        : %s" % plan.cube["ioc_rel"])
    _p("  .uvprojx    : %s" % plan.cube["uvprojx_rel"])
    _p("  .axf        : %s" % plan.cube["axf_rel"])
    _p("  器件        : %s" % (plan.cube["device"] or "（未知）"))
    _p("  Flash / RAM : %s / %s"
       % (S.human_bytes(plan.cube["flash_size"]), S.human_bytes(plan.cube["ram_size"])))
    _p("")

    created = [c for c in collisions if c.kind == "create"]
    same = [c for c in collisions if c.kind == "same"]
    diff = [c for c in collisions if c.kind == "different"]

    copied = [f for f in plan.files if f.kind == "copy"]
    tpl = [f for f in plan.files if f.kind == "template"]
    _p("将要写入 %d 个文件：逐字节复制 %d 个 + 模板实例化 %d 个"
       % (len(plan.files), len(copied), len(tpl)))
    _p("  新建            : %d" % len(created))
    _p("  已存在且内容相同 : %d" % len(same))
    _p("  已存在且内容不同 : %d" % len(diff))
    if diff:
        for c in diff:
            _p("      ! %s" % c.dst_rel)

    if plan.warnings:
        _p("")
        _p("WARN：")
        for w in plan.warnings:
            _p("  ⚠️  %s" % w)
    _p("")


# ============================================================================
# init
# ============================================================================
def cmd_init(args) -> int:
    target = Path(args.target).resolve()
    project_name = args.project_name or target.name

    opts = {
        "mcu": args.mcu,
        "board": args.board,
        "toolchain": args.toolchain,
        "dry_run": args.dry_run,
        "force": args.force,
        "no_run_checks": args.no_run_checks,
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
    }

    # ---- 1) 只读阶段：源校验 + 探测 + 实例化 + 扫描（此处仍零落盘） ----
    try:
        plan = S.build_plan(Path(args.platform), args.family, target,
                            project_name, opts, dict(os.environ))
    except S.InitError as e:
        _p("✗ 初始化未开始：%s" % e)
        _p("（目标目录零变化）")
        return e.exit_code

    collisions = S.detect_collisions(target, plan)
    diff = [c for c in collisions if c.kind == "different"]

    # ---- 2) 冲突：未加 --force 就拒绝，目标目录零变化 ----
    if diff and not args.force:
        _p("✗ 目标目录里已有 %d 个同名文件，且内容与将要写入的不同：" % len(diff))
        for c in diff:
            _p("    %s" % c.dst_rel)
        _p("")
        _p("本工具不覆盖它们（目标目录零变化）。两种选择：")
        _p("  · 先人工核对：确认这些文件本来就该被 Autolab 接管，再重跑并加 --force")
        _p("  · 或换个目标目录")
        _p("加 --force 后，被覆盖的原件会先备份到 .autolab-init/backup-<时间戳>/ "
           "并逐字节校验。")
        return S.EXIT_CONFLICT

    # ---- 3) dry-run：到此为止，磁盘零变化 ----
    if args.dry_run:
        print_plan(plan, collisions, opts)
        _p("--dry-run：没有写入任何文件，也没有在目标目录创建任何目录。")
        return S.EXIT_OK

    # ---- 4) staging 到系统临时目录 ----
    staging = S.make_temp_staging()
    backup_note = ""
    try:
        staged = S.stage_files(plan.files, staging)

        # ---- 5) 覆盖前备份（逐字节校验，失败则放弃覆盖） ----
        to_backup = [c.dst_rel for c in diff]
        to_backup += [r for r in S.REPORT_FILES if (target / r).is_file()]
        if to_backup:
            bdir = target / ".autolab-init" / (
                "backup-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            done = S.backup_files(target, to_backup, bdir)
            rel = bdir.relative_to(target).as_posix() if done else ""
            if done:
                backup_note = ("覆盖前已把 %d 个原件备份到 `%s`，并逐个校验过 SHA-256 "
                               "与原件一致：\n\n%s"
                               % (len(done), rel,
                                  "\n".join("- `%s`" % d for d in done)))
                _p("已备份 %d 个将被覆盖的原件 → %s" % (len(done), rel))

        # ---- 6) 落盘 ----
        written, skipped = S.commit_files(target, plan.files, staged)
        _p("已写入 %d 个文件（跳过 %d 个内容已相同的）。" % (len(written), len(skipped)))

        # ---- 7) L0 预检（只跑 preflight；不构建、不烧录、不碰串口） ----
        checks = {
            "L1": "NOT RUN",
            "L2": "NOT RUN",
            "L3": "NOT RUN",
            "L1_note": "本阶段不执行构建验证（min_compile_lines 尚未标定）",
            "L2_note": "同上；未构建则无 PASS 日志",
            "L3_note": "未获人工授权；[serial] 契约尚未确定",
        }
        if args.no_run_checks:
            checks["L0"] = {"status": "SKIPPED", "reason": "--no-run-checks"}
            _p("L0 预检：SKIPPED（--no-run-checks）")
        else:
            _p("跑 L0 预检：python tools/preflight.py（cwd=%s）" % target)
            try:
                res = S.run_preflight(target)
            except S.InitError as e:
                _p("✗ %s" % e)
                return S.EXIT_INPUT
            ok = res["exit_code"] == 0
            res["status"] = "PASS" if ok else "FAIL"
            checks["L0"] = res
            _p("-" * 72)
            _p(res["stdout"].rstrip("\n"))
            if res["stderr"].strip():
                _p(res["stderr"].rstrip("\n"))
            _p("-" * 72)
            _p("L0 退出码 = %d → %s" % (res["exit_code"], res["status"]))

        # ---- 8) 报告 ----
        rep = S.build_report(plan, collisions, written, skipped, checks, opts)
        made = S.write_reports(target, rep, plan, backup_note)
        _p("报告：%s" % "、".join("`%s`" % m for m in made))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # ---- 9) 退出码 ----
    if not args.no_run_checks and checks["L0"]["status"] != "PASS":
        _p("")
        _p("文件已写入，但 L0 预检未通过 —— 退出码 4。")
        _p("失败原因已记进 docs/AUTOLAB_INIT.md / tools/autolab_init.json。")
        return S.EXIT_CHECKS_FAILED

    _p("")
    _p("初始化完成。下一步见 docs/AUTOLAB_INIT.md 末节。")
    if plan.warnings:
        _p("⚠️ 有 %d 条 WARN 需要人工处理（见报告第三、四节）。" % len(plan.warnings))
    return S.EXIT_OK


# ============================================================================
def main(argv=None) -> int:
    _init_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd != "init":
        ap.print_help()
        _p("")
        _p("本阶段只实现了 `init`。`calibrate` / `gen-ioc` 留到后续阶段"
           "（刻意不建空壳命令）。")
        return S.EXIT_INPUT
    try:
        return cmd_init(args)
    except S.InitError as e:
        _p("✗ %s" % e)
        return e.exit_code
    except KeyboardInterrupt:
        _p("已中断。")
        return S.EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
