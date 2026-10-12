"""build.py 的日志来源选择 —— "双重计数"回归（**不调用 Keil**，毫秒级）。

背景（2026-10-12 缺陷，已修）
    uVision 把**同一份**构建日志**同时**写到 stdout 和 `-o` 文件。旧的 run_build()
    把两者**相加**，于是判据 3 拿到 2× 真实动作数（实测 48 = 24×2），
    阈值 min_compile_lines 等于被绕过一半（16 的真实含义退化成 8 条真实动作）。
    修法：pick_log_source() 只取**一份** —— 优先本次构建的 `-o` 日志，
    它缺失 / 为空 / 是上次残留时才回退 stdout，**绝不拼接**。

本文件锁定
    · stdout 与 `-o` 内容相同时只统计一次：24 条 → 24，不是 48；
    · 动作数不足（15 / 8）在阈值 16 下判 FAIL（旧缺陷会把 15→30、8→16 而误 PASS）；
    · `-o` 缺失 / 为空 / 是上次残留 → 回退 stdout 并打 [WARN]，且**仍然只算一份**；
    · 原有四条判据的失败分支（非零退出码、非零错误统计、产物未更新）不受影响。

不覆盖（诚实标注）
    真实 uVision 调用、真实日志格式 —— 那些只能由 tests/test_build.py 在装 Keil
    的机器上跑。**本文件的结论不能代替它。**
"""
import os
import time
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


# ------------------------------------------------------------ 合成工程树 / 日志

def make_tree(tmp_path, name, min_lines=16):
    """造一棵"看起来像工程"的树，返回 (工程根, axf 路径)。

    注意：**不**创建 `_last_build.log` —— 它由伪造的 uVision 按用例需要去写。
    """
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


def make_log(n_actions=24, errors=0, warnings=0):
    """合成一份 Keil 风格日志；恰好含 n_actions 条 compiling/assembling/linking。"""
    n = max(0, n_actions)
    lines = []
    for i in range(n):
        if i == n - 1:
            lines.append("linking...")
        elif i == n - 2:
            lines.append("assembling startup_stm32f407xx.s...")
        else:
            lines.append(f"compiling unit{i}.c...")
    lines.append("Program Size: Code=10456 RO-data=456 RW-data=36 ZI-data=1772")
    lines.append(f"{errors} Error(s), {warnings} Warning(s)")
    return "\n".join(lines) + "\n"


