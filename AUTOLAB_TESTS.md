# Test Contract

> 规则：只有真实执行并观察到结果的行才写「已验证」。其余一律标「未验证」。

## Build — ✅ 已验证（L2：一键执行 + 自动判定）

### 工具与命令
- `F:\keil\UV4\uVision.com`（控制台版，日志进 stdout）
- 手工命令（cwd = `MDK-ARM`）
  - 增量: `uVision.com -b SDIO_test.uvprojx -j0 -o <log>`
  - 全量: `uVision.com -r SDIO_test.uvprojx -j0 -o <log>`
- **脚本化**: `python tools/build.py`（`--incremental` 切换增量）
- 编译器: ARMCC V5.06 update 5 (build 528) — AC5

### PASS 判据（四条须同时满足）
1. 退出码 == 0
2. 日志匹配 `0 Error(s), 0 Warning(s)`
3. 日志中 compiling/assembling/linking 行 >= 20（防"增量假 0"）
4. 产物 `.axf` 时间戳晚于构建启动时刻（防"Target not created"）

### 双向验证证据（2026-10-10，用户亲手执行）
| 场景 | 脚本输出 | 脚本退出码 | 日志归档 |
|---|---|---|---|
| 正常 | PASS（四项全 PASS，56 条编译动作） | 0 | `build_20261010_105546_PASS.log` |
| main.c:104 语法错误 | **FAIL** — 判据1挂(退出码2)、判据2挂(1 Error)、判据4挂(.axf 不存在) | 1 | `build_20261010_105720_FAIL.log` |
| `git checkout` 回滚后 | PASS（56 条编译动作） | 0 | `build_20261010_105913_PASS.log` |

### 退出码标定（双向，已完成）
| 码 | 含义 | 依据 |
|---|---|---|
| **0** | 成功 | 多次实测 |
| **2** | 编译错误 | 实测语法错误 → `Target not created.` |
| **1** | **有警告** | 实测：5 条 `#128-D: loop is not reachable` → 退出码 1 |
| 15 | 无法读取工程文件 | 实测 `-b NOPE_NOT_EXIST.uvprojx` |

### 已知风险
- **`-r` 先删旧产物，失败时不留 `.axf`**：开发板上仍是旧固件，易造成"改了没生效"的错觉
- **构建日志覆盖**：已由脚本按时间戳归档解决
- **编码**：工程源文件为 GBK；VS Code 默认按 UTF-8 保存会产生大量假 diff（见 LEARNING.md）

## Flash — ✅ 已验证（2026-10-10）
- 工具: Keil `uVision.com -f`（**无需 STM32CubeProgrammer**）
- Command: `uVision.com -f SDIO_test.uvprojx -j0 -o flash.log`（cwd = `MDK-ARM`）
- 工程 Flash Download 配置（用户于 GUI 确认）: **Erase Sectors + Program + Verify + Reset and Run**
- 实测输出:
  `Load "SDIO_test\SDIO_test.axf"` / `Erase Done.` / `Programming Done.` /
  `Verify OK.` / `Application running ...`
- 退出码: **0**
- **前置条件：必须先 build**。`-f` 只下载不编译，跳过 build 会烧上一次的旧固件
- 安全约束: 扇区擦除（非全片）、不改 Option Bytes
- 状态: **已验证**

## 时序探针（Clock/Baud Ruler）— ✅ 已验证（2026-10-10）★ 本轮新增

### 为什么需要它
串口乱码时，"猜波特率"是死路：两个未知数（MCU 时钟、PC 侧实际波特率）同时存在。
**时间不参与任何假设** —— 让固件按墙钟发已知长度的突发，PC 只量字节到达的真实时间。

### 固件要求
`while(1) { HAL_UART_Transmit(&huart1, <256字节>, 256, 0xffff); HAL_Delay(1000); }`

### Command
`python tools/timing_probe.py`（自适应等待 4 段突发）
判读：`T_interval = 168e6 / 实际SYSCLK`；`B_actual ≈ 2560 / T_burst`

### 已验证证据（修复前后对照）
| 时点 | T_interval | 实际 SYSCLK | 硬件 UART 波特率 | PC@115200 现象 |
|---|---|---|---|---|
| 修复前 | **3.197 s** | ~52.5 MHz | ~36,872 | 恒定 `0x1C` 乱码 |
| 修复后 | **1.023 s** | ~164 MHz | ~125,484* | 干净 ASCII |

