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
├── tools/                      ← 【平台工具】在本仓库里跑，不拷进工程
│   └── autolab.py autolab_steps.py   ← autolab init（新工程接入）
├── skills/ai-embedded-autolab/ ← 【规则】skill 的受版本控制副本
├── skeleton/                   ← 【骨架】与芯片无关的通用件
│   ├── tools/   serial_tool.py(串口激励观测) hil.py(编排/取证/报告) preflight.py(环境自检)
│   ├── hooks/   pre-commit(提交前自动跑无板层)
│   ├── tests/   test_host.py + host/(宿主机单元测试)
│   ├── templates/  README.md / HARDENING.md / ci.yml 的 .template（待实例化）
│   └── pytest.ini  requirements.txt  CLAUDE.md.template  gitignore.template
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

**推荐路径：`autolab init`。**

> **前置条件**：目标目录里**已经有一个 CubeMX 生成好的工程** ——
> 含 `.ioc`、`MDK-ARM/*.uvprojx`、`Core/`、`Drivers/`。
> 本工具只做**平台接入**，不替你生成工程骨架；目录空着或结构不全，
> 它会直接拒绝（退出码 2），而不是伪造一个看起来像样的工程。

```bash
cd ~/embedded-ai-journey
python tools/autolab.py init \
  --family stm32-keil-ac5 \
  --target <新工程的绝对路径> \
  --project-name <工程名>          # 必须与 .ioc 的 ProjectManager.ProjectName 一致
```

**先加 `--dry-run` 看清单再执行** —— `--dry-run` 不写任何文件，也不在目标目录建任何目录。

它按固定顺序做四件事：

1. **只读探测**目标工程：器件型号、Keil 输出目录/输出名、器件内存区间、串口波特率
   全部**从工程里实测**，不照抄参考工程
2. 校验平台源文件齐全、未被本地改动，且平台 HEAD 已包含最低修复版本（用 git 祖先关系判，不比日期）
3. 逐字节复制骨架与家族适配器（**28** 个文件）+ 实例化 **6** 个模板，
   合计写入 **34** 个文件；再加报告 **2** 份，一次成功初始化共产出 **36** 个文件
4. 跑一次 **L0 自检**，把来源 SHA-256、L0 原始输出与全部 WARN 写进
   `docs/AUTOLAB_INIT.md` 与 `tools/autolab_init.json`

**默认只做「接入 + L0」**。要一次把软件验证闭环跑完，加 `--run-checks`：

```bash
python tools/autolab.py init --family stm32-keil-ac5 --target <目录> --run-checks
```

`--run-checks` 按**固定顺序**追加三步，任何一步失败就**立刻停止后面的**并把它们如实记成
`NOT RUN`（不跳过、不假装跑过、**不伪造构建日志**）：

| 顺序 | 步骤 | 工具 | 说明 |
|---|---|---|---|
| 1 | L0 环境自检 | `tools/preflight.py` | 默认路径也跑这一步 |
| 2 | 构建阈值标定 | `autolab calibrate` | **仅当** `tools/project.cfg` 里还没有生效阈值；跑两次真实全量构建 |
| 3 | L1 正式构建 | `tools/build.py` | 四判据全判 |
| 4 | L2 无板测试 | `pytest -m "not hardware"` | deselect 掉那条会重复构建的用例（L1 已经构建过） |

- `--skip-build`：配合 `--run-checks`，跳过 L1 **与**阈值标定（标定要跑两次构建），保留 L0 与 L2。
  L2 若因缺少构建证据而失败，**如实报失败**，不补日志。
- `--no-run-checks`：全部跳过，报告里记 `NOT RUN`（**不是** `PASS`）。

**它不会做的事**：不烧录、不连接调试器、不开串口、不执行 HIL、不做 Option Bytes；
不碰 Git（不建仓、不 `add`、不提交、不改 `.git/config`）；不修改平台仓库本身。
L3（真机 HIL）在报告里**永远**是 `NOT RUN`。

