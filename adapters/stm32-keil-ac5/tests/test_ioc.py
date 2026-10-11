"""Stage 9 —— CubeMX 配置一致性：project.cfg 指定的 `.ioc` 必须与生成代码一致。

为什么把它放进测试（而不是一个"偶尔想起来才跑"的脚本）
    本工程已经**两次**被"CubeMX 重新生成冲掉手改修复"这一类问题咬到：
      · 时钟修复（HSE 25->8MHz、PLLM 25->8）改了代码，.ioc 没同步
      · sdio.c 的 Init.BusWide 被 CubeMX 从 1B 改回 4B
    这类漂移**不会让当前构建失败**，只会在下一次"生成代码"时静默地毁掉修复 ——
    是最难查的那种。做成 CI 层1 的常驻检查后，漂移一发生 pre-commit 就会拦住。

覆盖：HSE_VALUE / PLLM / PLLN / PLLQ / APB2 分频，以及 .ioc 自身自洽性。
"""
import os
import subprocess
import sys

import pytest

import project_config   # sys.path 由本目录的 conftest.py 引导

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")


def _cfg():
    return project_config.load(TOOLS_DIR)


def test_ioc_matches_code():
    cfg = _cfg()
    if not cfg.has("ioc", "enabled"):
        pytest.skip("[ioc] enabled 未在 project.cfg 中配置 —— 跳过"
                    "（显式跳过，不当成 false）")
    if not cfg.bool("ioc", "enabled"):
        pytest.skip("本工程未启用 CubeMX（project.cfg 的 [ioc] enabled = false）")

    p = subprocess.run([sys.executable, os.path.join(TOOLS_DIR, "check_ioc.py")],
                       capture_output=True, cwd=ROOT, timeout=120)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    assert p.returncode == 0, \
        ".ioc 与代码漂移 —— CubeMX 重新生成会冲掉这些修复:\n" + out


pytestmark = pytest.mark.nohardware