\* 突发放法粒度较粗（±5%），以 `T_interval` 为准

### 已知局限
- 读数系统性偏高约 2.3%（1.023 s vs 理论 1.000 s）—— 探针偏差 or 晶振精度，未区分
- 需要固件配合（不是通用工具）

## Device Smoke Test — ✅ 已验证（2026-10-10）

### 配置
- 串口: **COM9**（CH340 USB-TTL，VID_1A86/PID_7523）@ **115200** 8N1
- 板上晶振: **8 MHz**；`HSE_VALUE = 8000000U`、`PLLM = 8` → SYSCLK 168 MHz
- 当前固件: 启动依次发 `U`(0x55)、`S`(0x53)、`h`(0x68) → 主循环为**回显**（收到什么原样发回）
  （SDIO 初始化已恢复；时钟修复后 SDIO 的 48 MHz 同步归位）

### Command
```bash
python -c "import serial; s=serial.Serial('COM9',115200,timeout=3); d=s.read(4000); s.close(); print(len(d))"
```
质量判据：读取的字节 **100% 落在预期字符集内**（突发字符 + 0x55 + 0x68）

### 已验证证据（2026-10-10）
- 400 字节: `0123456789ABCDEF0123456789ABCDEF...`（HEX 与 ASCII 均正确）
- **4000 字节: 非预期字节数 = 0 → 解码正确率 100.00%**
- 状态: **已验证**

### 复位同步抓取 — ✅ 已验证（2026-10-10）
- `python tools/serial_tool.py --smoke` + 按复位键 → 一次成功收到 `55 53 68`（`USh`）
- 说明：`U`=时钟/UART 就绪，`S`=**SDIO 初始化通过**（时钟修好后不再卡 Error_Handler），
  `h`=已进入主循环 —— 三个环节全部在真机上走通

## Serial Tool / Stage 3 契约 — ✅ 已验证（2026-10-10）

- 工具: `tools/serial_tool.py`
  - 能力: 连接 / **发送** / 接收 / 解析 / timeout / **有限重试** / **结构化 JSON 结果**
  - 退出码约定: **0 = PASS，1 = FAIL，2 = 参数或连接错误**
    （把"被测对象失败"和"工具自己坏了"分开，避免误判）
- 固件约定: 复位后依次发 `U S h`，随后进入回显循环
- 用例表（**2026-10-10 真机实测**）:

| 用例 | 命令 | 期望 | 实测 | 状态 |
|---|---|---|---|---|
| 冒烟（正常路径） | `--smoke`（按复位键） | PASS，收到 `USh` | `{"result":"PASS",...,"ascii":"USh"}` | **✅ 已验证** |
| 回显（收发闭环） | `--echo "hello 123"` | PASS，回显一致 | sent==recv==`hello 123` | **✅ 已验证** |
| 失败路径（被测对象失败） | `--smoke --expect ZZ` | FAIL + 退出码 1 | FAIL，重试 1 次后退出码 **1** | **✅ 已验证** |
| 连接失败路径 | `--smoke --port COM99` | FAIL + 退出码 **2** | FAIL，退出码 **2** | **✅ 已验证** |
| 监听 | `--monitor 3` | PASS，打印 hex/ascii | — | ⏳ 未验证 |

> **Stage 3 Gate：已通过。** 三种退出码（0 正常 / 1 被测失败 / 2 工具自身错误）全部亲眼见到，
> 判据完成了双向标定。

- 已知坑（cmd）：`cmd1 & echo %ERRORLEVEL%` 会**在解析整行时就展开**，
  打印的是**上一条命令**的退出码。要读真实退出码必须**分两行**写，或用 bash/`$?`。

## 回归套件（Stage 4）— ✅ 已跑通（2026-10-10）

- Command: `python -m pytest tests/ -v`（前置：固件已烧录、COM9 已接好）
- 结果: **8 passed（20.8s）** —— 缺陷修复后已全绿
- 取证: 每次调用工具都把原始输出落盘 `tests/artifacts/`（已 gitignore）

