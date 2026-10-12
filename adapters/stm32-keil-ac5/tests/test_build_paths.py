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
{min_lines_block}"""

# min_lines=None -> **整行省略**（模拟"新工程阈值尚未标定"的真实状态：
#   生成出来的 tools/project.cfg 里这一行就是被注释掉的，即字段根本不存在）。
CFG_NO_THRESHOLD = "# min_compile_lines = <待标定>\n"


def make_tree(tmp_path, name, min_lines=20):
    """造一棵"看起来像工程"的树，返回 (工程根, axf 路径)。"""
    root = tmp_path / name
    (root / "tools").mkdir(parents=True)
    (root / "MDK-ARM" / name).mkdir(parents=True)
    (root / "MDK-ARM" / f"{name}.uvprojx").write_text("<Project/>", encoding="utf-8")
    axf = root / "MDK-ARM" / name / f"{name}.axf"
    axf.write_bytes(b"fake-axf")
    if min_lines is None:
        block = CFG_NO_THRESHOLD
    else:
        block = "min_compile_lines = %s\n" % (min_lines,)
    (root / "tools" / "project.cfg").write_text(
        CFG_TMPL.format(name=name, min_lines_block=block), encoding="utf-8")
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


# ------------------------------------------------------------ 测量模式（--measure）
# 用途：新工程的 min_compile_lines 还没标定时，也要能跑**一次真实全量构建**把动作数
#      测出来 —— 否则永远标不出来。
# ⚠️ 这些用例专门盯住"测量模式**只**绕过阈值"这条边界：任何"顺手把整个判据放宽"的
#    改法都会让下面几条红（这就是它们的反向标定价值）。

def test_threshold_missing_is_a_hard_error_on_the_normal_path(tmp_path):
    """没有阈值时普通路径必须报配置错 —— 不许静默兜底、不许当成"没检查"。"""
    root, _ = make_tree(tmp_path, "Uncal", min_lines=None)
    with pytest.raises(project_config.ConfigKeyMissing):
        build.resolve_paths(project_config.load(root / "tools"))


def test_measure_mode_resolves_paths_without_a_threshold(tmp_path):
    root, axf = make_tree(tmp_path, "Uncal2", min_lines=None)
    p = build.resolve_paths(project_config.load(root / "tools"),
                            require_threshold=False)
    assert p.min_compile_lines is None
    # 只有阈值被放宽；路径项一个都不许放宽
    assert p.axf == axf.resolve()
    assert p.uvprojx_name == "Uncal2.uvprojx"


def test_measure_mode_does_not_judge_the_threshold(tmp_path):
    root, axf = make_tree(tmp_path, "Meas", min_lines=None)
    p = build.resolve_paths(project_config.load(root / "tools"),
                            require_threshold=False)
    passed, checks = build.evaluate(0, make_log(n_actions=3),
                                    axf.stat().st_mtime - 1, p, measure=True)

    assert passed is True, "阈值不判，但其余三条都过 -> 测量成功"
    assert len(checks) == 4
    name, ok, detail = checks[2]
    assert name == build.MEASURE_CHECK_NAME
    assert "实际 3 条" in detail, "测量模式必须**报出**实际条数，不能什么都不说"
    assert ok is True


def test_measure_row_is_not_named_like_a_threshold_pass(tmp_path):
    """测量模式那一行必须一眼可辨 —— 免得被当成"判据 3 通过了"。"""
    root, axf = make_tree(tmp_path, "Named", min_lines=None)
    p = build.resolve_paths(project_config.load(root / "tools"),
                            require_threshold=False)
    _, checks = build.evaluate(0, make_log(), axf.stat().st_mtime - 1, p, measure=True)
    assert ">=" not in checks[2][0], checks[2][0]
    assert "测量" in checks[2][0], checks[2][0]


def test_missing_threshold_on_a_non_measure_call_is_a_failure_not_a_pass(tmp_path):
    """测量模式是**显式**的。没给 measure=True 却拿着 None 阈值 -> 必须判 FAIL。"""
    root, axf = make_tree(tmp_path, "Sneaky", min_lines=None)
    p = build.resolve_paths(project_config.load(root / "tools"),
                            require_threshold=False)
    passed, checks = build.evaluate(0, make_log(), axf.stat().st_mtime - 1, p)

    assert passed is False
    assert checks[2][1] is False


@pytest.mark.parametrize("exit_code, errors, warnings, n_actions, stats", [
    (2, 1, 0, 30, True),      # 编译错误
    (1, 0, 3, 30, True),      # 有警告也算不通过
    (0, 0, 0, 30, False),     # 日志里没有统计行 -> 日志无效
])
def test_measure_mode_still_fails_on_bad_builds(tmp_path, exit_code, errors,
                                                warnings, n_actions, stats):
    """⚠️ 核心边界：测量模式**不得**把坏构建判成成功。"""
    root, axf = make_tree(tmp_path, "Bad%d" % exit_code, min_lines=None)
    p = build.resolve_paths(project_config.load(root / "tools"),
                            require_threshold=False)
    log = make_log(n_actions=n_actions, errors=errors, warnings=warnings, stats=stats)
    passed, _ = build.evaluate(exit_code, log, axf.stat().st_mtime - 1, p, measure=True)
    assert passed is False


@pytest.mark.parametrize("t0_delta, axf_gone", [
    (+100, False),            # 产物没更新
    (0, True),                # 产物根本不存在
])
def test_measure_mode_still_fails_on_unupdated_artifact(tmp_path, t0_delta, axf_gone):
    root, axf = make_tree(tmp_path, "Axf", min_lines=None)
    p = build.resolve_paths(project_config.load(root / "tools"),
                            require_threshold=False)
    if axf_gone:
        axf.unlink()
        t0 = 0.0
    else:
        t0 = axf.stat().st_mtime + t0_delta
    passed, _ = build.evaluate(0, make_log(), t0, p, measure=True)
    assert passed is False


def test_measure_payload_carries_only_measured_facts(tmp_path):
    """结果行是给 `autolab calibrate` 读的 —— 里面不许有推断值。"""
    root, axf = make_tree(tmp_path, "Payload", min_lines=None)
    p = build.resolve_paths(project_config.load(root / "tools"),
                            require_threshold=False)
    log = make_log(n_actions=24)
    t0 = axf.stat().st_mtime - 1
    payload = build.measure_payload(0, log, t0, p, True, Path("tools/build_logs/x.log"))

    assert payload["actions"] == 24
    assert payload["threshold_judged"] is False
    assert payload["errors"] == 0 and payload["warnings"] == 0
    assert payload["uvision_exit_code"] == 0
    assert payload["axf_updated"] is True
    assert payload["passed"] is True

    # 构建失败时 passed 必须是 False —— calibrate 只认这一个字段
    bad = build.measure_payload(2, make_log(errors=1), t0, p, False, Path("x.log"))
    assert bad["passed"] is False


def test_measure_prefix_is_ascii_and_unambiguous():
    """结果行前缀走的是 stdout，Windows 重定向到管道时是 GBK —— 必须纯 ASCII。"""
    assert build.MEASURE_PREFIX.isascii()
    assert build.MEASURE_PREFIX.endswith("=")
