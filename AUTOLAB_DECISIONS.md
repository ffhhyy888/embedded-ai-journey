# Engineering Decisions

## 2026-10-11 — 可移植性：**骨架 / 适配器分离**，且**不依赖 AI 记忆**

Context:
用户提出三个现实问题：换工程文件夹怎么办、换电脑怎么办、换芯片家族（含生态不成熟的
国产芯片）还适用吗。核对后发现两个关键事实：
- **AI 的项目记忆是按工程路径隔离的**，换文件夹不会带过去；而且它**不可版本控制、不可审阅**
- 本工程至今**没有往那里写过任何东西** —— 这套闭环活在**工程仓库**和
  `~/embedded-ai-journey/` 的文件里，不在"记忆"里

Decision:
1. **持久化只认"目录 + 文件"，不认 AI 记忆**（记忆会丢、不可审阅、按路径隔离）
2. 把闭环拆成 **骨架（通用）/ 适配器（按芯片家族）** 两层：
   - 骨架：串口观测、判定、取证、报告、停止条件、CI、宿主机单元测试
   - 适配器：构建、烧录/复位、调试取现场、配置源一致性检查
3. 目录结构：`~/embedded-ai-journey/skeleton/`（通用件）
   + `~/embedded-ai-journey/adapters/<芯片家族>/`（适配器 + 该家族的坑）
   → **新工程 = 拷 skeleton + 挑一个 adapters + 重写 CLAUDE.md 的坑**
4. 经验回写的**分流规则**：按「下一个用别的芯片的项目还用得上吗」判断 ——
   用得上 → `AUTOLAB_LEARNING.md` / `AUTOLAB_ARCHITECTURE.md`；
   用不上 → `adapters/<家族>/PITFALLS.md` 或工程的 `CLAUDE.md`

Why:
- **记忆不可审阅、不可版本控制，且按工程路径隔离** —— 把它当唯一载体，换环境就断。
- **骨架和适配器的分界线是稳定的**："怎么造/怎么烧/怎么看寄存器"随芯片变；
  "怎么激励、怎么判、怎么留证据、怎么停"不随芯片变。
- **生态越不成熟，骨架越重要**：硬件层可能根本没有好工具
  （例如 pyocd/openocd 对国产片支持缺失），只能靠"无板层"兜住大部分验证。

Rejected alternatives:
- **靠 AI 记忆承载**：换文件夹即失效，且不可审阅、不可回归。
- **每个新工程从头来一遍**：把已经验证过的编排/取证/CI 逻辑重写一遍，纯浪费，
  而且**判据的标定纪律最容易在重写时丢掉**。
- **强行做"一键迁移"**：只要 Keil 还在链路里就不可能（系统级安装+授权）。
  正确目标是"人工只做装 Keil + pip install + 跑一次环境自检"。

Impact:
- 新增 `AUTOLAB_ARCHITECTURE.md`（方法本身：分层、三种迁移的 recipe、目录结构、
  经验分流规则）
- 已落地：`requirements.txt`（锁定 pip 依赖）
- **待落地**：把写死的本机路径改成"环境变量+默认值"（`debug_probe.py` 的 `FROMELF`、
  `hil.py` 的 `UVISION` 尚无覆盖入口）；`tools/preflight.py` 环境自检；
  `skeleton/` 与 `adapters/` 的实际抽取
- **教训**：**不要指望"记忆"承载可复用的工程能力 —— 要指望"可版本控制的文件"。**

## 2026-10-11 — 交接文档要用「无上下文的外部消费者」检验

Context:
Stage 10 要交付 `CLAUDE.md` —— 让**没有历史对话上下文**的会话也能正确、安全地工作。
难点：**作者本人看不出自己文档缺什么**，因为缺的部分在他脑子里是"显然的"。

Decision:
写完不罢休。**派一个真的没有上下文的子 Agent，只允许读 `CLAUDE.md`，让它作答并挑刺**；
按反馈修补后再复验一轮。

Why:
这与"判据必须双向标定"是同一件事 —— **文档也是一种判据**，只会"看起来完整"的文档
等于没写。必须让一个真实的外部消费者去用它，看它会不会做错。
实测有效：第一轮挑出 8 处缺口（含"只说怎么做、不说为什么"的 BusWide、
"称四判据却不给阈值"）；第二轮更指出**我修补时新引入的一个错误** ——
我写"工具报不是 git 仓库是它目录不对，别管它"，
这**把真实信号提前豁免成了噪声**，且给了我证明不了的归因。

