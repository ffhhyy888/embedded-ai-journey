# -*- coding: utf-8 -*-
"""autolab init 的实现步骤（纯逻辑，不含命令行解析）。

分工：
  · 本文件            —— 只读探测 / 源完整性 / 模板实例化 / 冲突检测 / 备份 / 落盘 / 报告
  · tools/autolab.py  —— 命令行参数、退出码，把下面的步骤串起来

设计约束（与阶段 1 授权一致）：
  · 只用 Python 标准库，不引入新的运行时依赖。
  · 目标工程**要么完整落盘、要么一个字节都不动**：所有生成与守卫都先在
    系统临时目录里完成，任何守卫失败都在落盘之前退出。
  · `--dry-run` 对目标目录**零变化** —— 包括不创建 staging/缓存目录。
  · 平台仓库**只读**：本文件没有任何写入平台的代码路径。
  · 不初始化 Git、不设 remote、不碰任何仓库的暂存区与历史。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# ============================================================================
# 退出码（阶段 1 定下，阶段 2 启用 5）
# ============================================================================
EXIT_OK = 0            # 写入成功，且本次请求的检查全部通过
EXIT_INPUT = 2         # 参数 / 源文件 / 模板 / 配置错误；未对目标目录落盘
EXIT_CONFLICT = 3      # 目标文件存在且内容不同，未加 --force；目标目录零变化
EXIT_CHECKS_FAILED = 4 # 文件已经写入，但初始化后的检查（L0/L1/L2）未通过
EXIT_CALIBRATION = 5   # 阈值标定无法完成（计数不一致 / 构建失败 / 日志无效 /
                       # AXF 未更新）；project.cfg 逐字节不变


class InitError(Exception):
    """输入 / 源 / 模板 / 配置层面的错误 —— 一律在落盘之前抛出。"""

    exit_code = EXIT_INPUT


class ConflictError(InitError):
    """目标文件已存在且内容不同，且未给 --force。"""

    exit_code = EXIT_CONFLICT


# ============================================================================
# 机器可读的协议常量（工具之间靠它们对接；改一处就要改两处）
# ============================================================================
# `tools/build.py --measure` 的结果行前缀。适配器侧的对应常量是
# adapters/*/tools/build.py 里的 MEASURE_PREFIX —— 两处必须一致。
BUILD_MEASURE_PREFIX = "AUTOLAB_MEASURE="

# 生成的 tools/project.cfg 里包住"阈值"那一段的标记。
# 为什么用标记，而不是"找第 N 行 / 找正则"：换工程名、改注释文案、加 `;` 行尾注释
# 都不会让标定写错地方；标记个数不为 1 时直接失败 —— 不做猜测式改写。
CFG_BLOCK_BEGIN = "# --- autolab:min_compile_lines"
CFG_BLOCK_END = "# --- /autolab:min_compile_lines"
_CFG_BLOCK_RE = re.compile(
    r"(?ms)^# --- autolab:min_compile_lines[^\n]*\n"
    r".*?"
    r"^# --- /autolab:min_compile_lines[^\n]*\n")
# 生效的阈值。在标记块**之外**出现即拒绝改写：那会变成重复键，
# configparser 会直接报错 —— 这种失败必须由本工具提前说清楚，不留给下游去猜。
_LIVE_THRESHOLD_RE = re.compile(r"(?m)^\s*min_compile_lines\s*=")


# ============================================================================
# 版本下限
# ============================================================================
# 平台必须至少包含这个提交：
#   8a89241 fix: count build actions from a single log source
#       —— 构建动作计数的双重计数修复
# 它同时也晚于 36f7cc9（.ioc / axf 路径改为读 project.cfg，不再写死）。
# ⚠️ 判定用 git 祖先关系，**不比较哈希字符串、不比较提交日期**。
MIN_PLATFORM_COMMIT = "8a89241"
MIN_PLATFORM_WHY = "构建动作计数修复（双重计数）与更早的 .ioc/axf 路径配置化修复"

PLATFORM_ROOT_DEFAULT = Path(__file__).resolve().parent.parent

# ============================================================================
# 文件清单
# ============================================================================
# skeleton → 目标工程（与芯片家族无关）
_SKELETON_COPIES = (
    "skeleton/tools/serial_tool.py",
    "skeleton/tools/hil.py",
    "skeleton/tools/preflight.py",
    "skeleton/tools/project_config.py",
    "skeleton/hooks/pre-commit",
    "skeleton/pytest.ini",
    "skeleton/requirements.txt",
    "skeleton/tests/conftest.py",
    "skeleton/tests/test_hil.py",
    "skeleton/tests/test_host.py",
    "skeleton/tests/test_preflight.py",
    "skeleton/tests/test_project_config.py",
    "skeleton/tests/test_serial_tool.py",
    "skeleton/tests/host/test_ringbuf.c",
)

# adapters/<家族> → 目标工程
# ⚠️ 刻意**不**复制：
#   · adapters/<家族>/PITFALLS.md —— 那是平台的家族级知识，不是工程内容
#   · adapters/<家族>/tests/conftest.py —— 与 skeleton 版同名，骨架版优先
_ADAPTER_COPIES = (
    "tools/build.py",
    "tools/check_ioc.py",
    "tools/debug_probe.py",
    "tools/flash.py",
    "tools/serial_probe.py",
    "tools/timing_probe.py",
    "tests/test_build.py",
    "tests/test_build_log_source.py",
    "tests/test_build_paths.py",
    "tests/test_cfg_driven_paths.py",
    "tests/test_device.py",
    "tests/test_flash.py",
    "tests/test_ioc.py",
    "tests/test_static.py",
)

# 目标路径覆盖表。
# ⚠️ 骨架里 `hooks/` 在顶层，接进工程后归到 `tools/hooks/` 下
#    （工程根只留 pytest.ini / requirements.txt / .gitignore / CLAUDE.md / README.md
#     这一类真正的根文件）。这条差异**必须**显式写出来，不能靠"剥掉 skeleton/ 前缀"。
_DEST_OVERRIDE = {
    "skeleton/hooks/pre-commit": "tools/hooks/pre-commit",
}

# 模板种类：决定了用哪种实例化方式
TPL_CLAUDE = "claude"        # 显式映射（`<...>` 占位符）
TPL_GITIGNORE = "gitignore"  # 显式映射（`<...>` 占位符）
TPL_PROJECT_CFG = "cfg"      # 显式映射（逐项改写取值，不是占位符）
TPL_DOLLAR = "dollar"        # `${VARIABLE}` 变量替换

# 工具自己产出的报告文件：始终重写（它们就是"本次运行的结果"），
# 但若已存在会**先备份**。它们不参与冲突拒绝 —— 否则第二次 init 会被自己的旧报告挡住。
REPORT_FILES = ("docs/AUTOLAB_INIT.md", "tools/autolab_init.json")


@dataclass(frozen=True)
class Family:
    key: str
    title: str
    toolchain_label: str
    adapter_dir: str
    copies: tuple
    templates: tuple  # (平台内相对路径, 目标内相对路径, 种类)


def _build_family() -> Family:
    def dest(src_rel, default):
        return _DEST_OVERRIDE.get(src_rel, default)

    copies = [(p, dest(p, p[len("skeleton/"):])) for p in _SKELETON_COPIES]
    copies += [
        ("adapters/stm32-keil-ac5/" + p, dest("adapters/stm32-keil-ac5/" + p, p))
        for p in _ADAPTER_COPIES
    ]
    return Family(
        key="stm32-keil-ac5",
        title="STM32 + Keil MDK-ARM / ARM Compiler 5 (AC5)",
        toolchain_label="keil",
        adapter_dir="adapters/stm32-keil-ac5",
        copies=tuple(copies),
        templates=(
            ("skeleton/CLAUDE.md.template", "CLAUDE.md", TPL_CLAUDE),
            ("skeleton/gitignore.template", ".gitignore", TPL_GITIGNORE),
            ("adapters/stm32-keil-ac5/project.cfg.example", "tools/project.cfg", TPL_PROJECT_CFG),
            ("skeleton/templates/README.md.template", "README.md", TPL_DOLLAR),
            ("skeleton/templates/HARDENING.md.template", "docs/HARDENING.md", TPL_DOLLAR),
            ("skeleton/templates/ci.yml.template", ".github/workflows/ci.yml", TPL_DOLLAR),
        ),
    )


FAMILIES = {_build_family().key: _build_family()}

# 逐字节复制的文件数（本阶段验收要求：28）
COPY_COUNT = len(FAMILIES["stm32-keil-ac5"].copies)


def get_family(key: str) -> Family:
    fam = FAMILIES.get(key)
    if fam is None:
        raise InitError(
            "不支持的芯片家族 %r。v1 只支持：%s"
            % (key, ", ".join(sorted(FAMILIES)))
        )
    return fam


# ============================================================================
# 泄漏扫描用的**具名**清单
# ============================================================================
# ⚠️ 这些清单只用于**模板源 + 模板生成物**，是逐个写死的具名事实，
#    不是对全工程的全局黑名单 —— 后者会把"用来防回归的测试"一起误杀。
#    例如 tests/test_cfg_driven_paths.py 里**必须**出现参考工程名，
#    那正是它锁定"换工程后仍读配置"的手段。

# 平台模板里**不允许**出现的参考工程事实（模板必须与工程无关）
FORBIDDEN_IN_TEMPLATES = (
    "SDIO_test",
    "COM9",
    "stm32-bench",
    "autolab_stm32_demo",
    "1415b9d",
    "779788",
    "2019M",
    "SystemClock_Config",
    "F:\\keil",
    "F:/keil",
    "UV4",
    "uVision",
    "fromelf",
)

# 由 project.cfg.example 生成的 tools/project.cfg 里**不允许**残留的取值
FORBIDDEN_IN_PROJECT_CFG = (
    "SDIO_test",
    "COM9",
    "USh",
    "MX_SDIO_SD_Init",
    "HAL_UART_RxCpltCallback",
    "HAL_UART_ErrorCallback",
    "10456",
)

# 由三份新模板生成的文档里**不允许**残留的参考工程事实
FORBIDDEN_IN_DOCS = (
    "SDIO_test",
    "COM9",
    "USh",
    "stm32-bench",
    "autolab_stm32_demo",
    "1415b9d",
    "779788",
    "2019M",
)


def scan_output(text: str, needles, user_values):
    """对**生成物**做泄漏扫描。

    ⚠️ 与"扫平台模板源码"（`find_hits` 直接用 `FORBIDDEN_IN_TEMPLATES`）**不是一回事**：
    模板源码必须与任何工程无关，所以那里一条都不许有；
    而生成物里**允许**出现用户自己给的值 —— 工程名真叫 `autolab_stm32_demo`
    或 `SDIO_test` 是他选的，不是我们编的，判它泄漏是误报。

    所以规则是：凡是出现在**用户显式给出的值**里的针，一律豁免；
    其余的参考工程事实（COM9 / USh / 2019M / 779788 ...）照旧一条不剩。
    """
    exempt = "\n".join(str(x) for x in user_values if x)
    return [n for n in needles if n in text and n not in exempt]

# 模板里允许出现的变量名（白名单）—— 出现白名单外的变量即报错，
# 防止有人往模板里加变量却忘了在本文件里提供取值。
DOLLAR_TEMPLATE_VARS = {
    "README.md": {
        "PROJECT_NAME", "PROJECT_PATH", "MCU", "PACKAGE", "FLASH", "RAM",
        "BOARD", "TOOLCHAIN", "GENERATOR", "CLOCK", "PERIPHERALS", "GOAL",
        "FAMILY", "IOC_FILE", "PLATFORM_ROOT",
    },
    "docs/HARDENING.md": {"PROJECT_NAME", "PROJECT_PATH"},
    ".github/workflows/ci.yml": {"RUNNER_LABELS_SOFTWARE", "RUNNER_LABELS_HARDWARE"},
}

VAR_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
ANGLE_RE = re.compile(r"<[^<>\n]*>")

# `.gitignore` 模板里那些**真正的**占位符（要逐个替换掉的）。
# ⚠️ 这里必须用**具名清单**而不是"所有 <...>"——模板注释里还有
#    `runs/<时间戳>_FAIL/`、`git check-ignore -v <文件>` 这类**描述性**写法，
#    它们不是占位符，泛化扫描会把它们误判成"没替换干净"。
GITIGNORE_PLACEHOLDERS = ("<新工程>", "<平台仓库>", "<PROJECT_NAME>", "<工程名>")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def find_hits(text: str, needles) -> list:
    return [n for n in needles if n in text]


# ============================================================================
# 文本读写：保留平台源文件的原始行尾
# ============================================================================
def read_text_preserving_eol(path: Path):
    """返回 (行尾归一化为 \\n 的文本, 原始行尾)。"""
    raw = Path(path).read_bytes().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    return raw.replace("\r\n", "\n"), nl


def restore_eol(text: str, nl: str) -> str:
    return text if nl == "\n" else text.replace("\n", nl)


# ============================================================================
# 目标工程探测（只读）
# ============================================================================
def discover_cube_project(target: Path, project_name: str) -> dict:
    """确认目标是一个**已由 CubeMX 生成的工程**，并解析出构建相关事实。

    ⚠️ 这里刻意**不**接受空目录：本工具的目标是"在已有的 CubeMX 工程上接入
    Autolab 工具"，不是"凭空造一个工程"。造一个看起来完整的假工程比报错危险得多。
    """
    target = Path(target)
    if not target.exists():
        raise InitError("目标目录不存在：%s" % target)
    if not target.is_dir():
        raise InitError("目标路径不是目录：%s" % target)

    missing = [d for d in ("Core", "Drivers") if not (target / d).is_dir()]
    mdk = target / "MDK-ARM"
    if not mdk.is_dir():
        missing.append("MDK-ARM")
    if missing:
        raise InitError(
            "目标不是有效的 CubeMX 工程，缺少：%s\n"
            "  本工具要求目标是**已经由 CubeMX 生成**的工程\n"
            "  （应含 .ioc、MDK-ARM/*.uvprojx、Core/、Drivers/）。\n"
            "  若还没有工程，请先用 CubeMX 生成，再回来跑 autolab init。"
            % "、".join(missing)
        )

    uvprojxs = sorted(mdk.glob("*.uvprojx"))
    if not uvprojxs:
        raise InitError("MDK-ARM/ 下没有 .uvprojx：%s" % mdk)

    iocs = sorted(p for p in target.glob("*.ioc") if p.is_file())
    if not iocs:
        raise InitError("目标根目录下没有 .ioc：%s" % target)

    def pick(cands, suffix):
        exact = [p for p in cands if p.name == project_name + suffix]
        if len(exact) == 1:
            return exact[0]
        if len(cands) == 1:
            return cands[0]
        raise InitError(
            "发现多个 %s，无法确定用哪一个：%s\n"
            "  用 --project-name 指定工程名后再试。"
            % (suffix, ", ".join(p.name for p in cands))
        )

    ioc = pick(iocs, ".ioc")
    uvprojx = pick(uvprojxs, ".uvprojx")

    ioc_data = parse_ioc(ioc)
    prj = parse_uvprojx(uvprojx)

    ioc_name = ioc_data.get("ProjectManager.ProjectName")
    if ioc_name and ioc_name != project_name:
        raise InitError(
            ".ioc 里的 ProjectManager.ProjectName=%r 与 --project-name=%r 不一致。\n"
            "  请用 --project-name 指定这个工程真实的工程名（.ioc 与 .uvprojx 必须一致）。"
            % (ioc_name, project_name)
        )

    uvprojx_rel = uvprojx.relative_to(target).as_posix()
    out_dir = prj["output_directory"]
    axf_rel = "%s/%s/%s.axf" % (
        Path(uvprojx_rel).parent.as_posix(),
        out_dir,
        prj["output_name"],
    )

    return {
        "ioc_rel": ioc.relative_to(target).as_posix(),
        "ioc": ioc,
        "uvprojx_rel": uvprojx_rel,
        "uvprojx": uvprojx,
        "axf_rel": axf_rel,
        "target_name": prj["target_name"],
        "output_directory": out_dir,
        "output_name": prj["output_name"],
        "device": prj["device"],
        "cpu": prj["cpu"],
        "ioc_data": ioc_data,
        "flash_size": prj["flash_size"],
        "ram_size": prj["ram_size"],
    }


def parse_ioc(path: Path) -> dict:
    text = Path(path).read_bytes().decode("utf-8", errors="replace")
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_uvprojx(path: Path) -> dict:
    text = Path(path).read_bytes().decode("utf-8", errors="replace")

    def one(tag):
        m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), text, re.S)
        return m.group(1).strip() if m else ""

    out_dir = one("OutputDirectory").replace("\\", "/").strip("/")
    out_name = one("OutputName")
    if not out_dir or not out_name:
        raise InitError(
            "%s 里读不到 <OutputDirectory>/<OutputName> —— 无法确定 .axf 产物路径。\n"
            "  请确认这是一个 Keil MDK 工程（uvprojx 2.x/5.x）。" % path.name
        )

    cpu = one("Cpu")
    flash, ram = parse_cpu_memories(cpu)
    return {
        "target_name": one("TargetName"),
        "output_directory": out_dir,
        "output_name": out_name,
        "device": one("Device"),
        "cpu": cpu,
        "flash_size": flash,
        "ram_size": ram,
    }


MEM_FLOOR = 1024


def parse_cpu_memories(cpu: str):
    """从 Keil 的 <Cpu> 串里解析 Flash / RAM 容量（字节）。

    形如：IRAM(0x20000000-0x2001BFFF) IRAM2(0x2001C000-0x2001FFFF)
          IROM(0x8000000-0x807FFFF) ...
    ⚠️ 取的是**本工程 Keil 的 Device 设置**，不是数据手册 —— 两者应当一致，
       若不一致，说明 Keil 侧器件选错了，本项会跟着一起错。

    返回 (flash, ram)；任一项解析不出/不可信时为 None —— 调用方据此**省略**该配置项，
    而不是写一个假数字。

    ⚠️ 为什么要有 MEM_FLOOR：`IRAM(0x0-0x0)` 这种退化写法会算出 "1 字节"，
    写进 project.cfg 会让镜像契约测试报"RAM 预算 1 字节已超"，比省略更难查。
    省略 = 平台表达"未确定"的正规方式，会被 preflight 以 [WARN] 暴露出来。
    1 KiB 的下界对任何真实 MCU 都不会误伤（STM32F0 起步也有 16 KiB Flash）。
    """
    if not cpu:
        return None, None

    def total(pat):
        n = 0
        hit = False
        for m in re.finditer(pat, cpu):
            lo = int(m.group(1), 16)
            hi = int(m.group(2), 16)
            size = hi - lo + 1
            if hi > lo and size >= MEM_FLOOR:
                n += size
                hit = True
        return n if hit else None

    flash = total(r"IROM\(0x([0-9A-Fa-f]+)-0x([0-9A-Fa-f]+)\)")
    ram = total(r"IRAM\d*\(0x([0-9A-Fa-f]+)-0x([0-9A-Fa-f]+)\)")
    return flash, ram


# ============================================================================
# 平台源完整性
# ============================================================================
def git(platform: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(platform)] + list(args),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except FileNotFoundError:
        raise InitError("找不到 git —— 无法校验平台版本与源完整性。")


def platform_head(platform: Path) -> str:
    r = git(platform, "rev-parse", "HEAD")
    if r.returncode != 0:
        raise InitError(
            "平台目录不是可用的 Git 仓库：%s\n  git 说：%s"
            % (platform, (r.stderr or r.stdout).strip())
        )
    return r.stdout.strip()


def require_min_platform(platform: Path, min_commit: str = MIN_PLATFORM_COMMIT):
    """用 **Git 祖先关系** 校验平台版本下限。

    ⚠️ 不比较哈希字符串，也不比较提交日期 —— 那种做法在分支/重新提交后会失效。
    """
    r = git(platform, "merge-base", "--is-ancestor", min_commit, "HEAD")
    if r.returncode == 0:
        return
    if r.returncode == 1:
        raise InitError(
            "平台版本过旧：HEAD 不包含提交 %s（%s）。\n"
            "  请在平台仓库执行 `git pull` / 切到含该提交的分支后重试。"
            % (min_commit, MIN_PLATFORM_WHY)
        )
    raise InitError(
        "无法判断平台版本：本地仓库里找不到提交 %s。\n"
        "  git 说：%s"
        % (min_commit, (r.stderr or r.stdout).strip())
    )


def source_list(family: Family):
    rels = [src for src, _ in family.copies]
    rels += [src for src, _, _ in family.templates]
    return rels


def check_sources_exist(platform: Path, family: Family) -> None:
    missing = [r for r in source_list(family) if not (platform / r).is_file()]
    if missing:
        raise InitError(
            "平台源文件缺失（清单要求 %d 个，缺 %d 个）：\n%s\n"
            "  平台仓库可能不完整，或本工具的清单与该版本不匹配。"
            % (len(source_list(family)), len(missing),
               "\n".join("    " + m for m in missing))
        )


def check_sources_unmodified(platform: Path, family: Family) -> list:
    """工作树里与 HEAD **不一致**的源文件。

    ⚠️ 用 `git diff --name-only HEAD -- <paths>`，不用比较文件哈希 ——
       本机 `core.autocrlf=true`，直接比字节会把行尾转换误判成"被修改"。
    """
    rels = source_list(family)
    r = git(platform, "diff", "--name-only", "HEAD", "--", *rels)
    if r.returncode != 0:
        raise InitError("无法检查平台源文件是否被修改：%s"
                        % (r.stderr or r.stdout).strip())
    return [x.strip() for x in r.stdout.splitlines() if x.strip()]


# ============================================================================
# 模板实例化
# ============================================================================
def render_dollar(text: str, values: dict, name: str, allowed: set) -> str:
    """`${VARIABLE}` 替换。

    ⚠️ **不用** Python 的 str.format() / 百分号插值 —— 那会把 GitHub Actions 的
       `${{ ... }}` 双层花括号表达式当成格式串处理，直接改写流水线语义。
    """
    used = set()

    def sub(m):
        k = m.group(1)
        used.add(k)
        if k not in values:
            raise InitError("模板 %s 用到了变量 %s，但本工具没有为它准备取值。" % (name, k))
        return values[k]

    out = VAR_RE.sub(sub, text)

    extra = sorted(used - allowed)
    if extra:
        raise InitError(
            "模板 %s 出现了未声明的变量：%s\n"
            "  若是平台模板新增了变量，请在 tools/autolab_steps.py 的白名单与取值里同步补上。"
            % (name, ", ".join(extra))
        )
    left = sorted(set(VAR_RE.findall(out)))
    if left:
        raise InitError("模板 %s 实例化后仍残留变量：%s" % (name, ", ".join(left)))
    return out


def render_mapping(text: str, pairs, name: str) -> str:
    """逐条**显式映射**（不是对所有 <...> 做泛化替换）。

    每一条的原文都必须在模板里真实存在；找不到就说明平台模板变了，
    此时**拒绝生成**，绝不"尽力而为"地猜。
    """
    for old, new in pairs:
        if old not in text:
            raise InitError(
                "模板 %s 里找不到预期文本：%r\n"
                "  平台模板可能已变更 —— 拒绝生成（避免产出看起来正常、实则残缺的配置）。"
                % (name, old[:80])
            )
        text = text.replace(old, new)
    return text


# ============================================================================
# 三份"既有模板"的显式映射
# ============================================================================
def render_claude(text: str, v: dict) -> str:
    pairs = [
        (
            "> **用法**：把本文件复制成新工程的 `CLAUDE.md`，替换 `<...>` 占位符，\n"
            "> 然后**填第三节「本工程的坑」**（这部分每个工程都不一样，别人替不了）。\n"
            ">\n"
            "> ⚠️ **不要在本文件里复制平台规范全文**。规范只有一份，在上面的平台仓库里，\n"
            "> 本文件只做**引用 + 本工程特有信息**。",
            "> **用法**：本文件由 `autolab init` 从平台模板 `skeleton/CLAUDE.md.template`\n"
            "> 实例化生成。第二、四节的已知事实已按本工程填写；标记为「未配置」的项需人工补齐。\n"
            "> **第三节「本工程的坑」必须由人填写** —— 这部分每个工程都不一样，工具替不了。\n"
            ">\n"
            "> ⚠️ **不要在本文件里复制平台规范全文**。规范只有一份，在平台仓库里，\n"
            "> 本文件只做**引用 + 本工程特有信息**。",
        ),
        ("# CLAUDE.md — <工程名>", "# CLAUDE.md — " + v["PROJECT_NAME"]),
        ('- 芯片家族适配器：`<adapters/<家族>/ 或"本工程自带">`',
         "- 芯片家族适配器：`adapters/%s`（工具与用例已拷入本工程 `tools/`、`tests/`）"
         % v["FAMILY"]),
        ("- 路径：`<绝对路径>`", "- 路径：`%s`" % v["PROJECT_PATH"]),
        ("- MCU / 板子：`<型号 / 板子>`", "- MCU / 板子：`%s`" % v["MCU_BOARD"]),
        ("- 工具链：`<编译器 / IDE / 版本>`", "- 工具链：`%s`" % v["TOOLCHAIN"]),
        ("- 关键外设：`<UART / SPI / I2C / DMA / SDIO ...>`",
         "- 关键外设：`%s`" % v["PERIPHERALS"]),
        ("- 串口：`<COMx 或 /dev/ttyUSBx>`（可用环境变量 `AUTOLAB_PORT` 覆盖）",
         "- 串口：`未配置`（USB-TTL 芯片与 COM 口尚未确认；"
         "确认后写进 `tools/project.cfg` 的 `[serial] port`，"
         "临时可用环境变量 `AUTOLAB_PORT` 覆盖）"),
        ("## 二、本工程概况（填）", "## 二、本工程概况"),
        ("### 命令（填）", "### 命令"),
        ("## 五、本工程的状态（填 / 随进展更新）", "## 五、本工程的状态（随进展更新）"),
        ("1. `<例：本板晶振是 X MHz，不是生成器默认的 Y>`",
         "1. `（待填：本板晶振频率与生成器默认值不一致之处，以及由此踩过的坑）`"),
        ("2. `<例：源文件是 GBK，必须用字节级替换改>`",
         "2. `（待填：本工程的字符编码 / 文件格式坑）`"),
        ("3. `<例：生成器重新生成会冲掉哪几处手改>`",
         "3. `（待填：生成器重新生成会覆盖哪几处手改 —— 这正是 check_ioc 存在的原因）`"),
        ("- 当前阶段：`<...>`",
         "- 当前阶段：`autolab init 已执行；尚未构建、未烧录`"),
        ("- 最近一次 L0–L2：`<日期 / 结果>`",
         "- 最近一次 L0–L2：`L1/L2 NOT RUN；L0 结果见 docs/AUTOLAB_INIT.md`"),
        ("- 最近一次 L3：`<日期 / 结果 / 或 NOT RUN（原因）>`",
         "- 最近一次 L3：`NOT RUN（未获人工授权）`"),
        ("- 已知脆弱点：`<...>`", "- 已知脆弱点：`（待填 —— 首轮真实构建后回来更新）`"),
    ]
    out = render_mapping(text, pairs, "CLAUDE.md.template")

    # 平台仓库路径**在运行时确定**（--platform / 环境 / 本文件所在位置），
    # 绝不写死成某个人的机器路径 —— 模板里一律用 `<平台仓库>` 占位。
    # ⚠️ 这两条替换必须在 render_mapping 之后、ANGLE_RE 检查之前：
    #    残留的 `<...>` 会被下面那道检查当场抓出来。
    out = out.replace("<平台仓库>", v["PLATFORM_ROOT"])
    out = out.replace("<芯片家族>", v["FAMILY"])

    left = sorted(set(ANGLE_RE.findall(out)))
    if left:
        raise InitError(
            "CLAUDE.md 生成后仍有未替换的 `<...>` 占位符：%s\n"
            "  说明平台模板新增了占位符，而本工具的映射表没跟上。" % left
        )
    return out


def render_gitignore(text: str, v: dict) -> str:
    name = v["PROJECT_NAME"]
    pairs = [
        ("# .gitignore 模板 —— 拷成 <新工程>/.gitignore 后即可使用",
         "# .gitignore —— %s" % name),
        ("# 怎么用\n"
         "#   1) cp <平台仓库>/skeleton/gitignore.template <新工程>/.gitignore\n"
         "#   2) 把本文件里所有 <PROJECT_NAME> 替换成新工程名\n"
         "#      （= Keil 的 <OutputName>，也是 MDK-ARM 下那个输出目录的名字）",
         "# 怎么用\n"
         "#   1) 本文件由 autolab init 从平台模板 skeleton/gitignore.template 实例化生成\n"
         "#      （手工方式：cp %s/skeleton/gitignore.template %s/.gitignore）\n"
         "#   2) 工程名已按本工程替换（见 E 节）：工程名 = Keil 的 OutputName，\n"
         "#      也是 MDK-ARM 下那个输出目录的名字"
         % (v["PLATFORM_ROOT"], name)),
        ("E 节刻意用 <PROJECT_NAME> 占位而不是 MDK-ARM/*/：",
         "E 节刻意用工程名而不是 MDK-ARM/*/："),
        ("MDK-ARM/<PROJECT_NAME>/", "MDK-ARM/%s/" % name),
        ("      <工程名>.c / .h        源码", "      %s.c / .h        源码" % name),
    ]
    out = render_mapping(text, pairs, "gitignore.template")
    left = find_hits(out, GITIGNORE_PLACEHOLDERS)
    if left:
        raise InitError(
            "`.gitignore` 生成后仍有未替换的占位符：%s\n"
            "  平台模板可能新增了占位符，而本工具的映射表没跟上。" % left)
    if name not in out:
        raise InitError("`.gitignore` 生成后没有出现工程名 —— 替换没有生效。")
    if "/.autolab-init/" not in out:
        raise InitError("`.gitignore` 生成后缺少 `/.autolab-init/` 规则。")
    return out


def uncalibrated_threshold_block() -> str:
    """`autolab init` 生成的「尚未标定」阈值块（带机器可读标记）。

    与 `render_threshold_block()` 是**同一段文字的两种状态**：标定成功时
    `autolab calibrate` 把这一整块换成带实测依据的那一块。两块都用同一对标记
    夹住，所以"改哪里"是确定的，不靠找行号或正则猜。
    """
    L = [
        CFG_BLOCK_BEGIN + "（未标定） ---",
        "# 四判据之三的阈值：构建日志里 compiling/assembling/linking 的行数下界。",
        "# 作用：防\"增量构建什么都没编却返回 0\"这种假 0。",
        "#",
        "# ⚠️⚠️ 【尚未标定】",
        "#   build.py 用 cfg.int(\"build\", \"min_compile_lines\")（**无默认值**）读取本项，",
        "#   所以此刻直接跑 `python tools/build.py` 会**报配置错误并退出 1**。",
        "#   这是**有意的可见失败**，不是漏配：本工具不知道本工程一次全量构建会产生",
        "#   多少条动作，而凭经验猜一个数会让判据 3 变成\"永远绿\"，比缺失更危险。",
        "#",
        "# 标定方式（自动跑两次**真实全量软件构建**；不烧录、不接调试器、不开串口）：",
        "#   python tools/autolab.py calibrate --target <本工程根>",
        "#   或在 `autolab init --run-checks` 里自动完成。",
        "# 规则：连续两次构建的动作数一致、为正，且非阈值判据全过时，",
        "#   取 floor(2/3 × 动作数) 为下界写回本文件。",
        "#   计数不一致 / 任一构建失败 / 日志无效 / 产物时间戳未更新",
        "#   -> 退出码 5，本文件**逐字节不变**。",
        "# ⚠️ 只数**一份**日志 —— 某些编译器会把同一份日志同时写到 stdout 与",
        "#    `-o` 指定的文件；把两者相加会让计数翻倍、阈值等于被绕过一半",
        "#    （平台的适配器里真实发生过这个缺陷，已修）。",
        "#",
        "# 下面这一对标记是**机器读的**：`autolab calibrate` 只改这两个标记之间的内容",
        "# （标定成功时，连同上面这段\"尚未标定\"的说明一起换掉）。",
        "# 请勿删除或改名 —— 标记不是 1 个，标定会直接失败（不做猜测式改写）。",
        "# min_compile_lines = <待标定>",
        CFG_BLOCK_END + " ---",
    ]
    return "\n".join(L)


def render_project_cfg(text: str, v: dict, cube: dict, ioc_data: dict) -> str:
    name = v["PROJECT_NAME"]
    uvprojx_rel = cube["uvprojx_rel"]

    def kv(key):
        return ioc_data.get(key, "")

    # --- 波特率：来自本工程 .ioc 的 USART*.BaudRate（不在就整个省略） ---
    bauds = sorted(
        (k, val) for k, val in ioc_data.items()
        if re.fullmatch(r"USART\d+\.BaudRate", k) and val.isdigit()
    )
    if bauds:
        baud_block = (
            "# 波特率。必须与固件侧一致，否则 PC 收到的永远是同一个垃圾字节。\n"
            "# 依据：%s 里的 %s=%s。\n"
            "baud = %s\n"
            % (cube["ioc_rel"], bauds[0][0], bauds[0][1], bauds[0][1])
        )
    else:
        baud_block = (
            "# ⚠️ baud 【有意省略】：%s 里没有 USART*.BaudRate，\n"
            "#    本工具不猜。请查固件侧的串口初始化后填进来。\n"
            % cube["ioc_rel"]
        )

    # --- 内存预算：来自本工程 Keil 工程的 <Cpu> 串 ---
    flash, ram = cube["flash_size"], cube["ram_size"]
    if flash is None or ram is None:
        image_block = (
            "# ---- 内存预算（字节），供镜像契约测试使用 ----\n"
            "# 校验规则：Code + RO-data + RW-data <= flash_size ； RW-data + ZI-data <= ram_size\n"
            "# ⚠️ 【flash_size / ram_size 有意省略】：\n"
            "#    %s 的 <Cpu> 串里读不到可用的 IROM / IRAM 区间（实际读到：%r）。\n"
            "#    本工具不猜数字 —— 写大了等于没有检查。请按数据手册或 Keil 的 Device 设置填。\n"
            "# flash_size = \n"
            "# ram_size = \n"
        ) % (uvprojx_rel, cube["cpu"])
    else:
        image_block = (
            "# ---- 内存预算（字节），供镜像契约测试使用 ----\n"
            "# 校验规则：Code + RO-data + RW-data <= flash_size ； RW-data + ZI-data <= ram_size\n"
            "# ⚠️ 写大了等于没有检查 —— 所以取值**不是**照抄的，而是读自本工程 Keil 的 Device 设置：\n"
            "#    %s 的 <Cpu> 一行里，IROM 区间 -> flash_size；IRAM/IRAM2 区间之和 -> ram_size。\n"
            "#    实际读到：%s\n"
            "# 若日后换了器件而没改 Keil 的 Device 设置，本项会跟着一起错 —— 届时按数据手册核对。\n"
            "flash_size = %d\n"
            "\n"
            "ram_size = %d\n"
        ) % (uvprojx_rel, cube["cpu"], flash, ram)

    dev = cube["device"] or v["MCU"]

    pairs = [
        # ---- 顶部来源说明 ----
        (
            "# project.cfg —— 工程级配置（参考模板）\n"
            "# ============================================================================\n"
            "#\n"
            "# ⚠️ 这是一份【参考模板】，不是任何工程的运行配置。\n"
            "#    取值全部来自真实工程 SDIO_test（STM32F407VET6 + Keil MDK/ARMCC AC5），\n"
            "#    逐项核对过 tools/build.py、tools/serial_tool.py、tools/hil.py、\n"
            "#    tests/test_static.py 的实际常量与 tools/build_logs/ 的真实构建日志。\n"
            "#\n"
            "# 复制到新工程时，下列各项【必须重新校准】，不要照抄：\n"
            "#   1. [project] name               —— 工程名（= Keil 工程名 = 产物目录名）\n"
            "#   2. [build]   uvprojx / axf      —— 新工程的工程文件与产物路径\n"
            "#   3. [build]   min_compile_lines  —— 必须按新工程【实测】重定（用 autolab calibrate）；\n"
            "#                                      本例的 20 是参考工程的取值，**不是**新工程的实测值\n"
            "#   4. [serial]  port / baud        —— 换板子或换 USB 口后端口号会变\n"
            "#   5. [serial]  boot_marker        —— 新工程的启动契约多半与 SDIO_test 不同\n"
            "#   6. [image]   flash_size / ram_size / required_symbols —— 按新芯片与新工程填\n"
            "#\n"
            "# 用法：\n"
            "#   cp <平台仓库>/adapters/stm32-keil-ac5/project.cfg.example <新工程>/tools/project.cfg\n"
            "#   然后逐项按新工程填写，并把上面这段\"参考模板\"说明改写成新工程自己的说明。",
            "# project.cfg —— %s 工程级配置\n"
            "# ============================================================================\n"
            "#\n"
            "# 本文件由 `autolab init` 从平台模板\n"
            "#   adapters/stm32-keil-ac5/project.cfg.example\n"
            "# 实例化生成。下面每一项都写清了**取值来源**，没有一项是照抄参考工程的：\n"
            "#\n"
            "#   [project] name              <- --project-name（已核对 Keil <TargetName>）\n"
            "#   [build]   uvprojx           <- 目标工程里实际发现的那个 .uvprojx\n"
            "#   [build]   axf               <- 该 .uvprojx 的 <OutputDirectory> + <OutputName>\n"
            "#   [build]   min_compile_lines <- 【尚未标定】有意省略，见该节说明\n"
            "#   [serial]  baud              <- 本工程 .ioc 里的 USART*.BaudRate\n"
            "#   [serial]  port/boot_marker/reset_cmd_hex <- 【尚未确定】有意省略，见该节说明\n"
            "#   [ioc]     file              <- 目标工程里实际发现的那个 .ioc\n"
            "#   [image]   flash_size/ram_size <- 该 .uvprojx 的 <Cpu> 里 IROM / IRAM 区间\n"
            "#   [image]   required_symbols  <- 保守取值，见该节说明\n"
            "#\n"
            "# ⚠️ 仍须人工补齐的两件事（本工具**刻意不猜**）：\n"
            "#   1. min_compile_lines —— 必须跑一次真实全量构建、数出动作条数后才能填。\n"
            "#      猜一个数会让判据 3 变成假绿，比缺失危险。\n"
            "#   2. [serial] 的 port / boot_marker / reset_cmd_hex —— 只有固件真的实现了\n"
            "#      启动标记与软复位、并且人工确认过，才可以填。\n"
            "#\n"
            "# 本工程（探测所得）：器件 %s；Keil 工程 %s\n"
            % (name, dev, uvprojx_rel),
        ),
        # ---- [project] ----
        (
            "# 工程名。换工程必须改。\n"
            "# 依据：MDK-ARM/SDIO_test.uvprojx 里的 <TargetName>/<OutputName>，也是 Keil 输出目录名\n"
            "name = SDIO_test",
            "# 工程名（= Keil 工程名 = 产物目录名）。\n"
            "# 依据：%s 里的 <TargetName>（实测 %r）。\n"
            "name = %s" % (uvprojx_rel, cube["target_name"], name),
        ),
        # ---- [build] uvprojx ----
        (
            "# Keil 工程文件（相对工程根）。换工程必须改。\n"
            "uvprojx = MDK-ARM/SDIO_test.uvprojx",
            "# Keil 工程文件（相对工程根）。\n"
            "# 依据：目标工程里实际发现的那一个（本工具不按工程名拼路径）。\n"
            "uvprojx = %s" % uvprojx_rel,
        ),
        # ---- [build] axf ----
        (
            "# 构建产物 .axf（相对工程根）。\n"
            "# ⚠️ **按 Keil 的 Output Directory 设置填，别想当然** —— 实测发现\n"
            "#    `<OutputName>`（决定文件名）与 `<OutputDirectory>`（决定目录）是**两个独立设置**，\n"
            "#    目录**不跟工程名走**。SDIO_test 恰好两者同名，容易让人以为有对应关系。\n"
            "#    填错的表现很迷惑：**构建明明成功，判据 4 却报\"产物不存在\"**。\n"
            "# 四判据之四（\"产物时间戳已更新\"）读的就是这个文件。\n"
            "axf = MDK-ARM/SDIO_test/SDIO_test.axf",
            "# 构建产物 .axf（相对工程根）。\n"
            "# ⚠️ `<OutputName>`（决定文件名）与 `<OutputDirectory>`（决定目录）是**两个独立设置**，\n"
            "#    目录**不跟工程名走**；填错的表现很迷惑：构建明明成功，判据 4 却报\"产物不存在\"。\n"
            "# ⚠️ 下面的取值**不是**按工程名拼出来的，而是直接读自 %s：\n"
            "#      <OutputDirectory> = %r\n"
            "#      <OutputName>      = %r\n"
            "# 四判据之四（\"产物时间戳已更新\"）读的就是这个文件。\n"
            "axf = %s"
            % (uvprojx_rel, cube["output_directory"], cube["output_name"], cube["axf_rel"]),
        ),
        # ---- [build] min_compile_lines ----
        (
            "# 四判据之三的阈值：构建日志里 compiling/assembling/linking 的行数下界。\n"
            "# 作用：防\"增量构建什么都没编却返回 0\"这种假 0。\n"
            "# ⚠️ 必须按新工程【实测】标定，**不要照抄本文件的 20** —— 那是 SDIO_test 的值，\n"
            "#    不是你的工程的实测值。抄错的后果是判据 3 形同虚设（永远绿）。\n"
            "#\n"
            "# 标定方式（自动跑两次真实全量软件构建；不烧录、不接调试器、不开串口）：\n"
            "#   python tools/autolab.py calibrate --target <工程根>\n"
            "#   或在 `autolab init --run-checks` 里自动完成。\n"
            "# 规则：两次动作数一致且为正、且其余判据全过时，取 floor(2/3 × 动作数)。\n"
            "# ⚠️ 由 `autolab init` 生成的工程里，这一行**先是注释**（<待标定>），\n"
            "#    标定成功后由工具改写进标记块；本参考文件里的 20 只是历史取值，供对照。\n"
            "# 本例出处：SDIO_test 全量构建实测 56 条（见 tools/build_logs/ 的 PASS 日志），\n"
            "#          当时取约 1/3 作为下界 20。\n"
            "min_compile_lines = 20",
            uncalibrated_threshold_block(),
        ),
        # ---- [serial] ----
        (
            "# 串口端口。换板子或换 USB 口后会变（在设备管理器里查）。\n"
            "# 优先级：--port 参数 > 环境变量 AUTOLAB_PORT > 本项 > 内置默认\n"
            "port = COM9\n"
            "\n"
            "# 波特率。必须与固件侧一致，否则 PC 收到的永远是同一个垃圾字节。\n"
            "baud = 115200\n"
            "\n"
            "# 复位后固件应依次发出的\"启动标记\"。\n"
            "# SDIO_test 的契约：U=时钟/UART 就绪，S=SDIO 初始化通过，h=进入主循环。\n"
            "# ⚠️ 新工程的启动标记通常【不同】—— 例如没有 SDIO 的工程就不该有 S。\n"
            "boot_marker = USh\n"
            "\n"
            "# 软复位命令（十六进制字节）。固件侧约定：收到该字节即 NVIC_SystemReset()。\n"
            "# 1b = ESC。有了它，冒烟测试无需人工按复位键，可被 pytest / CI 直接调用。\n"
            "reset_cmd_hex = 1b",
            baud_block
            + "\n"
            "# ---------------------------------------------------------------------------\n"
            "# ⚠️ port / boot_marker / reset_cmd_hex【有意省略】\n"
            "#\n"
            "# 省略 = 本平台表达\"该值尚未确定\"的正规方式，不是漏配。后果是**可见的**：\n"
            "#   · tools/serial_tool.py 会退回内置默认并逐条打 [WARN]\n"
            "#   · tools/preflight.py 会把每一条降级作为 [WARN] 列出并附修复提示\n"
            "# 因此\"未配置\"不会被隐藏，也不会导致工具静默用错参数。\n"
            "#\n"
            "# 为什么不在这里填值 —— 这三项都是**未验证的硬件契约**，填入等于编造事实：\n"
            "#   · port          —— USB-TTL 芯片型号与 COM 口尚未确认；不同机器的 COM 号\n"
            "#                      不同，填入任何具体值都是照抄别的工程。\n"
            "#   · boot_marker   —— 取决于固件是否真的在复位后输出启动标记。\n"
            "#   · reset_cmd_hex —— 取决于固件是否真的实现了软复位命令。\n"
            "# 要填的前提是：**固件侧已实现、且人工确认过**，再回来写进来。\n"
            "#\n"
            "# 已知影响：L3（真机 / HIL）因此不可用 —— 这是如实反映工程当前状态。\n"
            "# ---------------------------------------------------------------------------",
        ),
        # ---- [ioc] ----
        (
            "# CubeMX 配置源文件名（相对工程根）；代码由它生成。\n"
            "file = SDIO_test.ioc",
            "# CubeMX 配置源文件名（相对工程根）；代码由它生成。\n"
            "# 依据：目标工程根目录下实际发现的那个 .ioc。\n"
            "file = %s" % cube["ioc_rel"],
        ),
        # ---- [image] ----
        (
            "# ---- 内存预算（字节），供镜像契约测试使用 ----\n"
            "# 校验规则：Code + RO-data + RW-data <= flash_size ； RW-data + ZI-data <= ram_size\n"
            "# ⚠️ 写大了等于没有检查；必须来自数据手册或 Keil 的 Device 设置。\n"
            "# 本例：STM32F407VET6 = 512 KiB Flash @0x08000000 / 128 KiB RAM @0x20000000\n"
            "#      （Keil 里 IROM=0x80000；IRAM=0x1C000 + IRAM2=0x4000，合计 0x20000）\n"
            "# 本例当前实际占用：Code=10456 RO-data=456 RW-data=36 ZI-data=1772\n"
            "#      -> Flash 10948 / 524288 ； RAM 1808 / 131072（余量充足）\n"
            "flash_size = 524288\n"
            "\n"
            "ram_size = 131072               ; 32 KiB 以内的小芯片别忘了改小",
            image_block,
        ),
        # ---- [image] required_symbols ----
        (
            "# 必须出现在最终镜像里的符号（逗号分隔），每个都会被逐一断言。\n"
            "# ⚠️ 换成新工程【真正关键】的符号。\n"
            "#    MX_SDIO_SD_Init 是 SDIO_test 的【业务专属】符号，新工程多半没有它；\n"
            "#    main / SystemClock_Config / UART 回调 是更通用的起点。\n"
            "required_symbols = main, SystemClock_Config, MX_SDIO_SD_Init, HAL_UART_RxCpltCallback, HAL_UART_ErrorCallback",
            "# 必须出现在最终镜像里的符号（逗号分隔），每个都会被逐一断言。\n"
            "#\n"
            "# ⚠️⚠️ 【只填了一个保守值 —— 需要按本工程补齐】\n"
            "#   本工具**不可能**知道本工程的业务符号是什么（某个外设初始化、某个自己实现的\n"
            "#   回调），所以只断言 `main` —— 它出现在任何有入口函数的镜像里，是**真事实**，\n"
            "#   不是占位文本。\n"
            "#   请在第一次构建通过后，用符号表工具（如 fromelf --text -s）列出真实符号，\n"
            "#   把本工程真正关键的那几个补进来（例如 MX_*_Init、自己实现的回调）。\n"
            "# ⚠️ 慎选 HAL 的 __weak 默认桩（例如各类 HAL_*Callback）：断言它只能证明\n"
            "#    \"该 HAL 模块被链接\"，**不能**证明固件真的实现了这个回调。\n"
            "required_symbols = main",
        ),
    ]

    out = render_mapping(text, pairs, "project.cfg.example")
    hits = scan_output(out, FORBIDDEN_IN_PROJECT_CFG,
                       [v.get("PROJECT_NAME"), v.get("PROJECT_PATH")])
    if hits:
        raise InitError(
            "生成 tools/project.cfg 时检测到参考工程的事实泄漏：%s\n"
            "  模板映射表可能漏改了某一项 —— 拒绝生成。" % hits
        )
    if re.search(r"(?m)^\s*min_compile_lines\s*=", out):
        raise InitError("生成的 tools/project.cfg 里出现了已生效的 min_compile_lines —— "
                        "本阶段不标定阈值，必须保持注释状态。")
    if re.search(r"(?m)^\s*(port|boot_marker|reset_cmd_hex)\s*=", out):
        raise InitError("生成的 tools/project.cfg 里出现了 port/boot_marker/reset_cmd_hex —— "
                        "这三项在未确认前必须保持省略。")
    return out


# ============================================================================
# runner 标签（ci.yml 用）
# ============================================================================
ALLOWED_LABEL_RE = re.compile(r"[A-Za-z0-9_.\-]+")


def normalise_labels(raw: str, what: str) -> str:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise InitError("%s 是空的 —— 至少要有一个标签。" % what)
    for p in parts:
        if not ALLOWED_LABEL_RE.fullmatch(p):
            raise InitError(
                "%s 里的标签 %r 含非法字符。\n"
                "  标签必须是 YAML 里安全的裸标量（字母/数字/下划线/点/连字符），\n"
                "  否则会被拼进 `runs-on: [ ... ]` 流式序列里而破坏 YAML 语法。" % (what, p)
            )
    return "[" + ", ".join(parts) + "]"


def resolve_runner_labels(env: dict, family: Family):
    """返回 (软件标签文本, 硬件标签文本, warnings)。

    生成策略（明确的优先级）：
        环境变量 AUTOLAB_RUNNER_LABELS_SOFTWARE / _HARDWARE
        > 按芯片家族给的内置默认（self-hosted + 本机平台 + 家族工具链标签）
    ⚠️ 两个变量**必须**有取值 —— 绝不能把未实例化的 `${RUNNER_LABELS...}`
       留在生成物里假装流水线可用。用了内置默认时**必须**打 WARN：
       默认标签只是"一个能实例化的起点"，不代表 runner 上真的注册了它们。
    """
    warnings = []
    tlabel = family.toolchain_label
    defaults = {
        "AUTOLAB_RUNNER_LABELS_SOFTWARE": ["self-hosted", "windows", tlabel],
        "AUTOLAB_RUNNER_LABELS_HARDWARE": ["self-hosted", "windows", tlabel, "board"],
    }
    out = []
    for var in ("AUTOLAB_RUNNER_LABELS_SOFTWARE", "AUTOLAB_RUNNER_LABELS_HARDWARE"):
        raw = (env.get(var) or "").strip()
        if raw:
            out.append(normalise_labels(raw, var))
        else:
            out.append("[" + ", ".join(defaults[var]) + "]")
            warnings.append(
                "未提供 %s —— ci.yml 使用了内置默认标签 %s。"
                "这只是**可实例化的起点**，不代表 runner 上真的注册了这些标签；"
                "请在 self-hosted runner 上创建同名标签，或用环境变量覆盖后重新生成。"
                % (var, out[-1])
            )
    return out[0], out[1], warnings


# ============================================================================
# 变量装配
# ============================================================================
def human_bytes(n) -> str:
    if n is None:
        return "（未知）"
    if n % 1024 == 0:
        return "%d 字节（%d KiB）" % (n, n // 1024)
    return "%d 字节" % n


PLACEHOLDER_GOAL = "（未配置 —— 由工程负责人填写）"


def build_variables(project_name, target, family: Family, cube: dict, opts: dict):
    """把 --mcu/--board/--toolchain 与**从工程里实测到的**事实合起来。

    ⚠️ 未提供、且无法从工程推导的项，一律写成显式的「未配置」并打 WARN ——
       绝不把占位文本伪装成真实硬件参数。
    """
    warnings = []
    ioc = cube["ioc_data"]

    mcu = (opts.get("mcu") or "").strip()
    if not mcu:
        mcu = cube["device"] or ioc.get("Mcu.CPN", "")
        if mcu:
            warnings.append(
                "未提供 --mcu —— 已从本工程实测取值 %r"
                "（Keil <Device>/Mcu.CPN）。这是**探测结果**，请人工核对。" % mcu)
    if not mcu:
        mcu = "未配置（未提供 --mcu，且工程里读不到器件型号）"
        warnings.append("未提供 --mcu，且 .uvprojx 与 .ioc 里都读不到器件型号 —— "
                        "README/CLAUDE.md 里会写成「未配置」，请补齐。")

    board = (opts.get("board") or "").strip()
    if not board:
        board = "未配置（未提供 --board）"
        warnings.append("未提供 --board —— 板卡型号无法从工程推导，生成物里会写成「未配置」。")

    toolchain = (opts.get("toolchain") or "").strip()
    if not toolchain:
        tc = ioc.get("ProjectManager.TargetToolchain", "")
        if tc:
            toolchain = "%s（%s）" % (tc, family.title)
            warnings.append("未提供 --toolchain —— 已从 .ioc 的 ProjectManager.TargetToolchain "
                            "实测取值 %r，请人工核对。" % tc)
        else:
            toolchain = family.title
            warnings.append("未提供 --toolchain，且 .ioc 里没有 TargetToolchain —— "
                            "已回退到家族默认描述 %r。" % family.title)

    package = ioc.get("Mcu.Package", "") or "（未知）"

    generator = "STM32CubeMX"
    if ioc.get("MxCube.Version"):
        generator += " " + ioc["MxCube.Version"]
    else:
        generator += "（.ioc 里没有 MxCube.Version）"

    hse = ioc.get("RCC.HSE_VALUE", "")
    if hse.isdigit():
        mhz = int(hse) / 1_000_000
        clock = "HSE %s Hz（%.3f MHz）；时钟树见 %s" % (hse, mhz, cube["ioc_rel"])
    else:
        clock = "（.ioc 里没有 RCC.HSE_VALUE —— 请人工填写时钟树）"

    n = ioc.get("Mcu.IPNb", "")
    periph = []
    if n.isdigit():
        for i in range(int(n)):
            v = ioc.get("Mcu.IP%d" % i)
            if v:
                periph.append(v)
    peripherals = " / ".join(periph) if periph else "（.ioc 里没有 Mcu.IP* —— 请人工填写）"

    # 内存预算读不出来时**必须**出声 —— 否则 project.cfg 里悄悄少了一项，
    # 使用者会以为镜像契约测试仍然在保护他。
    if cube["flash_size"] is None or cube["ram_size"] is None:
        warnings.append(
            "无法从 Keil 的 <Cpu> 串里可信地解析内存预算（Flash=%s / RAM=%s）—— "
            "tools/project.cfg 的 [image] flash_size / ram_size 已**有意省略**，"
            "镜像契约测试会因为缺配置而报错（可见失败），请按数据手册手工补齐。"
            % (human_bytes(cube["flash_size"]), human_bytes(cube["ram_size"])))

    values = {
        "PROJECT_NAME": project_name,
        "PROJECT_PATH": str(target),
        "MCU": mcu,
        "MCU_BOARD": "%s / %s" % (mcu, board),
        "PACKAGE": package,
        "FLASH": human_bytes(cube["flash_size"]),
        "RAM": human_bytes(cube["ram_size"]),
        "BOARD": board,
        "TOOLCHAIN": toolchain,
        "GENERATOR": generator,
        "CLOCK": clock,
        "PERIPHERALS": peripherals,
        "GOAL": PLACEHOLDER_GOAL,
        "FAMILY": family.key,
        "IOC_FILE": cube["ioc_rel"],
    }
    # ⚠️ 必须把 warnings 一起交出去 —— 降级可以，静默不行。
    return values, warnings


# ============================================================================
# 计划
# ============================================================================
@dataclass
class PlannedFile:
    dst_rel: str
    data: bytes
    origin: str
    src_rel: str
    kind: str = "copy"          # "copy" = 逐字节复制；"template" = 模板实例化

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.data)


@dataclass
class Plan:
    target: Path
    family: Family
    project_name: str
    platform_root: Path
    platform_head: str
    cube: dict
    variables: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    files: list = field(default_factory=list)

    def by_dest(self):
        return {f.dst_rel: f for f in self.files}


def build_plan(platform_root: Path, family_key: str, target: Path,
               project_name: str, opts: dict, env: dict) -> Plan:
    """只读阶段：把要写的东西全部算出来，一个字节都不落盘。"""
    family = get_family(family_key)
    target = Path(target).resolve()
    platform_root = Path(platform_root).resolve()

    # 1) 目标必须是一个已生成的 CubeMX 工程
    cube = discover_cube_project(target, project_name)

    # 2) 平台源完整性
    check_sources_exist(platform_root, family)
    head = platform_head(platform_root)
    require_min_platform(platform_root)
    modified = check_sources_unmodified(platform_root, family)
    if modified:
        raise InitError(
            "平台源文件在工作树里已被本地修改，拒绝拿它初始化：\n%s\n"
            "  请先在平台仓库里把这些改动提交（或 stash）后再试。"
            % "\n".join("    " + m for m in modified)
        )

    plan = Plan(
        target=target, family=family, project_name=project_name,
        platform_root=platform_root, platform_head=head, cube=cube,
    )

    # 3) 逐字节复制
    for src_rel, dst_rel in family.copies:
        data = (platform_root / src_rel).read_bytes()
        plan.files.append(
            PlannedFile(dst_rel, data, "逐字节复制自 " + src_rel, src_rel, "copy"))

    # 4) 变量装配
    v, var_warn = build_variables(project_name, target, family, cube, opts)
    # 用正斜杠，免得文档里出现 `C:\...\embedded-ai-journey/skeleton/...` 这种混写
    v["PLATFORM_ROOT"] = platform_root.as_posix()
    plan.variables = v

    # 5) 模板实例化
    soft, hard, label_warn = resolve_runner_labels(env, family)
    plan.warnings = list(var_warn) + list(label_warn)
    v = dict(v)
    v["RUNNER_LABELS_SOFTWARE"] = soft
    v["RUNNER_LABELS_HARDWARE"] = hard

    # 生成物里**允许**出现的字眼：用户自己给的值。
    # 工程名/路径是他选的，不该被判成"参考工程事实泄漏"。
    user_owned_values = [project_name, str(target)]
    for k in ("mcu", "board", "toolchain"):
        if opts.get(k):
            user_owned_values.append(str(opts[k]))

    for src_rel, dst_rel, kind in family.templates:
        raw, nl = read_text_preserving_eol(platform_root / src_rel)
        tpl_name = Path(src_rel).name

        if kind == TPL_DOLLAR:
            allowed = DOLLAR_TEMPLATE_VARS[dst_rel]
            body = render_dollar(raw, v, tpl_name, allowed)
        elif kind == TPL_CLAUDE:
            body = render_claude(raw, v)
        elif kind == TPL_GITIGNORE:
            body = render_gitignore(raw, v)
        elif kind == TPL_PROJECT_CFG:
            body = render_project_cfg(raw, v, cube, cube["ioc_data"])
        else:
            raise InitError("内部错误：未知的模板种类 %r" % kind)

        # 泄漏扫描：只扫**模板生成物**，且用**具名清单**，
        # 不对全工程做全局黑名单（那会误杀用来防回归的测试）。
        if kind == TPL_PROJECT_CFG:
            pass  # render_project_cfg 内部已扫过
        elif kind == TPL_DOLLAR:
            hits = scan_output(body, FORBIDDEN_IN_DOCS, user_owned_values)
            if hits:
                raise InitError("模板 %s 的生成物里出现参考工程事实：%s"
                                % (tpl_name, hits))
            tpl_text = raw
            thits = find_hits(tpl_text, FORBIDDEN_IN_TEMPLATES)
            if thits:
                raise InitError(
                    "平台模板 %s 本身含参考工程事实：%s\n"
                    "  平台侧模板被改脏了 —— 先修平台模板再重试。" % (src_rel, thits))

        out = restore_eol(body, nl)
        plan.files.append(
            PlannedFile(dst_rel, out.encode("utf-8"),
                        "由模板实例化：" + src_rel, src_rel, "template")
        )

    return plan


# ============================================================================
# 冲突检测
# ============================================================================
@dataclass
class Collision:
    dst_rel: str
    kind: str  # "create" | "same" | "different"


def detect_collisions(target: Path, plan: Plan) -> list:
    """**逐文件**判断，而不是把"目录非空"当成拒绝理由。

    正常目标是已经由 CubeMX 生成的工程 —— 目录本来就非空。
    """
    out = []
    for f in plan.files:
        p = Path(target) / f.dst_rel
        if not p.exists():
            out.append(Collision(f.dst_rel, "create"))
        elif p.read_bytes() == f.data:
            out.append(Collision(f.dst_rel, "same"))
        else:
            out.append(Collision(f.dst_rel, "different"))
    return out


# ============================================================================
# staging（系统临时目录；目标目录此时仍为零变化）
# ============================================================================
def stage_files(files, staging_dir: Path) -> dict:
    staging_dir = Path(staging_dir)
    staged = {}
    for f in files:
        dst = staging_dir / f.dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(f.data)
        got = sha256_file(dst)
        if got != f.sha256:
            raise InitError(
                "staging 写入校验失败：%s（期望 %s，实际 %s）"
                % (f.dst_rel, f.sha256, got)
            )
        staged[f.dst_rel] = dst
    return staged


# ============================================================================
# 备份（--force 覆盖之前）
# ============================================================================
def backup_files(target: Path, rels, backup_dir: Path) -> list:
    """把即将被覆盖的原件备份到 backup_dir，并**逐字节验证**。

    ⚠️ 验证不通过的备份**不允许**覆盖原件 —— 宁可整轮失败。
    """
    target = Path(target)
    backup_dir = Path(backup_dir)
    done = []
    for rel in rels:
        src = target / rel
        if not src.is_file():
            continue
        dst = backup_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        before = sha256_file(src)
        shutil.copyfile(src, dst)
        after = sha256_file(dst)
        if before != after or sha256_file(src) != before:
            raise InitError(
                "备份校验失败，**已放弃覆盖**：%s\n"
                "  备份前 %s / 备份后 %s\n"
                "  备份目录：%s" % (rel, before, after, backup_dir)
            )
        done.append(rel)
    return done


# ============================================================================
# 落盘
# ============================================================================
def commit_files(target: Path, files, staged: dict, skip_same=True):
    """把 staging 里的文件搬到目标目录，逐个校验 SHA-256。

    返回 (written, skipped)。**不删除任何已有文件** —— 失败的中间状态如实保留并上报。
    """
    target = Path(target)
    written, skipped = [], []
    for f in files:
        dst = target / f.dst_rel
        if skip_same and dst.is_file() and dst.read_bytes() == f.data:
            skipped.append(f.dst_rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(staged[f.dst_rel], dst)
        got = sha256_file(dst)
        if got != f.sha256:
            raise InitError(
                "落盘校验失败：%s（期望 %s，实际 %s）\n"
                "  已写入的文件**不会**被自动删除 —— 请人工核对后重试。"
                % (f.dst_rel, f.sha256, got)
            )
        written.append(f.dst_rel)
    return written, skipped


# ============================================================================
# L0 预检
# ============================================================================
def run_preflight(target: Path, python_exe=None, timeout: int = 300) -> dict:
    """在目标工程里跑 `python tools/preflight.py`。

    ⚠️ 只跑 L0。**不跑** pytest、**不跑** Keil 构建、**不烧录**、**不碰串口**。
    """
    import sys

    exe = python_exe or sys.executable
    cmd = [exe, "tools/preflight.py"]
    try:
        r = subprocess.run(
            cmd, cwd=str(target), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except FileNotFoundError:
        raise InitError("找不到 Python 解释器 %r —— 无法执行 L0 预检。" % exe)
    except subprocess.TimeoutExpired:
        raise InitError("L0 预检超时（%d 秒）。" % timeout)
    return {
        "cmd": " ".join(cmd),
        "cwd": str(target),
        "exit_code": r.returncode,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }


# ============================================================================
# 跑目标工程自己的工具（L1 构建 / L2 测试）
# ============================================================================
# 骨架层**不碰**工具链细节：构建怎么调、日志长什么样，全是适配器的事。
# 这里只做一件事：把 `python tools/<某个工具>` 跑起来，收回退出码与输出。
# 与 run_preflight() 同构。
def _child_env() -> dict:
    """给被调用的工程工具一套**确定性**的 IO 编码。

    Windows 上子进程 stdout 被重定向到管道时用的是本地代码页（GBK），
    而本工具按 UTF-8 解码 —— 不统一的话中文会变成乱码（内容本身是好的，
    但报告里就不可读了）。PYTHONIOENCODING 把子进程钉死在 UTF-8。
    ⚠️ 只影响**本工具启动的子进程**；不影响人工直接跑 tools/build.py 的行为。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _run_project_tool(target: Path, argv_tail, timeout: int, python_exe, what: str) -> dict:
    import sys

    exe = python_exe or sys.executable
    cmd = [exe] + list(argv_tail)
    try:
        r = subprocess.run(
            cmd, cwd=str(target), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            env=_child_env(),
        )
    except FileNotFoundError:
        raise InitError("找不到 Python 解释器 %r —— 无法执行 %s。" % (exe, what))
    except subprocess.TimeoutExpired:
        raise InitError("%s 超时（%d 秒）—— 已中止。" % (what, timeout))
    return {
        "cmd": " ".join(cmd),
        "cwd": str(target),
        "exit_code": r.returncode,
        "stdout": r.stdout,
        "stderr": r.stderr,
    }


