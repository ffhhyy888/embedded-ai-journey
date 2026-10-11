# AI Embedded Autolab Progress

## Current Stage
**✅ 路线图 12 个阶段全部完成**（2026-10-11）—— 以下是逐阶段记录

**Stage 3 - Device / Serial — 已通过**（2026-10-10）
- 串口链路跑通：`COM9 @115200` 读取 4000 字节，非预期字节 0 个 → 解码 **100.00% 正确** ✅
- 交付物 `tools/serial_tool.py`（连接/发送/解析/timeout/有限重试/结构化 JSON）真机验证：
  冒烟 PASS、回显 PASS、失败路径退出码 **1**、连接错误退出码 **2** —— **三种退出码已双向标定** ✅

**Stage 4 - Automated Test — 已通过**（2026-10-10）
- `tests/` 回归套件真机全绿：**8 passed（20.8s）**
- 覆盖：无硬件构建判定(1) / 正常路径(2) / 边界路径(3) / **异常路径(2)**（退出码 1、2 都亲眼见到）
- **可编程复位已打通**：串口 ESC → `NVIC_SystemReset()`，冒烟不再需要人工按键
- **完成一次完整闭环**：套件抓到"连续输入丢字节" → 证据定位 → 两处根因修复
  （① 中断接收+环形缓冲 ② 发送改非阻塞）→ 回归 8 passed，边界推到 64KB 仍全对

**Stage 5 - Debug — 进行中（侦查已完成，工具已选定）**（2026-10-10）
- 环境侦查结论：**没有 GDB / OpenOCD / STM32CubeProgrammer**
- Keil 官方路径 `UV4 -d 工程 -j0 -o 日志` + 调试初始化文件（`<tIfile>`）：
  文档确认支持自动化，但**本机盲试 4 次均未跑通**（GUI 隐藏时看不到是否弹窗，无法定位）
- **选定路线：pyocd（Python 原生 SWD 调试）** —— 已装 0.45.1，**直接认出 ST-Link，无需改驱动**
  - 已验证：连接目标 / halt / 读全部核心寄存器 / resume
  - 命令集覆盖 Stage 5 全部需求：`break`/`lsbreak`、`reg`(含 `-p` 外设寄存器)、
    `read8/16/32`、`write`、`find`、`savemem`、**`where`(地址→符号/文件/行)**、`disasm`、
    `step`/`go`/`halt`/`reset`、`watch`、还可 `gdbserver start`
- 固件带完整调试信息（`.debug_info`/`.debug_line`/`.symtab`）→ 可做源码级定位

**Stage 5 工具已就位**：`tools/debug_probe.py`（pyocd 封装）
- `--snapshot` 暂停取现场（PC→函数/偏移 + 寄存器 + 可选内存）后自动恢复
- `--break SYM [--reset]` 在符号处下断点、运行到它后取证，再自动恢复
- **硬约束**：任何路径结束都把目标放回 RUNNING（finally 里 ensure_running）
- 真机已验证：`--break main --reset` 精确命中 `main+0x0`；
  `--read` 直接读出环形缓冲内容（`rx_ring` = "zfinal"）与读写索引

**Stage 5 通过**（2026-10-10）：用调试器定位了一个故意注入的野指针 Bug
- 固件在 `main` 里调用 `BugDemo_TouchUnmapped()` 访问未映射地址 `0x30000000`
- 调试器给出：`faulting_pc = BugDemo_TouchUnmapped+0x6`、`BFAR = 0x30000000`、
  `CFSR = BFSR.PRECISERR|BFARVALID`、`HFSR = FORCED`
- 反汇编核对：该地址正是 `LDR r0,[r0,#0]` —— **精确到指令**
- 对比实验：写版本 -> `IMPRECISERR`（写缓冲，压栈 PC 指向 main+0x48，不是出错点）；
  读版本 -> `PRECISERR`（当场报，PC 精确）
- Bug 已移除、重新烧录，**回归 8 passed**

**Stage 6 - Simulation — 已通过（形式是"无板测试"，不是外设仿真）**（2026-10-10）
- 侦查结论：**无宿主机 C 编译器、无 QEMU**；Keil 自带 Simulator 存在但依赖
  `UV4 -d` + 初始化文件 —— 该路径 Stage 5 已实测跑不通 → **外设级仿真在本机不可用**