Rejected alternatives:
- **自己反复读几遍**：作者视角，看不出缺失项（这是问题的根源，不是解法）。
- **无限复验直到挑不出刺**：收益递减；剩下的多属"单文件无法自证"的项，
  正确做法是**在文档里给自证命令**，而不是继续加描述。

Impact:
- `CLAUDE.md` 现在是可交接的，且每条关键事实都配了**自证命令**
  （而不是让读者接受一个结论）
- **一般性规则**：**不要用"这不是问题"去消除矛盾** —— 给自证手段，让读者自己判定；
  替他判定是最危险的写法
- 该做法可复用到任何交接材料（README、测试契约、决策记录）

## 2026-10-10 — Stage 6 用「镜像契约测试」而非外设仿真

Context:
Stage 6 的目标是"至少一个测试可在无实体板情况下运行"。侦查本机可用资源：
- 宿主机 C 编译器（gcc / cl / clang / tcc）：**一个都没有** → 无法做 host 端单元测试
- QEMU：未安装，且没有 STM32F407VET6 机型
- Keil 自带指令集 Simulator：`SarmCM3.dll` 确实存在，但要用它必须改 uvoptx 的
  `<uSim>`，并依赖 `UV4 -d` + 调试初始化文件那条路 —— **Stage 5 已实测该路径跑不通**
- 而且即便跑起来，本固件也走不远：`MX_SDIO_SD_Init()` 需要一个**真实 SD 卡**响应
  CMD0/CMD8/ACMD41，仿真器没有卡模型 → 会直接掉进 `Error_Handler`

Decision:
不采用外设仿真，改为 **镜像契约测试** `tests/test_static.py`：对构建产物做结构性检查
（内存预算 / 关键符号在位 / 演示代码不残留 / 向量表可启动性 / 调试信息存在），
并把"仿真能证明什么、不能证明什么"写成显式文档。

Why:
- 有些错误**根本不需要上板**就能发现，而且应该在做任何硬件操作**之前**拦住 ——
  例如"内存超了"、"演示用的 Bug 忘了移除"、"向量表写坏导致根本起不来"。
  这些用 0.1 秒的静态检查拦下来，比烧到板子上再排查便宜得多。
- 与其投入大量时间去打通一条（已知受阻的）仿真路径，不如先把**能稳定拿到的验证**建好。
- 外设仿真对本项目的覆盖度本来就低（串口电气、SD 卡握手、8MHz 真实时钟都不在里面），
  投入产出比不高。

Rejected alternatives:
- **继续尝试打通 Keil Simulator**：`-d` 路径已实测失败且无任何错误反馈；
  即使打通，SdIO/串口仍需真实外设，能验证的内容有限。
- **装 MinGW 做 host 单元测试**：可行，但要下载 ~100MB 工具链；
  且需要把固件逻辑抽成可移植模块（重构）。**留作备选**，等真有复杂纯逻辑要测时再做。
- **装 QEMU**：没有本板机型，跑起来也不是同一套外设行为，价值更低。

Impact:
- 无板验证由 5 条静态测试承担（0.10 秒），并已双向标定（能红）
- 明确写入：**仿真通过 ≠ 真机通过**；功能正确性仍必须由 `tests/test_device.py` 上板验证
- 若将来需要真正的 host 单元测试，最小路径是装 MinGW + 抽出纯逻辑模块

## 2026-10-10 — 调试通道选 pyocd，而非 Keil 的 `-d` 调试脚本

Context:
Stage 5 需要 breakpoint / 寄存器 / 内存 / 符号定位能力。环境侦查结果：
- 没有 GDB、OpenOCD、STM32CubeProgrammer、ST-Link Utility
- 有 ST-Link 硬件 + Keil MDK（GUI 调试器）+ 完整调试信息（`.debug_info`/`.symtab`）
- Keil 官方文档支持 `UV4 -d 工程 -j0 -o 日志` + 调试初始化文件（`.uvoptx` 的 `<tIfile>`）
  做自动化调试，且有完整调试命令语言（`BreakSet`/`Go`/`EVALuate`/`DISPLAY`/`EXIT`…）
