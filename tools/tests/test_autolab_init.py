# -*- coding: utf-8 -*-
"""`tools/autolab.py init` 的隔离测试。

运行方式（**显式路径**，避免误收集 skeleton/tests 与 adapters/*/tests）：

    python -m pytest tools/tests/test_autolab_init.py -q

设计原则：
  · 所有会落盘的用例都在 `tmp_path` 里造工程，**不碰**任何真实工程。
  · 需要"平台仓库"的用例一律加 `--platform <平台根>`，平台**只读**。
  · 需要"平台源被改过"的负向用例，在 tmp_path 里造一个**伪造平台仓库**
    （git init + commit），这样不必也不许去改真平台。
  · 每个守卫都配一条**反向验证**（把守卫拿掉之后错误实现会露出来）——
    只会变绿的判据没有价值。
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
PLATFORM = HERE.parents[2]          # tools/tests/x.py -> tools -> 平台根
TOOLS = PLATFORM / "tools"
sys.path.insert(0, str(TOOLS))

import autolab_steps as S           # noqa: E402
import autolab                      # noqa: E402

FAMILY = "stm32-keil-ac5"

CPU_LINE = ('IRAM(0x20000000-0x2001BFFF) IRAM2(0x2001C000-0x2001FFFF) '
            'IROM(0x8000000-0x807FFFF) CLOCK(25000000) FPU2 CPUTYPE("Cortex-M4")')


# ============================================================================
# 助手
# ============================================================================
def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@example.invalid",
         "-c", "user.name=t"] + list(args),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def make_cube(root: Path, name="proj", ioc_name=None, out_dir=None,
              out_name=None, device="STM32F407VETx", cpu=CPU_LINE,
              ioc_project_name=None, with_dirs=("Core", "Drivers", "MDK-ARM"),
              extra_ioc=""):
    """造一个**最小但结构真实**的 CubeMX 工程。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    ioc_name = ioc_name or (name + ".ioc")
    out_dir = out_dir if out_dir is not None else name
    out_name = out_name or name

    (root / ioc_name).write_text(
        "#MicroXplorer Configuration settings - do not modify\n"
        "Mcu.CPN=STM32F407VET6\n"
        "Mcu.Family=STM32F4\n"
        "Mcu.IP0=NVIC\nMcu.IP1=RCC\nMcu.IP2=SYS\nMcu.IP3=USART1\n"
        "Mcu.IPNb=4\n"
        "Mcu.Package=LQFP100\n"
        "Mcu.UserName=STM32F407VETx\n"
        "MxCube.Version=6.11.0\n"
        "ProjectManager.ProjectFileName=%s\n" % ioc_name +
        "ProjectManager.ProjectName=%s\n" % (ioc_project_name or name) +
        "ProjectManager.TargetToolchain=MDK-ARM V5.32\n"
        "RCC.HSE_VALUE=8000000\n"
        "USART1.BaudRate=115200\n" + extra_ioc,
        encoding="utf-8")

    for d in with_dirs:
        (root / d).mkdir(parents=True, exist_ok=True)
    if "Core" in with_dirs:
        (root / "Core" / "Src").mkdir(parents=True, exist_ok=True)
        (root / "Core" / "Src" / "main.c").write_text(
            "int main(void){return 0;}\n", encoding="utf-8")

    (root / "MDK-ARM" / (name + ".uvprojx")).write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'
        '<Project xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        "  <Targets><Target>\n"
        "    <TargetName>%s</TargetName>\n"
        "    <TargetOption><TargetCommonOption>\n"
        "      <Device>%s</Device>\n"
        "      <Vendor>STMicroelectronics</Vendor>\n"
        "      <Cpu>%s</Cpu>\n"
        "      <OutputDirectory>%s\\</OutputDirectory>\n"
        "      <OutputName>%s</OutputName>\n"
        "    </TargetCommonOption></TargetOption>\n"
        "  </Target></Targets>\n"
        "</Project>\n" % (name, device, cpu, out_dir, out_name),
        encoding="utf-8")
    return root


