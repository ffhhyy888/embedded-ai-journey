"""serial_tool 的配置优先级 —— **不打开任何串口、不碰硬件**。

覆盖（Task 6）
    · 优先级链：CLI > 环境变量 > project.cfg > 内置默认
    · 端口认 AUTOLAB_PORT；baud / expect / reset_cmd_hex 不设环境变量
    · cfg 缺失 -> 用内置默认 + notes 逐项说明降级（降级允许、静默不允许）
    · cfg 在但缺字段 -> 只有那一个字段报降级，其余仍取 cfg
    · reset_cmd_hex 非法 -> ValueError（不清不楚地崩掉）
    · CLI 表面不新增开关

不覆盖：真实串口收发（需硬件与授权，本轮 NOT RUN）。
"""
import os
import re
import subprocess
import sys

import pytest

import project_config
import serial_tool  # 由 tests/conftest.py 把 tools/ 加进 sys.path

pytestmark = pytest.mark.nohardware

CFG_TMPL = """\
[serial]
port = {port}
baud = {baud}
boot_marker = {marker}
reset_cmd_hex = {reset_hex}
"""


def write_cfg(tmp_path, port="CFGPORT", baud=9600, marker="CFG", reset_hex="2a"):
    path = tmp_path / "project.cfg"
    path.write_text(CFG_TMPL.format(port=port, baud=baud, marker=marker,
                                    reset_hex=reset_hex), encoding="utf-8")
    return path


def load(path):
    return project_config.ProjectConfig(path, path.parent)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """测试环境里别混进真实的 AUTOLAB_PORT。"""
    monkeypatch.delenv("AUTOLAB_PORT", raising=False)


# ------------------------------------------------------------ 优先级链

def test_port_cli_beats_env_and_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_PORT", "ENVPORT")
    st, _ = serial_tool.resolve_settings("CLIPORT", None, None, load(write_cfg(tmp_path)))
    assert st.port == "CLIPORT"


def test_port_env_beats_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_PORT", "ENVPORT")
    st, notes = serial_tool.resolve_settings(None, None, None, load(write_cfg(tmp_path)))
    assert st.port == "ENVPORT"
    assert notes == []                       # 取自环境变量，不算降级


def test_port_cfg_beats_default(tmp_path):
    st, notes = serial_tool.resolve_settings(None, None, None, load(write_cfg(tmp_path)))
    assert st.port == "CFGPORT"
    assert notes == []


def test_all_values_come_from_cfg(tmp_path):
    cfg = load(write_cfg(tmp_path, port="COM7", baud=57600, marker="XYZ", reset_hex="1b"))
    st, notes = serial_tool.resolve_settings(None, None, None, cfg)

    assert st.port == "COM7"
    assert st.baud == 57600                  # int，不是 str
    assert st.expect == "XYZ"
    assert st.reset_cmd == b"\x1b"           # 十六进制已转成字节
    assert notes == []


# ------------------------------------------------------------ 降级（不静默）

def test_missing_cfg_falls_back_with_notes():
    st, notes = serial_tool.resolve_settings(None, None, None, None)

    assert st.port == serial_tool.DEFAULT_PORT
    assert st.baud == serial_tool.DEFAULT_BAUD
    assert st.expect == serial_tool.DEFAULT_BOOT_MARKER
    assert st.reset_cmd == bytes.fromhex(serial_tool.DEFAULT_RESET_HEX)

    assert any("project.cfg" in n for n in notes)     # 缺文件这件事本身要明说
    for field in ("serial.port", "serial.baud", "serial.boot_marker", "serial.reset_cmd_hex"):
        assert any(field in n for n in notes), f"{field} 的降级没被记录"


def test_missing_field_falls_back_alone(tmp_path):
    """cfg 在、只缺 baud -> 只有 baud 报降级，其余字段仍取 cfg 的值。"""
    path = tmp_path / "project.cfg"
    path.write_text("[serial]\nport = P1\nboot_marker = M1\nreset_cmd_hex = 1b\n",
                    encoding="utf-8")
    st, notes = serial_tool.resolve_settings(None, None, None, load(path))

    assert st.port == "P1"
    assert st.baud == serial_tool.DEFAULT_BAUD
    assert [n for n in notes if "serial.baud" in n], "缺 baud 应当被记录"
    assert not any("serial.port" in n for n in notes), "port 取自 cfg，不该报降级"


def test_explicit_cli_does_not_emit_false_note():
    """用户显式传了参数时，不该凭空报"降级了" —— 这是误报，会让人不信告警。"""
    st, notes = serial_tool.resolve_settings("COM7", 9600, "AB", None)

    assert st.port == "COM7"
    assert not any("serial.port" in n for n in notes)
    assert not any("serial.baud" in n for n in notes)
    assert not any("serial.boot_marker" in n for n in notes)


def test_reset_cmd_hex_can_still_fall_back_when_cli_given():
    """reset_cmd_hex 没有 CLI 开关，所以即使别的参数给了，它仍会报降级。"""
    _, notes = serial_tool.resolve_settings("COM7", 9600, "AB", None)
    assert [n for n in notes if "serial.reset_cmd_hex" in n]


# ------------------------------------------------------------ baud 兼容

def test_baud_cli_overrides_cfg(tmp_path):
    st, _ = serial_tool.resolve_settings(None, 115200, None, load(write_cfg(tmp_path, baud=57600)))
    assert st.baud == 115200


def test_baud_has_no_environment_variable(tmp_path, monkeypatch):
    """baud 只走 CLI > cfg > 默认；设了 AUTOLAB_BAUD 也不该生效。"""
    monkeypatch.setenv("AUTOLAB_BAUD", "1234")
    st, _ = serial_tool.resolve_settings(None, None, None, load(write_cfg(tmp_path, baud=57600)))
    assert st.baud == 57600


def test_invalid_reset_hex_raises(tmp_path):
    with pytest.raises(ValueError) as e:
        serial_tool.resolve_settings(None, None, None, load(write_cfg(tmp_path, reset_hex="zz")))
    assert "reset_cmd_hex" in str(e.value)


# ------------------------------------------------------------ CLI 表面不变

def test_cli_surface_has_no_new_flags():
    """usage 行里的开关集合必须与改造前一致（不新增 CLI）。"""
    out = subprocess.run(
        [sys.executable, str(serial_tool.TOOLS_DIR / "serial_tool.py"), "--help"],
        capture_output=True, timeout=60,
        env=dict(os.environ, PYTHONIOENCODING="utf-8"))

    assert out.returncode == 0
    text = out.stdout.decode("utf-8", "replace")

    # argparse 会按终端宽度把 usage 折成多行 —— 收集整块再比对，别只看第一行。
    usage_lines = []
    for ln in text.splitlines():
        if ln.startswith("usage:"):
            usage_lines = [ln]
        elif usage_lines and ln[:1] in (" ", "\t") and ln.strip():
            usage_lines.append(ln)
        elif usage_lines:
            break

    # usage 行里长选项写作 `--port`、短选项写作 `-h`（没有 `--help`，那是 options 区的事）
    expected = {"-h", "--port", "--baud", "--timeout", "--retries",
                "--smoke", "--reset", "--expect", "--echo", "--monitor"}
    found = set(re.findall(r"-{1,2}[a-z][a-z-]*", " ".join(usage_lines)))
    assert found == expected, f"CLI 开关发生变化：多了 {found - expected}，少了 {expected - found}"