- 实测：该路径**盲试 4 次均未跑通**（初始化文件未被执行；`-j0` 隐藏 GUI 时看不到是否弹窗）

Decision:
采用 **pyocd**（Python 原生的 SWD 调试库，`pip install pyocd`）作为调试通道。

Why:
- 已实测可用：0.45.1 直接认出 ST-Link（UID `39FF6C06...`），能连接 / halt /
  读全部核心寄存器 / resume —— **且不需要更换驱动**（Keil 的烧录链路不受影响）
- **返回明确错误**：Keil `-j0` 路径失败时无任何反馈，pyocd 会告诉你哪条命令不认
  （实测 `resume` 不是命令名，立即报 `unrecognized command`）
- Python 原生 → 可被脚本/pytest/后续 Agent 直接调用，符合 Autolab "先建稳定工具" 的思路
- 命令集覆盖 Stage 5 全部需求：`break`/`lsbreak`、`reg`(含 `-p` 外设寄存器)、
  `read/write/find/savemem`、**`where`（地址→符号/文件/行）**、`disasm`、`step/go/halt/reset`、
  `watch`，且可 `gdbserver start` 以后再接 GDB

Rejected alternatives:
- **继续盲试 Keil `-d -j0`**：连续 4 次无反馈，成本高、信息量为零。
  （保留结论：该机制存在，但本机环境未跑通；Keil GUI 手动调试仍可用。）
- **装 arm-none-eabi-gdb + OpenOCD（完整 GDB 链路）**：下载体积大、环节多；
  pyocd 已能覆盖 breakpoint/寄存器/内存/符号，GDB 需要时可用 pyocd 的 gdbserver 再补。
- **用 Zadig 把 ST-Link 改绑 WinUSB**：有**打断 Keil 烧录链路**的风险，不可取 ——
  实测 pyocd 在现有驱动绑定下即可工作，无需改驱动。

Impact:
- Stage 5 及后续（HIL、闭环）的调试能力建立在 pyocd 上，可脚本化、可回归
- **一般性教训**：选工具时，"失败时能否给出错误信息" 和 "功能列表" 同样重要

## 2026-10-10 — 串口数据通道改为「中断接收 + 环形缓冲 + 非阻塞发送」

Context:
回归套件抓到缺陷：一口气发 128 字节只收回 117（丢 11）；逐字节发则全对。
根因是回显用阻塞轮询（收 1 字节 → `HAL_UART_Transmit` 阻塞），收发同速时抖动即溢出。
第一版修复只加了中断接收 + 环形缓冲，128~2048 字节通过，但**4096 起又开始丢**
（丢 123 / 6.1% @8192 / 7.7% @16384）—— 丢失比例随长度递增，是"缓冲被持续填满"的签名。
真正的限速点是**发送端仍在等 TC**（~87µs），使"发"比"收"慢约 7%。

Decision:
- **接收**：`HAL_UART_Receive_IT` + `HAL_UART_RxCpltCallback` 入队并重新武装；
  256 字节环形缓冲；补 `HAL_UART_ErrorCallback` 清 ORE 并重新武装（否则溢出后通道永久停摆）
- **发送**：不等 TC，只等 `TXE` 直接写 `USART1->DR`

Why:
数据通道的吞吐上限由**较慢的一端**决定。"加缓冲"只能吸收突发，不能改变平均速率；
只要生产快于消费，缓冲迟早填满。把发送从"等 TC"改成"等 TXE"后发送速率远高于输入，
缓冲几乎不再积压 —— 修复后 128 / 1024 / 4096 / 16384 / **65536** 字节全部逐字节一致。

Rejected alternatives:
- **只加环形缓冲、把缓冲开大**：治标。丢失比例随长度递增，说明是持续积压，
  缓冲再大也只是把溢出点往后推。
- **主机端分块发送 / 加延时**：把设备缺陷转嫁给调用方，且掩盖了真实吞吐能力。
- **改测试的载荷长度让它通过**：等于删掉证据（本轮已明确拒绝这种做法）。