# L2 里那条**会再跑一次真实构建**的用例。L1 刚刚跑过（--skip-build 时更是明确
# 要求不构建），所以显式 deselect 掉它 —— 这是**去重**，不是"跳过构建"：
# 完整验证里至少有一次真实构建步骤，由 L1 承担。
L2_DESELECT = "tests/test_build.py::test_build_passes"


def run_build_l1(target: Path, python_exe=None, timeout: int = 1800) -> dict:
    """L1：正式构建。四判据**全判**，其中判据 3 要求 min_compile_lines 已标定。

    ⚠️ 不烧录、不接调试器、不开串口。
    """
    return _run_project_tool(target, ["tools/build.py"], timeout, python_exe, "L1 构建")


def run_pytest_l2(target: Path, python_exe=None, timeout: int = 1800) -> dict:
    """L2：无板测试（`pytest -m "not hardware"`，deselect 掉重复构建的那条）。

    ⚠️ 不跑 hardware 用例：没有板子、没有串口、没有调试器。
    """
    return _run_project_tool(
        target,
        ["-m", "pytest", "-m", "not hardware", "--deselect", L2_DESELECT, "-q"],
        timeout, python_exe, "L2 无板测试")


# ============================================================================
# 构建动作阈值：标定与写回（命令 `autolab calibrate`）
# ============================================================================
class CalibrationError(InitError):
    """阈值无法标定 —— 此时 tools/project.cfg 必须**逐字节不变**。"""
    exit_code = EXIT_CALIBRATION


