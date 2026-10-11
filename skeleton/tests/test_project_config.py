"""project_config 单元测试 —— 骨架层，不依赖芯片 / Keil / 硬件。

覆盖（按 Task 1 要求逐条对应）
    1. 两个工程各读自己的配置，路径不串用        -> test_two_projects_do_not_cross
       （更强的变体：同名工程、不同目录也要隔离） -> test_same_name_different_root_isolated
    2. 缺少配置文件 / 缺少必需字段时明确报错     -> test_missing_cfg_* / test_missing_key_*
    3. 优先级 CLI > 环境变量 > 配置文件 > 默认   -> test_precedence_*
    4. list / int / 路径解析                     -> test_list_parsing / test_int_parsing
                                                    / test_path_resolved_against_project_root
    5. 值里的百分号不被插值                      -> test_percent_is_not_interpolated
    6. Windows 风格路径行为明确                  -> test_backslash_path_equals_forward_slash

全部用 tmp_path 临时目录，产物不落仓库（遵守 PYTHON_AUTOMATION_STANDARD §6）。
"""
import os

import pytest

import project_config  # 由 tests/conftest.py 把 tools/ 加进 sys.path

pytestmark = pytest.mark.nohardware


# ------------------------------------------------------------------ 夹具

CFG_TMPL = """\
[project]
name = {name}

[build]
uvprojx = MDK-ARM/{name}.uvprojx
axf = MDK-ARM/{name}/{name}.axf
min_compile_lines = {min_lines}

[flash]
log = MDK-ARM/flash.log

[serial]
port = {port}
baud = 115200
boot_marker = {marker}

[ioc]
file = {name}.ioc
enabled = {ioc_enabled}

[image]
flash_size = 524288
ram_size = 131072
required_symbols = main, SystemClock_Config
"""


def cfg_text(name, *, min_lines=20, port="COM9", marker="USh", ioc_enabled="true"):
    return CFG_TMPL.format(name=name, min_lines=min_lines, port=port,
                           marker=marker, ioc_enabled=ioc_enabled)


def make_project(tmp_path, dirname, text):
    """在 tmp_path/dirname/tools/ 下建配置。text=None 表示**不建配置文件**。"""
    tools = tmp_path / dirname / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    if text is not None:
        (tools / "project.cfg").write_text(text, encoding="utf-8")
    return tools


# ------------------------------------------------- 1. 两工程互不串用

def test_two_projects_do_not_cross(tmp_path):
    a_tools = make_project(tmp_path, "proj_a", cfg_text("ProjA", min_lines=20, port="COM7"))
    b_tools = make_project(tmp_path, "proj_b", cfg_text("ProjB", min_lines=8, port="COM8"))

    a = project_config.load(a_tools)
    b = project_config.load(b_tools)

    assert a.get("project", "name") == "ProjA"
    assert b.get("project", "name") == "ProjB"

    # 路径必须落在各自工程根下，且**互不出现对方的名字**
    assert a.path("build", "axf") == (tmp_path / "proj_a" / "MDK-ARM" / "ProjA" / "ProjA.axf").resolve()
    assert b.path("build", "axf") == (tmp_path / "proj_b" / "MDK-ARM" / "ProjB" / "ProjB.axf").resolve()
    assert "ProjB" not in str(a.path("build", "axf"))
    assert "ProjA" not in str(b.path("build", "axf"))

    # 同名字段在不同工程里取到不同值
    assert a.get("serial", "port") == "COM7"
    assert b.get("serial", "port") == "COM8"
    assert a.int("build", "min_compile_lines") == 20
    assert b.int("build", "min_compile_lines") == 8


def test_same_name_different_root_isolated(tmp_path):
    """**同名**工程放在不同目录时，路径必须各自落在自己的根下。

    工程名相同是真实场景（比如两个分支的同一个工程）：
    若实现里缓存了"上次解析的根"，这里就会串。
    """
    a_tools = make_project(tmp_path, "clone_a", cfg_text("Same"))
    b_tools = make_project(tmp_path, "clone_b", cfg_text("Same"))

    a = project_config.load(a_tools)
    b = project_config.load(b_tools)

    assert a.path("build", "axf") == (tmp_path / "clone_a" / "MDK-ARM" / "Same" / "Same.axf").resolve()
    assert b.path("build", "axf") == (tmp_path / "clone_b" / "MDK-ARM" / "Same" / "Same.axf").resolve()
    assert a.path("build", "axf") != b.path("build", "axf")


