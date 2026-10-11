# AI 嵌入式研发平台（Autolab）

一套**可跨电脑、跨工程、跨芯片复用**的 AI 辅助嵌入式研发工作流。

> 从"某台电脑上的一个 Claude Code Skill"升级成"一个可以 `git clone` 的仓库"——
> 因为 **skill 和进度原本都躺在 `~/.claude/` 里，不在版本控制中**，换机就断。

---

## 一句话说明它解决什么

让 AI（Claude Code）在嵌入式工程里**可靠地做事**，而不是每次重新摸索：

| 问题 | 这套东西怎么解决 |
|---|---|
| AI 换个会话就不懂这个工程 | `CLAUDE.md`（工程内）+ 本仓库的 skill（规则） |
| 换了电脑，一切重来 | 本仓库 `git clone` + 一条 `install.sh` + `preflight.py` 自检 |
| 换芯片就得重做一遍 | **骨架/适配器分离**：通用件不动，只换 `adapters/<家族>/` |
| 踩过的坑反复踩 | 坑按"家族/工程/方法"三级归档（见下） |
| 任务做完没有证据 | 工具链自带判据、取证、报告、停止条件 |

---

## 仓库结构

```
embedded-ai-journey/            ← 本仓库（平台）
├── README.md                   ← 你正在读
├── install.sh                  ← 新机器一键安装
├── skills/ai-embedded-autolab/ ← 【规则】skill 的受版本控制副本
├── skeleton/                   ← 【骨架】与芯片无关的通用件
│   ├── tools/   serial_tool.py(串口激励观测) hil.py(编排/取证/报告) preflight.py(环境自检)
│   ├── hooks/   pre-commit(提交前自动跑无板层)
│   ├── tests/   test_host.py + host/(宿主机单元测试)
│   └── pytest.ini  requirements.txt
├── adapters/                   ← 【适配器】按芯片家族
│   └── stm32-keil-ac5/         ← 本仓库目前有的一套（含 PITFALLS.md）
├── docs/AUTOLAB_ARCHITECTURE.md← 【方法】分层、迁移 recipe、经验分流规则
├── AUTOLAB_PROGRESS.md         ← 【状态】能力进度（⚠️ skill 依赖此固定路径）
├── AUTOLAB_LEARNING.md         ← 【状态】跨工程的学习记录
├── AUTOLAB_TESTS.md            ← 【状态】测试契约
└── AUTOLAB_DECISIONS.md        ← 【状态】工程决策
```

⚠️ **`AUTOLAB_*.md` 必须在仓库根**：skill（`SKILL.md` 第 45 行起）写死了这个路径。
想改结构，得同时改 skill。

---

## 三种用法

### A. 换一台新电脑

```bash
git clone <本仓库> ~/embedded-ai-journey
cd ~/embedded-ai-journey && ./install.sh        # 装 skill + 检查环境
```

`install.sh` 会：
1. 把 `skills/ai-embedded-autolab/` 复制到 `~/.claude/skills/`（Claude Code 只认那里）
2. 检查 Python / pip 依赖（缺失时给 `pip install -r skeleton/requirements.txt`）
3. 提示还需要人工装的部分（**Keil 这类系统级工具链，`pip` 装不了**）

然后在具体工程里跑 `python tools/preflight.py` 做环境自检。

### B. 新起一个工程（同芯片家族）

```bash
# 1) 拷骨架 + 对应家族的适配器进新工程
cp -r ~/embedded-ai-journey/skeleton/*            <新工程>/
cp -r ~/embedded-ai-journey/adapters/stm32-keil-ac5/* <新工程>/
# 2) 写工程自己的 CLAUDE.md（坑要各工程自己填）
# 3) ⚠️ 重新标定所有判据 —— 亲手制造一次失败，看它会不会红
```

### C. 换一个新芯片家族

**骨架完全不动**，新建 `adapters/<新家族>/`，按 `docs/AUTOLAB_ARCHITECTURE.md` 第二节的分界线，
只实现四个适配器：**构建 / 烧录复位 / 调试取现场 / 配置源一致性检查**。

> **生态越不成熟，越要靠骨架里的"无板层"** ——
> 国产 MCU 可能根本没有 pyocd/openocd 支持，硬件层只能退回串口甚至人工。

---

## 坑往哪写（这条最容易忘）

| 坑的类型 | 写到哪里 |
|---|---|
| **这个芯片家族的**（时钟、外设怪癖、厂商工具） | `adapters/<家族>/PITFALLS.md` |
| **这个工程的**（板子接线、业务逻辑） | 该工程的 `CLAUDE.md` |
| **方法层面的**（怎么测、怎么判、怎么不踩） | `AUTOLAB_LEARNING.md` |
| **一个决定及其取舍** | `AUTOLAB_DECISIONS.md` |

**判断标准一句话**：**"下一条用别的芯片的项目，还用得上吗？"**
用得上 → 往上走（LEARNING / ARCHITECTURE）；用不上 → 往下沉（adapters / 工程 CLAUDE.md）。

---

## 与 AI 记忆的关系（别搞混）

| 载体 | 换电脑 | 可版本控制 | 说明 |
|---|---|---|---|
| 本仓库（skill + 骨架 + 适配器 + 状态） | 需 `git clone` | ✅ | **真正的载体** |
| `~/.claude/skills/` | ❌ | ❌ | Claude Code 只从本地目录加载 skill，所以 `install.sh` 要复制一份过去 |
| AI 的"项目记忆"（`~/.claude/projects/<路径>/memory/`） | ❌ **按工程路径隔离** | ❌ | **不要指望它承载这套东西** |

> **核心原则：持久化只认"可版本控制的文件"，不认"记忆"。**
> 记忆会丢、不可审阅、按路径隔离；文件不会。

---

## 当前状态

- 已在真实工程 `F:\stm32xiangmu\SDIO_test`（STM32F407VET6 + Keil AC5）上跑通完整闭环：
  `构建 → 烧录 → 判定 → 失败自动取证 → 报告`，并接入了提交前自动检查
- 已知脆弱点（未修/修不了）见该工程的 `docs/HARDENING.md`
- 进度详情见 `AUTOLAB_PROGRESS.md`