def threshold_for(n: int) -> int:
    """稳定动作数 n -> 判据 3 的下界：**floor(2n/3)**（授权书定死的规则）。"""
    return (2 * n) // 3


def threshold_is_sane(n: int, thr: int) -> bool:
    """阈值必须"真构建过得去、空转过不去"，且留有余量。

    · n <= 0     —— 根本没有动作，谈不上标定
    · thr < 1    —— 0 等于没有检查（1 条动作就能"通过"）
    · thr >= n   —— 阈值等于/超过真实条数，动作数稍有抖动就误红
    """
    return n > 0 and 1 <= thr < n


def threshold_is_missing(target: Path) -> bool:
    """目标工程的 tools/project.cfg 里有没有**生效**的 min_compile_lines。

    读不到文件也返回 True —— 调用方随后会因为"没有 project.cfg"明确失败。
    """
    p = Path(target) / "tools" / "project.cfg"
    if not p.is_file():
        return True
    try:
        text = p.read_bytes().decode("utf-8", "replace")
    except OSError:
        return True
    return not _LIVE_THRESHOLD_RE.search(text)


def parse_measure_line(stdout: str) -> dict:
    """从 `tools/build.py --measure` 的输出里取**最后一条**协议行。

    ⚠️ 取不到就失败，绝不"没读到就当成默认值" —— 那正是最危险的一种假绿。
       最常见的原因：目标工程里的 tools/build.py 是**旧版**（不认识 --measure）。
    """
    hit = None
    for line in stdout.splitlines():
        if line.startswith(BUILD_MEASURE_PREFIX):
            hit = line[len(BUILD_MEASURE_PREFIX):]
    if hit is None:
        raise CalibrationError(
            "目标工程的 tools/build.py 没有输出测量行（前缀 %s）。\n"
            "  最可能的原因：该文件的版本过旧，不支持 --measure。\n"
            "  修法：重跑一次 `autolab init` 把平台工具同步进这个工程，再标定。"
            % BUILD_MEASURE_PREFIX)
    try:
        data = json.loads(hit)
    except ValueError as e:
        raise CalibrationError("测量行不是合法 JSON：%r（%s）" % (hit[:200], e)) from None
    if not isinstance(data, dict) or not isinstance(data.get("actions"), int) \
            or "passed" not in data:
        raise CalibrationError(
            "测量行缺少 actions / passed 字段：%r\n"
            "  适配器的测量协议变了而本工具没跟上？" % (hit[:200],))
    return data