- 采用 **镜像契约测试** `tests/test_static.py`（5 条，**0.10 秒**，不接板）：
  内存预算(Flash/RAM)、关键符号在位、**演示 Bug 符号不得残留**、
  向量表可启动性(SP/复位向量)、调试信息存在
- **双向标定**：把演示符号塞回固件后该测试准确变红，移除后恢复绿
- **写清了仿真与真机的边界**：结构正确 ≠ 功能正确；串口时序/SD 卡握手/真实时钟
  都在覆盖之外（详见 AUTOLAB_TESTS.md）
- 追加：装上 `ziglang`（pip）拿到宿主机 C 编译器 -> **宿主机单元测试**
  - 把环形缓冲抽成可移植 `Core/Inc/ringbuf.h`，**固件与 PC 测试共用同一份**（不给测试留副本）
  - `tests/host/test_ringbuf.c`：空/满/FIFO/**回绕 200 轮**/边收边发，**0.38 秒**、不接板
  - 双向标定：取模漏掉 -1 -> 立刻报红；还原 -> ALL OK
  - 重构后固件已烧录验证，**全量 14 passed**（无板 7 = 构建1+静态5+宿主1 ｜ 真机 7）

**Stage 7 - HIL — 已通过（2026-10-11）**
- 交付物 `tools/hil.py`：**一条命令**跑完整轮次
  `构建 -> 烧录 -> 真机判定 -> 失败自动取证`，退出码 0/1
- 判定**复用已验证的 pytest 真机用例**，不重复造
- **失败自动取证**（任一步失败都触发）落盘 `runs/<时间戳>_FAIL/`：
  固件指纹(.axf SHA256 + git HEAD + dirty) / 串口原始字节 / 运行态快照 /
  **故障现场** / flash.log / 判定输出
- **两条失败路径都做了标定**：
  1. 真机失败：注入野指针 Bug -> 自动抓到
     `HardFault(ipsr=3)`、`faulting_pc=BugDemo_TouchUnmapped+0x6`、
     `CFSR=PRECISERR|BFARVALID`、`BFAR=0x30000000`；串口原始字节是 23104 个 `'H'`
  2. 构建失败：注入语法错误 -> `build_output.txt` 落盘并记录挂掉的判据
- 正常路径：`build PASS -> Verify OK -> 7 passed in 4.5s`
- 附带发现：`-r` 失败会**先删旧 .axf**（Stage 1 已记为风险，这次真的撞上），需重建

**Stage 8 - CI/CD — 已通过（层1 真跑通；层2 定义就绪）**（2026-10-11）
- 现实约束：仓库**无 remote** + 构建依赖 **Windows 独占的 Keil** → 托管 runner 跑不了
- **测试分层**（`pytest.ini` + 标记）：`hardware` / `nohardware`
  - 层1（无板，7 条）：`pytest -m "not hardware"` —— build 1 + 静态 5 + 宿主 1
  - 层2（真机，7 条）：`pytest -m hardware`
- **CI 层1 真跑通：`tools/hooks/pre-commit`**
  - 用 `git config core.hooksPath tools/hooks` 启用，**hook 存在仓库里、受版本控制**
  - 每次提交自动跑，**不合格直接拦截提交**
  - **双向标定**：正常提交 → `[ci] ✓ 层1 通过`；
    注入语法错误 → `4 failed` → `[ci] ✗ 层1 未通过` → **HEAD 不变（确实没提交进去）**
- **CI 层2 定义**：`.github/workflows/ci.yml`（self-hosted runner，手动触发，
  失败自动上传 `runs/**/evidence/`）—— **⚠️ 尚未在真实 CI 上执行过**（无 remote）

**Stage 9 - Hardware/EDA — 已通过（结论：本项目无 EDA，但找到更有价值的东西）**（2026-10-11）
- **侦查结论：不适用 KiCad 那套** —— 无原理图/PCB/BOM，是现成开发板的固件工程。
  如实记录，不硬凑流程
- **但找到一条真正的硬件侧自动化线索**：`SDIO_test.ioc`（CubeMX 机器可读配置源，
  代码由它生成）→ 于是做了 **`.ioc` ↔ 代码 一致性检查**
