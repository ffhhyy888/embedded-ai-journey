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
├── CLAUDE.md                   ← 【入口】规则路由 + 硬约束速查（Claude Code 进来自动读）
├── docs/engineering/           ← 【规范】5 份治理文档（单一来源）
│     变更策略 / Git 安全 / 嵌入式 C / Python 自动化 / 测试放行
├── docs/AUTOLAB_ARCHITECTURE.md← 【方法】分层、迁移 recipe、经验分流规则
├── AUTOLAB_PROGRESS.md         ← 【状态】能力进度（⚠️ skill 依赖此固定路径）
├── AUTOLAB_LEARNING.md         ← 【状态】跨工程的学习记录
├── AUTOLAB_TESTS.md            ← 【状态】测试契约
└── AUTOLAB_DECISIONS.md        ← 【状态】工程决策
```

⚠️ **`AUTOLAB_*.md` 必须在仓库根**：skill（`SKILL.md` 第 45 行起）写死了这个路径。
想改结构，得同时改 skill。

---

## 工程治理规范（AI Coding Governance）

平台不只提供工具，也提供**约束**：Claude Code 在任何工程里工作时，
必须遵守 [`CLAUDE.md`](CLAUDE.md) 里指向的 5 份规范（变更范围 / Git 安全 /
嵌入式 C / Python 自动化 / 测试门禁）。规范全文只有一份，在 `docs/engineering/`。

**统一任务生命周期**：检查状态 → 制定计划 → 最小修改 → 自动检查 → 按需测试 →
审查 diff → 报告风险（含回滚方法）。

**硬约束速查**：
- 未提交的修改是用户的 —— 不覆盖、不清理、不顺手提交
- 禁止 `reset --hard` / `git clean` / `checkout .` / `force push` / 改写已推送历史
- 未经授权不 commit / push
- **NOT RUN ≠ PASS**；判据必须双向标定过
- 烧录 / 擦除 / Option Bytes / 复位 / 占用串口 → 需人工授权

### 启用平台提交门禁（新电脑克隆后做一次）

平台自带一个**提交前门禁**（`.githooks/pre-commit`，只做确定性检查，
**不依赖任何芯片工具链** —— 不需要 Keil / pyocd / 串口）：

```bash
git -C ~/embedded-ai-journey config core.hooksPath .githooks
```

它检查两件事：

1. **暂存内容的空白错误** —— `git diff --cached --check`（尾随空格等，零误报）
2. **治理规则一致性** —— 调 `tools/check_governance.py`

> ⚠️ 第 2 项检查的是**暂存内容**：hook 先把 index 导出到临时目录再检查，
> 所以"工作区正常但暂存内容不一致"的情况**也会被抓到** ——
> 检查结果只代表**将要提交的那份**，与工作区是否干净无关。

失败时**拦截提交**并给修复提示；临时跳过：`git commit --no-verify`；
停用：`git config --unset core.hooksPath`。

> **首次使用注意**：治理文档（`CLAUDE.md` / `docs/engineering/` 等）
> 必须**已经进入暂存区**，否则门禁会报"规范文档缺失" —— 这是有意的：
> 提交里不能缺规则。

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