def run_build_measure(target: Path, python_exe=None, timeout: int = 1800) -> dict:
    """在目标工程里跑**一次真实全量软件构建**，只为测出动作条数。

    ⚠️ 不烧录、不接调试器、不开串口。测量模式**只**放宽判据 3 的阈值，
       退出码 / 0 Error 0 Warning / AXF 时间戳三条照常判定并由这里的
       `exit_code` 与 `data["passed"]` 双重把关。
    """
    try:
        res = _run_project_tool(
            target, ["tools/build.py", "--measure"], timeout, python_exe,
            "构建（测量模式）")
    except InitError as e:
        # 连构建都没跑起来（解释器不在 / 超时）—— 这是"标不出来"，不是"参数错"。
        raise CalibrationError(str(e)) from None
    res["data"] = parse_measure_line(res["stdout"])
    return res


def split_threshold_block(text: str):
    """把 project.cfg 切成 (标记块之前, 标记块, 标记块之后)。

    标记块不唯一就失败 —— 本工具**不做猜测式改写**。
    """
    ms = list(_CFG_BLOCK_RE.finditer(text))
    if len(ms) != 1:
        raise CalibrationError(
            "tools/project.cfg 里的阈值标记块有 %d 个（期望恰好 1 个）：%s … %s\n"
            "  本工具不做猜测式改写。请把该文件恢复成 `autolab init` 生成的样子"
            "（或手工把这两个标记补回来）后重试。" % (len(ms), CFG_BLOCK_BEGIN, CFG_BLOCK_END))
    return text[:ms[0].start()], ms[0].group(0), text[ms[0].end():]