def clone_platform(dest_parent):
    """在临时目录里造一个"干净"的平台副本：**字节等同当前工作树**，但已提交。

    **为什么需要这个副本**：§3 的源完整性守卫**正确地**拒绝"从源被本地改过的
    平台初始化"，而平台在本项目的开发流程里长期处于未提交状态（每个阶段收尾都
    明确不提交）。测试因此需要一个内容等同工作树、但处于"已提交"状态的副本。

    ⚠️ **不能用 `git clone`**：本机 `core.autocrlf=true`，clone 时会把纯 LF 的工作树
    文件转成 CRLF，副本的字节就与真平台**不一致**了 —— 而本工具是逐字节复制的，
    那样的副本会导出错误的结论（实测：`skeleton/tools/serial_tool.py` 真平台 248 个
    纯 LF、clone 出来是 CRLF）。所以这里整目录拷贝（含 `.git`，保住最低版本提交的
    祖先关系），再在副本里把工作树内容提交成新的 HEAD。

    ⚠️ 这个提交只发生在 `tmp_path` 里的一次性副本上，**不碰任何真实仓库**。
    """
    fam = S.get_family(FAMILY)
    dst = Path(dest_parent) / "platform"
    shutil.copytree(PLATFORM, dst,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    assert (dst / ".git").is_dir(), "副本必须带上 .git —— 最低版本校验要用祖先关系"

    git(dst, "add", "-A")
    r = git(dst, "commit", "--no-verify", "-qm", "sync working tree")
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        raise AssertionError("副本提交失败：%s%s" % (r.stdout, r.stderr))

    # 副本必须真的"干净"，否则用例会在错误的理由上失败（假绿）
    assert S.check_sources_unmodified(dst, fam) == [], "副本不干净"
    # 且每个拷贝源都要与真平台**逐字节相同** —— 否则测的是另一个平台
    for rel in S.source_list(fam):
        assert (dst / rel).read_bytes() == (PLATFORM / rel).read_bytes(), \
            "副本与真平台字节不一致：%s" % rel
    return dst


PLAT = {"dir": PLATFORM}


@pytest.fixture(scope="session", autouse=True)
def _clean_platform(tmp_path_factory):
    """整个测试会话共用一个干净的平台副本（克隆一次，不逐例重复）。"""
    PLAT["dir"] = clone_platform(tmp_path_factory.mktemp("platform"))
    return PLAT["dir"]


def run_init(target, extra=(), platform=None, expect_exit=None):
    platform = PLAT["dir"] if platform is None else platform
    argv = ["init", "--family", FAMILY, "--target", str(target),
            "--platform", str(platform), "--no-run-checks"] + list(extra)
    code = autolab.main(argv)
    if expect_exit is not None:
        assert code == expect_exit, "期望退出码 %s，实际 %s" % (expect_exit, code)
    return code


def snapshot(root: Path):
    """目录树的 (相对路径 -> sha256)。用于证明"磁盘零变化"。"""
    root = Path(root)
    return {
        p.relative_to(root).as_posix(): sha(p)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# ============================================================================
# 1. family
# ============================================================================
def test_v1_supports_only_the_verified_family():
    assert set(S.FAMILIES) == {FAMILY}
    assert S.get_family(FAMILY).toolchain_label == "keil"
    with pytest.raises(S.InitError):
        S.get_family("avr-gcc")


def test_unsupported_family_returns_2_and_touches_nothing(tmp_path):
    proj = make_cube(tmp_path / "proj")
    before = snapshot(proj)
    code = autolab.main(["init", "--family", "esp32-idf", "--target", str(proj),
                         "--platform", str(PLAT["dir"])])
    assert code == 2
    assert snapshot(proj) == before


def test_missing_required_argument_returns_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        autolab.main(["init", "--target", str(tmp_path)])
    assert e.value.code == 2


def test_no_subcommand_returns_2(capsys):
    assert autolab.main([]) == 2


# ============================================================================
# 2. 文件清单本身
# ============================================================================
def test_copy_manifest_is_exactly_28_and_all_sources_exist():
    fam = S.get_family(FAMILY)
    assert len(fam.copies) == 28 == S.COPY_COUNT
    for src, dst in fam.copies:
        assert (PLATFORM / src).is_file(), src
        assert not dst.startswith("/") and ".." not in dst
    dests = [d for _, d in fam.copies]
    assert len(set(dests)) == 28
    # 骨架里的 hooks/ 必须归到 tools/hooks/ 下，而不是工程根
    assert "tools/hooks/pre-commit" in dests
    assert "hooks/pre-commit" not in dests
    # 刻意不拷的两份
    assert "PITFALLS.md" not in " ".join(dests)
    assert "tests/conftest.py" in dests


def test_template_manifest_is_six():
    fam = S.get_family(FAMILY)
    assert len(fam.templates) == 6
    kinds = {d: k for _, d, k in fam.templates}
    assert kinds["CLAUDE.md"] == S.TPL_CLAUDE
    assert kinds[".gitignore"] == S.TPL_GITIGNORE
    assert kinds["tools/project.cfg"] == S.TPL_PROJECT_CFG
    assert kinds["README.md"] == S.TPL_DOLLAR
    assert kinds["docs/HARDENING.md"] == S.TPL_DOLLAR
    assert kinds[".github/workflows/ci.yml"] == S.TPL_DOLLAR


# ============================================================================
# 3. 平台源完整性
# ============================================================================
def test_platform_head_is_an_ancestor_of_the_minimum_commit():
    """用 git 祖先关系判定，**不是**比哈希字符串/日期。"""
    S.require_min_platform(PLATFORM)          # 不抛 = 通过
    with pytest.raises(S.InitError):
        S.require_min_platform(PLATFORM, "0000000000000000000000000000000000000000")


def test_non_git_platform_is_rejected(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(S.InitError):
        S.platform_head(plain)


def test_missing_source_is_rejected(tmp_path):
    fam = S.get_family(FAMILY)
    empty = tmp_path / "empty-platform"
    empty.mkdir()
    with pytest.raises(S.InitError) as e:
        S.check_sources_exist(empty, fam)
    assert "缺失" in str(e.value)


def test_locally_modified_source_is_rejected(tmp_path):
    plat = clone_platform(tmp_path)
    fam = S.get_family(FAMILY)

    # 干净基线：一个都没有（否则下面的"命中"证明不了任何事）
    assert S.check_sources_unmodified(plat, fam) == []

    victim = fam.copies[0][0]
    p = plat / victim
    p.write_bytes(p.read_bytes() + b"\n# local edit\n")
    assert S.check_sources_unmodified(plat, fam) == [victim]


def test_init_refuses_when_source_modified(tmp_path):
    """源被本地改过 → 拒绝初始化，**理由是"源被改"而不是别的**。

    这条用例以前建在一个不含最低版本提交的伪造仓库上，结果是在"版本太旧"上失败 ——
    那样即使源完整性守卫**完全不存在**，用例照样绿。现在改用带真实历史的克隆，
    并把拒绝理由也钉住。
    """
    plat = clone_platform(tmp_path)
    victim = S.get_family(FAMILY).copies[0][0]
    (plat / victim).write_bytes((plat / victim).read_bytes() + b"\n# local edit\n")

    proj = make_cube(tmp_path / "proj")
    before = snapshot(proj)

    with pytest.raises(S.InitError) as e:
        S.build_plan(plat, FAMILY, proj, "proj", {}, {})
    assert victim in str(e.value) and "本地修改" in str(e.value)

    code = autolab.main(["init", "--family", FAMILY, "--target", str(proj),
                         "--project-name", "proj", "--platform", str(plat),
                         "--no-run-checks"])
    assert code == 2
    assert snapshot(proj) == before


# ============================================================================
# 4. 目标工程探测
# ============================================================================
@pytest.mark.parametrize("missing", ["Core", "Drivers", "MDK-ARM"])
def test_target_missing_cubemx_pieces_returns_2(tmp_path, missing):
    proj = make_cube(tmp_path / "proj")
    shutil.rmtree(proj / missing)
    before = snapshot(proj)
    assert run_init(proj, ["--project-name", "proj"]) == 2
    assert snapshot(proj) == before


def test_empty_target_returns_2(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert run_init(empty, ["--project-name", "x"]) == 2
    assert list(empty.iterdir()) == []


def test_nonexistent_target_returns_2(tmp_path):
    assert run_init(tmp_path / "nope", ["--project-name", "x"]) == 2


def test_project_name_must_match_the_ioc(tmp_path):
    """这正是"目录名 ≠ 工程名"的场景（如 autolab_init_probe 里的老工程名）。"""
    proj = make_cube(tmp_path / "some-dir", name="realname")
    assert run_init(proj, ["--project-name", "some-dir"]) == 2
    assert run_init(proj, ["--project-name", "realname"]) == 0


def test_multiple_ioc_files_need_explicit_project_name(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    (proj / "other.ioc").write_text("ProjectManager.ProjectName=other\n",
                                    encoding="utf-8")
    # 指定了工程名 → 能挑出来
    assert run_init(proj, ["--project-name", "proj"]) == 0


def test_uvprojx_output_dir_does_not_follow_project_name(tmp_path):
    """Keil 的 <OutputDirectory> 与 <OutputName> 是两个独立设置。"""
    proj = make_cube(tmp_path / "proj", name="proj",
                     out_dir="build/out", out_name="firmware")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    cfg = (proj / "tools" / "project.cfg").read_text(encoding="utf-8")
    assert re.search(r"(?m)^axf\s*=\s*MDK-ARM/build/out/firmware\.axf\s*$", cfg)
    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    assert rep["cube_project"]["axf"] == "MDK-ARM/build/out/firmware.axf"
    assert rep["cube_project"]["output_directory"] == "build/out"
    assert rep["cube_project"]["output_name"] == "firmware"


def test_image_budget_comes_from_the_keil_cpu_string(tmp_path):
    assert S.parse_cpu_memories(CPU_LINE) == (0x80000, 0x20000)
    assert S.parse_cpu_memories("") == (None, None)

    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    cfg = (proj / "tools" / "project.cfg").read_text(encoding="utf-8")
    assert re.search(r"(?m)^flash_size\s*=\s*524288\s*$", cfg)
    assert re.search(r"(?m)^ram_size\s*=\s*131072\s*$", cfg)


def test_degenerate_memory_range_is_not_accepted_as_a_real_budget():
    """`IRAM(0x0-0x0)` 会算出 "1 字节" —— 那是个假事实，必须拒绝而不是写进配置。

    这一条是**双向**的：恰好 1024 字节接受，少一个字节就拒绝 ——
    只有上界没有下界的话，"拒绝"可能只是碰巧。
    """
    assert S.parse_cpu_memories("IRAM(0x0-0x0)") == (None, None)
    assert S.parse_cpu_memories("IROM(0x8000000-0x8000000)")[0] is None
    assert S.parse_cpu_memories("IROM(0x8000000-0x80003FE)")[0] is None      # 1023
    assert S.parse_cpu_memories("IROM(0x8000000-0x80003FF)")[0] == 1024      # 1024
    # 一段可疑不影响另一段可信的
    mixed = "IRAM(0x0-0x0) IROM(0x8000000-0x807FFFF)"
    assert S.parse_cpu_memories(mixed) == (0x80000, None)


def test_image_budget_omitted_when_cpu_string_unreadable(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj", cpu="CPUTYPE(\"Cortex-M4\")")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    cfg = (proj / "tools" / "project.cfg").read_text(encoding="utf-8")
    assert not re.search(r"(?m)^flash_size\s*=", cfg)
    assert not re.search(r"(?m)^ram_size\s*=", cfg)

    # 省略必须**出声** —— 静默少一项会让人以为契约测试还在保护他
    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    assert rep["cube_project"]["flash_size"] is None
    assert any("内存预算" in w for w in rep["warnings"]), rep["warnings"]
    md = (proj / "docs" / "AUTOLAB_INIT.md").read_text(encoding="utf-8")
    assert "未知" in md


# ============================================================================
# 5. 正常初始化
# ============================================================================
def test_happy_path_writes_28_verbatim_copies_byte_identical(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    core = sha(proj / "Core" / "Src" / "main.c")
    assert run_init(proj, ["--project-name", "proj"]) == 0

    fam = S.get_family(FAMILY)
    for src, dst in fam.copies:
        assert (proj / dst).is_file(), dst
        assert sha(PLATFORM / src) == sha(proj / dst), "%s 与平台源不一致" % dst

    # 6 个模板产物 + 2 个报告
    for _, dst, _ in fam.templates:
        assert (proj / dst).is_file(), dst
    assert (proj / "docs" / "AUTOLAB_INIT.md").is_file()
    assert (proj / "tools" / "autolab_init.json").is_file()

    # 原有工程文件未被碰
    assert sha(proj / "Core" / "Src" / "main.c") == core
    assert (proj / "MDK-ARM" / "proj.uvprojx").is_file()
    assert (proj / "proj.ioc").is_file()


def test_output_file_accounting_is_28_plus_6_plus_2(tmp_path):
    """§C 的产出账要对得上：**28** 直接拷贝 + **6** 模板实例化 = **34** 集成文件；
    再加 **2** 份报告 = **36** 个产出文件。

    这几个数必须**从报告与磁盘实测**，不能只在文档里喊口号；而且要证明
    两份清单**没有重叠**（否则 28+6=34 是虚的），也没有为了凑数多写文件。
    """
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    fam = S.get_family(FAMILY)

    assert len(fam.copies) == 28 == len(rep["copied"])
    assert len(fam.templates) == 6 == len(rep["generated_from_templates"])
    assert len(rep["files"]["written"]) == 34 == len(rep["files"]["created"])
    assert rep["files"]["unchanged"] == [] and rep["files"]["skipped_identical"] == []

    listed = ([e["dst"] for e in rep["copied"]]
              + [e["dst"] for e in rep["generated_from_templates"]])
    assert len(set(listed)) == 34, "两份清单有重叠 —— 那 28+6=34 就不成立了"
    assert sorted(listed) == sorted(rep["files"]["written"]), \
        "报告里两份清单与 files.written 对不上"

    # 报告是被**产出**的，但**不**属于那 34 个 Autolab 集成文件
    assert S.REPORT_FILES == ("docs/AUTOLAB_INIT.md", "tools/autolab_init.json")
    for rel in S.REPORT_FILES:
        assert (proj / rel).is_file(), rel
        assert rel not in listed, "报告不该混进 34 个集成文件里"

    # 磁盘实测：CubeMX 原有 3 个输入 + 34 + 2 = 39，一个不多一个不少
    cube_inputs = {"proj.ioc", "Core/Src/main.c", "MDK-ARM/proj.uvprojx"}
    on_disk = {p.relative_to(proj).as_posix() for p in proj.rglob("*") if p.is_file()}
    assert on_disk == cube_inputs | set(listed) | set(S.REPORT_FILES), \
        "磁盘上出现了清单之外的文件：%s" % sorted(on_disk - cube_inputs - set(listed)
                                                   - set(S.REPORT_FILES))
    assert len(on_disk) == 3 + 34 + 2 == 39

    # 账也要写进**生成的报告**（§C：文档与报告里都要能直接读到这三个数）
    md = (proj / "docs" / "AUTOLAB_INIT.md").read_text(encoding="utf-8")
    assert "**28** 直接拷贝 + **6** 模板实例化 = **34** 个 Autolab 集成文件" in md
    assert "另加本节报告 **2** 份" in md
    assert "本次共产出 **36** 个文件" in md


def test_rerun_is_idempotent(tmp_path):
    """第二次 init 是空转：34 个文件全部"已存在且相同"，一个都不重写。

    唯一会变的是**报告本身**（里面记了本轮的时间戳与 L0 结果），
    所以它被备份后重写 —— 备份目录里**只应有这两个报告文件**，
    不许夹带任何用户文件。这正是"报告总是重写、其它文件绝不擅动"的落地。
    """
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    after_first = snapshot(proj)
    assert run_init(proj, ["--project-name", "proj"]) == 0
    second = snapshot(proj)

    assert set(after_first) == set(second) - {
        r for r in second if r.startswith(".autolab-init/")}, \
        "第二轮多出了非备份文件"
    changed = {rel for rel in after_first if after_first[rel] != second[rel]}
    assert changed == set(S.REPORT_FILES), "只有报告允许变，实际变了：%s" % (changed,)

    backups = sorted((proj / ".autolab-init").glob("backup-*"))
    assert len(backups) == 1
    got = sorted(p.relative_to(backups[0]).as_posix()
                 for p in backups[0].rglob("*") if p.is_file())
    assert got == sorted(S.REPORT_FILES), "备份里只该有报告文件，实际：%s" % (got,)

    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    assert len(rep["files"]["unchanged"]) == 34
    assert rep["files"]["written"] == []
    assert rep["files"]["created"] == []


# ============================================================================
# 6. 模板占位符 & YAML
# ============================================================================
def test_no_residual_placeholders_in_generated_templates(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0

    for _, dst, kind in S.get_family(FAMILY).templates:
        text = (proj / dst).read_text(encoding="utf-8")
        assert not S.VAR_RE.findall(text), "%s 残留变量" % dst
        if dst == "CLAUDE.md":
            assert not S.ANGLE_RE.findall(text), "%s 残留 <...>" % dst
        if dst == ".gitignore":
            assert not S.find_hits(text, S.GITIGNORE_PLACEHOLDERS), dst
            assert "/.autolab-init/" in text
            assert "MDK-ARM/proj/" in text


def test_generated_ci_yml_is_valid_and_gated(tmp_path):
    yaml = pytest.importorskip("yaml")
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    text = (proj / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert text.count("${{") == 2          # GitHub 双层表达式原样保留
    assert not S.VAR_RE.findall(text)      # 模板变量已全部替换
    assert not S.ANGLE_RE.findall(text)

    doc = yaml.safe_load(text)
    jobs = doc.get("jobs", {})
    assert set(jobs) == {"no-hardware", "hil-preflight", "hardware"}
    assert jobs["no-hardware"]["runs-on"] == ["self-hosted", "windows", "keil"]
    assert jobs["hardware"]["runs-on"] == ["self-hosted", "windows", "keil", "board"]
    assert isinstance(jobs["no-hardware"]["runs-on"], list)
    assert isinstance(jobs["hardware"]["runs-on"], list)
    assert len(jobs["hardware"]["runs-on"]) == len(jobs["no-hardware"]["runs-on"]) + 1
    assert jobs["hardware"]["needs"] == "hil-preflight"
    assert "ready" in str(jobs["hardware"].get("if", ""))
    assert "workflow_dispatch" in str(jobs["hil-preflight"].get("if", ""))


def test_runner_labels_come_from_env_when_given(tmp_path, monkeypatch):
    proj = make_cube(tmp_path / "proj", name="proj")
    monkeypatch.setenv("AUTOLAB_RUNNER_LABELS_SOFTWARE", "self-hosted,linux,mykit")
    monkeypatch.setenv("AUTOLAB_RUNNER_LABELS_HARDWARE",
                       "self-hosted,linux,mykit,myboard")
    assert autolab.main(["init", "--family", FAMILY, "--target", str(proj),
                         "--project-name", "proj", "--platform", str(PLAT["dir"]),
                         "--no-run-checks"]) == 0
    text = (proj / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "[self-hosted, linux, mykit, myboard]" in text
    assert "[self-hosted, linux, mykit]" in text

    import yaml
    jobs = yaml.safe_load(text)["jobs"]
    assert jobs["no-hardware"]["runs-on"] == ["self-hosted", "linux", "mykit"]
    assert jobs["hardware"]["runs-on"] == ["self-hosted", "linux", "mykit", "myboard"]


@pytest.mark.parametrize("bad", ["has space", "semi;colon", "", "a]b"])
def test_bad_runner_label_is_rejected(bad):
    with pytest.raises(S.InitError):
        S.normalise_labels(bad, "TEST")


def test_default_runner_labels_are_always_instantiable(tmp_path):
    """没有环境变量时也必须给出**可实例化**的标签，绝不留下未替换变量。"""
    fam = S.get_family(FAMILY)
    soft, hard, warn = S.resolve_runner_labels({}, fam)
    assert soft.startswith("[") and soft.endswith("]")
    assert hard.startswith("[") and hard.endswith("]")
    assert len(warn) == 2
    assert "${" not in soft + hard


# ============================================================================
# 7. 参考工程事实不得泄漏
# ============================================================================
def test_reference_facts_do_not_leak_into_generated_project_cfg(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    cfg = (proj / "tools" / "project.cfg").read_text(encoding="utf-8")

    assert S.find_hits(cfg, S.FORBIDDEN_IN_PROJECT_CFG) == []
    # 这三项在未确认前必须保持"省略"状态
    assert not re.search(r"(?m)^\s*port\s*=", cfg)
    assert not re.search(r"(?m)^\s*boot_marker\s*=", cfg)
    assert not re.search(r"(?m)^\s*reset_cmd_hex\s*=", cfg)
    # 阈值本阶段不标定 —— 必须保持注释状态
    assert not re.search(r"(?m)^\s*min_compile_lines\s*=", cfg)
    # 也不许把模板里的原始数字带过来
    assert "SDIO_test" not in cfg and "COM9" not in cfg and "USh" not in cfg


# ---- 平台仓库路径：模板不许写死任何一台机器的路径 ----------------------------

AUTHOR_PATHS = ("C:\\Users\\HP", "C:/Users/HP", "~/embedded-ai-journey",
                "\\Users\\HP\\", "/Users/HP/", "F:\\keil")

# 任何盘符绝对路径 / UNC / home 相对路径，模板里都不该出现。
# group(1) 是**整条路径**（前缀 + 路径体），以便断言它落在哪个根下。
ABS_PATH_RE = re.compile(
    r"(?m)(?:^|[\s`(])((?:[A-Za-z]:[\\/]|\\\\[^\s]|~/[\w.-]+/)[^\s`)\]|,]*)")


def test_platform_templates_contain_no_machine_specific_path():
    """**平台模板源码**不得写死任何一台机器的路径（含作者的家目录）。

    这是本缺陷的正面回归：模板必须与"谁的电脑"无关。
    A.3 要求不许把作者路径换成另一条固定绝对路径 —— 所以这里连
    `X:\\...` 这种泛化的绝对路径也一并禁掉，而不只是拉黑两个具体字符串。
    """
    fam = S.get_family(FAMILY)
    tpls = [s for s, _, _ in fam.templates]
    assert tpls, "模板清单不该为空（否则本用例是空转）"
    for rel in tpls:
        text = (PLATFORM / rel).read_text(encoding="utf-8")
        assert not S.find_hits(text, AUTHOR_PATHS), \
            "%s 里仍写死了作者机器路径：%s" % (rel, S.find_hits(text, AUTHOR_PATHS))
        hits = sorted({m.group(1) for m in ABS_PATH_RE.finditer(text)})
        assert hits == [], "%s 里出现了机器相关路径：%s" % (rel, hits)


def test_rendered_claude_md_uses_the_runtime_platform_root_not_the_author_path():
    """生成后的 `CLAUDE.md` 必须带**运行时确定的**平台路径，而不是作者电脑的路径。

    用一个人为的平台根渲染：输出里必须出现这个根，
    且**一个作者路径特征都不许有**（哪怕本机平台恰好就在作者的家目录下）。
    """
    raw = (PLATFORM / "skeleton" / "CLAUDE.md.template").read_text(encoding="utf-8")
    fake_root = "D:/ci-runner/autolab-platform"
    v = {"PROJECT_NAME": "proj", "PROJECT_PATH": "D:/work/proj",
         "PLATFORM_ROOT": fake_root, "FAMILY": FAMILY, "MCU_BOARD": "STM32F407VETx / B",
         "TOOLCHAIN": "MDK-ARM", "PERIPHERALS": "USART1"}
    out = S.render_claude(raw, v)

    assert not S.find_hits(out, AUTHOR_PATHS), S.find_hits(out, AUTHOR_PATHS)
    # 生成物里**允许**有绝对路径 —— 但只应是运行时给的这两个，没有第三个
    for m in ABS_PATH_RE.finditer(out):
        tok = m.group(1)
        assert tok.startswith(("D:/ci-runner", "D:/work")), \
            "生成物里出现了第三个机器路径：%r" % tok
    # 路径确实写进去了（否则上面的"没命中"只说明渲染器把整段丢了）
    assert out.count(fake_root) >= 6, "规范表里的每一行都该指向运行时平台根"
    assert "%s/adapters/%s/PITFALLS.md" % (fake_root, FAMILY) in out
    assert "ANGLE" not in out and S.ANGLE_RE.findall(out) == []


def test_claude_template_leaves_platform_root_to_runtime(tmp_path):
    """整条 init 链路上，`CLAUDE.md` 里的平台路径来自运行参数。"""
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    out = (proj / "CLAUDE.md").read_text(encoding="utf-8")
    # 本机平台根就是 PLATFORM —— 生成物应指向它
    assert PLAT["dir"].as_posix() in out
    # 而作者路径的**字面量**写法（反斜杠形式）不该出现
    assert "C:\\Users\\HP\\embedded-ai-journey" not in out
    assert S.ANGLE_RE.findall(out) == []


def test_project_named_like_the_reference_project_is_not_a_leak(tmp_path):
    """工程**真的**叫 autolab_stm32_demo（或 SDIO_test）时，不许判成泄漏。

    这是 `autolab_init_probe` 的实测场景：抽出来的 CubeMX 工程仍然带着原工程名。
    生成物里出现用户自己给的名字是**正确输出**，不是参考事实泄漏 ——
    扫描器豁免"用户显式给出的值"，但**只豁免这些**，其余一条不放过。
    """
    for name in ("autolab_stm32_demo", "SDIO_test"):
        proj = make_cube(tmp_path / name, name=name)
        assert run_init(proj, ["--project-name", name]) == 0
        readme = (proj / "README.md").read_text(encoding="utf-8")
        assert name in readme                       # 名字确实写进去了
        cfg = (proj / "tools" / "project.cfg").read_text(encoding="utf-8")
        assert re.search(r"(?m)^name\s*=\s*%s\s*$" % re.escape(name), cfg)

    # 反向：豁免只认"用户给的值" —— 我们**编不出来**的参考事实照样必须被拦
    assert S.scan_output("板子 COM9 上的 USh", S.FORBIDDEN_IN_DOCS,
                         ["autolab_stm32_demo"]) == ["COM9", "USh"]
    assert S.scan_output("工程名 autolab_stm32_demo", S.FORBIDDEN_IN_DOCS,
                         ["autolab_stm32_demo"]) == []
    # 而模板**源码**的扫描不享受任何豁免 —— 模板必须与工程无关
    assert S.find_hits("工程名 autolab_stm32_demo", S.FORBIDDEN_IN_TEMPLATES) == \
        ["autolab_stm32_demo"]


def test_leak_scanner_is_bidirectionally_calibrated():
    """扫描器**确实能看见**参考事实 —— 否则"没命中"只说明扫描器瞎了。"""
    example = (PLATFORM / "adapters" / FAMILY / "project.cfg.example").read_text(
        encoding="utf-8")
    hits = S.find_hits(example, S.FORBIDDEN_IN_PROJECT_CFG)
    assert "SDIO_test" in hits and "COM9" in hits and "MX_SDIO_SD_Init" in hits

    for _, dst, kind in S.get_family(FAMILY).templates:
        if kind != S.TPL_DOLLAR:
            continue
        raw = (PLATFORM / dict((d, s) for s, d, _ in
                              S.get_family(FAMILY).templates)[dst]).read_text(
            encoding="utf-8")
        assert S.find_hits(raw, S.FORBIDDEN_IN_TEMPLATES) == [], dst

    assert S.find_hits("x SDIO_test y", S.FORBIDDEN_IN_DOCS) == ["SDIO_test"]


def test_unresolved_placeholder_is_caught_by_the_scanner(tmp_path, monkeypatch):
    """负向探针：模板里多出一个未声明变量时，必须**拒绝生成**而不是照常写盘。"""
    def fake(path):
        return ("hello ${NOT_DECLARED} world", "\n")
    monkeypatch.setattr(S, "read_text_preserving_eol", fake)

    proj = make_cube(tmp_path / "proj", name="proj")
    before = snapshot(proj)
    with pytest.raises(S.InitError):
        S.build_plan(PLAT["dir"], FAMILY, proj, "proj", {}, {})
    assert snapshot(proj) == before


def test_template_change_makes_the_mapper_refuse():
    """映射表里的原文找不到时，**拒绝生成**，绝不"尽力而为"地猜。"""
    with pytest.raises(S.InitError) as e:
        S.render_mapping("完全不相干的文本", [("预期原文", "新文")], "t")
    assert "找不到预期文本" in str(e.value)


# ============================================================================
# 8. 冲突检测
# ============================================================================
def test_conflict_without_force_returns_3_and_target_is_frozen(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0

    target = proj / "README.md"
    target.write_text("用户手改过的内容\n", encoding="utf-8")
    tampered = sha(target)
    before = snapshot(proj)

    assert run_init(proj, ["--project-name", "proj"]) == 3
    assert sha(proj / "README.md") == tampered      # 内容前后不变
    assert snapshot(proj) == before                  # 整个目录零变化
    assert not (proj / ".autolab-init").exists()


def test_identical_existing_file_is_not_a_conflict(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    assert run_init(proj, ["--project-name", "proj"]) == 0


def test_collision_guard_is_load_bearing(tmp_path, monkeypatch):
    """**反向验证**：把冲突检测拿掉，错误实现就会把用户文件覆盖掉。

    这条用例的价值在于证明上一条不是"因为别的原因才没写"。
    """
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0

    keep = proj / "CLAUDE.md"
    keep.write_text("用户手改过的内容\n", encoding="utf-8")

    # 取消保护 —— 只这一处，别的什么都不改
    monkeypatch.setattr(S, "detect_collisions",
                        lambda target, plan: [S.Collision(f.dst_rel, "create")
                                              for f in plan.files])
    assert run_init(proj, ["--project-name", "proj"]) == 0
    assert keep.read_text(encoding="utf-8") != "用户手改过的内容\n", \
        "取消保护后仍然没被覆盖 —— 说明这条用例没有真的标定到守卫"


# ============================================================================
# 9. --force 与备份
# ============================================================================
def test_force_backs_up_byte_identical_before_overwriting(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0

    tampered = b"\xe7\x94\xa8\xe6\x88\xb7\xe6\x89\x8b\xe6\x94\xb9\xe8\xbf\x87\n"
    (proj / "README.md").write_bytes(tampered)

    assert run_init(proj, ["--project-name", "proj", "--force"]) == 0

    backups = sorted((proj / ".autolab-init").glob("backup-*/README.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == tampered, "备份必须与原件逐字节一致"

    # 已被正确的生成物覆盖
    assert (proj / "README.md").read_bytes() != tampered
    assert S.VAR_RE.findall((proj / "README.md").read_text(encoding="utf-8")) == []


def test_force_aborts_when_backup_verification_fails(tmp_path, monkeypatch):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0

    (proj / "README.md").write_bytes(b"ORIGINAL\n")
    real_copy = shutil.copyfile

    def broken(src, dst, *a, **k):
        real_copy(src, dst, *a, **k)
        Path(dst).write_bytes(b"CORRUPTED BACKUP\n")   # 备份写坏

    monkeypatch.setattr(S.shutil, "copyfile", broken)
    code = run_init(proj, ["--project-name", "proj", "--force"])
    assert code == 2, "备份校验失败必须整轮失败"
    assert (proj / "README.md").read_bytes() == b"ORIGINAL\n", \
        "备份没验证通过就覆盖了原件"


# ============================================================================
# 10. staging 失败不留残渣
# ============================================================================
def test_staging_failure_leaves_no_partial_output(tmp_path, monkeypatch):
    proj = make_cube(tmp_path / "proj", name="proj")
    before = snapshot(proj)

    real_stage = S.stage_files

    def boom(files, staging):
        real_stage(files[:5], staging)        # 真的先写了 5 个文件到 staging
        raise S.InitError("synthetic staging failure")

    monkeypatch.setattr(S, "stage_files", boom)
    assert run_init(proj, ["--project-name", "proj"]) == 2
    assert snapshot(proj) == before, "staging 阶段失败，目标目录必须是零变化"
    assert not (proj / "tools").exists()


# ============================================================================
# 11. --dry-run
# ============================================================================
def test_dry_run_changes_nothing_on_disk(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    before = snapshot(proj)
    assert run_init(proj, ["--project-name", "proj", "--dry-run"]) == 0
    assert snapshot(proj) == before
    # 连暂存/缓存目录都不许留下 —— 上面的 snapshot 只看文件，看不见空目录，
    # 所以这里必须把目录也逐一列出来断言。
    assert sorted(p.name for p in proj.iterdir()) == ["Core", "Drivers", "MDK-ARM",
                                                      "proj.ioc"]
    dirs = sorted(d.relative_to(proj).as_posix()
                  for d in proj.rglob("*") if d.is_dir())
    assert dirs == ["Core", "Core/Src", "Drivers", "MDK-ARM"]
    assert not (proj / ".autolab-init").exists()
    assert not (proj / "tools").exists()


def test_dry_run_on_conflicting_target_still_reports_conflict(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    (proj / "README.md").write_text("手改\n", encoding="utf-8")
    before = snapshot(proj)
    assert run_init(proj, ["--project-name", "proj", "--dry-run"]) == 3
    assert snapshot(proj) == before


# ============================================================================
# 12. 不碰 Git
# ============================================================================
def test_init_does_not_touch_git_state(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    git(proj, "init", "-q")
    git(proj, "add", "-A")
    r = git(proj, "commit", "-qm", "cube baseline")
    if r.returncode != 0:                     # 无身份配置等环境问题 → 跳过该用例
        pytest.skip("无法在临时目录建 Git 基线：%s" % r.stderr.strip())

    head_before = git(proj, "rev-parse", "HEAD").stdout.strip()
    branch_before = git(proj, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    config_before = (proj / ".git" / "config").read_bytes()

    assert run_init(proj, ["--project-name", "proj"]) == 0

    assert git(proj, "rev-parse", "HEAD").stdout.strip() == head_before
    assert git(proj, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == branch_before
    assert git(proj, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert git(proj, "stash", "list").stdout.strip() == ""
    assert (proj / ".git" / "config").read_bytes() == config_before
    # 新增的文件是未跟踪的，**没有**被顺手提交
    assert "A\t" not in git(proj, "status", "--porcelain").stdout


# ============================================================================
# 13. 报告与 L0
# ============================================================================
def _fake_preflight(exit_code, out):
    def f(target, python_exe=None, timeout=300):
        return {"cmd": "python tools/preflight.py", "cwd": str(target),
                "exit_code": exit_code, "stdout": out, "stderr": ""}
    return f


def test_report_records_source_hashes_and_l0_result(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "run_preflight", _fake_preflight(0, "[ OK ] all good\n"))
    proj = make_cube(tmp_path / "proj", name="proj")
    assert autolab.main(["init", "--family", FAMILY, "--target", str(proj),
                         "--project-name", "proj", "--platform", str(PLAT["dir"])]) == 0

    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    assert rep["family"] == FAMILY
    assert rep["project_name"] == "proj"
    assert rep["platform"]["head"] == S.platform_head(PLAT["dir"])
    assert rep["platform"]["min_required_commit"] == S.MIN_PLATFORM_COMMIT
    assert len(rep["copied"]) == 28
    for c in rep["copied"]:
        assert c["sha256"] == sha(Path(c["src"]))
    assert len(rep["generated_from_templates"]) == 6
    assert rep["checks"]["L0"]["status"] == "PASS"
    assert rep["checks"]["L0"]["exit_code"] == 0
    # v2：L1/L2 由裸字符串改成结构化 dict（要记退出码、日志、cmd）
    assert rep["checks"]["L1"]["status"] == "NOT RUN"
    assert rep["checks"]["L2"]["status"] == "NOT RUN"
    assert rep["checks"]["calibration"]["status"] == "NOT RUN"
    assert rep["checks"]["L3"]["status"] == "NOT RUN"
    assert rep["report_version"] == 2
    assert rep["warnings"]

    md = (proj / "docs" / "AUTOLAB_INIT.md").read_text(encoding="utf-8")
    assert "NOT RUN" in md and "L0" in md


def test_exit_4_when_l0_fails_but_files_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "run_preflight",
                        _fake_preflight(1, "[FAIL] keil not found\n"))
    proj = make_cube(tmp_path / "proj", name="proj")
    code = autolab.main(["init", "--family", FAMILY, "--target", str(proj),
                         "--project-name", "proj", "--platform", str(PLAT["dir"])])
    assert code == 4
    assert (proj / "tools" / "project.cfg").is_file()      # 文件保留
    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    assert rep["checks"]["L0"]["status"] == "FAIL"
    assert rep["checks"]["L0"]["exit_code"] == 1


def test_no_run_checks_skips_preflight_entirely(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("--no-run-checks 时不该调用 preflight")
    monkeypatch.setattr(S, "run_preflight", forbidden)
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    # 跳过必须记成 `NOT RUN`（**不是** PASS，也不再是更含糊的 SKIPPED）——
    # 用户选择不跑，不等于那些检查通过了。
    assert rep["checks"]["L0"]["status"] == "NOT RUN"
    for k in ("L1", "L2", "calibration"):
        assert rep["checks"][k]["status"] == "NOT RUN"
        assert "--no-run-checks" in rep["checks"][k]["reason"]
    md = (proj / "docs" / "AUTOLAB_INIT.md").read_text(encoding="utf-8")
    assert "**NOT RUN**" in md


# ============================================================================
# 14. 未提供的可选参数不得伪装成事实
# ============================================================================
def test_unspecified_board_is_marked_unconfigured_and_warned(tmp_path):
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    assert "未配置" in rep["variables"]["BOARD"]
    assert "未配置" in rep["variables"]["GOAL"]
    assert any("--board" in w for w in rep["warnings"])
    assert rep["variables"]["MCU"] == "STM32F407VETx"   # 从工程实测，不是编的
    readme = (proj / "README.md").read_text(encoding="utf-8")
    assert "未配置" in readme


def test_explicit_options_win(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_RUNNER_LABELS_SOFTWARE", "self-hosted,linux,k")
    monkeypatch.setenv("AUTOLAB_RUNNER_LABELS_HARDWARE", "self-hosted,linux,k,b")
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj", "--mcu", "STM32F405XX",
                           "--board", "MyBoard", "--toolchain", "MyTool 1.0"]) == 0
    rep = json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))
    assert rep["variables"]["MCU"] == "STM32F405XX"
    assert rep["variables"]["BOARD"] == "MyBoard"
    assert rep["variables"]["TOOLCHAIN"] == "MyTool 1.0"


# ============================================================================
# 15. 环境相关的卫生检查
# ============================================================================
def test_tools_are_pure_stdlib():
    """初始化器不许引入新的运行时第三方依赖。"""
    for f in ("autolab.py", "autolab_steps.py"):
        src = (TOOLS / f).read_text(encoding="utf-8")
        for mod in re.findall(r"(?m)^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", src):
            top = mod.split(".")[0]
            assert top in {
                "argparse", "datetime", "hashlib", "json", "os", "pathlib", "re",
                "shutil", "subprocess", "sys", "tempfile", "dataclasses",
                "autolab_steps", "__future__",
            }, "%s 引入了非标准库依赖：%s" % (f, mod)


def platform_fingerprint():
    """平台工作树的指纹：Git 状态 + 全部非 .git 文件的内容哈希。

    用"前后对比"而不是"源码里有没有某个字符串"来证明平台只读 ——
    字符串扫描既漏（换个写法就看不见）又误（读平台也长着 `platform_root / `）。
    指纹法对上任何写路径都成立。
    """
    items = {}
    for p in sorted(PLATFORM.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(PLATFORM).as_posix()
        if rel.startswith(".git/") or "__pycache__" in rel:
            continue
        items[rel] = sha(p)
    return {
        "status": git(PLATFORM, "status", "--porcelain").stdout,
        "head": git(PLATFORM, "rev-parse", "HEAD").stdout.strip(),
        "files": items,
    }


def test_platform_repo_is_never_written_to(tmp_path):
    before = platform_fingerprint()
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    # 连"冲突 + --force"这条最可能误伤平台的路径也走一遍
    (proj / "README.md").write_text("手改\n", encoding="utf-8")
    assert run_init(proj, ["--project-name", "proj", "--force"]) == 0
    after = platform_fingerprint()

    assert after["head"] == before["head"], "init 动了平台的提交"
    assert after["status"] == before["status"], "init 改了平台的工作树状态"
    assert set(after["files"]) == set(before["files"]), "平台文件清单变了"
    changed = {k for k in before["files"] if after["files"][k] != before["files"][k]}
    assert changed == set(), "init 改写了平台文件：%s" % (changed,)


# ============================================================================
# 15. 阶段 2：构建阈值标定（`autolab calibrate`）
#
# ⚠️ 这里**不起 Keil、不碰硬件**：`run_build_measure` 被换成受控假实现。
#    真实工具链的证据只能由端到端那一轮给。本节的职责是钉住**判定逻辑**：
#    什么情况该写阈值、什么情况**必须不写**。
# ============================================================================
def fake_measure(counts, exit_code=0, errors=0, warnings=0, axf_updated=True,
                 log="tools/build_logs/cal_fake.log"):
    """伪造 `S.run_build_measure`：按 counts 依次给出每次的动作条数。

    `passed` 的算法**照抄真实 build.py 的语义**（判据 1/2/4 的合取；判据 3 在
    测量模式下不判）—— 否则"假实现比真实现宽松"会让这些用例失去意义。
    """
    seq = list(counts)
    state = {"i": 0}

    def f(target, python_exe=None, timeout=1800):
        n = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return {
            "cmd": "python tools/build.py --measure", "cwd": str(target),
            "exit_code": exit_code, "stdout": "=== Build（测量模式）===\n",
            "stderr": "",
            "data": {
                "actions": n, "threshold_judged": False,
                "errors": errors, "warnings": warnings,
                "uvision_exit_code": exit_code, "axf": "fake.axf",
                "axf_updated": axf_updated, "log": log,
                "passed": bool(exit_code == 0 and errors == 0 and warnings == 0
                               and axf_updated),
            },
        }
    return f


def a_project(tmp_path):
    """造一个已接入 Autolab 的工程（含 tools/project.cfg 与标记块）。"""
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0
    return proj


def cfg_bytes(proj):
    return (proj / "tools" / "project.cfg").read_bytes()


def live_threshold(proj):
    """读出生效的阈值；没有则 None。"""
    m = re.search(r"(?m)^min_compile_lines\s*=\s*(\d+)\s*$",
                  (proj / "tools" / "project.cfg").read_text(encoding="utf-8"))
    return int(m.group(1)) if m else None


def run_calibrate(proj, expect_exit=None):
    code = autolab.main(["calibrate", "--target", str(proj)])
    if expect_exit is not None:
        assert code == expect_exit, "期望退出码 %s，实际 %s" % (expect_exit, code)
    return code


def test_init_leaves_the_threshold_uncalibrated_but_marked(tmp_path):
    """init 生成的文件里：阈值仍是注释，且**标记块恰好一个**（标定靠它定位）。"""
    proj = a_project(tmp_path)
    assert live_threshold(proj) is None
    text = (proj / "tools" / "project.cfg").read_text(encoding="utf-8")
    _, block, _ = S.split_threshold_block(text)
    assert "# min_compile_lines = <待标定>" in block
    assert block.startswith(S.CFG_BLOCK_BEGIN)
    assert S.CFG_BLOCK_END in block
    assert "calibrate" in block and "退出码 5" in block


def test_threshold_rule_is_floor_two_thirds():
    """授权书定死的规则：稳定动作数的 2/3 向下取整。24 -> 16。"""
    assert S.threshold_for(24) == 16
    assert S.threshold_for(56) == 37          # SDIO_test 参考工程的 56 条
    assert S.threshold_for(3) == 2
    assert S.threshold_for(2) == 1
    # n=1 -> 0：阈值 0 等于没有检查 -> 必须判"不合理"
    assert S.threshold_for(1) == 0
    assert not S.threshold_is_sane(1, 0)
    assert S.threshold_is_sane(2, 1)
    assert S.threshold_is_sane(24, 16)
    assert not S.threshold_is_sane(24, 24)    # 等于真实条数：没有余量
    assert not S.threshold_is_sane(0, 0)      # 没有动作可言
    assert not S.threshold_is_sane(-3, 0)


def test_calibrate_writes_the_threshold_when_both_counts_agree(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "run_build_measure", fake_measure([24, 24]))
    proj = a_project(tmp_path)
    before = cfg_bytes(proj)

    assert run_calibrate(proj, expect_exit=0) == 0

    assert live_threshold(proj) == 16, "两次均为 24 -> floor(2/3 × 24) = 16"
    after = cfg_bytes(proj)
    assert after != before
    # 改写**只**发生在标记块内：块外必须逐字节不变
    pre_b, blk_b, suf_b = S.split_threshold_block(before.decode("utf-8"))
    pre_a, blk_a, suf_a = S.split_threshold_block(after.decode("utf-8"))
    assert (pre_b, suf_b) == (pre_a, suf_a), "标定改动了标记块之外的内容"
    assert blk_b != blk_a
    # 块里留下了可追溯的实测依据，而不是一个孤零零的数字
    assert "第 1 次" in blk_a and "第 2 次" in blk_a
    assert "floor(2/3 × 24) = **16**" in blk_a
    assert "不代表" in blk_a and "增量" in blk_a      # 不许声称增量已验证
    assert "尚未标定" not in blk_a                    # 旧说明被整块换掉，不留自相矛盾


def test_calibrate_writes_a_report_of_the_measured_values(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "run_build_measure", fake_measure([30, 30]))
    proj = a_project(tmp_path)
    res = S.calibrate_threshold(proj)
    assert res["status"] == "PASS"
    assert res["actions"] == [30, 30]
    assert res["actions_stable"] is True
    assert res["threshold"] == 20
    assert res["written"] is True
    assert [r["actions"] for r in res["runs"]] == [30, 30]
    assert [r["round"] for r in res["runs"]] == [1, 2]
    assert all(r["exit_code"] == 0 for r in res["runs"])
    assert all(r["log"] for r in res["runs"])


@pytest.mark.parametrize("label, kwargs", [
    ("两次计数不同", dict(counts=[24, 23])),
    ("构建失败（退出码 2）", dict(counts=[24, 24], exit_code=2)),
    ("有编译错误（1 Error）", dict(counts=[24, 24], errors=1)),
    ("有警告（1 Warning）", dict(counts=[24, 24], warnings=1)),
    ("日志里没有统计行（无效日志）", dict(counts=[24, 24], warnings=None)),
    ("产物 .axf 时间戳未更新", dict(counts=[24, 24], axf_updated=False)),
    ("动作数为 0（其实什么都没编）", dict(counts=[0, 0])),
    ("动作数为负（解析异常）", dict(counts=[-1, -1])),
    ("动作数只有 1 条 -> floor(2/3) = 0，不合理", dict(counts=[1, 1])),
])
def test_calibrate_never_writes_when_anything_is_off(tmp_path, monkeypatch,
                                                     label, kwargs):
    """⚠️ 核心边界：这些情形**一个都不许**写阈值，必须退出码 5。"""
    counts = kwargs.pop("counts")
    if kwargs.get("warnings") is None:
        # "无效日志"：统计行读不出来 -> warnings/errors 都是 None
        kwargs["warnings"] = None
        kwargs["errors"] = None
    monkeypatch.setattr(S, "run_build_measure", fake_measure(counts, **kwargs))
    proj = a_project(tmp_path)
    before = cfg_bytes(proj)

    assert run_calibrate(proj, expect_exit=S.EXIT_CALIBRATION) == 5
    assert cfg_bytes(proj) == before, "%s：project.cfg 被改动了" % label
    assert live_threshold(proj) is None


def test_calibrate_exit_5_is_distinct_from_exit_4(tmp_path, monkeypatch):
    """标定失败是 5（配置一字未动），不是 4（文件已写但检查没过）—— 不混为一谈。"""
    monkeypatch.setattr(S, "run_build_measure", fake_measure([24, 25]))
    proj = a_project(tmp_path)
    assert run_calibrate(proj) == S.EXIT_CALIBRATION == 5
    assert S.EXIT_CALIBRATION != S.EXIT_CHECKS_FAILED


@pytest.mark.parametrize("setup", ["empty", "missing"])
def test_calibrate_refuses_a_project_that_was_never_initialised(tmp_path, setup):
    """不是 Autolab 工程 -> 退出码 2（参数/配置错），不是 5，也不写任何文件。"""
    target = tmp_path / "empty"
    if setup == "empty":
        target.mkdir()
    code = autolab.main(["calibrate", "--target", str(target)])
    assert code == S.EXIT_INPUT
    assert not (target / "tools" / "project.cfg").exists()


def test_calibrate_fails_loudly_when_build_py_cannot_measure(tmp_path, monkeypatch):
    """目标工程的 tools/build.py 是旧版（不认 --measure）-> 明确失败，不猜、不写。"""
    proj = a_project(tmp_path)
    before = cfg_bytes(proj)

    def old_build(target, python_exe=None, timeout=1800):
        res = {"cmd": "python tools/build.py --measure", "cwd": str(target),
               "exit_code": 0, "stdout": "=== Build ===\n结果: PASS\n", "stderr": ""}
        res["data"] = S.parse_measure_line(res["stdout"])    # 旧版没有测量行 -> 抛
        return res
    monkeypatch.setattr(S, "run_build_measure", old_build)

    with pytest.raises(S.CalibrationError) as ei:
        S.calibrate_threshold(proj)
    assert "--measure" in str(ei.value) or "测量行" in str(ei.value)
    assert cfg_bytes(proj) == before
    assert live_threshold(proj) is None


@pytest.mark.parametrize("stdout", [
    "",                                              # 什么都没有
    "=== Build ===\n结果: PASS\n",                    # 旧版 build.py 的输出
    S.BUILD_MEASURE_PREFIX + "{not json",            # 协议行坏了
    S.BUILD_MEASURE_PREFIX + '{"actions": "24", "passed": true}',   # actions 不是整数
    S.BUILD_MEASURE_PREFIX + '{"actions": 24}',      # 少了 passed
    S.BUILD_MEASURE_PREFIX + '[1, 2]',               # 不是对象
])
def test_measure_line_parser_never_invents_data(stdout):
    """读不到 / 读坏了都必须失败 —— 绝不"没读到就当默认值"。"""
    with pytest.raises(S.CalibrationError):
        S.parse_measure_line(stdout)


def test_measure_line_parser_takes_the_last_one():
    good = S.BUILD_MEASURE_PREFIX + '{"actions": 24, "passed": true}'
    data = S.parse_measure_line("x\n" + good + "\ny\n")
    assert data["actions"] == 24 and data["passed"] is True


def test_calibrate_requires_exactly_one_marker_block(tmp_path):
    proj = a_project(tmp_path)
    p = proj / "tools" / "project.cfg"
    p.write_text(p.read_text(encoding="utf-8").replace(S.CFG_BLOCK_END, "", 1),
                 encoding="utf-8")
    before = cfg_bytes(proj)
    assert autolab.main(["calibrate", "--target", str(proj)]) == S.EXIT_CALIBRATION
    assert cfg_bytes(proj) == before


def test_calibrate_refuses_when_a_live_threshold_exists_outside_the_block(tmp_path):
    """标记块外已经有一处生效的阈值 -> 改下去会变成重复键。必须拒绝，不许硬写。"""
    proj = a_project(tmp_path)
    p = proj / "tools" / "project.cfg"
    p.write_text(p.read_text(encoding="utf-8") + "\nmin_compile_lines = 7\n",
                 encoding="utf-8")
    before = cfg_bytes(proj)
    assert autolab.main(["calibrate", "--target", str(proj)]) == S.EXIT_CALIBRATION
    assert cfg_bytes(proj) == before


def test_calibrate_is_repeatable_and_only_rewrites_its_own_block(tmp_path, monkeypatch):
    proj = a_project(tmp_path)
    monkeypatch.setattr(S, "run_build_measure", fake_measure([24, 24]))
    assert run_calibrate(proj) == 0
    assert live_threshold(proj) == 16
    once = cfg_bytes(proj)
    monkeypatch.setattr(S, "run_build_measure", fake_measure([30, 30]))
    assert run_calibrate(proj) == 0
    assert live_threshold(proj) == 20
    pre_1, _, suf_1 = S.split_threshold_block(once.decode("utf-8"))
    pre_2, _, suf_2 = S.split_threshold_block(cfg_bytes(proj).decode("utf-8"))
    assert (pre_1, suf_1) == (pre_2, suf_2)


def test_threshold_is_missing_reads_the_real_file(tmp_path):
    proj = a_project(tmp_path)
    assert S.threshold_is_missing(proj) is True
    p = proj / "tools" / "project.cfg"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "# min_compile_lines = <待标定>", "min_compile_lines = 16"), encoding="utf-8")
    assert S.threshold_is_missing(proj) is False


# ============================================================================
# 16. 阶段 2：`init --run-checks` 的 L0–L2 闭环
# ============================================================================
def stub_layers(monkeypatch, calls, l0=0, measure=(24, 24), l1=0, l2=0,
                calib_error=None, threshold_present=False):
    """把四个步骤换成记账用的桩（**不起 Keil、不跑 pytest**），按调用顺序记名字。"""
    def preflight(target, python_exe=None, timeout=300):
        calls.append("L0")
        return {"cmd": "python tools/preflight.py", "cwd": str(target),
                "exit_code": l0, "stdout": "[ OK ] stub\n", "stderr": ""}
    monkeypatch.setattr(S, "run_preflight", preflight)

    def calibrate(target, python_exe=None, timeout=1800):
        calls.append("calibrate")
        if calib_error:
            raise S.CalibrationError(calib_error)
        counts = list(measure)
        return {"status": "PASS", "rule": "floor(2/3 × 稳定动作数)",
                "actions": counts, "actions_stable": True,
                "threshold": S.threshold_for(counts[0]), "written": True,
                "cfg": "tools/project.cfg",
                "generated_at": "2026-10-12T00:00:00",
                "runs": [{"round": i + 1, "exit_code": 0, "actions": n,
                          "errors": 0, "warnings": 0, "axf_updated": True,
                          "passed": True,
                          "log": "tools/build_logs/cal_%d.log" % (i + 1)}
                         for i, n in enumerate(counts)]}
    monkeypatch.setattr(S, "calibrate_threshold", calibrate)
    monkeypatch.setattr(S, "threshold_is_missing",
                        lambda target: not threshold_present)

    def build(target, python_exe=None, timeout=1800):
        calls.append("L1")
        return {"cmd": "python tools/build.py", "cwd": str(target),
                "exit_code": l1, "stderr": "",
                "stdout": "=== Build ===\n结果: %s\n"
                          % ("PASS" if l1 == 0 else "FAIL")}
    monkeypatch.setattr(S, "run_build_l1", build)

    def pytest_l2(target, python_exe=None, timeout=1800):
        calls.append("L2")
        return {"cmd": "python -m pytest -m 'not hardware' --deselect ... -q",
                "cwd": str(target), "exit_code": l2,
                "stdout": "fake pytest\n", "stderr": ""}
    monkeypatch.setattr(S, "run_pytest_l2", pytest_l2)
    return calls


def run_checks_init(proj, extra=(), expect_exit=None):
    argv = ["init", "--family", FAMILY, "--target", str(proj),
            "--platform", str(PLAT["dir"]), "--project-name", "proj",
            "--force"] + list(extra)
    code = autolab.main(argv)
    if expect_exit is not None:
        assert code == expect_exit, "期望退出码 %s，实际 %s" % (expect_exit, code)
    return code


def report_of(proj):
    return json.loads((proj / "tools" / "autolab_init.json").read_text(encoding="utf-8"))


def test_run_checks_runs_l0_calibration_l1_l2_in_that_order(tmp_path, monkeypatch):
    """授权书定死的顺序：L0 -> 阈值标定 -> L1 -> L2。"""
    calls = stub_layers(monkeypatch, [])
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks"], expect_exit=0) == 0
    assert calls == ["L0", "calibrate", "L1", "L2"], calls

    rep = report_of(proj)
    assert rep["checks"]["L0"]["status"] == "PASS"
    assert rep["checks"]["calibration"]["status"] == "PASS"
    assert rep["checks"]["calibration"]["threshold"] == 16
    assert rep["checks"]["L1"]["status"] == "PASS"
    assert rep["checks"]["L2"]["status"] == "PASS"
    assert rep["checks"]["L3"]["status"] == "NOT RUN"


def test_calibration_failure_stops_before_l1_and_l2(tmp_path, monkeypatch):
    calls = stub_layers(monkeypatch, [], calib_error="两次动作数不同（24 vs 23）")
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks"],
                           expect_exit=S.EXIT_CALIBRATION) == 5
    assert calls == ["L0", "calibrate"], calls

    rep = report_of(proj)
    assert rep["checks"]["calibration"]["status"] == "FAIL"
    assert "24 vs 23" in rep["checks"]["calibration"]["reason"]
    for k in ("L1", "L2"):
        assert rep["checks"][k]["status"] == "NOT RUN", k
        assert rep["checks"][k]["status"] != "PASS"
    # 接入文件本身仍然落盘了 —— "初始化成功"与"完整软件验证成功"是两件事
    assert (proj / "tools" / "build.py").is_file()


def test_l0_failure_stops_before_calibration(tmp_path, monkeypatch):
    calls = stub_layers(monkeypatch, [], l0=1)
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks"],
                           expect_exit=S.EXIT_CHECKS_FAILED) == 4
    assert calls == ["L0"], calls
    rep = report_of(proj)
    assert rep["checks"]["L0"]["status"] == "FAIL"
    for k in ("calibration", "L1", "L2"):
        assert rep["checks"][k]["status"] == "NOT RUN", k


def test_build_failure_gives_exit_4_and_l2_still_runs(tmp_path, monkeypatch):
    calls = stub_layers(monkeypatch, [], l1=2)
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks"],
                           expect_exit=S.EXIT_CHECKS_FAILED) == 4
    assert calls == ["L0", "calibrate", "L1", "L2"], calls
    rep = report_of(proj)
    assert rep["checks"]["L1"]["status"] == "FAIL"
    assert rep["checks"]["L2"]["status"] == "PASS"      # L1 失败也照样留 L2 证据


def test_l2_failure_gives_exit_4_and_no_faked_build_log(tmp_path, monkeypatch):
    calls = stub_layers(monkeypatch, [], l2=1)
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks"],
                           expect_exit=S.EXIT_CHECKS_FAILED) == 4
    assert calls == ["L0", "calibrate", "L1", "L2"]
    rep = report_of(proj)
    assert rep["checks"]["L2"]["status"] == "FAIL"
    # 初始化器绝不替用户伪造构建日志
    assert not list((proj / "tools").glob("build_logs/*.log"))


def test_existing_threshold_is_not_recalibrated(tmp_path, monkeypatch):
    """已经标定过的工程：`--run-checks` 直接跑 L1/L2，不重复烧两次构建。"""
    calls = stub_layers(monkeypatch, [], threshold_present=True)
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks"]) == 0
    assert calls == ["L0", "L1", "L2"], calls
    rep = report_of(proj)
    assert rep["checks"]["calibration"]["status"] == "SKIPPED"
    assert "无需重新标定" in rep["checks"]["calibration"]["reason"]


def test_skip_build_skips_l1_and_calibration_but_keeps_l0_and_l2(tmp_path, monkeypatch):
    calls = stub_layers(monkeypatch, [])
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks", "--skip-build"]) == 0
    assert calls == ["L0", "L2"], calls
    rep = report_of(proj)
    assert rep["checks"]["L0"]["status"] == "PASS"
    assert rep["checks"]["calibration"]["status"] == "NOT RUN"
    assert rep["checks"]["L1"]["status"] == "NOT RUN"
    assert rep["checks"]["L2"]["status"] == "PASS"
    assert "--skip-build" in rep["checks"]["L1"]["reason"]
    # 没跑过构建 -> 阈值当然没被写进去
    assert live_threshold(proj) is None


def test_skip_build_reports_l2_failure_honestly_when_build_evidence_is_missing(
        tmp_path, monkeypatch):
    """--skip-build 时 L2 若因缺构建证据而失败，**如实**报 4 —— 不补构建日志。"""
    calls = stub_layers(monkeypatch, [], l2=1)
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks", "--skip-build"],
                           expect_exit=S.EXIT_CHECKS_FAILED) == 4
    assert calls == ["L0", "L2"], calls
    assert report_of(proj)["checks"]["L2"]["status"] == "FAIL"
    assert not list((proj / "tools").glob("build_logs/*.log"))


def test_no_run_checks_marks_everything_not_run(tmp_path, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("--no-run-checks 时不该调用任何检查步骤")
    calls = stub_layers(monkeypatch, [])
    monkeypatch.setattr(S, "calibrate_threshold", forbidden)
    monkeypatch.setattr(S, "run_build_l1", forbidden)
    monkeypatch.setattr(S, "run_pytest_l2", forbidden)
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_init(proj, ["--project-name", "proj"]) == 0   # 自带 --no-run-checks
    assert calls == [], calls
    rep = report_of(proj)
    for k in ("L0", "calibration", "L1", "L2", "L3"):
        assert rep["checks"][k]["status"] == "NOT RUN", k
        assert rep["checks"][k]["status"] != "PASS"


def test_default_init_does_not_build_or_calibrate(tmp_path, monkeypatch):
    """默认（既没 --run-checks 也没 --no-run-checks）= 接入 + L0，绝不构建。"""
    calls = stub_layers(monkeypatch, [])
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj) == 0
    assert calls == ["L0"], calls
    rep = report_of(proj)
    assert rep["checks"]["L0"]["status"] == "PASS"
    for k in ("calibration", "L1", "L2"):
        assert rep["checks"][k]["status"] == "NOT RUN", k


@pytest.mark.parametrize("extra", [
    ["--run-checks", "--no-run-checks"],
    ["--skip-build"],
    ["--skip-build", "--no-run-checks"],
])
def test_bad_flag_combinations_return_2_and_touch_nothing(tmp_path, extra):
    proj = make_cube(tmp_path / "proj", name="proj")
    before = snapshot(proj)
    code = autolab.main(["init", "--family", FAMILY, "--target", str(proj),
                         "--platform", str(PLAT["dir"]), "--project-name", "proj"]
                        + extra)
    assert code == S.EXIT_INPUT
    assert snapshot(proj) == before, "参数组合不对时目标目录必须零变化"


def test_run_checks_report_lists_the_measured_evidence(tmp_path, monkeypatch):
    stub_layers(monkeypatch, [], measure=(24, 24))
    proj = make_cube(tmp_path / "proj", name="proj")
    assert run_checks_init(proj, ["--run-checks"]) == 0
    md = (proj / "docs" / "AUTOLAB_INIT.md").read_text(encoding="utf-8")
    assert "构建阈值标定" in md
    assert "两次动作数一致：`24` = `24`" in md
    assert "floor(2/3 × 24) = **16**" in md
    assert "只改阈值标记块" in md
    assert "不代表" in md and "增量" in md          # 不声称增量已验证
    assert "NOT RUN" in md and "PASS" in md


def test_calibrate_subcommand_is_wired_and_unknown_ones_are_refused(capsys):
    """`calibrate` 已接上；`gen-ioc` 仍是明确的"未实现"，不建空壳。"""
    assert autolab.main([]) == S.EXIT_INPUT
    out = capsys.readouterr().out
    assert "gen-ioc" in out and "未实现" in out.lower() or "刻" in out
    with pytest.raises(SystemExit) as ei:      # argparse 拒绝未知子命令 = 退出码 2
        autolab.main(["gen-ioc"])
    assert ei.value.code == 2


# ============================================================================
# 17. 标定不得在标记块**之外**留下自相矛盾的状态断言
#
# 由来（真实端到端跑出来的）：`calibrate` 只改标记块，而 `init` 生成的
# project.cfg **文件头**里写着 "min_compile_lines <- 【尚未标定】有意省略"。
# 标定成功后，文件里就同时存在"已写入 16"和"尚未标定"两种说法 ——
# 谁来读都会以为有一个是错的。修法是把文件头改成**状态中立**、指向标记块，
# 而不是让 calibrate 去改块外的内容（那会破坏"只改一块"这条安全不变量）。
# ============================================================================
def test_no_calibration_status_is_asserted_outside_the_marker_block(tmp_path,
                                                                    monkeypatch):
    """标定状态**只能**由标记块声明。

    块外的文字（文件头）如果也写死"尚未标定"，标定成功后文件里就会同时存在
    "已写入 16"与"尚未标定"两种说法 —— 读的人必然以为有一个是错的。
    修法是让块外保持**状态中立**，而不是让 calibrate 去改块外的内容
    （那会破坏"只改一块"这条安全不变量）。
    """
    monkeypatch.setattr(S, "run_build_measure", fake_measure([24, 24]))
    proj = a_project(tmp_path)

    def split_now():
        text = (proj / "tools" / "project.cfg").read_text(encoding="utf-8")
        pre, blk, suf = S.split_threshold_block(text)
        return pre + suf, blk

    before, blk0 = split_now()
    assert "尚未标定" not in before, "文件头把标定状态写死到了标记块外"
    assert "尚未标定" in blk0, "状态本该由标记块声明（否则这个用例是空的）"

    assert run_calibrate(proj) == 0

    after, blk1 = split_now()
    assert "尚未标定" not in after, "标定后块外仍写着「尚未标定」，与已写入的阈值矛盾"
    assert "尚未标定" not in blk1, "标定后标记块里还留着「尚未标定」"
    assert "min_compile_lines = 16" in blk1
    # 而**确实**尚未确定的东西不许被顺手改掉（串口三项仍是省略状态）
    assert "【尚未确定】" in after
    assert not re.search(r"(?m)^\s*(port|boot_marker|reset_cmd_hex)\s*=", after)