| 用例 | 类型 | 结果 |
|---|---|---|
| `test_build_passes` | 无硬件 | PASS |
| `test_soft_reset_smoke` | 正常 | PASS（`RSTUSh`，**无需人工按键**） |
| `test_echo_roundtrip` | 正常 | PASS |
| `test_echo_single_byte` | 边界 | PASS |
| `test_echo_long_payload`（128B） | 边界 | PASS（修复后） |
| `test_echo_4k_burst`（4KB） | 边界 | PASS（修复后新增，锁住边界） |
| `test_wrong_expect_must_fail` | 异常 | PASS（退出码 1） |
| `test_bad_port_must_exit_2` | 异常 | PASS（退出码 2） |

### 已修复缺陷 #1：连续输入丢字节（2026-10-10，闭环全过程）
- 现象: 一口气发 128 字节 → 收 117（丢 11）；逐字节发 → 128/128 全对
- **根因有两处，分两步才修净**:
  1. **接收侧**：回显用阻塞轮询 —— 收 1 字节后阻塞发送期间不读 RX，
     输入/回显同速时抖动即溢出
  2. **发送侧**（只加缓冲还不够）：`HAL_UART_Transmit` 要等 **TC**(~87µs)，
     使"发"比"收"慢约 7%，持续积压终将填满缓冲
     → 实测 128~2048 通过，**4096 起又丢**(丢 123)、16384 丢 1255
- 修复: ① 中断接收 + 环形缓冲（`HAL_UART_RxCpltCallback` 入队并重新武装，
  补 `HAL_UART_ErrorCallback` 清 ORE）；② 发送改为**只等 TXE** 直写 DR（不阻塞）
- 修复后实测: 128 / 1024 / 4096 / 16384 / **65536** 字节全部逐字节一致
- 状态: **已修复并回归通过**

## 调试能力（Stage 5）— ✅ 工具已验证（2026-10-10）

### 通道选型
- 环境无 GDB / OpenOCD / STM32CubeProgrammer
- Keil 官方自动化路径（`UV4 -d 工程 -j0 -o 日志` + 调试初始化文件 `<tIfile>`）：
  文档支持，但本机盲试 4 次未跑通，`-j0` 下无任何错误信息 → 放弃
- **采用 pyocd**（`pip install pyocd`，实测 0.45.1）：直接认出 ST-Link，
  **无需更换驱动**，Keil 烧录链路不受影响

### 工具
- `python tools/debug_probe.py --snapshot [--read ADDR LEN]...`
- `python tools/debug_probe.py --break SYMBOL [--reset] [--wait MS] [--read ...]`
- **硬约束**：任何路径结束都把目标放回 RUNNING（finally → `ensure_running()`）
- 退出码：0 PASS / 1 FAIL（断点未命中）/ 2 参数错误

### 真机证据（非烧录式验证，目标固件未改动）
| 能力 | 命令 | 结果 |
|---|---|---|
| 断点 | `--break main --reset` | 命中 `pc=0x08002990` = **main+0x0**，`lr=0x080002A1` |
| 快照 | `--snapshot` | `pc_function=main`，`pc_offset=+0x50`，`r2=0x40011000`（USART1 基址） |
| 内存 | `--read 0x2000001C 16` | `6e69667a 00006c61` = **"zfinal"**（环形缓冲里上次回显的内容） |
| 内存 | `--read 0x20000000 8` | `rx_tail=0x6c` / `rx_head=6`（实时读写索引） |
| 自动恢复 | 每次都复验 | 目标回到 RUNNING，串口回显正常 |

### 故障现场（`--fault`）— ✅ 真机演示成功（2026-10-10）
故意在 `main` 里调用 `BugDemo_TouchUnmapped()`（`__attribute__((noinline))`），
访问未映射地址 `0x30000000`（SRAM 只到 0x20020000），触发 BusFault→升级 HardFault。
板上现象：串口 2 秒吐出 23104 个 `H`（`HardFault_Handler` 的标志字节）——**程序开机就死了**。

| 版本 | CFSR | 压栈 PC（出错点） | 结论 |
|---|---|---|---|
| **写** `*wild = ...` | `BFSR.IMPRECISERR` | `main+0x48` ← **不是出错点** | 写进写缓冲，错误"事后"才报；只能靠寄存器残留线索（r0=0x30000000）推断 |
| **读** `v = *wild;` | `BFSR.PRECISERR \| BFARVALID` | **`BugDemo_TouchUnmapped+0x6`** | 精确故障，直接给出出错函数+偏移 |