def render_threshold_block(thr: int, n: int, runs, target: Path, generated_at: str) -> str:
    """标定后的阈值标记块。写清**取值来源**，不写"经验值"。"""
    target = Path(target)

    def rel(p):
        try:
            return Path(p).resolve().relative_to(target.resolve()).as_posix()
        except (ValueError, TypeError):
            # 不在工程内（例如用户改了输出目录）——原样给出绝对路径，别编。
            return str(p)

    L = [
        CFG_BLOCK_BEGIN,
        "# 四判据之三的阈值：构建日志里 compiling/assembling/linking 的行数下界。",
        "# 作用：防\"增量空转却返回 0\"这种假 0。",
        "#",
        "# 由 `python tools/autolab.py calibrate` 于 %s 实测标定：" % generated_at,
        "#   连续两次**真实全量构建**的动作数均为 **%d** 条，" % n,
        "#   按 floor(2/3 × %d) = **%d** 取为下界。" % (n, thr),
    ]
    for r in runs:
        L.append("#   · 第 %s 次：%s 条，退出码 %s，日志 %s"
                 % (r.get("round"), r.get("actions"), r.get("exit_code"),
                    rel(r.get("log") or "")))
    L += [
        "#",
        "# ⚠️ 阈值只防\"空转假 0\"，**不代表**\"真实增量构建已验证\" ——",
        "#    本工程用 CLI 构建时每次都全量重编，\"空转\"根本没被构造出来过。",
        "# ⚠️ 换工程 / 换工具链 / 改动源码集之后再构建，都要重新标定。",
        "min_compile_lines = %d" % thr,
        CFG_BLOCK_END + " ---",
    ]
    # 结尾必须带换行：`split_threshold_block()` 切出来的块含行尾的 `\n`，
    # 两侧一致才能保证"改写只发生在块内"这条校验真的可比。
    return "\n".join(L) + "\n"


