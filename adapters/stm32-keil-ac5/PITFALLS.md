# STM32 + Keil MDK/AC5 家族的坑

> **这个文件的作用**：下次再用 STM32 + Keil，**这些坑不用再踩一遍**。
> 判断标准：**只对"这个芯片家族 + 这套工具链"成立的**才写这里；
> 跨芯片通用的教训写进 `~/embedded-ai-journey/AUTOLAB_LEARNING.md`。
>
> 全部来自 2026-10-10/11 在一个真实 STM32F407VET6 工程上的实测。

---

## 1. 🔴 晶振频率：**必须实测确认，不要信 CubeMX 默认值**

CubeMX 模板默认假设 **25 MHz**，而很多板子是 **8 MHz**。
配错不会编译失败，而是**整条时钟树按 3.125 倍偏低**：

| | 以为 | 实际 |
|---|---|---|
| SYSCLK | 168 MHz | 53.76 MHz |
| APB2 | 84 MHz | 26.88 MHz |
| **UART 波特率** | 115200 | **36872** |

**可观察现象**：PC 按 115200 读，收到的永远是**同一个垃圾字节**（比例 3.125 导致解码恒定）。
当年花了数小时才定位到 `HSE_VALUE` 这一行。

**三处必须一致**：`stm32f4xx_hal_conf.h` 的 `HSE_VALUE`、`main.c` 的 `PLLM`、`.ioc` 的对应项。
- 8 MHz 配方：`HSE_VALUE=8000000U` + `PLLM=8`（VCO 输入 = 1 MHz，与 25/25 等价，派生频率全不变）
- **验尸工具**：`tools/check_ioc.py` 会比对这三处，并校验 `.ioc` 自身自洽

## 2. 🔴 CubeMX 重新生成会**静默冲掉**手改的修复

这类 bug **不影响当前构建**，只在下次"生成代码"时发作 —— 最难查的一种。
已实测撞到两次：

| 位置 | 被冲成什么 | 后果 |
|---|---|---|
| `stm32f4xx_hal_conf.h` / `main.c` 的时钟 | 回 25 MHz | 时钟整条错，串口全乱 |
| `sdio.c` 的 `Init.BusWide` | 回 `SDIO_BUS_WIDE_4B` | SD 卡初始化失败 |

**`BusWide` 为什么必须是 `1B`**（不是历史遗留）：HAL 的 `SD_InitCard()` 在识别序列末尾会执行
`SDIO_Init(hsd->Instance, hsd->Init)`，按 `Init.BusWide` 把外设**提前切到 4 位**；
而通知卡片切换的 ACMD6 要等后面的 `HAL_SD_ConfigWideBusOperation()` 才发
→ **外设 4 位 / 卡片 1 位，顺序反了**。

> **规矩**：改完生成代码，**回头改 `.ioc`**，并跑一致性检查。
> ⚠️ 目前自动检查**只覆盖时钟**，`BusWide` 尚未纳入 —— 改 `sdio.c` 时人工确认。

## 3. 🟡 Keil 工具链的若干行为

- **`-r`（全量重建）失败时会先删掉旧 `.axf`** → 失败构建后产物**不存在**，
  下游会报"文件找不到"。修好后记得重建。
- **退出码 1 = 有警告**（官方只说"仅警告"，实测：5 条 `#128-D: loop is not reachable` → 1）
- `UV4.exe` 是 GUI 程序（日志要 `-o` 落盘）；**脚本请用 `uVision.com`**（日志直接进 stdout）
- **单靠退出码不可判成功**。要用四判据交叉验证：
  退出码 / `0 Error(s), 0 Warning(s)` / 编译动作数 ≥ 20 / `.axf` mtime 晚于构建启动时刻
  （第 3 条防"增量构建假 0"——什么都没编也返回 0）

## 4. 🟡 调试：pyocd 能用，但**不要动驱动**

- Keil 的 `stlinkdbgwinusb` 驱动下 **pyocd 可直接工作**（实测 0.45.1）
- **不要用 Zadig 把 ST-Link 改绑 WinUSB** —— 会**打断 Keil 的烧录链路**
- pyocd 自带的 `where`（地址→源码行）在 **Keil ARMCC 的 DWARF 上报错**
  → 改用 `fromelf --text -s` 的符号表自己实现「地址 → 函数」（**只能到函数级**）
- **Cortex-M 的故障取证**（很值钱，通用但实现放这里）：
  - 异常帧 `SP+0x00..0x1C` = R0,R1,R2,R3,R12,**LR,PC**,xPSR → `SP+0x18` 就是**出错的那条指令**
  - `CFSR` 在 `0xE000ED28`，`HFSR` `0xE000ED2C`，`BFAR` `0xE000ED38`
  - **写操作触发的总线错误常常是 `IMPRECISERR`（非精确）** —— 写缓冲导致"事后才报"，
    **压栈的 PC 不是出错点**；读操作才是 `PRECISERR`（精确）。
    查"莫名崩溃"时可在可疑写后插 `DSB` 把它逼成精确故障

## 5. 🟡 编解码与 Windows 环境（在 Windows 上做嵌入式几乎必踩）

- **CubeMX 生成的源码常是 GBK**：用普通编辑器/工具改会被存成 UTF-8，
  中文注释整段变化 → 产生**大量假 diff**，淹没真实改动。
  → 用 Python **字节级替换**写回（中文要 `.encode("gbk")`）