Impact:
- 串口通道可承受任意长度连续突发（实测到 64 KB）
- 新增 `test_echo_4k_burst` 锁住该边界，防止回退
- **一般性教训**：判断丢数据是"随机抖动"还是"持续积压"，看**丢失比例是否随数据量增长**

## 2026-10-10 — SDIO 初始化：`Init.BusWide` 4B → 1B（CubeMX 模板顺序坑）

Context:
用户在实际调试中发现：CubeMX 生成的 `MX_SDIO_SD_Init()` 里
`hsd.Init.BusWide = SDIO_BUS_WIDE_4B;` 会导致 SD 卡初始化失败；
改成 `SDIO_BUS_WIDE_1B` 后成功。
查 HAL 源码（`Drivers/STM32F4xx_HAL_Driver/Src/stm32f4xx_hal_sd.c`）确认了机制：
- `SD_InitCard()`（:2673）在**识别序列末尾**执行 `(void)SDIO_Init(hsd->Instance, hsd->Init);`（:2752）
  → 把 `hsd->Init` 整体下发外设，**外设提前切到 4 位**
- 而通知卡片切换到 4 位的 ACMD6，要到后面 `HAL_SD_ConfigWideBusOperation()`
  → `SD_WideBus_Enable()` 里才发（`SDMMC_CmdAppCommand` + `SDMMC_CmdBusWidth(2)`）
- 结果：**外设 4 位 / 卡片 1 位**，顺序反了

Decision:
`Core/Src/sdio.c`: `hsd.Init.BusWide` 由 `SDIO_BUS_WIDE_4B` 改为 `SDIO_BUS_WIDE_1B`。
4 位切换仍由其后的 `HAL_SD_ConfigWideBusOperation(&hsd, SDIO_BUS_WIDE_4B)` 完成，
**最终总线宽度不变**，只是把切换挪到了正确的位置（先 ACMD6 通知卡片，再改 CLKCR）。

Why:
识别阶段本就必须在 1 位下进行（HAL 的 `HAL_SD_InitCard()` 自己也用局部 `Init`
强制 1 位 + `SDIO_INIT_CLK_DIV`）。让 `hsd->Init.BusWide = 4B` 在识别刚结束就
把外设切 4 位，属于模板生成的顺序缺陷，对某些卡会直接导致初始化失败。

Rejected alternatives:
- **不改，靠后面的 `BusWide_Operation` 兜回来**：顺序反了就是反了，
  依赖"恰好没坏事"不是工程做法。
- **在 `HAL_SD_Init()` 之外改成先 `BusWide_Operation(1B)` 再切 4B**：
  多绕一步，不如直接把 `Init.BusWide` 写对。

Impact:
- SD 卡初始化在真机上通过（串口冒烟收到 `S` 标记，未触发 `Error_Handler`）
- **注意**：`sdio.c` 是 CubeMX 生成文件，**下次用 CubeMX 重新生成会被覆盖**。
  重新生成后必须检查这一行是否又变回 `4B`。
- 教训：判断某个配置字段"有没有用"，必须 grep 到它的**全部**使用点再下结论
  （只看 `HAL_SD_Init()` 会误判为 no-op）。

## 2026-10-10 — 修正时钟配置：HSE 25 MHz → 8 MHz（本轮根因修复）

Context:
串口在任何波特率下都解不出数据，115200 下恒定解出同一个字节 `0x1C`。
用 `tools/timing_probe.py` 做**不依赖解码**的时序测量：
固件发 256 字节突发 + `HAL_Delay(1000)`，PC 只量突发周期。
实测 `T_interval = 3.197 s`（应 1.000 s）→ 反推实际 SYSCLK 52.5 MHz、
晶振 ≈ 8 MHz。与代码假设（`HSE_VALUE=25MHz`、`PLLM=25` → 168 MHz）差 3.125 倍。

Decision:
- `Core/Inc/stm32f4xx_hal_conf.h`: `HSE_VALUE` 25000000U → **8000000U**
- `Core/Src/main.c` `SystemClock_Config()`: `PLLM` 25 → **8**
  （`PLLN=336 / PLLP=2 / PLLQ=7` 不变 → SYSCLK=168 MHz，SDIO/USB 48 MHz）