⚠️ **「初始化成功」≠「完整软件验证成功」**：前者是「36 个文件都写对了」，后者是
「L0–L2 全绿」。前者**不等于**后者，报告里分列，不合并表述。`NOT RUN`（没跑）与
`SKIPPED`（本轮不需要跑）**都不是** `PASS`。

退出码：

| 码 | 含义 | 目标目录 |
|---|---|---|
| `0` | 成功，且**本次请求的**检查全部通过 | 已写入 |
| `2` | 参数 / 平台源 / 模板 / 工程结构有问题 | **零变化** |
| `3` | 已有同名且内容不同的文件，未加 `--force` | **零变化** |
| `4` | 文件已写入，但 L0 / L1 / L2 有未通过的 | 已写入（保留，便于排查） |
| `5` | 阈值标定无法完成（计数不一致 / 构建失败 / 日志无效 / 产物未更新） | 已写入，但 `tools/project.cfg` **逐字节不变**；L1/L2 记 `NOT RUN` |

### 构建阈值标定（`autolab calibrate`）

`build.py` 的判据 3（编译动作条数 ≥ `min_compile_lines`）防的是「增量空转返回 0」的假 0。
这个阈值**只能实测**，不能抄参考工程的数：

```bash
python tools/autolab.py calibrate --target <工程根>
```

它跑**两次真实全量软件构建**（不烧录、不接调试器、不开串口），两次动作数一致且为正、
且退出码 / `0 Error 0 Warning` / 产物时间戳三条判据全过时，取 **`floor(2/3 × 动作数)`**
写入 `tools/project.cfg` 的阈值标记块（例如两次均 24 条 → 16）。只改这一块，其余逐字节不变。

标不出来（两次计数不同 / 任一构建失败 / 日志无效 / 产物未更新）就返回 **5**，
且 `tools/project.cfg` **逐字节不变** —— 不做"猜一个数"式兜底。

⚠️ 阈值只防「空转假 0」，**不代表**「真实增量构建已验证」：某些命令行构建工具每次都全量重编，
"空转"根本没被构造出来过。这种情况只能声明**判定逻辑**已双向标定。

加 `--force` 覆盖前，原件先备份到 `.autolab-init/backup-<时间戳>/` 并**逐字节校验**；
校验不过就整轮失败，绝不覆盖。

**不确定的东西不会被编造**：`--board` 没给就写「未配置」并进 WARN 列表；
`min_compile_lines` 生成时**仍是注释**（`# min_compile_lines = <待标定>`），
理由与标定命令写在生成文件的标记块里；串口的 `port` / `boot_marker` / `reset_cmd_hex`
同样留空 —— 猜一个值比留空更危险，因为猜错会让判据变成"永远绿"。
生成的 `CLAUDE.md` 第三节「本工程的坑」**必须人工填**。

未标定的工程直接跑 `python tools/build.py` 会**报配置错误并退出 1** ——
这是**有意的可见失败**，不是漏配。

<details>
<summary><b>手工接入（应急 / 非 CubeMX 工程）</b></summary>

`autolab init` 是**清单的唯一真相来源** —— 只有它知道哪些文件该逐字节复制、
哪些该实例化后改名。手工照抄这份清单，迟早漂移。

真要走手工路，别凭记忆列文件，让工具把清单打出来：

```bash
python tools/autolab.py init --family stm32-keil-ac5 \
       --target <新工程> --project-name <工程名> --dry-run
```

它会打印全部 **34** 个目标文件 = **28** 份直接拷贝 + **6** 份模板实例化。
一次成功初始化共产出 **36** 个文件：另外 **2** 个是报告，即
`docs/AUTOLAB_INIT.md` 与 `tools/autolab_init.json`（它们**总是**被覆盖，
但覆盖前同样先备份）。其中 **6 个是模板，必须实例化后改名**，不能原样拷：