def _run_summary(r: dict) -> dict:
    d = r["data"]
    return {
        "round": r.get("round"),
        "exit_code": r["exit_code"],
        "cmd": r["cmd"],
        "actions": d.get("actions"),
        "errors": d.get("errors"),
        "warnings": d.get("warnings"),
        "axf_updated": d.get("axf_updated"),
        "passed": d.get("passed"),
        "log": d.get("log", ""),
    }


def _tail(text: str, n: int = 25) -> str:
    lines = text.rstrip("\n").splitlines()
    return "\n".join("      | " + x for x in lines[-n:])


def calibrate_threshold(target: Path, python_exe=None, timeout: int = 1800) -> dict:
    """两次真实全量构建 -> 稳定动作数 -> floor(2/3) -> 写回 tools/project.cfg。

    只有**全部**通过才写文件；任何失败路径都抛 CalibrationError（退出码 5），
    且 tools/project.cfg **逐字节不变**（写盘是最后一步，之前一个字节都没动过）。
    """
    target = Path(target)
    cfg_path = target / "tools" / "project.cfg"
    build_py = target / "tools" / "build.py"
    if not cfg_path.is_file():
        raise InitError("目标工程里没有 tools/project.cfg：%s\n"
                        "  这不是一个已接入 Autolab 的工程。" % cfg_path)
    if not build_py.is_file():
        raise InitError("目标工程里没有 tools/build.py：%s\n"
                        "  这不是一个已接入 Autolab 的工程。" % build_py)

    original = cfg_path.read_bytes()
    text = original.decode("utf-8")
    pre, _old_block, suf = split_threshold_block(text)
    for where, part in (("标记块之前", pre), ("标记块之后", suf)):
        if _LIVE_THRESHOLD_RE.search(part):
            raise CalibrationError(
                "tools/project.cfg 的%s已经有生效的 min_compile_lines —— 拒绝改写\n"
                "  （改下去会变成重复键，configparser 会直接报错）。" % where)

    runs = []
    for i in (1, 2):
        r = run_build_measure(target, python_exe, timeout)
        r["round"] = i
        runs.append(r)
        print("  [%d/2] 全量构建：%s 条动作，退出码 %s，日志 %s"
              % (i, r["data"].get("actions"), r["exit_code"], r["data"].get("log")))

    # ---- 非动作数判据 + 计数稳定性：任一不过就不写 ----
    reasons = []
    for r in runs:
        d = r["data"]
        if r["exit_code"] != 0 or d.get("passed") is not True:
            reasons.append("第 %s 次构建未通过非阈值判据（子进程退出码 %s，"
                           "0 Error/0 Warning 与产物时间戳见下）" % (r["round"], r["exit_code"]))
            reasons.append(_tail(r["stdout"]))
        elif d.get("axf_updated") is not True:
            reasons.append("第 %s 次构建后产物 .axf 时间戳未更新（%s）"
                           % (r["round"], d.get("axf")))

    n1, n2 = runs[0]["data"].get("actions"), runs[1]["data"].get("actions")
    if n1 is None or n2 is None:
        reasons.append("构建日志里没数出动作条数 —— 日志无效")
    else:
        if n1 <= 0 or n2 <= 0:
            reasons.append("动作数为 %s / %s —— 这次构建其实什么都没编" % (n1, n2))
        elif n1 != n2:
            reasons.append("两次动作数不同（%s vs %s）—— 不可复现，拒绝标定"
                           "（可能是并行度/依赖图不稳定，或多进程同时在编同一工程）"
                           % (n1, n2))

    if reasons:
        # 自证：走到这里一个字节都还没写过。
        assert cfg_path.read_bytes() == original, "标定失败路径竟改动过 project.cfg"
        raise CalibrationError(
            "阈值标定未能完成，tools/project.cfg **逐字节未改动**。\n"
            "  原因：\n%s\n"
            "  两次构建的实测值：\n%s"
            % ("\n".join("    · " + x for x in reasons),
               "\n".join("    · 第 %s 次：%s 条，退出码 %s，日志 %s"
                         % (r["round"], r["data"].get("actions"), r["exit_code"],
                            r["data"].get("log")) for r in runs)))

    n = n1
    thr = threshold_for(n)
    if not threshold_is_sane(n, thr):
        assert cfg_path.read_bytes() == original, "标定失败路径竟改动过 project.cfg"
        raise CalibrationError(
            "按 floor(2/3 × %d) = %d 算出的阈值不合理，**不写入**。\n"
            "  阈值必须满足 1 <= thr < 动作数（等于动作数就没有余量）。\n"
            "  这一般说明这个工程的动作数太少（< 3 条），阈值起不到作用 ——"
            " 请人工确认构建是否真的在编东西。" % (n, thr))

    generated_at = datetime.datetime.now().replace(microsecond=0).isoformat()
    summaries = [_run_summary(r) for r in runs]
    new_text = pre + render_threshold_block(thr, n, summaries, target, generated_at) + suf

    # ---- 校验：改写只发生在标记块内，其余逐字符不变 ----
    npre, nblock, nsuf = split_threshold_block(new_text)
    if (npre, nsuf) != (pre, suf):
        raise CalibrationError("内部错误：改写跑到了标记块之外，**未写入**。")
    if not re.search(r"(?m)^min_compile_lines\s*=\s*%d\s*$" % thr, nblock):
        raise CalibrationError("内部错误：新标记块里读不回阈值 %d，**未写入**。" % thr)

    cfg_path.write_bytes(new_text.encode("utf-8"))
    if cfg_path.read_bytes() != new_text.encode("utf-8"):
        raise CalibrationError("写入后回读不一致：%s —— 请人工核对。" % cfg_path)

    return {
        "status": "PASS",
        "rule": "floor(2/3 × 稳定动作数)",
        "actions": [n1, n2],
        "actions_stable": True,
        "threshold": thr,
        "written": True,
        "cfg": "tools/project.cfg",
        "generated_at": generated_at,
        "runs": summaries,
    }