- 两版都：`HFSR = FORCED`（BusFault 默认未使能，升级为 HardFault）
- 读版本 `BFAR = 0x30000000` = 实际访问的坏地址
- **反汇编核对**：`BugDemo_TouchUnmapped+0x6` 处正是 `LDR r0,[r0,#0]` —— 精确到指令
- Bug 已移除、重新烧录，回归 **8 passed**

### 已知限制
- pyocd 自带 `where`（地址→源码行）在本工程 ARMCC DWARF 上报错
  → 改用 **fromelf 符号表**自己实现「地址→函数」，定位到**函数级**（到不了行级）
- 断点等待用固定 `sleep`（非阻塞轮询），命中判定依赖 `status` 文本

## 无板测试（Stage 6）— ✅ 已验证（2026-10-10）

### 环境侦查：本机能用什么"无板运行"
| 方案 | 状态 |
|---|---|
| 宿主机 C 编译器（gcc/cl/clang/tcc） | ❌ 都没有 → 做不了 host 端单元测试 |
| QEMU | ❌ 未安装，且没有 STM32F407VET6 机型 |
| Keil 自带 Simulator（`SarmCM3.dll` 存在） | ⚠️ 要改 uvoptx 的 `<uSim>`，且依赖 `UV4 -d` + 初始化文件 —— **该路径 Stage 5 已实测跑不通** |

### 采用的方案：镜像契约测试 `tests/test_static.py`（5 条，**0.10 秒**）
真机测试依赖"板子插着、串口连好、SD 卡在位"。而有些错误**根本不需要上板就能发现**，
且应该在任何硬件操作之前就拦住。

| 用例 | 检查什么 |
|---|---|
| `test_image_fits_flash_and_ram` | 解析构建日志的 Program Size，校验装得进 512KB Flash / 128KB RAM |
| `test_required_symbols_present` | `main` / `SystemClock_Config` / `MX_SDIO_SD_Init` / UART 中断回调都在 |
| `test_no_demo_or_debug_leftovers` | **演示用 Bug 符号绝不能留在正式镜像里** |
| `test_vector_table_looks_bootable` | 解析 ELF 的 PT_LOAD：初始 SP 在 SRAM、复位向量在 Flash 且最低位为 1(Thumb) |
| `test_elf_has_debug_info` | `.debug_info`/`.debug_line` 存在（否则 Stage 5 调试能力整体失效） |

### 双向标定（不是只会绿）
把 `BugDemo_TouchUnmapped` 临时塞回固件后重建 →
`test_no_demo_or_debug_leftovers` **FAILED**，断言 `assert not ['BugDemo_TouchUnmapped']`。
随后移除并重建，恢复全绿。

### ⚠️ 仿真与真机的边界（本阶段最重要的产出）
这些测试只能证明**镜像结构正确**，**不能证明固件功能正确**。明确**不**覆盖：
- 串口收发时序、波特率、帧错误（真实电气特性）
- SD 卡握手（CMD0/CMD8/ACMD41）与实物卡的行为差异
- 时钟实际频率（我们这块板是 **8MHz 晶振**，仿真模型的时钟源未必一致）
- 中断延迟、DMA 时序、电源、EMC

> **仿真通过 ≠ 真机通过。** 功能正确性仍由 `tests/test_device.py` 上板验证。

## 宿主机单元测试（Stage 6）— ✅ 已验证（2026-10-11）

### 工具链
- **`pip install ziglang`**（自带 C 编译器，无需管理员权限，可干净卸载）
- 调用：`python -m ziglang cc -I Core/Inc tests/host/test_ringbuf.c -o t.exe && t.exe`
- 背景：之前判断"没有宿主机编译器所以做不了 host 单元测试"是**偷懒** ——
  实际查一下就发现 PyPI 上有。**别把"当前没有"当成"做不到"。**