# ------------------------------------------------- 2. 缺配置 / 缺字段

def test_missing_cfg_raises_with_fix_hint(tmp_path):
    tools = make_project(tmp_path, "no_cfg", None)

    with pytest.raises(project_config.ConfigMissing) as e:
        project_config.load(tools)

    msg = str(e.value)
    assert "project.cfg" in msg
    assert "project.cfg.example" in msg
    assert "修复" in msg          # 规范要求失败必须给可复制的修复建议


def test_missing_section_raises(tmp_path):
    tools = make_project(tmp_path, "no_section", "[project]\nname = X\n")
    c = project_config.load(tools)

    with pytest.raises(project_config.ConfigKeyMissing) as e:
        c.get("build", "uvprojx")

    assert "uvprojx" in str(e.value)
    assert "build" in str(e.value)


def test_missing_key_raises(tmp_path):
    text = "[project]\nname = X\n\n[build]\naxf = MDK-ARM/X/X.axf\n"
    tools = make_project(tmp_path, "no_key", text)
    c = project_config.load(tools)

    with pytest.raises(project_config.ConfigKeyMissing):
        c.get("build", "uvprojx")

    # path() 走的也是同一条路径：缺失即报错，绝不静默返回
    with pytest.raises(project_config.ConfigKeyMissing):
        c.path("build", "uvprojx")

    with pytest.raises(project_config.ConfigKeyMissing):
        c.list("image", "required_symbols")


def test_missing_key_with_explicit_default_returns_default(tmp_path):
    """只有**显式**传 default 才允许回落 —— 这是调用方的主动选择，不是隐式兜底。"""
    tools = make_project(tmp_path, "with_default", "[project]\nname = X\n")
    c = project_config.load(tools)

    assert c.get("build", "min_compile_lines", "20") == "20"
    assert c.int("build", "min_compile_lines", 20) == 20
    assert c.bool("ioc", "enabled", True) is True


# ------------------------------------------------- 3. 优先级

def test_precedence_cli_beats_env_and_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_PORT", "COM5")
    assert project_config.resolve("COM7", "AUTOLAB_PORT", "COM9", "COM1") == "COM7"


def test_precedence_env_beats_cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOLAB_PORT", "COM5")
    assert project_config.resolve(None, "AUTOLAB_PORT", "COM9", "COM1") == "COM5"


