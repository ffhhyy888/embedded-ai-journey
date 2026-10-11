# AI Embedded Autolab Roadmap

## Stage 0 — 基线
把现有 STM32 工程变成 Claude Code 容易理解的项目：明确源码、构建入口、固件产物、烧录方法、运行方法、验证方法和 Git 状态。

## Stage 1 — Build Automation
目标：`build` 一条命令可重复构建。优先使用项目现有构建方式；缺失时逐步引入 CMake/Ninja 或可靠的 CLI 构建入口。

## Stage 2 — Flash / Reset Automation
目标：脚本化下载、Verify、Reset。STM32 优先从 STM32CubeProgrammer CLI 开始，可补充 OpenOCD。

## Stage 3 — Device / Serial Automation
目标：Python 控制真实设备：连接、发送命令、解析响应、timeout/retry、结构化日志。

## Stage 4 — Automated Test
目标：pytest 驱动设备 smoke/regression；C 侧单元测试和静态分析随后引入。每个测试都有前置条件、输入、期望结果、超时和失败证据。

## Stage 5 — Debug Automation
目标：掌握 GDB/SWD 的基础调试能力，再把 backtrace、寄存器、内存、fault 信息暴露给脚本/Agent。

## Stage 6 — Simulation / Emulation
目标：用 Renode/QEMU/native simulation 提供快速回归；明确哪些问题只能真机验证。

## Stage 7 — HIL
目标：PC 驱动真实 DUT，并自动采集真实世界数据。先 UART/GPIO/继电器/简单电源控制，再逐步扩展到示波器、逻辑分析仪、CAN、电子负载等。

## Stage 8 — CI/CD
目标：Git push → Build → Test → Artifact。之后加入 self-hosted runner 和真机 smoke/HIL 回归。

## Stage 9 — Hardware / EDA Automation
目标：优先 KiCad，先做 CLI/脚本化 ERC/DRC/BOM/Gerber 等稳定动作，再让 Agent 调用；不追求第一天就让 AI 自主完成商用 PCB。

## Stage 10 — Claude Code Engineering
目标：让 Claude Code 从“写代码”升级为“调用工程工具”。依次掌握 CLAUDE.md、Skills、MCP、Hooks、Subagents、验证型工作流。原则是工具先稳定，Agent 后接入。

## Stage 11 — Closed Loop
目标：完成至少一次真实 Bug 的修改→Build→Flash→Test→证据→诊断→修复→Regression→Report。

## Stage 12 — Hardening
目标：提高可靠性：可重复环境、版本化产物、结构化日志、超时、设备恢复、权限、故障隔离、失败恢复和指标。

# 推荐技术栈

起步：Git + Python + STM32CubeProgrammer CLI + pyserial + pytest。

之后：GDB/OpenOCD、Renode/QEMU、GitHub Actions、self-hosted runner、KiCad CLI/Python API、Claude Code Skills/MCP/Hooks/Subagents。

# 每阶段的学习目标

不要只追求“会用”。每阶段都应同时得到：

1. 一个概念心智模型；
2. 一个当前项目里的工具/脚本；
3. 一个可以重复运行的验收；
4. 一条可以被 Claude Code 调用的工程路径。
