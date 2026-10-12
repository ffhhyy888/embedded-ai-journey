"""工具路径必须来自 tools/project.cfg —— 锁住"不再写死 SDIO_test"这条契约。

为什么需要这个文件
    check_ioc.py 与 debug_probe.py 曾经把路径写死在模块级：
        IOC = os.path.join(ROOT, "SDIO_test.ioc")
        AXF = os.path.join(ROOT, "MDK-ARM", "SDIO_test", "SDIO_test.axf")
    后果不是"报一个清楚的错"，而是**换工程后静默失效**：
      · check_ioc.py  -> 找不到 SDIO_test.ioc，退出码非 0，被调用方当成"有漂移"
      · debug_probe.py-> 去读另一个工程的镜像，症状是"符号表里查不到函数"
    这两个都是"防护看起来在、其实不在"——最难查的一类。

本文件怎么证明它真的读配置
    造一个**工程名与 .ioc/.axf 路径都与 SDIO_test 不同**的临时工程，
    且该工程里**根本不存在** SDIO_test.ioc / SDIO_test.axf。
    于是：
        若工具仍用写死的名字 -> 必然失败（文件不存在）
        若工具读配置         -> 正常通过，且输出里出现配置里的名字
    这样断言"成功"本身就等价于断言"读了配置"。

外部进程
    · check_ioc.py 以子进程方式真实运行 —— 它**只读文件**，不碰硬件、不碰 uVision。
    · debug_probe.py **绝不运行**（它会连调试探针）。只测它的纯函数，
      并把 subprocess.run 换成间谍函数，断言传给 fromelf 的路径来自配置。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import check_ioc
import debug_probe
import project_config

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "tools"
# project_config.py 属**骨架层**，不在适配器 tools/ 下（平台布局里在 skeleton/tools/）。
# 用已导入模块的真实位置来定位，两种布局都成立，也不写死平台路径。
PROJECT_CONFIG_PY = Path(project_config.__file__)

pytestmark = pytest.mark.nohardware

# 一组自洽的时钟参数：8000000 // 8 * 336 // 2 == 168000000
HSE, PLLM, PLLN, PLLQ, PLLP, SYSCLK = 8000000, 8, 336, 7, 2, 168000000


def make_project(tmp_path, proj_name, ioc_name):
    """造一个临时工程：真实工具 + 合成的 .ioc/代码，**名字与 SDIO_test 无关**。

    返回 (root, ioc_file)。root 里**没有** SDIO_test.ioc / SDIO_test.axf。
    """
    root = tmp_path / proj_name
    (root / "tools").mkdir(parents=True)
    (root / "Core" / "Inc").mkdir(parents=True)
    (root / "Core" / "Src").mkdir(parents=True)

    # 拷**真实**的工具与解析器 —— 测的是发布出去的那份代码，不是复制品
    shutil.copy2(TOOLS / "check_ioc.py", root / "tools" / "check_ioc.py")
    shutil.copy2(PROJECT_CONFIG_PY, root / "tools" / "project_config.py")

    (root / "tools" / "project.cfg").write_text(
        "[project]\n"
        f"name = {proj_name}\n"
        "\n"
        "[build]\n"
        f"axf = Firmware/{proj_name}_app.axf\n"
        "\n"
        "[ioc]\n"
        f"file = {ioc_name}\n"
        "enabled = true\n",
        encoding="utf-8")

    ioc_file = root / ioc_name
    ioc_file.write_text(
        f"RCC.HSE_VALUE={HSE}\n"
        f"RCC.PLLM={PLLM}\n"
        f"RCC.PLLN={PLLN}\n"
        f"RCC.PLLQ={PLLQ}\n"
        "RCC.APB2CLKDivider=RCC_HCLK_DIV2\n"
        f"RCC.SYSCLKFreq_VALUE={SYSCLK}\n",
        encoding="utf-8")

    (root / "Core" / "Inc" / "stm32f4xx_hal_conf.h").write_text(
        f"#define HSE_VALUE    {HSE}U\n", encoding="utf-8")
    (root / "Core" / "Src" / "main.c").write_text(
        "PLL.PLLM = %d;\nPLL.PLLN = %d;\nPLL.PLLQ = %d;\n"
        "PLL.PLLP = RCC_PLLP_DIV%d;\nAPB2CLKDivider = RCC_HCLK_DIV2;\n"
        % (PLLM, PLLN, PLLQ, PLLP),
        encoding="utf-8")
    return root, ioc_file


def run_check_ioc(root):
    return subprocess.run(
        [sys.executable, str(root / "tools" / "check_ioc.py")],
        capture_output=True, cwd=str(root), timeout=120,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))


# --------------------------------------------------- check_ioc.py

def test_check_ioc_reads_ioc_path_from_cfg(tmp_path):
    """核心断言：工程改名 + .ioc 改名后**照样通过**，输出里出现新名字。

    工程里没有 SDIO_test.ioc —— 若脚本仍用写死的名字，本用例必红。
    """
    root, ioc_file = make_project(tmp_path, "AnotherBoard", "AnotherBoard.ioc")

    assert not (root / "SDIO_test.ioc").exists(), "前提：临时工程里没有 SDIO_test.ioc"

    p = run_check_ioc(root)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")

    assert p.returncode == 0, f"应当一致并返回 0，实际 {p.returncode}:\n{out}"
    assert "AnotherBoard.ioc" in out, f"输出里应出现配置里的 .ioc 名:\n{out}"
    assert "SDIO_test" not in out, f"不应再出现 SDIO_test:\n{out}"


def test_check_ioc_second_project_uses_its_own_ioc(tmp_path):
    """再换一组完全不同的名字 —— 排除"恰好撞上某个默认值"的可能。"""
    root, _ = make_project(tmp_path, "SensorHub", "SensorHub_cfg.ioc")
    p = run_check_ioc(root)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    assert p.returncode == 0, out
    assert "SensorHub_cfg.ioc" in out, out


def test_check_ioc_reports_drift_as_1(tmp_path):
    """真漂移仍是退出码 1 —— 没把"检查逻辑"换成别的东西。"""
    root, ioc_file = make_project(tmp_path, "Drifted", "Drifted.ioc")
    ioc_file.write_text(ioc_file.read_text(encoding="utf-8").replace(
        f"RCC.PLLM={PLLM}", "RCC.PLLM=25"), encoding="utf-8")

    p = run_check_ioc(root)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    assert p.returncode == 1, f"漂移应返回 1，实际 {p.returncode}:\n{out}"
    assert "PLLM" in out, out


def test_check_ioc_config_error_is_2_not_1(tmp_path):
    """cfg 缺 [ioc] file：应报"无法确定路径"(2)，**不是**"漂移"(1)，更不是静默通过。

    要求里明确"不允许通过禁用检查或静默 SKIP 来消除失败"——这里断言它必须
    以非 0 退出，且把原因说清楚。
    """
    root, _ = make_project(tmp_path, "NoIocKey", "NoIocKey.ioc")
    cfg = root / "tools" / "project.cfg"
    cfg.write_text(cfg.read_text(encoding="utf-8").replace(
        "file = NoIocKey.ioc\n", ""), encoding="utf-8")

    p = run_check_ioc(root)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    assert p.returncode == 2, f"配置错误应为 2，实际 {p.returncode}:\n{out}"
    assert "无法确定" in out, out


def test_check_ioc_ioc_path_function_follows_cfg(tmp_path, monkeypatch):
    """直接打函数：ioc_path() 必须跟着 TOOLS_DIR 走。"""
    root, ioc_file = make_project(tmp_path, "DirectCall", "DirectCall.ioc")
    monkeypatch.setattr(check_ioc, "TOOLS_DIR", str(root / "tools"))
    assert Path(check_ioc.ioc_path()) == ioc_file.resolve()


# --------------------------------------------------- debug_probe.py

def test_debug_probe_axf_path_follows_cfg(tmp_path, monkeypatch):
    """直接打函数：axf_path() 必须跟着 TOOLS_DIR 走。"""
    root, _ = make_project(tmp_path, "ProbeBoard", "ProbeBoard.ioc")
    monkeypatch.setattr(debug_probe, "TOOLS_DIR", str(root / "tools"))
    got = Path(debug_probe.axf_path())
    assert got == (root / "Firmware" / "ProbeBoard_app.axf").resolve()
    assert "SDIO_test" not in str(got)


def test_debug_probe_load_symtab_passes_configured_axf(tmp_path, monkeypatch):
    """间谍 subprocess.run：断言传给 fromelf 的路径来自配置，且不含 SDIO_test。

    **不启动任何真实进程** —— subprocess.run 被替换成只记录参数的假函数。
    """
    root, _ = make_project(tmp_path, "SpyBoard", "SpyBoard.ioc")
    monkeypatch.setattr(debug_probe, "TOOLS_DIR", str(root / "tools"))

    calls = []

    class _Result:
        returncode = 0
        stdout = b""

    def fake_run(argv, **kw):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(debug_probe.subprocess, "run", fake_run)

    debug_probe.load_symtab()          # path 省略 -> 应取配置里的 [build] axf

    assert len(calls) == 1, f"应只调用一次 fromelf，实际 {len(calls)}"
    argv = calls[0]
    assert argv[0] == debug_probe.FROMELF
    assert argv[1:3] == ["--text", "-s"]
    passed = argv[3]
    assert "SDIO_test" not in passed, f"仍传了 SDIO_test 路径: {passed}"
    assert passed.endswith("SpyBoard_app.axf"), f"应传配置里的 axf: {passed}"


def test_debug_probe_explicit_path_still_wins(tmp_path, monkeypatch):
    """显式传 path 时仍以参数为准 —— 原有可测试性没有被破坏。"""
    monkeypatch.setattr(debug_probe, "TOOLS_DIR", str(tmp_path / "nonexistent"))
    calls = []

    class _Result:
        returncode = 0
        stdout = b""

    monkeypatch.setattr(debug_probe.subprocess, "run",
                        lambda argv, **kw: calls.append(argv) or _Result())

    debug_probe.load_symtab("C:/explicit/path.axf")   # 不该去读 cfg
    assert calls[0][3] == "C:/explicit/path.axf"


def test_debug_probe_no_cfg_reports_fail_without_touching_probe(tmp_path,
                                                               monkeypatch):
    """没有 project.cfg 时：结构化 FAIL + 退出码 2，且**不调用任何外部进程**。

    ⚠️ 这条尤其重要：配置守卫必须挡在 ensure_running() 之前，
       否则"没配好"会变成"去连一次调试探针"。
    """
    root = tmp_path / "Empty"
    (root / "tools").mkdir(parents=True)
    # 显式指向这个空 tools/ —— 不能靠"平台 tools/ 恰好也没有 cfg"来碰运气
    monkeypatch.setattr(debug_probe, "TOOLS_DIR", str(root / "tools"))

    calls = []
    monkeypatch.setattr(debug_probe.subprocess, "run",
                        lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(sys, "argv", ["debug_probe.py", "--snapshot"])

    rc = debug_probe.main()

    assert rc == 2, f"配置缺失应返回 2，实际 {rc}"
    assert calls == [], f"配置都没配好就碰了外部进程: {calls}"