class _FakeProc:
    def __init__(self, returncode, stdout=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = b""


def fake_uvision(monkeypatch, stdout_text, log_text=None, returncode=0):
    """伪造 uVision.com，**忠实复现"同一份日志写两个去处"的行为**。

    stdout_text  始终作为 stdout 返回（真 uVision 会回显日志）。
    log_text=None → 不碰 `-o` 文件（保持调用前状态：通常=不存在，或预置的残留文件）
    log_text=str  → 写入 `-o` 文件并把 mtime 设成"刚刚之后"（模拟本次构建刚写完）

    mtime 用 os.utime 显式设定：文件系统时间粒度可能较粗，不能让用例碰运气。
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if log_text is not None:
            log_path = Path(cmd[cmd.index("-o") + 1])
            log_path.write_text(log_text, encoding="gbk")
            touch_fresh(log_path)
        return _FakeProc(returncode, stdout_text.encode("gbk"))

    monkeypatch.setattr(build.subprocess, "run", fake_run)
    return calls


def touch_fresh(p: Path):
    """把 mtime 设到"刚刚之后" —— 模拟本次构建刚产出/写过这个文件。"""
    now = time.time() + 1
    os.utime(p, (now, now))


def run_and_evaluate(paths, axf, refresh_axf=True):
    """跑一次（伪造的）构建并判定；refresh_axf 模拟"本次构建产出了新固件"。"""
    code, text, t0 = build.run_build(True, paths)
    if refresh_axf:
        touch_fresh(axf)
    passed, checks = build.evaluate(code, text, t0, paths)
    return code, text, passed, checks


# ------------------------------------------------------------ 核心回归

def test_same_log_in_stdout_and_file_is_counted_once(tmp_path, monkeypatch):
    """★ 核心回归：同一份日志同时在 stdout 和 -o，只统计一次 24 条 —— 不是 48。"""
    root, axf = make_tree(tmp_path, "Same24")
    paths = paths_of(root)
    log = make_log(n_actions=24)
    fake_uvision(monkeypatch, stdout_text=log, log_text=log)

    _, text, passed, checks = run_and_evaluate(paths, axf)

    assert text == log, "日志来源被拼接了（双重计数复发）"
    assert text.count("compiling") == 22         # 22 compiling，而非 44
    assert checks[2][2] == "实际 24 条"           # 判据 3 的实测数就是真实动作数
    assert passed is True


@pytest.mark.parametrize("n_actions", [15, 8])
def test_insufficient_actions_fail_at_threshold_16(tmp_path, monkeypatch, n_actions):
    """阈值 16 下 15 条 / 8 条必须 FAIL —— 旧缺陷会翻倍成 30 / 16 而误 PASS。"""
    root, axf = make_tree(tmp_path, f"Few{n_actions}", min_lines=16)
    paths = paths_of(root)
    log = make_log(n_actions=n_actions)
    fake_uvision(monkeypatch, stdout_text=log, log_text=log)

    _, _, passed, checks = run_and_evaluate(paths, axf)

    assert checks[2][2] == f"实际 {n_actions} 条"
    assert checks[2][1] is False
    assert passed is False


def test_exactly_threshold_passes(tmp_path, monkeypatch):
    """边界内侧：恰好 16 条（= 阈值）判 PASS。"""
    root, axf = make_tree(tmp_path, "Exactly16", min_lines=16)
    paths = paths_of(root)
    log = make_log(n_actions=16)
    fake_uvision(monkeypatch, stdout_text=log, log_text=log)

    _, _, passed, checks = run_and_evaluate(paths, axf)

    assert checks[2][2] == "实际 16 条"
    assert passed is True


# ------------------------------------------------------------ 回退路径

def test_falls_back_to_stdout_when_log_missing(tmp_path, monkeypatch, capsys):
    """`-o` 日志不存在 → 回退 stdout，仍只取一份，并打 [WARN]（降级不静默）。"""
    root, axf = make_tree(tmp_path, "NoLog")
    paths = paths_of(root)
    log = make_log(n_actions=24)
    fake_uvision(monkeypatch, stdout_text=log, log_text=None)

    assert not paths.tmp_log.exists()

    _, text, passed, checks = run_and_evaluate(paths, axf)

    assert text == log
    assert checks[2][2] == "实际 24 条"
    assert passed is True
    assert "回退到 stdout" in capsys.readouterr().err


def test_falls_back_to_stdout_when_log_empty(tmp_path, monkeypatch, capsys):
    """`-o` 日志存在但为空 → 回退 stdout。"""
    root, axf = make_tree(tmp_path, "EmptyLog")
    paths = paths_of(root)
    log = make_log(n_actions=24)
    fake_uvision(monkeypatch, stdout_text=log, log_text="")

    _, text, passed, checks = run_and_evaluate(paths, axf)

    assert text == log
    assert checks[2][2] == "实际 24 条"
    assert passed is True
    assert "回退到 stdout" in capsys.readouterr().err


def test_stale_residual_log_is_not_used(tmp_path, monkeypatch, capsys):
    """`-o` 文件是上一次构建的残留（mtime 早于 t0）→ 不采用，回退 stdout。

    否则会把**上一次**的日志当成本次结果 —— 判据 2/3 的实测细节会张冠李戴。
    """
    root, axf = make_tree(tmp_path, "Stale")
    paths = paths_of(root)
    log = make_log(n_actions=24)

    # 预置一份"上次构建"的残留日志，mtime 明确设成一小时前
    paths.tmp_log.write_text(make_log(n_actions=99), encoding="gbk")
    old = time.time() - 3600
    os.utime(paths.tmp_log, (old, old))

    # 本次伪造的 uVision **不写** -o（log_text=None）→ 残留留在原地
    fake_uvision(monkeypatch, stdout_text=log, log_text=None)

    _, text, _, checks = run_and_evaluate(paths, axf)

    assert text == log, "采用了上一次构建的残留日志"
    assert checks[2][2] == "实际 24 条"          # 不是残留里的 99 条
    assert "回退到 stdout" in capsys.readouterr().err


# ------------------------------------------------------------ 原有判据未受影响

def test_nonzero_exit_still_fails(tmp_path, monkeypatch):
    """非零退出码 + 非零错误统计：判据 1、2 仍会红。"""
    root, axf = make_tree(tmp_path, "BadExit")
    paths = paths_of(root)
    log = make_log(n_actions=24, errors=3)
    fake_uvision(monkeypatch, stdout_text=log, log_text=log, returncode=2)

    code, _, passed, checks = run_and_evaluate(paths, axf)

    assert code == 2
    assert checks[0][1] is False
    assert checks[1][1] is False
    assert checks[1][2] == "3 Error(s), 0 Warning(s)"
    assert passed is False


def test_missing_stats_line_still_fails(tmp_path, monkeypatch):
    """日志里没有 "N Error(s), M Warning(s)" 统计行 → 判据 2 仍会红。"""
    root, axf = make_tree(tmp_path, "NoStats")
    paths = paths_of(root)
    log = make_log(n_actions=24).replace("0 Error(s), 0 Warning(s)\n", "")
    fake_uvision(monkeypatch, stdout_text=log, log_text=log)

    _, _, passed, checks = run_and_evaluate(paths, axf)

    assert checks[1][2] == "未找到统计行"
    assert checks[1][1] is False
    assert passed is False


def test_stale_artifact_still_fails(tmp_path, monkeypatch):
    """AXF 时间戳未更新（没产出新固件）→ 判据 4 仍会红。"""
    root, axf = make_tree(tmp_path, "StaleAxf")
    paths = paths_of(root)
    log = make_log(n_actions=24)
    fake_uvision(monkeypatch, stdout_text=log, log_text=log)

    _, _, passed, checks = run_and_evaluate(paths, axf, refresh_axf=False)

    assert checks[3][1] is False
    assert passed is False


def test_missing_artifact_still_fails(tmp_path, monkeypatch):
    """产物不存在（-r 失败会先删旧 .axf）→ 判据 4 仍会红。"""
    root, axf = make_tree(tmp_path, "NoAxf")
    paths = paths_of(root)
    axf.unlink()
    log = make_log(n_actions=24)
    fake_uvision(monkeypatch, stdout_text=log, log_text=log)

    _, _, passed, checks = run_and_evaluate(paths, axf, refresh_axf=False)

    assert checks[3][1] is False
    assert "不存在" in checks[3][2]
    assert passed is False
