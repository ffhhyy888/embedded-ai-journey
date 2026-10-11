"""build.py 的路径解析与四判据 —— **不调用 Keil**，毫秒级。

为什么能不起 Keil
    build.py 的路径来自 tools/project.cfg（经 project_config），四判据是纯函数。
    于是可以在临时目录造"合成工程树"来验证：
    · 换个工程名，路径是否跟着变（拆掉 SDIO_test 硬编码的直接证据）
    · 四判据在"假 0"、"编译错误"、"产物没更新"等情形下是否会红

不覆盖（诚实标注）
    真实 uVision 调用、真实日志格式、真实退出码 —— 那些只能由 tests/test_build.py
    在有 Keil 的机器上跑。**本文件的结论不能代替它。**
"""
from pathlib import Path

import pytest

import build            # sys.path 由本目录的 conftest.py 引导
import project_config

pytestmark = pytest.mark.nohardware

CFG_TMPL = """\
[project]
name = {name}

[build]
uvprojx = MDK-ARM/{name}.uvprojx
axf = MDK-ARM/{name}/{name}.axf
min_compile_lines = {min_lines}
"""


def make_tree(tmp_path, name, min_lines=20):
    """造一棵"看起来像工程"的树，返回 (工程根, axf 路径)。"""
    root = tmp_path / name
    (root / "tools").mkdir(parents=True)
    (root / "MDK-ARM" / name).mkdir(parents=True)
    (root / "MDK-ARM" / f"{name}.uvprojx").write_text("<Project/>", encoding="utf-8")
    axf = root / "MDK-ARM" / name / f"{name}.axf"
    axf.write_bytes(b"fake-axf")
    (root / "tools" / "project.cfg").write_text(
        CFG_TMPL.format(name=name, min_lines=min_lines), encoding="utf-8")
    return root, axf


def paths_of(root):
    return build.resolve_paths(project_config.load(root / "tools"))


def make_log(n_actions=30, errors=0, warnings=0, stats=True):
    """合成一份 Keil 风格的构建日志。n_actions = compiling+assembling+linking 条数。"""
    lines = [f"compiling unit{i}.c..." for i in range(max(0, n_actions - 2))]
    lines.append("assembling startup_stm32f407xx.s...")
    lines.append("linking...")
    lines.append("Program Size: Code=10456 RO-data=456 RW-data=36 ZI-data=1772")
    if stats:
        lines.append(f"{errors} Error(s), {warnings} Warning(s)")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ 路径解析

def test_paths_follow_project_name(tmp_path):
    """两个工程各解析各的，路径不串用 —— 这是"去硬编码"的核心证据。"""
    ra, axf_a = make_tree(tmp_path, "ProjA")
    rb, axf_b = make_tree(tmp_path, "ProjB")
    pa, pb = paths_of(ra), paths_of(rb)

    assert pa.axf == axf_a.resolve()
    assert pb.axf == axf_b.resolve()
    assert "ProjB" not in str(pa.axf)
    assert pa.uvprojx_name == "ProjA.uvprojx"
    assert pb.uvprojx_name == "ProjB.uvprojx"
    assert pa.mdk == (ra / "MDK-ARM").resolve()
    assert pa.tmp_log == (ra / "MDK-ARM" / "_last_build.log").resolve()


def test_uvision_stays_module_level():
    """preflight.py 靠 `import build` 读 build.UVISION 做环境自检 —— 契约不能破。"""
    assert hasattr(build, "UVISION"), "build.UVISION 被移走了，preflight 会跟着坏"
    assert isinstance(build.UVISION, Path)


def test_cfg_is_read_lazily_from_tools_dir(tmp_path, monkeypatch):
    """_cfg() 跟着 TOOLS_DIR 走；且不在 import 期读配置（否则 preflight 会崩）。"""
    root, _ = make_tree(tmp_path, "Wired")
    monkeypatch.setattr(build, "TOOLS_DIR", root / "tools")
    assert build._cfg().get("project", "name") == "Wired"


# ------------------------------------------------------------ 四判据

def test_evaluate_all_pass(tmp_path):
    root, axf = make_tree(tmp_path, "Ok")
    passed, checks = build.evaluate(0, make_log(), axf.stat().st_mtime - 1, paths_of(root))

    assert passed is True
    assert len(checks) == 4
    assert all(ok for _, ok, _ in checks)


def test_evaluate_detects_fake_zero(tmp_path):
    """增量构建"什么都没编也返回 0" —— 判据 3 必须抓到。"""
    root, axf = make_tree(tmp_path, "FakeZero")
    passed, checks = build.evaluate(0, make_log(n_actions=3),
                                    axf.stat().st_mtime - 1, paths_of(root))

    assert passed is False
    assert checks[2][1] is False
    assert "实际 3 条" in checks[2][2]


def test_evaluate_detects_compile_error(tmp_path):
    root, axf = make_tree(tmp_path, "BadCode")
    passed, checks = build.evaluate(2, make_log(errors=1),
                                    axf.stat().st_mtime - 1, paths_of(root))

    assert passed is False
    assert checks[0][1] is False                              # 判据 1：退出码
    assert checks[1][1] is False                              # 判据 2：0 Error/0 Warning
    assert checks[1][2] == "1 Error(s), 0 Warning(s)"


def test_evaluate_detects_missing_stats_line(tmp_path):
    root, axf = make_tree(tmp_path, "NoStats")
    passed, checks = build.evaluate(0, make_log(stats=False),
                                    axf.stat().st_mtime - 1, paths_of(root))

    assert passed is False
    assert checks[1][1] is False
    assert checks[1][2] == "未找到统计行"


def test_evaluate_detects_stale_artifact(tmp_path):
    """产物在，但时间戳没更新（构建其实没产出新固件）。"""
    root, axf = make_tree(tmp_path, "Stale")
    passed, checks = build.evaluate(0, make_log(),
                                    axf.stat().st_mtime + 100, paths_of(root))

    assert passed is False
    assert checks[3][1] is False


def test_evaluate_detects_missing_artifact(tmp_path):
    """产物不存在（-r 失败时会先删旧 .axf）。"""
    root, axf = make_tree(tmp_path, "NoAxf")
    axf.unlink()
    passed, checks = build.evaluate(0, make_log(), 0.0, paths_of(root))

    assert passed is False
    assert checks[3][1] is False
    assert "不存在" in checks[3][2]


def test_threshold_comes_from_cfg_not_code(tmp_path):
    """同一份日志：阈值 20 判过、阈值 56 判不过 —— 证明阈值来自配置而非写死。"""
    log = make_log(n_actions=30)
    root_lo, axf_lo = make_tree(tmp_path, "Lo", min_lines=20)
    root_hi, axf_hi = make_tree(tmp_path, "Hi", min_lines=56)
    p_lo, p_hi = paths_of(root_lo), paths_of(root_hi)

    assert p_lo.min_compile_lines == 20
    assert p_hi.min_compile_lines == 56

    _, checks_lo = build.evaluate(0, log, axf_lo.stat().st_mtime - 1, p_lo)
    _, checks_hi = build.evaluate(0, log, axf_hi.stat().st_mtime - 1, p_hi)

    assert checks_lo[2][1] is True      # 30 >= 20
    assert checks_hi[2][1] is False     # 30 < 56
