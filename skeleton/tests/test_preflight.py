"""preflight 的**配置链路** —— 不碰硬件。

只覆盖 Task 8.2 新增的 check_serial_params()：取值来源与降级提示。
真实串口 / pyocd / uVision / 烧录 / 板卡**一律不在本文件范围内** ——
这里验证的是"配置怎么来的"，不是"硬件能不能用"。
"""
import sys

import pytest

import preflight
import serial_tool

pytestmark = pytest.mark.nohardware


@pytest.fixture
def proj(tmp_path, monkeypatch):
    """一个临时工程根；并清空 preflight 的结果列表，避免用例间互相污染。"""
    (tmp_path / "tools").mkdir()
    monkeypatch.setattr(preflight, "ROOT", str(tmp_path))
    monkeypatch.setattr(preflight, "results", [])
    saved = list(sys.path)
    yield tmp_path
    sys.path[:] = saved       # 被测函数会往 sys.path 插临时目录；不复原会盖住后面要用的真模块


def write_cfg(proj, port="CFGPORT", baud=57600, reset_hex="1b"):
    (proj / "tools" / "project.cfg").write_text(
        f"[serial]\nport = {port}\nbaud = {baud}\n"
        f"boot_marker = CFG\nreset_cmd_hex = {reset_hex}\n",
        encoding="utf-8")


def levels():
    return [level for level, _, _, _ in preflight.results]


def test_cfg_values_are_used(proj, monkeypatch):
    monkeypatch.delenv("AUTOLAB_PORT", raising=False)
    write_cfg(proj)
    assert preflight.check_serial_params() == ("CFGPORT", 57600)
    assert levels() == ["OK"]


def test_env_overrides_cfg(proj, monkeypatch):
    write_cfg(proj)
    monkeypatch.setenv("AUTOLAB_PORT", "ENVPORT")
    assert preflight.check_serial_params() == ("ENVPORT", 57600)   # 环境变量 > cfg


def test_missing_cfg_warns_and_falls_back(proj, monkeypatch):
    monkeypatch.delenv("AUTOLAB_PORT", raising=False)
    assert preflight.check_serial_params() == (serial_tool.DEFAULT_PORT,
                                               serial_tool.DEFAULT_BAUD)
    assert set(levels()) == {"WARN"}      # 降级要逐条可见，不能静默


def test_invalid_reset_hex_is_fail_not_traceback(proj):
    write_cfg(proj, reset_hex="zz")
    assert preflight.check_serial_params() == (None, None)     # 返回而不是抛异常
    assert levels() == ["FAIL"]


def test_missing_serial_tool_is_skip(proj, monkeypatch):
    (proj / "tools" / "serial_tool.py").write_text(
        'raise ModuleNotFoundError("No module named \'serial\'", name="serial")',
        encoding="utf-8")
    monkeypatch.delitem(sys.modules, "serial_tool", raising=False)
    assert preflight.check_serial_params() == (None, None)
    assert levels() == ["SKIP"]