| 平台模板 | 目标 | 处理 |
|---|---|---|
| `skeleton/CLAUDE.md.template` | `CLAUDE.md` | 替换 `<...>` 占位符 |
| `skeleton/gitignore.template` | `.gitignore` | 替换占位符 |
| `adapters/stm32-keil-ac5/project.cfg.example` | `tools/project.cfg` | **按本工程实测值重填** |
| `skeleton/templates/README.md.template` | `README.md` | 替换 `${VAR}` |
| `skeleton/templates/HARDENING.md.template` | `docs/HARDENING.md` | 替换 `${VAR}` |
| `skeleton/templates/ci.yml.template` | `.github/workflows/ci.yml` | 替换 `${VAR}`，**保留 `${{ ... }}`** |

> ⚠️ **不要 `cp -r skeleton/* <新工程>/`。** 那会把 `.template` 文件原样铺进去
> （`CLAUDE.md.template`、`gitignore.template` 就成了没人认的怪文件），
> 也会把平台自己的目录结构一并复制进工程。

无论走哪条路，最后一步都一样：
**⚠️ 重新标定所有判据 —— 亲手制造一次失败，看它会不会红**（见 `docs/engineering/TEST_AND_RELEASE_POLICY.md`）。

</details>

### C. 换一个新芯片家族

**骨架完全不动**，新建 `adapters/<新家族>/`，按 `docs/AUTOLAB_ARCHITECTURE.md` 第二节的分界线，
只实现四个适配器：**构建 / 烧录复位 / 调试取现场 / 配置源一致性检查**。

> **生态越不成熟，越要靠骨架里的"无板层"** ——
> 国产 MCU 可能根本没有 pyocd/openocd 支持，硬件层只能退回串口甚至人工。

---

## 迁移注意事项

### 新工程：模板要各自实例化一次

**`autolab init` 就是干这件事的**（见上面「B. 新起一个工程」）—— 它会实例化
全部 6 份模板，其中每一份都**必须先改再用**。手工接入的清单也在那一节。

最容易被照抄出错的还是这两份：

```bash
# ⚠️ 仅在手工接入时使用；模板要先实例化，不能原样拷
cp adapters/stm32-keil-ac5/project.cfg.example  <新工程>/tools/project.cfg
cp skeleton/gitignore.template                  <新工程>/.gitignore
```

- `project.cfg` 里照抄参考值（工程名 / 编译动作阈值 / 内存预算 / 必需符号 / 串口）
  会**直接判错** —— 那是 SDIO_test 的值，不是你的。
  `autolab init` 会把能实测的项（工程名 / 输出路径 / 内存预算 / 波特率）按本工程填好；
  **实测不出的项不猜**：`min_compile_lines` 写成注释并附标定命令
  （`autolab calibrate`），串口 `port` 等同样留空并进 WARN 列表
- `.gitignore` 里忘了替换 `<PROJECT_NAME>`，规则就匹配不到任何东西，
  **Keil 的输出目录会进版本库**

### 已有工程升级：`pytest.ini` 与 `hil.py` **必须同批**同步

`pytest.ini` 的 `addopts` 默认排除 `hardware`（裸跑 `pytest` 不碰硬件）。
若只同步它、不同步 `hil.py`，后者跑真机用例时会被全部 deselect →
pytest 返回**退出码 5**（没跑任何用例）→ **HIL 被判 FAIL**。

> **顺序：先 `hil.py`，后 `pytest.ini`；或者一次把两个都换掉。**
> 只同步 `hil.py` 是安全的（多一个 `-m hardware` 无副作用）。

### 串口参数只在 `project.cfg` 里定一次

端口 / 波特率 / 启动标记 / 复位命令统一放工程的 `tools/project.cfg`；
临时换端口用环境变量 `AUTOLAB_PORT` 覆盖即可。

**不要在测试代码里再解析一遍端口** —— 那会变成第二份真相：配置改了却不生效，
而且从现象上看不出来。（完整优先级见 `docs/AUTOLAB_ARCHITECTURE.md` 第二节。）

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