### 关键做法：不给测试留"副本"
把「收数据的缓冲逻辑」从 main.c 抽成可移植头文件 **`Core/Inc/ringbuf.h`**
（不依赖 HAL/寄存器/任何硬件），**固件与 PC 测试 include 同一份**。
> 测副本是经典陷阱：副本测通过了，发货的那份照样错。

### 用例（`tests/host/test_ringbuf.c`，pytest 驱动 `tests/test_host.py`）
| 用例 | 覆盖 |
|---|---|
| test_empty | 新建为空、空时 pop 失败 |
| test_fifo | 先进先出顺序、计数 |
| test_full_rejects | 满时拒绝写入，且**不破坏已有数据** |
| test_wraparound | 反复填满/清空 **200 轮**，逼索引回绕（取模写错必炸） |
| test_interleaved | 边压边取，模拟"中断收 + 主循环发"的真实节奏 |

- 耗时 **0.38 秒**（含编译），不需要板子
- **双向标定**：把 `rb_push` 的取模漏掉 `-1` → 立刻大量报红；还原后 `ALL OK`
  （顺带发现失败输出会刷上千行，已限制只打印前 8 条，计数照常）

### 三类测试的分工
| 文件 | 验证什么 | 需要板子 |
|---|---|---|
| `test_static.py` | 构建产物结构（符号/预算/向量表），不执行代码 | ❌ |
| `test_host.py` | **在 PC 上执行固件里的真实代码**（同一份 ringbuf.h） | ❌ |
| `test_device.py` | 串口电气/SD 卡握手等**仿真覆盖不到**的行为 | ✅ |

### 真机验证（2026-10-11）
重构后固件烧录 → `Verify OK` → **全量 14 passed**（无板 7 = 构建1+静态5+宿主1 ｜ 真机 7）

## HIL 主控（Stage 7）— ✅ 已验证（2026-10-11）

### 命令
```bash
python tools/hil.py                # 完整轮次：构建 -> 烧录 -> 真机判定 -> (失败则)自动取证
python tools/hil.py --skip-build   # 只重烧+判定
python tools/hil.py --skip-flash   # 只判定
```
退出码：0 = 整轮 PASS；1 = 有步骤 FAIL（**证据已落盘**）

### 一轮包含
| 步骤 | 做什么 | 判据 |
|---|---|---|
| build | `tools/build.py` | 四判据（退出码/0错0警/编译动作数/产物时间戳） |
| flash | `uVision.com -f` | 退出码 0 **且** 日志含 `Verify OK` |
| device-tests | `pytest tests/test_device.py` | 退出码 0（**复用已验证的真机用例，不重复造**） |
| 失败取证 | 任一步失败即触发 | 落到 `runs/<时间戳>_FAIL/evidence/` |

### 证据包内容（`runs/<时间戳>_FAIL/`）
- `evidence/fingerprint.json` — **.axf SHA256 + git HEAD + 是否有未提交改动**（能唯一对上"当时烧的是哪一版"）
- `evidence/serial_monitor.txt` — 串口原始字节（hex + ascii）
- `evidence/debug_snapshot.txt` / `debug_fault.txt` — 运行态快照 / **故障现场**
- `evidence/flash.log` — 烧录日志
- `pytest_output.txt` / `build_output.txt` — 判定输出
- `result.json` — 结构化汇总

### 双向标定（两条失败路径都验过）
| 注入 | 结果 |
|---|---|
| **野指针 Bug**（真机失败） | FAIL；证据自动含 `HardFault(ipsr=3)`、`faulting_pc=0x080002AE=BugDemo_TouchUnmapped+0x6`、`CFSR=PRECISERR\|BFARVALID`、`BFAR=0x30000000`；串口原始字节是 **23104 个 `0x48`('H')**（HardFault 处理程序在刷标志，一眼可判） |
| **语法错误**（构建失败） | FAIL；`build_output.txt` 落盘并记录挂掉的判据 `[FAIL] 日志 0 Error / 0 Warning (2 Error(s), 1 Warning(s))` |

### 正常路径
`build PASS -> Verify OK -> 7 passed in 4.5s`（一条命令）

### 撞到并记录的坑
`-r`（全量重建）**失败时会先删旧 `.axf`** → 失败构建后产物不存在，必须重建。
（Stage 1 已把它记为"已知风险"，这次真的撞上了。）

