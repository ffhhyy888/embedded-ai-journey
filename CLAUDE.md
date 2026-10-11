# CLAUDE.md — 平台仓库工作须知

这是 **AI 嵌入式研发平台**仓库（skill + 骨架 + 适配器 + 规范 + 状态）。
在这里或在**任何使用本平台规则的工程**里工作时，先读本文件。

---

## 一、规则在哪里（**单一来源，不要在本文件里找全文**）

| 主题 | 文件 |
|---|---|
| 变更范围（最小差异 / 规模红线） | [`docs/engineering/CHANGE_POLICY.md`](docs/engineering/CHANGE_POLICY.md) |
| Git 安全（保护未提交修改 / 回滚 / 禁止清单） | [`docs/engineering/GIT_SAFETY.md`](docs/engineering/GIT_SAFETY.md) |
| 嵌入式 C 规范 | [`docs/engineering/EMBEDDED_C_STANDARD.md`](docs/engineering/EMBEDDED_C_STANDARD.md) |
| Python 自动化脚本规范 | [`docs/engineering/PYTHON_AUTOMATION_STANDARD.md`](docs/engineering/PYTHON_AUTOMATION_STANDARD.md) |
| 测试门禁与放行 | [`docs/engineering/TEST_AND_RELEASE_POLICY.md`](docs/engineering/TEST_AND_RELEASE_POLICY.md) |
| 平台分层/迁移方法 | [`docs/AUTOLAB_ARCHITECTURE.md`](docs/AUTOLAB_ARCHITECTURE.md) |
| 平台用法（换机器/换工程/换芯片） | [`README.md`](README.md) |
| 工作方式（教练模式、阶段推进） | [`skills/ai-embedded-autolab/SKILL.md`](skills/ai-embedded-autolab/SKILL.md) |

> **本文件只给"速查 + 入口"**。完整规则以上面各文件为准；发现冲突时**以 `docs/engineering/` 为准**。

---

## 二、硬约束速查（违反即停）

### 🔒 Git
- **先看再动**：`git status --short` / `git diff --stat` / `git log --oneline -3`
- **未提交的修改是用户的**：不覆盖、不清理、不顺手提交
- **禁止**：`reset --hard`、`git clean`、`checkout .` / `restore .`（宽泛恢复）、
  `push --force`、改写已推送历史、`stash drop/clear`
- **未经授权不 commit、不 push**；提交时**逐个路径** `git add`，不用 `-A`
- 每次改动都要能说清**怎么回滚**

### ✏️ 变更
- **最小合理差异**：只改必须改的，跟所在文件风格一致
- **禁止**：无关重构、全局格式化、顺手升级依赖、批量改注释
- 超过**规模红线**（文件数 > 计划×1.5、新增行 > 200、触及计划外目录）→ **暂停报告**

### 🔌 硬件与生成代码
- 烧录 / 擦除 / Option Bytes / 复位 / 占用串口 → **需人工授权**
- 改生成代码 → **必须回头改配置源**，并跑一致性检查
- 调试会话结束**必须把目标恢复运行**

### ✅ 证据
- **NOT RUN ≠ PASS**：没执行的必须标"未验证"
- **判据必须双向标定过**（亲手制造失败，看它准确报错）
- 失败必须留下：**产物指纹 + 代码版本 + 原始观测**
- 🚫 绝不为了过门禁而删测试 / 放宽断言 / 吞异常 / 跳过检查

---

## 三、任务生命周期（每个任务都走）

```
1. 检查状态   git status / diff / log；确认有无他人的未提交修改
2. 制定计划   要改哪些文件、为什么、怎么回滚；声明"不改什么"
3. 最小修改   只改计划内的；触及红线就停
4. 自动检查   L0 静态 → L1 构建 → L2 无板测试
5. 按需测试   L3 真机（需授权 / 否则显式标 NOT RUN）
6. 审查 diff  每处改动都能对应到任务要求；无计划外文件
7. 报告风险   已验证 / 未验证 / 已知限制 + 回滚方法
```

> 详细判据见 [`docs/engineering/TEST_AND_RELEASE_POLICY.md`](docs/engineering/TEST_AND_RELEASE_POLICY.md)。

---

## 四、这个仓库的结构（怎么改它）

```
skills/ai-embedded-autolab/   ← 【规则】skill 真实来源；改完要跑 install.sh 同步到 ~/.claude/skills/
skeleton/                     ← 【骨架】与芯片无关：跑 install.sh 前先读它上面的 README
adapters/<芯片家族>/           ← 【适配器】含 PITFALLS.md（该家族踩过的坑）
docs/engineering/             ← 【规范】5 份治理文档（单一来源）
AUTOLAB_*.md                  ← 【状态】能力进度（skill 写死了这个路径，不能挪）
CLAUDE.md  README.md  install.sh
```

**新工程** = 拷 `skeleton/`（含 `CLAUDE.md.template`）+ 挑一个 `adapters/<家族>/` +
写工程自己的坑 + **重新标定所有判据**。

---

## 五、常见误区（都是踩过的）

| 误区 | 事实 |
|---|---|
| "我改了文件 = 磁盘上变了" | 编辑器可能改的是编码而非内容；改完先 `git diff` 确认 |
| "测试是绿的所以没问题" | 没标定过的判据只会绿；亲手制造失败它才算数 |
| "仿真通过 = 真机通过" | 串口电气 / SD 卡握手 / 真实时钟都覆盖不到 |
| "requirements.txt 随便写注释" | pip 按**本地编码**读它，中文注释会让安装直接崩 |
| "被 .gitignore 忽略 = 安全" | 忽略 ≠ 保护；文件仍在磁盘上 |
| "这改动很小，不用报告" | 规模红线是**暂停并报告**的触发条件，不是建议 |