- **抓到真实潜伏问题**：`.ioc` 写 `HSE_VALUE=25000000 / PLLM=25`，
  代码是 `8000000 / 8` → **谁用 CubeMX 点一下"生成代码"，Stage 3 修好的时钟会被带回来**
  （与 `sdio.c` 的 `BusWide` 同一类：不影响当前构建，只在下次生成时静默毁掉修复）
- 交付 `tools/check_ioc.py`：逐项比对 HSE/PLLM/PLLN/PLLQ/APB2 分频 +
  **校验 .ioc 自身自洽**
  - 有趣的验证：标定时把 .ioc 改回 PLLM=25，自洽检查**独立算出 SYSCLK=53760000**
    —— 正是 Stage 3 在硬件上实测反推的那个 53.76 MHz
- 修复 .ioc 3 处（HSE/PLLM/RTCHSEDivFreq），并**接入 CI 层1**
  （`tests/test_ioc.py`，双向标定过：改回 25 立刻 FAIL）
- 全量回归 **15 passed**（无板 8 + 真机 7）；pre-commit 自动跑层1 已含该检查

**Stage 10 - Claude Code Engineering — 已通过（2026-10-10/11）**
- 问题：工具都齐了，但用法散落在状态文件和历史对话里 —— 换会话/换 Agent 要重新摸索，
  甚至可能做出危险操作
- 交付 **`CLAUDE.md`**（面向"没有上下文的会话"）：三个坑 / 工具集与退出码语义 /
  build 四判据阈值与出处 / 验证纪律 / 硬件安全红线 / 写死路径与覆盖方式 / 依赖版本
- 外加 `README.md` 人类入口
- **关键做法：不是写完就算，做了两轮「可交接性测试」**
  - 派**无任何上下文的子 Agent，只允许读 CLAUDE.md** 作答并诚实挑刺（这就是
    "判据要双向标定"用到文档上：**文档也是一种判据，也要被检验**）
  - 第一轮挑出 8 处缺口，修了最扎眼的几条（BusWide 缺原因、判据无阈值、
    USh 无解释、路径无处可改、依赖无版本）
  - 第二轮复验指出我那句"工具报不是 git 仓库是它目录不对，别管它"
    **把真实信号提前豁免成了噪声** —— 这条批评是对的，已改成给自证命令
- Git hook 已启用（`core.hooksPath`），提交前自动跑层1，**本轮每次提交都真的拦过**

**Stage 11 - Closed Loop — 已通过（2026-10-11）**
- 补齐 `tools/hil.py` 两块：**结构化复现报告**（`runs/<ts>_FAIL/report.md`：
  判断依据 / 原始证据 / 下一步 / **本报告不能证明的**）+ **明确停止条件**
- **停止条件（刻意写死）**：不做无界重试（`--attempts` 默认 1、硬上限 3，
  任一 PASS 立即停）；**绝不自动修改代码** —— 改不改由人决定
- **完整闭环演示**（一个只有真机能发现的 Bug：波特率 115200→103000）：
  - 无板层 8 条**全过**（对这类问题完全无感 —— 这正是硬件层的价值）
  - `hil.py`: build PASS / flash Verify OK / device-tests **4 failed**
    → 自动取证 → 报告**自己推出正确结论**
  - 修回 115200 → 重跑 → **PASS（7 passed in 4.44s）**
- 过程中还修掉了**报告自身的两个 bug**：子进程 GBK 输出导致的乱码误判；
  以及从 `--monitor` 收 0 字节错误推断"固件没在跑"（该固件空闲时本就静默）

**Stage 12 - Hardening — 已通过（2026-10-11）**
- 审计方式：**AST 静态审计 + 逐项制造反面证据**（不是"看一眼觉得没问题"）
- **修掉 6 处真实脆弱点**，每处带前后对比证据：
  1. 缺 timeout 的 subprocess **4 处 -> 0 处**（最严重的是 build.py 调 uVision 那处：
     编译器挂死会拖死整条流水线，而 pre-commit 每次提交都要跑它）
  2. 写死的 Keil 路径 3 处 -> 全部支持 `AUTOLAB_UVISION` / `AUTOLAB_FROMELF`
  3. 子进程编码不一致（GBK vs UTF-8）-> 统一设 `PYTHONIOENCODING=utf-8`
  4. 脏工作树的固件不可复现 -> 指纹追加 `dirty_files` + `dirty_diff_sha256`
  5. 环境隐性依赖无自检 -> 新增 **`tools/preflight.py`**（双向标定过）
  6. pip 依赖未锁定 -> `requirements.txt`