## CI（Stage 8）— 层1 ✅ 真跑通 ｜ 层2 ⏳ 定义就绪未执行

### 现实约束
仓库**无 remote**；构建依赖 **Windows 独占 + 授权的 Keil MDK**。
=> 托管 runner 跑不了我们的构建，必须 self-hosted。所以"持续"要落在**本地能真跑**的地方。

### 测试分层（`pytest.ini`）
| 标记 | 含义 | 命令 |
|---|---|---|
| `nohardware` | 不依赖任何硬件 | `pytest -m "not hardware"`（**CI 层1**） |
| `hardware` | 需要真机（板子/串口/调试器） | `pytest -m hardware`（**CI 层2**） |

真机用例是好的，但**不能作为"持续"的基础**：硬件一掉线就变成随机红灯，
红灯一多就没人看了。（本工程已经出现过两次 USB 掉线。）

### CI 层1 —— `tools/hooks/pre-commit`（✅ 真跑通）
- 启用：`git config core.hooksPath tools/hooks`
- **hook 存在仓库里，受版本控制**（不是 .git/hooks 里的私有文件，可复现）
- 每次提交自动跑「构建 + 静态契约 + 宿主机单元测试」，**不碰硬件**
- 未通过 → **拦截提交**；临时跳过：`git commit --no-verify`

**双向标定：**
| 场景 | 结果 |
|---|---|
| 正常提交 | `[ci] ✓ 层1 通过`（7 passed, 7 deselected）→ 提交创建成功 |
| 注入语法错误后提交 | `4 failed, 3 passed` → `[ci] ✗ 层1 未通过` → **HEAD 不变，提交被拦下** |

第二条还顺带演示了信号是成串的：构建挂了之后，静态契约测试同时报
"符号集合为空 / .axf 不存在"——**一个真实故障会触发多条互相印证的失败**。

### CI 层2 —— `.github/workflows/ci.yml`（⏳ 尚未执行）
- self-hosted runner；真机层用 `workflow_dispatch` 手动触发
- 失败自动上传 `runs/**/evidence/`（复用 hil.py 的证据包）
- **⚠️ 本仓库无 remote，该文件从未在真实 CI 上跑过** —— 本地等价物是
  `pre-commit`(层1) 与 `tools/hil.py`(层2)，两者都已验证

## CubeMX 配置一致性（Stage 9）— ✅ 已验证（2026-10-11）

### 侦查结论：本工程没有 EDA
仓库内**没有**原理图 / PCB / BOM 文件 —— 它是**现成开发板上的固件工程**。
KiCad 那套（ERC/DRC/BOM/Gerber）**不适用**，如实记录，不硬凑流程。

### 但找到一条真正的硬件侧自动化线索
`SDIO_test.ioc`（CubeMX 的**机器可读配置源**，代码由它生成）。
于是做 **`.ioc` ↔ 代码 一致性检查**：`tools/check_ioc.py`

| 检查项 | 来源 A | 来源 B |
|---|---|---|
| HSE_VALUE | `.ioc` 的 `RCC.HSE_VALUE` | `stm32f4xx_hal_conf.h` 的宏 |
| PLLM / PLLN / PLLQ | `.ioc` 的 `RCC.PLL*` | `main.c` 的 `SystemClock_Config` |
| APB2 分频 | `.ioc` 的 `RCC.APB2CLKDivider` | `main.c` |
| **.ioc 自洽** | 由 HSE/PLLM×PLLN/PLLP 推算 SYSCLK | `.ioc` 自己记录的 `SYSCLKFreq_VALUE` |

### 抓到的真实潜伏问题
```
.ioc   : RCC.HSE_VALUE=25000000, RCC.PLLM=25
代码   : HSE_VALUE=8000000U,      PLL.PLLM=8
```
=> **谁用 CubeMX 打开工程点一下"生成代码"，Stage 3 花几小时修好的时钟会被原样带回来。**
与 `sdio.c` 的 `Init.BusWide 4B→1B` 属同一类陷阱：
**不影响当前构建，只在下次生成代码时静默毁掉修复** —— 最难查的那种。

### 修复
`.ioc` 3 处：`HSE_VALUE 25e6→8e6`、`PLLM 25→8`、`RTCHSEDivFreq_Value 12.5e6→4e6(HSE/2)`
（PLLM 25→8 后 VCO 输入仍是 1 MHz，所有派生频率不变，故只需这 3 处。）

