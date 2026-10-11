"""Stage 6 —— 无实体板测试：对「构建产物」做结构/契约检查。

为什么需要它
    真机测试依赖于"板子插着、串口连好、SD 卡在位"。这些条件不总是成立，
    而有些错误（内存超了、把演示用的 Bug 漏进正式固件、向量表写坏）
    **根本不需要上板就能发现**，而且应该在做任何硬件操作之前就发现。

边界（重要）
    这些测试只能证明"镜像结构正确"，**不能证明固件功能正确**：
    串口时序、SD 卡握手、外设真实行为，统统不在覆盖范围内。
    仿真通过 ≠ 真机通过。功能正确性仍然由 test_device.py 上板验证。

运行:  python -m pytest tests/ -v
"""
import glob
import os
import re
import struct
import subprocess

import pytest

import project_config   # sys.path 由本目录的 conftest.py 引导

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS_DIR = os.path.join(ROOT, "tools")
LOGS = os.path.join(TOOLS_DIR, "build_logs")
# 机器级：fromelf 装在哪 —— 换电脑才变，走环境变量而不是 project.cfg
FROMELF = os.environ.get("AUTOLAB_FROMELF", r"F:\keil\ARM\ARMCC\bin\fromelf.exe")

# 家族常量：Cortex-M 从 Flash 启动的基址 / SRAM 起始。
# 这些是**芯片家族**的东西，不是工程级参数，所以留在适配器里，不进 project.cfg。
VECTOR_BASE = 0x08000000
SRAM_BASE = 0x20000000


def _cfg():
    """工程配置。缺失即抛 ConfigMissing —— 用例会 FAIL 并给出修复提示，不静默跳过。"""
    return project_config.load(TOOLS_DIR)


# --------------------------------------------------------------- 辅助

def symbols():
    """从 fromelf 符号表取 (地址, 大小, 名字, 类型)。"""
    out = subprocess.run([FROMELF, "--text", "-s", str(_cfg().path("build", "axf"))],
                         capture_output=True, timeout=60).stdout.decode("utf-8", "replace")
    pat = re.compile(r"^\s*\d+\s+(\S+)\s+0x([0-9a-fA-F]{8})\s+\S+\s+\d+\s+(\S+)\s+\S+\s*(0x[0-9a-fA-F]+)?\s*$")
    out_syms = []
    for line in out.splitlines():
        m = pat.match(line)
        if m:
            out_syms.append((int(m.group(2), 16), int(m.group(4) or "0", 16),
                             m.group(1), m.group(3)))
    return out_syms


def elf_first_load_words(path, n=2):
    """读 ELF 最低地址 PT_LOAD 段开头的 n 个 32bit 字（ARM 上即向量表前两项）。"""
    d = open(path, "rb").read()
    assert d[:4] == b"\x7fELF", "不是 ELF 文件"
    phoff = struct.unpack_from("<I", d, 28)[0]
    phentsize = struct.unpack_from("<H", d, 42)[0]
    phnum = struct.unpack_from("<H", d, 44)[0]
    loads = []
    for i in range(phnum):
        off = phoff + i * phentsize
        if struct.unpack_from("<I", d, off)[0] != 1:      # PT_LOAD
            continue
        p_offset, p_vaddr, p_paddr, p_filesz = struct.unpack_from("<IIII", d, off + 4)
        loads.append((p_paddr, p_offset, p_filesz))
    assert loads, "没有 PT_LOAD 段"
    paddr, offset, _ = min(loads)
    return paddr, [struct.unpack_from("<I", d, offset + 4 * k)[0] for k in range(n)]


# --------------------------------------------------------------- 镜像契约

def test_image_fits_flash_and_ram():
    """内存预算：Code+RO+RW 必须装得进 Flash，RW+ZI 必须装得进 RAM。

    这是最容易在"加个缓冲区"时被悄悄突破的红线，而上板前就能查出来。
    """
    logs = sorted(glob.glob(os.path.join(LOGS, "build_*_PASS.log")))
    assert logs, "找不到构建日志（先运行 tools/build.py）"
    text = open(logs[-1], encoding="utf-8", errors="replace").read()
    m = re.search(r"Program Size:\s*Code=(\d+)\s+RO-data=(\d+)\s+RW-data=(\d+)\s+ZI-data=(\d+)", text)
    assert m, "构建日志里没有 Program Size 行"
    code, ro, rw, zi = (int(x) for x in m.groups())

    flash_size = _cfg().int("image", "flash_size")
    ram_size = _cfg().int("image", "ram_size")
    flash_used = code + ro + rw
    ram_used = rw + zi
    assert flash_used <= flash_size, f"Flash 超了: {flash_used} > {flash_size}"
    assert ram_used <= ram_size, f"RAM 超了: {ram_used} > {ram_size}"


def test_required_symbols_present():
    """关键函数必须都编进镜像（清单来自 project.cfg 的 [image] required_symbols）。"""
    want = _cfg().list("image", "required_symbols")
    if not want:
        pytest.skip("project.cfg 未配置 [image] required_symbols —— 跳过"
                    "（显式跳过，不静默通过）")
    names = {s[2] for s in symbols()}
    for name in want:
        assert name in names, f"镜像里缺少符号 {name}"


def test_no_demo_or_debug_leftovers():
    """演示用的故障注入**绝不能**留在正式镜像里。

    （2026-10-10 Stage 5 演示时注入过 BugDemo_TouchUnmapped；
      这条测试就是防它被忘记移除 —— 上板前就能拦住。）
    """
    names = {s[2] for s in symbols()}
    bad = [n for n in names if n.startswith("BugDemo") or "BUG-DEMO" in n]
    assert not bad, f"镜像里残留演示代码符号: {bad}"


def test_vector_table_looks_bootable():
    """向量表头两项必须合理：初始 SP 落在 SRAM，复位向量落在 Flash 且为 Thumb。"""
    cfg = _cfg()
    ram_size = cfg.int("image", "ram_size")
    flash_size = cfg.int("image", "flash_size")
    base, (sp, reset) = elf_first_load_words(cfg.path("build", "axf"))
    assert base == VECTOR_BASE, f"镜像加载基址异常: 0x{base:08X}"
    assert SRAM_BASE <= sp <= SRAM_BASE + ram_size, f"初始 SP 不在 SRAM: 0x{sp:08X}"
    assert VECTOR_BASE <= (reset & ~1) < VECTOR_BASE + flash_size, f"复位向量不在 Flash: 0x{reset:08X}"
    assert reset & 1, "复位向量最低位应为 1（Thumb 状态）"


def test_elf_has_debug_info():
    """镜像必须带调试信息，否则 Stage 5 的调试能力会整体失效。"""
    d = open(_cfg().path("build", "axf"), "rb").read()
    for sec in (b".debug_info", b".debug_line"):
        assert sec in d, f"缺少调试信息段 {sec.decode()}"


pytestmark = pytest.mark.nohardware