- 交付 **`docs/HARDENING.md`**：逐项列现状/风险/前后证据，并**如实列出 8 项未修/修不了的**
- 同时把「可移植性」的方法论落成 **`AUTOLAB_ARCHITECTURE.md`**
  （骨架/适配器分层、换工程/换机器/换芯片三张 recipe、经验回写分流规则）

## Current Skill
串口链路验证 + **时序测量法**（用一个不依赖假设的"确定性标尺"反推系统真实参数）

## Current Project
F:\stm32xiangmu\SDIO_test
- MCU: STM32F407VETx（512KB Flash @ 0x08000000）
- **晶振: 8 MHz**（2026-10-10 实测确认；CubeMX 模板原假设 25 MHz，已修正）
- 外设: USART1(115200, PA9/PA10)；SDIO 初始化已恢复在启动路径
- 构建: `python tools/build.py` → PASS/FAIL
- 烧录: `uVision.com -f`（cwd = MDK-ARM）
- 串口: **COM9**（CH340 USB-TTL，VID_1A86/PID_7523 → 板子 USART1）
- 调试: ST-Link（Status=OK）
- Git: 仓库根 = 工程根；提交 1c062a8 → ec45903 → 4970e4b(时钟根因修复) → 2bead5d → **b4b56a6(SDIO 顺序坑)**
- 固件行为: 复位后发 `U S h`，随后回显
- 源文件编码: **GBK**（编辑必须走字节级替换，不能用 UTF-8 编辑器直存）

## Completed
- Stage 0 基线盘点（工程结构 / 工具链 / 产物）
- **Stage 1 Build**：命令行构建 + 失败路径标定（退出码 2）+ 四判据双向验证
- **Git 基线** + 回滚闭环
- **tools/build.py**：一键构建 + 四判据判定 + 时间戳归档日志（L2）
- **Stage 2 Flash**：CLI 烧录（Erase Sectors + Program + Verify + Reset and Run）
- **工具沉淀**：`tools/serial_probe.py`（波特率扫描探针）、`tools/timing_probe.py`（时序探针）
- **Stage 3 里程碑：串口链路跑通（根因分析 → 修复 → 验证）**
  - 症状：任何波特率都解不出数据；115200 下恒定解出 `0x1C`
  - 根因：板上晶振是 **8 MHz**，代码按 25 MHz 配 PLL
    → 实际 SYSCLK 53.76 MHz（以为 168）→ APB2 26.88 MHz
    → UART 实际波特率 **36,872**，PC 按 115200 读 = 比例 3.125 → 恒定乱码
  - 定位手段：让固件按墙钟发"已知长度突发"，PC 只量**时间**（不依赖解码）
    `T_interval = 3.197 s` → 反推 SYSCLK 52.5 MHz → 晶振 8 MHz
  - 修复：`HSE_VALUE = 8000000U` + `PLLM = 8` → SYSCLK 168 MHz → 波特率 115200
  - 验证：`T_interval` 3.197→1.023 s；4000 字节解码 **100.00% 正确**
- **SDIO 顺序坑修复**（用户实测发现，已查源码证实）
  - `sdio.c` 的 `hsd.Init.BusWide` 4B → 1B
  - 机制：`SD_InitCard()` 末尾 `SDIO_Init(hsd->Instance, hsd->Init)` 提前把外设切 4 位，
    而通知卡片的 ACMD6 要稍后 `HAL_SD_ConfigWideBusOperation` 才发 → 顺序反了
- **Stage 3 通过**：串口冒烟(`USh`)/回显真机 PASS；退出码 0/1/2 三种全部标定

## Not Yet Mastered
- **可复用串口脚本**（Stage 3 交付物：连接/发送/解析/timeout/retry/结构化结果）
- 自动测试（Stage 4）、Debug/GDB（5）、仿真（6）、HIL（7）、CI/CD（8）…