### 双向标定（有个意外的印证）
把 `.ioc` 改回 `PLLM=25` → 测试 FAIL，且**同时报两项**：
1. 项不一致：`PLLM: .ioc=25 代码=8`
2. 自洽失败：**`由 HSE/PLLM/PLLN/PLLP 算出 SYSCLK=53760000`，但它记录的是 168000000**

第 2 条正是 Stage 3 在硬件上实测反推出的 **53.76 MHz**（当时实测约 52.5 MHz）——
**这个静态检查器能独立复现当初的根因。**

### 接入 CI
`tests/test_ioc.py`（`nohardware` 标记）→ 自动进入 pre-commit 层1。
漂移一发生，提交就会被拦住。

## 闭环（Stage 11）— ✅ 已验证（2026-10-11）

### 停止条件（写死在 `tools/hil.py` 里）
- **不做无界重试**：`--attempts` 默认 1、硬上限 3；任一轮 PASS 立即停止
- **绝不自动修改代码**：失败就停下来出报告，**改不改由人决定**
  > 自动化最危险的形式，就是「失败了自己乱改再试」。
- 每步都有 timeout；任一步失败都不继续往下走

### 失败产物（`runs/<时间戳>_FAIL/`）
- `report.md` — **结构化复现报告**：判断依据 / 原始证据清单 / 下一步（由人决定）/
  **本报告不能证明的**
- `evidence/` — 固件指纹、`serial_reset.txt`、`serial_monitor.txt`、
  `debug_snapshot.txt`、`debug_fault.txt`、`flash.log`
- `pytest_output.txt` / `build_output.txt` / `result.json`

### 完整闭环实测（一个只有真机能发现的 Bug）
注入错误波特率 `115200 -> 103000`：

| 层 | 结果 |
|---|---|
| 无板层（build/静态/宿主/IOC） | **8 passed** —— 对这类问题**完全无感** |
| `hil.py` → device-tests | **4 failed** → 自动取证 + 出报告 |

报告自动归纳出的判断依据：
```
串口：软复位后没收到启动标记 USh -> 问题在「串口链路 / 波特率 / 启动阶段」，不是纯软件逻辑
  · 实际收到 1 字节，不同取值数=1（字节数少、取值单一 -> 更像波特率错位后解出的残片）
调试器：不在异常处理中（IPSR=0）-> 固件没崩，是行为不对
调试器：PC 落在 main+0x50 —— 固件没崩、在正常跑
```
**报告自己推出了正确结论**：固件在跑、没崩，问题在串口层。

修复（改回 115200）后重跑 → **PASS（7 passed in 4.44s）**。

### 过程中修掉的「报告自身的 bug」
| 问题 | 真因 | 修法 |
|---|---|---|
| 中文变乱码导致判断分支走错 | 子进程 stdout 在 Windows 默认按 **GBK** 输出，而我们按 UTF-8 解码 | 给子进程设 `PYTHONIOENCODING=utf-8` |
| 误判"固件可能没在跑" | 从 `--monitor` 收 0 字节推断 —— 但**该固件空闲时本来就静默** | 主判据改用 `--reset`（主动激励并期待启动标记） |

> **教训**：报告本身也是要验证的产品。「自动生成的结论」如果错了，
> 比没有结论更危险 —— 它会把人带向错误的假设。

## Regression
- Command: `python -m pytest tests/ -v`
- Expected result: 8 passed
- 状态: **✅ 全部通过**（8 passed / 20.8s）

## 环境事实（已核实 2026-10-09/10）
- git 2.54.0 ✅ | Python 3.12.1 ✅ | pyserial 3.5 ✅
- Keil UV4 @ F:\keil\UV4\ ✅（ARMCC AC5 @ F:\keil\ARM\ARMCC\Bin）
- arm-none-eabi-gcc ❌ | make ❌ | cmake ❌
- STM32CubeProgrammer ❌ | ST-LINK Utility ❌
- 串口设备: **COM9 = USB-SERIAL CH340**（VID_1A86&PID_7523）
- 开发板: STM32F407VETx，**8 MHz 晶振**（实测）
