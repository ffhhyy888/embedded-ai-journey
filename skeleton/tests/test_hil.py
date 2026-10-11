"""hil.py 的编排层单元测试 —— 不碰 Keil、不接板、不烧录。

覆盖（Task 5 要求）
    · 取证工具三态：缺工具 -> SKIP ｜ 跑了成功 -> PASS ｜ 跑了失败 -> FAIL
    · 取证工具超时 -> FAIL（不能让整条流水线挂死）
    · 烧录适配器缺失 -> 明确的 FAIL，而不是 traceback
    · 烧录**委托给适配器**（本文件不再内联 Keil 命令）
    · 固件指纹的产物路径取自 project.cfg，不是写死的

不覆盖（诚实标注）
    完整 HIL 轮次（构建 -> 烧录 -> 真机判定）需要 Keil 与真实开发板，
    本轮**未运行**，标 NOT RUN。本文件只测拆出来的纯编排逻辑。
"""
from pathlib import Path

import pytest

import hil  # 由 tests/conftest.py 把 tools/ 加进 sys.path

pytestmark = pytest.mark.nohardware

CFG_TMPL = """\
[project]
name = {name}

[build]
uvprojx = MDK-ARM/{name}.uvprojx
axf = MDK-ARM/{name}/{name}.axf
min_compile_lines = 20

[flash]
log = MDK-ARM/flash.log
"""


def make_project(tmp_path, name="Proj"):
    """造一个最小的工程根（tools/ + project.cfg），返回根目录。"""
    root = tmp_path / name
    (root / "tools").mkdir(parents=True)
    (root / "tools" / "project.cfg").write_text(CFG_TMPL.format(name=name), encoding="utf-8")
    return root


def put_tool(root, name, body):
    (root / "tools" / name).write_text(body, encoding="utf-8")


def use_project(monkeypatch, root):
    """把 hil 的 TOOLS 指向临时工程，并清掉配置缓存。"""
    monkeypatch.setattr(hil, "TOOLS", str(root / "tools"))
    monkeypatch.setattr(hil, "_CFG", None)


# ------------------------------------------------------- 取证工具三态

def test_evidence_tool_missing_is_skip(tmp_path, monkeypatch):
    """缺工具 = SKIP（该芯片家族没这项能力），**不是**失败，但必须显式记下来。"""
    root = make_project(tmp_path)
    use_project(monkeypatch, root)          # 故意不放 debug_probe.py
    dest = tmp_path / "debug_snapshot.txt"

    status, detail = hil.run_evidence_tool("debug_probe.py", ["--snapshot"], str(dest), 30)

    assert status == "SKIP"
    assert "debug_probe.py" in detail
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("SKIP:")
    assert "未提供此取证能力" in text


def test_evidence_tool_success_is_pass(tmp_path, monkeypatch):
    root = make_project(tmp_path)
    use_project(monkeypatch, root)
    put_tool(root, "debug_probe.py", 'import sys; print(\'{"pc": "main"}\'); sys.exit(0)')
    dest = tmp_path / "debug_snapshot.txt"

    status, detail = hil.run_evidence_tool("debug_probe.py", ["--snapshot"], str(dest), 30)

    assert status == "PASS"
    assert "exit=0" in detail
    text = dest.read_text(encoding="utf-8")
    assert "status=PASS" in text
    assert "main" in text                    # 原始输出被完整落盘，没被加工掉


def test_evidence_tool_failure_is_fail(tmp_path, monkeypatch):
    """工具在、但执行失败 -> FAIL，且退出码如实落盘（**不吞异常**）。"""
    root = make_project(tmp_path)
    use_project(monkeypatch, root)
    put_tool(root, "debug_probe.py", 'import sys; print("boom"); sys.exit(3)')
    dest = tmp_path / "debug_fault.txt"

    status, detail = hil.run_evidence_tool("debug_probe.py", ["--fault"], str(dest), 30)

    assert status == "FAIL"
    assert "exit=3" in detail
    text = dest.read_text(encoding="utf-8")
    assert "status=FAIL" in text and "exit=3" in text
    assert "boom" in text


def test_evidence_tool_timeout_is_fail(tmp_path, monkeypatch):
    """工具挂死 -> FAIL，不能让整条流水线跟着挂。"""
    root = make_project(tmp_path)
    use_project(monkeypatch, root)
    put_tool(root, "serial_tool.py", "import time; time.sleep(5)")
    dest = tmp_path / "serial_reset.txt"

    status, detail = hil.run_evidence_tool("serial_tool.py", ["--reset"], str(dest), 1)

    assert status == "FAIL"
    assert "超时" in detail
    text = dest.read_text(encoding="utf-8")
    assert text.startswith("FAIL:")
    assert "超过 1s 未返回" in text          # 超时时长要落盘，便于事后判读


# ------------------------------------------------------- 烧录委托给适配器

def test_flash_step_missing_adapter_is_fail(tmp_path, monkeypatch):
    """没有 tools/flash.py -> 明确 FAIL，并说清是"缺适配器"，不抛 traceback。"""
    root = make_project(tmp_path)
    use_project(monkeypatch, root)

    ok, detail = hil.step_flash()

    assert ok is False
    assert "缺少适配器" in detail and "flash.py" in detail


@pytest.mark.parametrize("exit_code,expect_ok", [(0, True), (1, False), (2, False)])
def test_flash_step_delegates_to_adapter(tmp_path, monkeypatch, exit_code, expect_ok):
    """烧录结果只认适配器的退出码 —— 证明本文件不再内联 Keil、也不再自己解析日志。"""
    root = make_project(tmp_path)
    use_project(monkeypatch, root)
    put_tool(root, "flash.py", f'import sys; print("Verify OK"); sys.exit({exit_code})')

    ok, detail = hil.step_flash()

    assert ok is expect_ok
    if not expect_ok:
        assert f"退出码={exit_code}" in detail


# ------------------------------------------------------- 路径来自 cfg

def test_fingerprint_uses_cfg_axf(tmp_path, monkeypatch):
    """指纹里的 .axf 路径取自 project.cfg —— 换工程名就换路径，无硬编码。"""
    root = make_project(tmp_path, "Renamed")
    axf = root / "MDK-ARM" / "Renamed" / "Renamed.axf"
    axf.parent.mkdir(parents=True)
    axf.write_bytes(b"pretend-firmware")
    use_project(monkeypatch, root)

    fp = hil.firmware_fingerprint()

    assert fp["axf_sha256"] == __import__("hashlib").sha256(b"pretend-firmware").hexdigest()[:16]
    assert "axf_mtime" in fp


def test_config_is_lazy_cached_and_reports_missing(tmp_path, monkeypatch):
    """import 本模块不要求配置就绪；缺失时报 ConfigMissing；加载后缓存复用。"""
    import project_config

    root = make_project(tmp_path, "Ok")
    use_project(monkeypatch, root)
    first = hil.config()
    assert first is hil.config()             # 缓存：同一对象，不重复解析

    # 指向一个没有 project.cfg 的目录 -> 明确报错，而不是静默兜底
    monkeypatch.setattr(hil, "TOOLS", str(tmp_path / "empty" / "tools"))
    monkeypatch.setattr(hil, "_CFG", None)
    (tmp_path / "empty" / "tools").mkdir(parents=True)
    with pytest.raises(project_config.ConfigMissing):
        hil.config()