## Current Blocker
- 无（2026-10-11：USB 设备重新插好，重构固件已烧录验证，**14 passed**）
- ⚠️ 遗留观察：本工程已两次出现 USB 设备掉线（ST-Link + CH340 一起消失）。
  更像供电/接触/线缆问题而非偶发 —— 建议单独做一次**接线可靠性排查**，
  否则 CI/HIL 阶段会反复咬人
- 无

## 🏁 路线图状态：12 个阶段全部完成（2026-10-11）

`Stage 0 基线 → 1 Build → 2 Flash → 3 Device/Serial → 4 Test → 5 Debug →
 6 Simulation → 7 HIL → 8 CI/CD → 9 Hardware/EDA → 10 CC Engineering →
 11 Closed Loop → 12 Hardening`  **全部通过，且每一阶段都有真机证据。**

## 🚀 平台化（2026-10-11，路线图之后的升级）

**问题**：12 阶段都建在"这台电脑"上 —— **规则**（skill）和**状态**（AUTOLAB_*.md）
都躺在 `~/.claude/` 与用户目录里，**都不在版本控制中**，换机即断。

**改造**：把 `~/embedded-ai-journey/` 变成 **Git 仓库 = 平台本身**
```
skills/    ← 规则（skill 的受版本控制副本）
skeleton/  ← 骨架（与芯片无关：串口观测/编排取证报告/环境自检/CI hook）
adapters/  ← 适配器（按芯片家族；stm32-keil-ac5 已含 PITFALLS.md）
docs/      ← 方法（AUTOLAB_ARCHITECTURE.md）
AUTOLAB_*.md ← 状态（必须留根目录：skill 写死了该路径）
README.md + install.sh
```
- 已在平台仓库提交 3 次（`58290e6` / `68d236f` / `8e59419`）
- `install.sh` **实测通过**：把 skill 复制到 `~/.claude/skills/` 并同步验证一致
- **实测抓到一个真 bug**：`requirements.txt` 里写中文注释会让 `pip install -r`
  **直接崩**（pip 按本机编码 GBK 解码）→ 加 PEP-263 声明修复；
  同一个 bug 在工程仓库里也存在，一并修了（`b35b9f2`）

## Next Micro Task
- **没有下一阶段了** —— 剩下的是"可选深化"，按价值排序：
  1. **抽骨架**：把通用件抽到 `~/embedded-ai-journey/skeleton/`，
     适配器抽到 `adapters/stm32-keil-ac5/`（含 `PITFALLS.md`）
     —— 这是"换工程 / 换芯片"能复用的前提，也是目前最大的未落地项
  2. **补一致性检查的覆盖**：把 `sdio.c` 的 `BusWide` 纳入自动检查
     （目前只查了时钟 —— 见 docs/HARDENING.md 脆弱点 #4）
  3. **排查接线可靠性**（USB 两次掉线，脆弱点 #2）
  4. 行级调试（装 ARM GNU 工具链，脆弱点 #3）

## Acceptance Criteria（若继续）
- 骨架抽取后，**用一个新工程验证**：只拷 skeleton + 换适配器就能跑通无板层
- 每补一项，都要有前后对比证据；未验证的一律标注

## 遗留 / 待办
- ⚠️ **接线可靠性**：本工程已两次 USB 设备掉线（ST-Link + CH340 一起消失），
  建议单独排查（换口/换线/供电），否则 CI/HIL 会反复被咬
- 调试只能到**函数级**（pyocd 的 `where` 在 Keil DWARF 上报错）—— 需要行级时再评估装 ARM GNU 工具链
- 受 CubeMX 生成影响的位置（`sdio.c` 的 BusWide、`main.c` 的 SystemClock_Config、
  `stm32f4xx_hal_conf.h` 的 HSE_VALUE）**都是重新生成时的雷区**；
  目前只对时钟做了自动检查，`BusWide` 尚未纳入
- `tools/timing_probe.py` 依赖"突发固件"，当前固件是回显 → 该工具暂时不可用（保留待用）
- 依赖尚未用 `requirements.txt` 锁定（版本号只在 CLAUDE.md 里）
- 硬件连接状态仍未做过一次完整的图解确认

## Last Verified
2026-10-11

## Notes
- 进度文件记录「人的工程化能力」，与具体工程解耦。
- 当前工程状态见上方 Current Project。