def newest_build_log(target: Path) -> str:
    """tools/build_logs/ 里最近一次的构建日志（相对工程根）；没有则空串。"""
    target = Path(target)
    d = target / "tools" / "build_logs"
    if not d.is_dir():
        return ""
    logs = [p for p in d.glob("build_*.log") if p.is_file()]
    if not logs:
        return ""
    newest = max(logs, key=lambda p: p.stat().st_mtime)
    try:
        return newest.relative_to(target).as_posix()
    except ValueError:
        return str(newest)


# ============================================================================
# 报告
# ============================================================================
def build_report(plan: Plan, collisions, written, skipped, checks, opts) -> dict:
    created = [c.dst_rel for c in collisions if c.kind == "create"]
    same = [c.dst_rel for c in collisions if c.kind == "same"]
    different = [c.dst_rel for c in collisions if c.kind == "different"]
    return {
        "tool": "autolab init",
        # v1 -> v2：checks 里的 L1/L2 由字符串改成结构化 dict，并新增 calibration 一节
        "report_version": 2,
        "generated_at": opts.get("generated_at", ""),
        "dry_run": bool(opts.get("dry_run")),
        "forced": bool(opts.get("force")),
        "platform": {
            "root": str(plan.platform_root),
            "head": plan.platform_head,
            "min_required_commit": MIN_PLATFORM_COMMIT,
            "min_required_reason": MIN_PLATFORM_WHY,
        },
        "family": plan.family.key,
        "project_name": plan.project_name,
        "target": str(plan.target),
        "cube_project": {
            "ioc": plan.cube["ioc_rel"],
            "uvprojx": plan.cube["uvprojx_rel"],
            "axf": plan.cube["axf_rel"],
            "target_name": plan.cube["target_name"],
            "output_directory": plan.cube["output_directory"],
            "output_name": plan.cube["output_name"],
            "device": plan.cube["device"],
            "cpu": plan.cube["cpu"],
            "flash_size": plan.cube["flash_size"],
            "ram_size": plan.cube["ram_size"],
        },
        "variables": plan.variables,
        "copied": [
            {
                "src": str(plan.platform_root / f.src_rel),
                "src_rel": f.src_rel,
                "dst": f.dst_rel,
                "sha256": f.sha256,
            }
            for f in plan.files if f.kind == "copy"
        ],
        "generated_from_templates": [
            {"src_rel": f.src_rel, "dst": f.dst_rel, "sha256": f.sha256}
            for f in plan.files if f.kind == "template"
        ],
        "files": {
            "created": created,
            "unchanged": same,
            "would_overwrite": different,
            "written": written,
            "skipped_identical": skipped,
        },
        "warnings": list(plan.warnings),
        "checks": checks,
    }