Why:
板上晶振是 8 MHz。原配置把 VCO 输入压到 0.32 MHz（规范要求 1~2 MHz），
PLL 虽然锁住了，但整条时钟树按 25/8 的比例整体偏低：
- APB2 = 26.88 MHz → UART 实际波特率 **36,872**（不是 115200）→ PC 解码必乱
- `HAL_Delay(1000)` 实际耗时 3.197 s
- SDIO 的 48 MHz 同样错 → 很可能是 SD 卡初始化失败的真正原因
改成 `PLLM=8` 后 VCO 输入回到规范的 1 MHz，SYSCLK 回到 168 MHz，全链路一起正确。

Rejected alternatives:
- **命令行盲扫波特率（300~400000 全扫）**：已实测失败。多未知数下扫参数是错的方向。
- **写仿真反推波特率**：三次仿真给出三套互相矛盾的结论，全部错误。仿真无法解决
  "连未知量个数都没搞清楚"的问题。
- **只改 `HSE_VALUE` 为 8e6、不动 `PLLM`**：能让 HAL 算对波特率（BRR≈233），
  但 SYSCLK 仍是 53.76 MHz、VCO 输入仍 0.32 MHz（超规范），性能与稳定性都不对。
- **优先怀疑硬件（CH340/USB-TTL/接线）**：串口设备身份、线序、供电均已排查正常。
  在拿到时序证据前，硬件怀疑是"武断归因"。

Impact:
- 串口 115200 恢复正常：4000 字节解码 **100.00% 正确**
- `HAL_Delay`、SysTick、SDIO 时钟一并归位
- **教训固化**：代码里每一个"关于硬件的假设"（晶振频率、供电、外设型号）都是
  未验证的隐含前提。以后遇到"整体按比例错"的现象，优先怀疑时钟树假设。
- 遗留：SDIO 初始化目前仍从启动路径摘除（用户要求），时钟修好后应恢复验证

## 2026-10-09 — 构建入口选用 uVision.com，判据采用四重交叉验证

Context:
实测 Keil 命令行构建时发现两个问题：
1. 增量构建 `-b` 返回退出码 0，但实际未编译任何文件（"无需编译"的假 0）。
2. 同样的成功构建，在 cmd 下返回过 1，而 bash / Python / .bat / uVision.com 下均返回 0，未能复现。

Decision:
- 构建入口统一使用控制台版 `F:\keil\UV4\uVision.com`（日志直接进 stdout，便于脚本捕获）。
- 构建 PASS 判据采用四条同时成立：退出码 == 0 ∧ 日志匹配 `0 Error(s), 0 Warning(s)`
  ∧ 日志含 ≥20 条 compiling/linking ∧ 产物 .axf 时间戳晚于命令启动时刻。

Why:
单一退出码在真实工具链上已被证明不可靠。多证据交叉验证把"工具返回码"与"实际发生的物理结果"
分开检查，任何一条不符即可判定异常，避免自动化闭环建立在虚假的成功之上。

Rejected alternatives:
- 只用退出码：被上述两次实测直接证伪。
- 改用 arm-none-eabi-gcc + Makefile 重写构建：改动面过大，且会丢失 Keil 专有的 AC5 编译行为；
  Stage 1 阶段应先把现有工具链编排稳，而不是更换工具链。
- 用 `UV4.exe` 作为标准入口：GUI 版日志必须落盘再读取，多一次 IO 且编码为 GBK；
  `uVision.com` 直接给 stdout，更简单。

Impact:
- 后续所有构建类自动化（build 脚本、CI、Agent 调用）都基于此判据实现。
- 退出码 1 的成因已由 2026-10-10 的失败标定补全：**有警告即返回 1**。

## 2026-10-08 — 进度文件使用固定全局目录

Context:
Autolab skill 与 Entrepreneur skill 都需要持久的进度/状态文件，但两者会被在不同工程目录下调用。

Decision:
所有进度文件统一放在 `~/embedded-ai-journey/`，不放在各个工程目录里。

Why:
能力进度属于「人」，不属于「某个工程」。固定目录让跨项目的连续性和复盘成为可能。

Rejected alternatives:
- 放在当前工程目录：换目录后进度断裂，且会污染工程 Git 仓库。
- 放在 skill 目录内：skill 升级/重装时容易丢数据。

Impact:
Skill 中被调用时必须读写该固定目录；进度与具体工程解耦，工程状态另行查看。