- **cmd 的 `%ERRORLEVEL%` 在解析整行时就展开** → `cmd & echo %ERRORLEVEL%`
  读到的是**上一条**命令的退出码。要读真实退出码：**分两行写**
- **子进程 stdout 默认按 GBK 输出**，而调用方常按 UTF-8 解码 → 中文乱码 →
  **字符串比较失败、判断分支走错**（实测踩过：报告把 `Thread(非异常)` 误判成"在异常处理中"）
  → 给子进程设 `PYTHONIOENCODING=utf-8`
- **Git Bash 的 heredoc 会吃掉 Windows 路径里的反斜杠**（`\U`/`\t` 变转义）
  → 补丁**写成 .py 文件再运行**，不要内联

## 6. 🟢 板子/接线层面（不归代码，但会反复咬人）

- 本工程**两次出现 ST-Link 与 CH340 一起从 USB 掉线** →
  表现为 `Flash Download failed - Target DLL has been cancelled` + 串口打不开。
  **这不是固件问题**，先查供电/线缆/USB 口。
- 换 USB 口后**串口号会变**：先查端口，别急着判定"硬件坏了"
  （用 `AUTOLAB_PORT` 覆盖，或跑 `preflight.py`）

---

## 7. 🔴 平台化改造时踩到的三个坑（2026-10-11）

这三条跟"芯片"无关，是**工具/流程**层面的 —— 但和 §5 一样属于"在这个工程里会反复咬人"，
所以在同一个工程里实测出来后就记在这里。

### 7.1 argparse 的 `default=` 会让"没传参"变得不可区分

**现象**：`--port` 原本是 `default="COM9"`。于是工具**永远无法判断**
用户是"没传 `--port`"还是"主动传了 COM9" —— argparse 填进去的默认值
和用户显式传的值长得一模一样。

**后果**：环境变量 `AUTOLAB_PORT` **永远轮不到生效**。
实测过这个具体形态：平台文档写着"串口端口可用 `AUTOLAB_PORT` 覆盖"，
但只有 `test_device.py` 认它，`serial_tool.py` 自己不认。

**改法**：凡是参与优先级解析的参数，`default` 一律设 `None`，
解析完再按 `CLI > 环境变量 > project.cfg > 内置默认` 统一合成。

> 加新参数时留意：任何"带默认值"的开关，都可能悄悄吃掉环境变量或工程配置。

### 7.2 `.gitignore` 不支持"规则 + 行尾注释"

**错的写法** —— 整条规则失效：`#` 后面那串会**变成规则本身的一部分**。

```
runs/                # hil.py 的取证目录
```

**对的写法**：规则独占一行，注释放上一行。

```
# hil.py 的取证目录
runs/
```

只有**行首**的 `#` 才是注释。第一次写 `.gitignore` 的人几乎都会踩这一下。

### 7.3 `pytest.ini` 与 `hil.py` 必须**同批**迁移

`skeleton/pytest.ini` 的 `addopts` 现在默认排除 `hardware`（裸跑 `pytest`
不会碰板子/串口 —— 这是有意的）。

**副作用**：`hil.py` 调 pytest 跑真机用例时**必须显式加 `-m hardware`**。
否则真机用例全部被 deselect，pytest 返回**退出码 5（没跑任何用例）**，
而 `hil.py` 只看 `rc == 0` → **整条 HIL 循环被判 FAIL**。

**实测证据**：不写 `-m hardware` 跑 `test_device.py`，输出就是
`no tests collected (7 deselected)`、`exit=5`。

**规矩**：
- 两个文件**同批同步**，或**先同步 `hil.py`**（它多一个 `-m hardware` 无副作用）
- ⚠️ **只同步 `pytest.ini` 会立刻打断 HIL**

---

## 附：这个适配器里有什么

| 文件 | 作用 |
|---|---|
| `tools/build.py` | Keil 构建 + 四判据（读 `AUTOLAB_UVISION`；工程名/路径/阈值取自工程配置） |
| `tools/flash.py` | Keil 烧录（`uVision.com -f`）+ `Verify OK` 判定；`--dry-run` 只打印命令、**不启动 uVision** |
| `tools/debug_probe.py` | pyocd 封装：快照 / 断点 / **故障现场**（读 `AUTOLAB_FROMELF`） |
| `tools/check_ioc.py` | CubeMX `.ioc` ↔ 代码 一致性 |
| `tools/serial_probe.py`、`tools/timing_probe.py` | 历史诊断工具（时序探针需"突发固件"，当前固件是回显，暂不可用） |
| `tests/test_build.py`、`test_static.py`、`test_ioc.py`、`test_build_paths.py`、`test_flash.py` | 无板层的适配器部分 |
| `tests/test_device.py` | 真机层的串口用例（**跑它必须显式 `-m hardware`**，见 §7.3） |
| `project.cfg.example` | **工程级参数的模板**：拷成 `<工程>/tools/project.cfg` 再用（含工程名、路径、阈值、内存预算、必需符号、串口参数） |

> ⚠️ 这里是**参考实现（快照）**。正在用的副本在各工程里 ——
> 改了行为要**两边同步**，或直接以工程为准、需要时覆盖回来。
