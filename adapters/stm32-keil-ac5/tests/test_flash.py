"""flash.py 的无板验证 —— **不启动 uVision、不接触开发板**。

覆盖（Task 4 要求）：参数、命令构造、超时、退出码处理。
**不覆盖**：真实烧录行为 —— 那必须在获得人工授权后到真机上做，本轮标 NOT RUN。
"""
import sys

import pytest

import build             # sys.path 由本目录的 conftest.py 引导
import flash
import project_config

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


def make_tree(tmp_path, name):
    root = tmp_path / name
    (root / "tools").mkdir(parents=True)
    (root / "MDK-ARM" / name).mkdir(parents=True)
    (root / "MDK-ARM" / f"{name}.uvprojx").write_text("<Project/>", encoding="utf-8")
    (root / "tools" / "project.cfg").write_text(CFG_TMPL.format(name=name), encoding="utf-8")
    return root


def paths_and_log(root):
    cfg = project_config.load(root / "tools")
    return build.resolve_paths(cfg), cfg.path("flash", "log")


class _FakeProc:
    def __init__(self, rc, out=b""):
        self.returncode, self.stdout = rc, out


# ------------------------------------------------------- 参数与命令构造

def test_command_construction_follows_project_name(tmp_path):
    """命令由 cfg 的工程名构造 —— 换工程就换命令，无硬编码。"""
    pa, log_a = paths_and_log(make_tree(tmp_path, "ProjA"))
    pb, log_b = paths_and_log(make_tree(tmp_path, "ProjB"))

    ca, cb = flash.build_command(pa, log_a), flash.build_command(pb, log_b)

    assert ca == [str(build.UVISION), "-f", "ProjA.uvprojx", "-j0", "-o", str(log_a)]
    assert cb[2] == "ProjB.uvprojx"
    assert ca[1] == "-f"                       # -f = 下载（不重新编译）
    assert ca[3] == "-j0"
    assert "-o" in ca and str(log_a) in ca      # 日志落盘，供取证


def test_run_flash_passes_cwd_timeout_and_reads_log(tmp_path, monkeypatch):
    root = make_tree(tmp_path, "Runner")
    paths, log_path = paths_and_log(root)
    log_path.write_text("Erase Done.\nVerify OK.\n", encoding="gbk")
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen.update(kw)
        return _FakeProc(0, b"Programming Done.\r\n")
    monkeypatch.setattr(flash.subprocess, "run", fake_run)

    rc, text = flash.run_flash(paths, log_path, 42)

    assert rc == 0
    assert seen["cwd"] == str(paths.mdk)                       # 在 MDK-ARM 下执行
    assert seen["timeout"] == 42                               # timeout 必须传下去
    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"          # 防 GBK 乱码
    assert "Verify OK" in text and "Programming Done" in text   # stdout 与日志都读进来了


def test_timeout_precedence(tmp_path, monkeypatch):
    """--timeout > AUTOLAB_FLASH_TIMEOUT > 180。"""
    monkeypatch.setenv("AUTOLAB_FLASH_TIMEOUT", "99")
    assert flash.resolve_timeout(7) == 7          # CLI 最优先
    assert flash.resolve_timeout() == 99          # 其次环境变量
    monkeypatch.delenv("AUTOLAB_FLASH_TIMEOUT", raising=False)
    assert flash.resolve_timeout() == 180         # 最后内置默认


# ------------------------------------------------------- 退出码处理

@pytest.mark.parametrize("returncode,log,expect_pass", [
    (0, "Erase Done.\nVerify OK.\n", True),
    (0, "Erase Done.\n", False),                 # 烧了但没验过
    (1, "Erase Done.\nVerify OK.\n", False),     # 退出码非 0
    (0, "", False),                              # 日志缺失
])
def test_evaluate_exit_codes(returncode, log, expect_pass):
    passed, checks = flash.evaluate(returncode, log)

    assert passed is expect_pass
    assert len(checks) == 2
    assert checks[0][1] is (returncode == 0)


def test_evaluate_reports_missing_log_distinctly(tmp_path):
    """日志为空时，细节要说明"日志为空或不存在"，而不是笼统"未出现"。"""
    _, checks = flash.evaluate(0, "")
    assert checks[1][1] is False
    assert "日志为空" in checks[1][2]


# ------------------------------------------------------- 绝不在未授权时启动 uVision

def test_dry_run_does_not_launch_uvision(tmp_path, monkeypatch, capsys):
    """--dry-run：必须返回 2（未执行）且**一次都不调用 uVision**。"""
    root = make_tree(tmp_path, "Dry")
    monkeypatch.setattr(build, "TOOLS_DIR", root / "tools")
    calls = []
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["flash.py", "--dry-run"])

    rc = flash.main()

    assert rc == 2                  # 未执行 -> 2，绝不用 0
    assert calls == []              # 关键：没有启动 uVision
    assert '"NOT_RUN"' in capsys.readouterr().out


def test_missing_keil_reports_error_without_launching(tmp_path, monkeypatch, capsys):
    """Keil 不存在时应报 ERROR 并退出 2，而不是抛 traceback 或硬跑。"""
    root = make_tree(tmp_path, "NoKeil")
    monkeypatch.setattr(build, "TOOLS_DIR", root / "tools")
    monkeypatch.setattr(build, "UVISION", tmp_path / "nope" / "uVision.com")
    calls = []
    monkeypatch.setattr(flash.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(sys, "argv", ["flash.py"])

    rc = flash.main()

    assert rc == 2
    assert calls == []
    assert '"ERROR"' in capsys.readouterr().out
