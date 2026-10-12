# -*- coding: utf-8 -*-
"""autolab —— 把平台骨架 + 芯片家族适配器接进一个新工程的命令行工具。

已实现两个子命令：

    python tools/autolab.py init --family stm32-keil-ac5 --target <目录> [选项]
    python tools/autolab.py calibrate --target <目录>

`init`
    默认只做"接入 + L0 环境自检"。
    `--run-checks` 追加完整软件验证闭环：L0 -> 阈值标定（仅当尚未标定）
        -> L1 正式构建 -> L2 无板测试。
    `--skip-build` 在 `--run-checks` 之上跳过 L1 **与**阈值标定（标定本身要跑两次
        真实构建），保留 L0 与 L2。
    `--no-run-checks` 全部跳过，报告里记为 `NOT RUN`（不是 `PASS`）。

`calibrate`
    对合法工程跑**两次真实全量软件构建**，动作数一致且为正时取 floor(2/3) 写入
    `tools/project.cfg` 的阈值标记块。标不出来就退出码 5，且配置文件逐字节不变。
    ⚠️ 只做软件构建：不烧录、不接调试器、不开串口。

⚠️ `gen-ioc`（CubeMX 配置生成）**尚未实现** —— 刻意不建空壳命令：
   一个跑不动的命令比没有命令更容易让人误以为已经可用。

退出码
------
    0  成功：落盘完成，且**本次请求的**检查全部通过
    2  参数 / 源文件 / 模板 / 配置错误；**未对目标目录落盘**
    3  目标文件已存在且内容不同，且未加 --force；目标目录**零变化**
    4  文件已经写入，但后续检查（L0 / L1 / L2）有未通过的
    5  阈值标定无法完成（两次动作数不一致 / 构建失败 / 日志无效 / AXF 未更新）
       —— L1/L2 记为 `NOT RUN`，`tools/project.cfg` 逐字节不变

⚠️ 严格区分"初始化成功"（文件写对了）与"完整软件验证成功"（L0–L2 全绿）：
   两者是**不同**的退出码语义，报告里也分列，绝不合并表述。

设计要点
--------
· 目标工程**要么完整落盘、要么一个字节都不动**：所有守卫都在落盘之前。
· `--dry-run` 对目标目录**零变化**（staging 放在系统临时目录）。
· 平台仓库**只读**：本工具没有任何写入平台的代码路径。
· 不初始化 Git、不设 remote、不碰任何仓库的暂存区与历史。
· 只有 `calibrate` 会写**目标工程**的 `tools/project.cfg`，且只写它的阈值标记块。
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
        description="Autolab 工具（init / calibrate）",
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
                      help="落盘后**什么都不检查**（L0/L1/L2 一律记为 NOT RUN，"
                           "不是 PASS）；与 --run-checks 互斥")
    init.add_argument("--run-checks", action="store_true",
                      help="落盘后跑完整软件验证闭环：L0 -> 阈值标定（仅当尚未标定）"
                           " -> L1 正式构建 -> L2 无板测试")
    init.add_argument("--skip-build", action="store_true",
                      help="配合 --run-checks：跳过 L1 与阈值标定（标定要跑两次真实构建），"
                           "保留 L0 与 L2。L2 若因缺构建证据而失败，如实报失败")
    init.add_argument("--platform", default=str(S.PLATFORM_ROOT_DEFAULT),
                      help="平台仓库路径；默认取本脚本所在的平台仓库")

    cal = sub.add_parser(
        "calibrate",
        help="实测标定构建动作阈值 min_compile_lines（跑两次真实全量构建）",
        description=(
            "对目标工程连续跑**两次真实全量软件构建**，动作数一致且为正、"
            "且退出码 / 0 Error 0 Warning / 产物时间戳三条判据全过时，"
            "取 floor(2/3 × 动作数) 写入 tools/project.cfg 的阈值标记块。\n\n"
            "⚠️ 只做软件构建：不烧录、不接调试器、不开串口。\n"
            "⚠️ 标不出来（计数不一致 / 构建失败 / 日志无效 / 产物未更新）时返回退出码 5，"
            "且 tools/project.cfg **逐字节不变**。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    cal.add_argument("--target", required=True,
                     help="已接入 Autolab 的工程目录（含 tools/project.cfg 与 tools/build.py）")
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
# 初始化后的检查：L0 -> [阈值标定] -> L1 -> L2
# ============================================================================
def checks_skeleton() -> dict:
    """四个检查位的初始状态：**全是 NOT RUN**。

    默认值刻意不是 PASS —— "没跑"和"跑过了"必须是两种可分辨的状态。
    """
    not_run = {"status": "NOT RUN", "reason": "本轮未请求（默认只跑 L0）"}
    return {
        "L0": dict(not_run),
        "L1": dict(not_run),
        "L2": dict(not_run),
        "L3": {"status": "NOT RUN",
               "reason": "未获人工授权；[serial] 契约尚未确定"},
        "calibration": dict(not_run),
    }


def _print_calibration(res: dict) -> None:
    _p("阈值标定：PASS")
    for r in res["runs"]:
        _p("  · 第 %s 次全量构建：%s 条动作，退出码 %s，日志 `%s`"
           % (r.get("round"), r.get("actions"), r.get("exit_code"), r.get("log")))
    _p("  两次动作数一致：%s = %s" % (res["actions"][0], res["actions"][1]))
    _p("  阈值计算：%s，即 floor(2/3 × %s) = **%d**"
       % (res["rule"], res["actions"][0], res["threshold"]))
    _p("  写入 `%s`：%s（只改阈值标记块，其余逐字节不变）"
       % (res["cfg"], "是" if res["written"] else "否"))


def _print_layered(checks: dict, key: str, what: str) -> None:
    """把某一层的结果打出来（含工具原始输出的首尾）。"""
    c = checks[key]
    _p("%s：%s%s" % (what, c.get("status"),
                    "" if c.get("status") == "PASS" else "（%s）" % c.get("reason", "")))
    if c.get("status") in ("PASS", "FAIL") and c.get("stdout") is not None:
        _p("-" * 72)
        _p("$ %s（cwd=%s）" % (c.get("cmd", ""), c.get("cwd", "")))
        _p(c["stdout"].rstrip("\n"))
        if (c.get("stderr") or "").strip():
            _p("---- stderr ----")
            _p(c["stderr"].rstrip("\n"))
        _p("-" * 72)


def run_post_init_checks(args, target: Path, checks: dict) -> int:
    """跑初始化后的检查，返回本轮退出码。顺序由授权书定死，不重排。

    ⚠️ 每一步失败都**立刻停止后面的步骤**，并把没跑的那些如实记成 `NOT RUN`：
       既不跳过、也不假装跑过，更不伪造构建日志。
    """
    # ---- 0) 全部跳过：显式记 NOT RUN，绝不记 PASS ----
    if args.no_run_checks:
        for k in ("L0", "calibration", "L1", "L2"):
            checks[k] = {"status": "NOT RUN",
                         "reason": "--no-run-checks：用户显式跳过（跳过 != 通过）"}
        _p("L0 / 阈值标定 / L1 / L2：**NOT RUN**（--no-run-checks —— 跳过，不是通过）")
        return S.EXIT_OK

    # ---- 1) L0 环境自检 ----
    _p("跑 L0 预检：python tools/preflight.py（cwd=%s）" % target)
    try:
        res = S.run_preflight(target)
    except S.InitError as e:
        _p("✗ %s" % e)
        return S.EXIT_INPUT
    res["status"] = "PASS" if res["exit_code"] == 0 else "FAIL"
    checks["L0"] = res
    _p("-" * 72)
    _p(res["stdout"].rstrip("\n"))
    if res["stderr"].strip():
        _p(res["stderr"].rstrip("\n"))
    _p("-" * 72)
    _p("L0 退出码 = %d → %s" % (res["exit_code"], res["status"]))
    if res["status"] != "PASS":
        for k in ("calibration", "L1", "L2"):
            checks[k] = {"status": "NOT RUN",
                         "reason": "L0 未通过 —— 环境没就绪，后面的步骤不跑"}
        _p("")
        _p("L0 未通过：阈值标定 / L1 / L2 一律 NOT RUN（不往下跑）。")
        return S.EXIT_CHECKS_FAILED

    if not args.run_checks:
        _p("")
        _p("未加 --run-checks：阈值标定 / L1 / L2 保持 NOT RUN"
           "（默认只做初始化 + L0）。")
        return S.EXIT_OK

    # ---- 2) 阈值标定（**仅当** tools/project.cfg 里还没有生效的阈值）----
    if args.skip_build:
        checks["calibration"] = {
            "status": "NOT RUN",
            "reason": "--skip-build：阈值标定要跑两次真实全量构建，一并跳过"}
        _p("阈值标定：NOT RUN（--skip-build）")
    elif not S.threshold_is_missing(target):
        checks["calibration"] = {
            "status": "SKIPPED",
            "reason": "tools/project.cfg 里 min_compile_lines 已存在，无需重新标定"}
        _p("阈值标定：SKIPPED（阈值已标定，本轮不重标）")
    else:
        _p("tools/project.cfg 里 min_compile_lines 尚未标定 —— 先跑两次真实全量构建")
        try:
            cal = S.calibrate_threshold(target)
        except S.CalibrationError as e:
            checks["calibration"] = {"status": "FAIL", "reason": str(e)}
            _p("✗ 阈值标定未能完成：%s" % e)
            _p("")
            _p("L1 / L2 记为 NOT RUN（标定失败时不继续）—— 退出码 5。")
            return S.EXIT_CALIBRATION
        checks["calibration"] = cal
        _print_calibration(cal)

    # ---- 3) L1 正式构建 ----
    if args.skip_build:
        checks["L1"] = {"status": "NOT RUN", "reason": "--skip-build：用户显式跳过 L1"}
        _p("L1 构建：NOT RUN（--skip-build）")
    else:
        _p("跑 L1 构建：python tools/build.py（全量重建）")
        try:
            r = S.run_build_l1(target)
        except S.InitError as e:
            checks["L1"] = {"status": "FAIL", "reason": str(e)}
            _p("✗ %s" % e)
            r = None
        if r is not None:
            r["status"] = "PASS" if r["exit_code"] == 0 else "FAIL"
            r["log"] = S.newest_build_log(target)
            checks["L1"] = r
            _print_layered(checks, "L1", "L1 构建")
            _p("L1 退出码 = %d → %s（日志 `%s`）"
               % (r["exit_code"], r["status"], r["log"] or "（未归档）"))

    # ---- 4) L2 无板测试（**总是**跑：即便 L1 失败也要留下证据）----
    _p("跑 L2 无板测试：pytest -m \"not hardware\"（不碰硬件；"
       "已 deselect 掉会重复构建的 %s）" % S.L2_DESELECT)
    try:
        r2 = S.run_pytest_l2(target)
    except S.InitError as e:
        checks["L2"] = {"status": "FAIL", "reason": str(e)}
        _p("✗ %s" % e)
    else:
        r2["status"] = "PASS" if r2["exit_code"] == 0 else "FAIL"
        checks["L2"] = r2
        _print_layered(checks, "L2", "L2 无板测试")
        _p("L2 退出码 = %d → %s" % (r2["exit_code"], r2["status"]))

    bad = [k for k in ("L1", "L2") if checks[k].get("status") == "FAIL"]
    if bad:
        _p("")
        _p("文件已写入，但 %s 未通过 —— 退出码 4。" % " / ".join(bad))
        return S.EXIT_CHECKS_FAILED

    _p("")
    _p("文件已写入，且 L0 / L1 / L2 全部通过。")
    return S.EXIT_OK


# ============================================================================
# init
# ============================================================================
def cmd_init(args) -> int:
    target = Path(args.target).resolve()
    project_name = args.project_name or target.name

    # 参数组合在**任何落盘之前**校验 —— 组合不对时目标目录必须零变化。
    if args.run_checks and args.no_run_checks:
        _p("✗ --run-checks 与 --no-run-checks 互斥：一个要求跑检查，一个要求全跳过。")
        _p("（目标目录零变化）")
        return S.EXIT_INPUT
    if args.skip_build and not args.run_checks:
        _p("✗ --skip-build 只与 --run-checks 搭配才有意义"
           "（默认本来就不跑 L1）。")
        _p("（目标目录零变化）")
        return S.EXIT_INPUT


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

        # ---- 7) 初始化后的检查：L0 -> [阈值标定] -> L1 -> L2 ----
        # ⚠️ 即使某一步失败也要走到报告：报告本身就是失败时的取证材料。
        checks = checks_skeleton()
        exit_code = run_post_init_checks(args, target, checks)

        # ---- 8) 报告 ----
        rep = S.build_report(plan, collisions, written, skipped, checks, opts)
        made = S.write_reports(target, rep, plan, backup_note)
        _p("报告：%s" % "、".join("`%s`" % m for m in made))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # ---- 9) 退出码 ----
    if exit_code == S.EXIT_OK:
        _p("")
        _p("初始化完成。下一步见 docs/AUTOLAB_INIT.md 末节。")
        if plan.warnings:
            _p("⚠️ 有 %d 条 WARN 需要人工处理（见报告第三、四节）。"
               % len(plan.warnings))
    else:
        _p("")
        _p("失败原因已记进 docs/AUTOLAB_INIT.md / tools/autolab_init.json。")
    return exit_code


# ============================================================================
# calibrate
# ============================================================================
def cmd_calibrate(args) -> int:
    target = Path(args.target).resolve()
    _hr("autolab calibrate —— 构建动作阈值标定")
    _p("目标工程   : %s" % target)
    _p("做法       : 连续两次**真实全量软件构建**，数出动作条数")
    _p("⚠️ 只做软件构建：不烧录、不接调试器、不开串口。")
    _p("")
    if not target.is_dir():
        _p("✗ 目标目录不存在：%s" % target)
        return S.EXIT_INPUT
    res = S.calibrate_threshold(target)      # 失败抛 CalibrationError -> 退出码 5
    _print_calibration(res)
    _p("")
    _p("下一步：跑 `python tools/build.py`（L1，四判据全判），"
       "再跑 `pytest -m \"not hardware\"`（L2）。")
    _p("⚠️ 阈值只防\"空转假 0\"，**不代表**\"真实增量构建已验证\"。")
    return S.EXIT_OK


# ============================================================================
def main(argv=None) -> int:
    _init_stdout()
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.cmd == "init":
        try:
            return cmd_init(args)
        except S.InitError as e:
            _p("✗ %s" % e)
            return e.exit_code
        except KeyboardInterrupt:
            _p("已中断。")
            return S.EXIT_INPUT
    if args.cmd == "calibrate":
        try:
            return cmd_calibrate(args)
        except S.InitError as e:
            _p("✗ %s" % e)
            return e.exit_code
        except KeyboardInterrupt:
            _p("已中断。")
            return S.EXIT_INPUT
    ap.print_help()
    _p("")
    _p("已实现 `init` 与 `calibrate`。`gen-ioc`（CubeMX 配置生成）留到后续阶段"
       "（刻意不建空壳命令）。")
    return S.EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