def test_precedence_cfg_beats_default(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOLAB_PORT", raising=False)
    assert project_config.resolve(None, "AUTOLAB_PORT", "COM9", "COM1") == "COM9"


def test_precedence_default_is_last(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTOLAB_PORT", raising=False)
    assert project_config.resolve(None, "AUTOLAB_PORT", None, "COM1") == "COM1"
    assert project_config.resolve(None, None, None, "COM1") == "COM1"


def test_precedence_empty_string_means_not_provided(monkeypatch):
    """空串 = "没给"，不是"给了一个空值" —— 否则 CLI 传空会静默盖掉环境变量。"""
    monkeypatch.setenv("AUTOLAB_PORT", "COM5")
    assert project_config.resolve("", "AUTOLAB_PORT", "COM9", "COM1") == "COM5"

    monkeypatch.setenv("AUTOLAB_PORT", "")
    assert project_config.resolve(None, "AUTOLAB_PORT", "COM9", "COM1") == "COM9"


# ------------------------------------------------- 4. list / int / path

def test_list_parsing(tmp_path):
    tools = make_project(tmp_path, "listy", cfg_text("L"))
    c = project_config.load(tools)

    assert c.list("image", "required_symbols") == ["main", "SystemClock_Config"]


def test_list_empty_value_is_empty_list(tmp_path):
    """字段**存在但值为空** -> 空列表（显式表达"本工程无必需符号"）。"""
    text = "[project]\nname = X\n\n[image]\nrequired_symbols =\n"
    tools = make_project(tmp_path, "empty_list", text)
    c = project_config.load(tools)

    assert c.list("image", "required_symbols") == []


def test_int_parsing(tmp_path):
    tools = make_project(tmp_path, "inty", cfg_text("I", min_lines=56))
    c = project_config.load(tools)

    assert c.int("build", "min_compile_lines") == 56


def test_int_rejects_non_numeric(tmp_path):
    text = "[project]\nname = X\n\n[build]\nmin_compile_lines = 二十\n"
    tools = make_project(tmp_path, "bad_int", text)
    c = project_config.load(tools)

    with pytest.raises(project_config.ConfigValueInvalid) as e:
        c.int("build", "min_compile_lines")
    assert "min_compile_lines" in str(e.value)


def test_bool_parsing(tmp_path):
    on = project_config.load(make_project(tmp_path, "on", cfg_text("X", ioc_enabled="true")))
    off = project_config.load(make_project(tmp_path, "off", cfg_text("X", ioc_enabled="false")))

    assert on.bool("ioc", "enabled") is True
    assert off.bool("ioc", "enabled") is False


def test_path_resolved_against_project_root(tmp_path):
    tools = make_project(tmp_path, "paths", cfg_text("P"))
    c = project_config.load(tools)

    p = c.path("build", "uvprojx")
    assert p.is_absolute()
    assert p == (tmp_path / "paths" / "MDK-ARM" / "P.uvprojx").resolve()
    assert c.root == (tmp_path / "paths").resolve()


# ------------------------------------------------- 5. 百分号不被插值

def test_percent_is_not_interpolated(tmp_path):
    """配置值里的 % 必须原样保留。

    若用默认的 BasicInterpolation，`%20` / `%z` 会直接抛 InterpolationSyntaxError，
    或把值改掉 —— 路径与启动标记里出现 % 完全合法，不能被解释成占位符。
    """
    text = ("[project]\nname = X\n\n[serial]\n"
            "boot_marker = USh%20%zz\nport = COM9%d\n")
    tools = make_project(tmp_path, "percents", text)
    c = project_config.load(tools)

    assert c.get("serial", "boot_marker") == "USh%20%zz"
    assert c.get("serial", "port") == "COM9%d"


# ------------------------------------------------- 6. Windows 风格路径

def test_backslash_path_equals_forward_slash(tmp_path):
    """cfg 里写 `\\` 与写 `/` 必须解析成同一个路径。

    工程配置文件是给人手写的，Windows 用户天然会写反斜杠。
    实现里统一归一化，行为在 Windows / POSIX 上都一致且可复现。
    """
    fwd = ("[project]\nname = X\n\n[build]\n"
           "axf = MDK-ARM/X/X.axf\n")
    bwd = ("[project]\nname = X\n\n[build]\n"
           "axf = MDK-ARM\\X\\X.axf\n")

    cf = project_config.load(make_project(tmp_path, "fwd", fwd))
    cb = project_config.load(make_project(tmp_path, "bwd", bwd))

    expected = (tmp_path / "fwd" / "MDK-ARM" / "X" / "X.axf").resolve()
    assert cf.path("build", "axf") == expected
    # 反斜杠那条：相对段与正斜杠那条相同，只是根目录不同
    assert cb.path("build", "axf") == (tmp_path / "bwd" / "MDK-ARM" / "X" / "X.axf").resolve()
    assert str(cb.path("build", "axf")).endswith(os.path.join("MDK-ARM", "X", "X.axf"))


# ------------------------------------------------- 附：注释与键名大小写

def test_inline_comment_is_stripped(tmp_path):
    """`key = 值  ; 说明` 里的行尾注释必须被去掉，否则 int() 会当场炸。

    （配置模板大量使用行尾注释，这条是模板可用性的前提。）
    """
    text = ("[project]\nname = X\n\n[build]\n"
            "min_compile_lines = 20          ; 四判据之三的阈值，换工程须重新标定\n")
    tools = make_project(tmp_path, "comments", text)
    c = project_config.load(tools)

    assert c.int("build", "min_compile_lines") == 20


def test_key_lookup_is_case_insensitive(tmp_path):
    """键名大小写不敏感（configparser 默认行为）—— 明确下来并锁住。"""
    tools = make_project(tmp_path, "casey", cfg_text("C"))
    c = project_config.load(tools)

    assert c.get("build", "MIN_COMPILE_LINES") == "20"