def report_markdown(rep: dict, plan: Plan, backup_note: str = "") -> str:
    L = []
    A = L.append
    A("# autolab init 报告 —— %s" % rep["project_name"])
    A("")
    A("> 本文件由 `autolab init` 自动生成，记录**这一次**初始化的来龙去脉。")
    A("> ⚠️ `NOT RUN` / `SKIPPED` **都不等于** `PASS` —— 它们各自说明了为什么没跑。")
    A("> ⚠️ **\"初始化成功\"**（文件写对了）与**\"完整软件验证成功\"**（L0–L2 全绿）"
      "是两件事，本报告分列，不合并表述。")
    A("")
    A("- 生成时间：`%s`" % rep["generated_at"])
    A("- 目标工程：`%s`" % rep["target"])
    A("- 芯片家族：`%s`（%s）" % (rep["family"], plan.family.title))
    A("- 平台仓库：`%s`" % rep["platform"]["root"])
    A("- 平台 HEAD：`%s`" % rep["platform"]["head"])
    A("- 版本下限：`%s`（%s）—— 经 **git 祖先关系**校验通过"
      % (rep["platform"]["min_required_commit"], rep["platform"]["min_required_reason"]))
    A("- 模式：`%s`" % ("dry-run（未落盘）" if rep["dry_run"] else "实际写入"))
    A("")
    A("---")
    A("")
    A("## 一、探测到的工程事实（不是猜的）")
    A("")
    cp = rep["cube_project"]
    A("| 项 | 值 | 来源 |")
    A("|---|---|---|")
    A("| CubeMX 配置源 | `%s` | 目标根目录实际发现 |" % cp["ioc"])
    A("| Keil 工程 | `%s` | MDK-ARM/ 下实际发现 |" % cp["uvprojx"])
    A("| 产物 .axf | `%s` | `<OutputDirectory>` + `<OutputName>` |" % cp["axf"])
    A("| Keil TargetName | `%s` | `<TargetName>` |" % cp["target_name"])
    A("| 器件 | `%s` | `<Device>` |" % cp["device"])
    A("| Flash 预算 | %s | `<Cpu>` 的 IROM 区间 |" % human_bytes(cp["flash_size"]))
    A("| RAM 预算 | %s | `<Cpu>` 的 IRAM/IRAM2 区间之和 |" % human_bytes(cp["ram_size"]))
    A("")
    A("## 二、写入的文件")
    A("")
    A("- 新建：**%d**" % len(rep["files"]["created"]))
    A("- 内容已相同、未改动：**%d**" % len(rep["files"]["unchanged"]))
    if rep["dry_run"]:
        A("- 会被覆盖（dry-run 未执行）：**%d**" % len(rep["files"]["would_overwrite"]))
        A("- 构成为 **%d** 直接拷贝 + **%d** 模板实例化 = **%d** 个 Autolab 集成文件；"
          "dry-run 不落盘，也不写报告"
          % (len(rep["copied"]), len(rep["generated_from_templates"]),
             len(rep["copied"]) + len(rep["generated_from_templates"])))
    else:
        A("- 实际写入：**%d**" % len(rep["files"]["written"]))
        if rep["files"]["skipped_identical"]:
            A("- 跳过（内容已相同）：**%d**" % len(rep["files"]["skipped_identical"]))
        n_int = len(rep["copied"]) + len(rep["generated_from_templates"])
        A("- 构成：**%d** 直接拷贝 + **%d** 模板实例化 = **%d** 个 Autolab 集成文件"
          % (len(rep["copied"]), len(rep["generated_from_templates"]), n_int))
        A("- 另加本节报告 **2** 份（`docs/AUTOLAB_INIT.md`、`tools/autolab_init.json`）"
          " —— 它们是**产出**，但**不**属于上面那 %d 个集成文件" % n_int)
        A("- 本次共产出 **%d** 个文件" % (n_int + 2))
    A("")
    A("### 逐字节复制的文件（%d 个，源码 SHA-256）" % len(rep["copied"]))
    A("")
    A("| 目标 | 源（平台内相对路径） | SHA-256 |")
    A("|---|---|---|")
    for c in rep["copied"]:
        A("| `%s` | `%s` | `%s` |" % (c["dst"], c["src_rel"], c["sha256"][:16] + "…"))
    A("")
    A("> SHA-256 只列前 16 位；完整值见 `tools/autolab_init.json`。")
    A("")
    A("### 由模板实例化的文件")
    A("")
    A("| 目标 | 模板 | 生成物 SHA-256 |")
    A("|---|---|---|")
    for g in rep["generated_from_templates"]:
        A("| `%s` | `%s` | `%s` |" % (g["dst"], g["src_rel"], g["sha256"][:16] + "…"))
    A("")
    A("## 三、未提供 / 已降级的项（WARN）")
    A("")
    if rep["warnings"]:
        for w in rep["warnings"]:
            A("- ⚠️ %s" % w)
    else:
        A("（无）")
    A("")
    A("## 四、初始化后检查")
    A("")
    checks = rep["checks"]
    L0 = checks.get("L0") or {}
    cal = checks.get("calibration") or {}
    L1 = checks.get("L1") or {}
    L2 = checks.get("L2") or {}
    L3 = checks.get("L3") or {}

    def brief(c, extra=""):
        if c.get("status") in ("PASS", "FAIL") and c.get("exit_code") is not None:
            return "退出码 %s%s" % (c["exit_code"], extra)
        return c.get("reason", "")

    A("| 层 | 状态 | 说明 |")
    A("|---|---|---|")
    A("| L0 环境自检 | **%s** | %s |"
      % (L0.get("status", "?"), brief(L0)))
    if cal.get("status") == "PASS":
        cal_note = ("两次均为 **%s** 条动作，`%s` = **%s**，已写入 `%s`"
                    % (cal["actions"][0], cal["rule"], cal["threshold"], cal["cfg"]))
    else:
        cal_note = cal.get("reason", "")
    A("| 构建阈值标定 | **%s** | %s |" % (cal.get("status", "?"), cal_note.replace("\n", "<br>")))
    A("| L1 构建 | **%s** | %s |"
      % (L1.get("status", "?"), brief(L1, "，日志 `%s`" % L1.get("log", ""))))
    A("| L2 无板测试 | **%s** | %s |" % (L2.get("status", "?"), brief(L2)))
    A("| L3 真机 HIL | **%s** | %s |" % (L3.get("status", "?"), brief(L3)))
    A("")
    A("> `NOT RUN`（没跑）与 `SKIPPED`（本轮不需要跑）**都不是** `PASS`。")
    A("")

    def layer_output(title, c):
        A("### %s —— `%s`" % (title, c.get("status", "?")))
        A("")
        if c.get("status") not in ("PASS", "FAIL"):
            A("未执行：%s" % c.get("reason", "（无说明）"))
            A("")
            return
        A("```")
        A("$ %s" % c.get("cmd", ""))
        A("（cwd = %s）" % c.get("cwd", ""))
        A("退出码 = %s" % c.get("exit_code"))
        A("```")
        A("")
        A("原始输出：")
        A("")
        A("```text")
        A((c.get("stdout") or "").rstrip("\n"))
        if (c.get("stderr") or "").strip():
            A("---- stderr ----")
            A(c["stderr"].rstrip("\n"))
        A("```")
        A("")

    layer_output("L0 环境预检", L0)

    A("### 构建阈值标定 —— `%s`" % cal.get("status", "?"))
    A("")
    if cal.get("status") == "PASS":
        A("| 轮次 | 动作数 | 退出码 | 产物时间戳已更新 | 日志 |")
        A("|---|---|---|---|---|")
        for r in cal["runs"]:
            A("| 第 %s 次 | %s | %s | %s | `%s` |"
              % (r.get("round"), r.get("actions"), r.get("exit_code"),
                 "是" if r.get("axf_updated") else "**否**", r.get("log", "")))
        A("")
        A("- 两次动作数一致：`%s` = `%s`（稳定）" % (cal["actions"][0], cal["actions"][1]))
        A("- 取值规则：`%s`，即 floor(2/3 × %s) = **%s**"
          % (cal["rule"], cal["actions"][0], cal["threshold"]))
        A("- 写入 `%s`：**已写入**（只改阈值标记块，其余逐字节不变）" % cal["cfg"])
        A("- 标定时间：`%s`" % cal.get("generated_at", ""))
        A("")
        A("> ⚠️ 阈值只防\"空转假 0\"，**不代表**\"真实增量构建已验证\"。")
    elif cal.get("status") in ("NOT RUN", "SKIPPED", None, "?"):
        A("未执行：%s" % cal.get("reason", "（无说明）"))
    else:
        A("失败原因（`tools/project.cfg` **逐字节未改动**）：")
        A("")
        A("```text")
        A(cal.get("reason", ""))
        A("```")
    A("")

    layer_output("L1 构建", L1)
    layer_output("L2 无板测试（不含硬件用例）", L2)
    layer_output("L3 真机 HIL", L3)

    if backup_note:
        A("## 五、覆盖前备份")
        A("")
        A(backup_note)
        A("")
    A("---")
    A("")
    A("## 下一步（按顺序）")
    A("")
    A("1. 打开 `CLAUDE.md`，填第三节「本工程的坑」。")
    A("2. 核对 `tools/project.cfg` 里 `[image] required_symbols`（现只有 `main`）。")
    if cal.get("status") == "PASS":
        A("3. 阈值已标定（`min_compile_lines = %s`）；要重标就跑 "
          "`python tools/autolab.py calibrate --target <本工程根>`。"
          % cal["threshold"])
    else:
        A("3. 标定阈值：`python tools/autolab.py calibrate --target <本工程根>`"
          "（或 `autolab init --run-checks` 自动完成）。")
    A("4. 完整软件验证：`autolab init --run-checks`，"
      "或分开跑 `python tools/build.py`（L1）与 "
      "`pytest -m \"not hardware\"`（L2）。")
    A("5. L3 需要先实现固件的启动标记与软复位，并确认串口硬件 —— 且需人工授权。")
    A("")
    return "\n".join(L)


def write_reports(target: Path, rep: dict, plan: Plan, backup_note: str = "") -> list:
    target = Path(target)
    md = report_markdown(rep, plan, backup_note)
    js = json.dumps(rep, ensure_ascii=False, indent=2) + "\n"
    out = []
    for rel, data in (("docs/AUTOLAB_INIT.md", md.encode("utf-8")),
                      ("tools/autolab_init.json", js.encode("utf-8"))):
        p = target / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        out.append(rel)
    return out


# ============================================================================
# 小工具
# ============================================================================
def make_temp_staging():
    """staging 放在**系统临时目录**，不放目标目录。

    这样两个性质天然成立：
      · `--dry-run` 对目标目录零变化（连一个隐藏目录都不建）；
      · staging 阶段失败时目标目录一个字节都没动过。
    """
    return Path(tempfile.mkdtemp(prefix="autolab-init-staging-"))
