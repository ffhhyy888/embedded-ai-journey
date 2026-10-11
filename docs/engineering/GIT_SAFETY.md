# Git 安全规则（GIT_SAFETY）

> **一句话**：仓库里最值钱的往往不是代码，而是**还没提交的那点东西**。破坏它，工作就没了。
>
> 本文件是 **Git 操作边界的单一来源**。其他文件只引用，不复制。

---

## 1. 适用范围

一切 `git` 命令，以及在仓库目录里会间接影响工作区的操作（切换分支、worktree、stash、脚本里的 git 调用）。

---

## 2. 🔒 保护已有未提交修改（最高优先级）

**规则**：未提交的修改属于**用户的工作**。Agent **不得**：

| 禁止 | 原因 |
|---|---|
| `git checkout -- .` / `git restore .` / `git checkout HEAD -- .` | 宽泛恢复 = 一次性抹掉**所有**未提交工作 |
| `git reset --hard` | 同上，且更难挽回 |
| `git clean -f/-fd/-x` | 删除未跟踪文件——**新写的脚本、日志、证据全在里面** |
| `git stash` 后忘记 pop | 用户的东西"消失"了 |
| `git add -A` / `git add .` | 会把用户的改动一起暂存，混进你的提交 |

**改单个文件**：用**精确路径**，不要用宽泛命令。
```bash
git restore -- path/to/one_file.py      # ✅ 只影响这一个
git checkout -- path/to/one_file.py     # ✅ 等价写法
git restore .                            # ❌ 禁止
```

**任务开始前**先记录基线：
```bash
git status --short ; git diff --stat ; git log --oneline -3
git stash list          # 确认没有别人遗留的 stash
```

---

## 3. 分支 / worktree 策略

| 场景 | 做法 |
|---|---|
| 小改动（几行、单文件） | 留在当前分支，直接改 |
| 较大改动（多文件/结构性/需要隔离验证） | `git worktree add` 到独立目录，**在 worktree 里做**，主工作区不受影响 |
| 需要试验性方案 | worktree + 可以随时删掉，**不要**在主工作区反复试 |

**禁止**：
- 未经授权**新建/切换/删除分支**（会改变用户所处的上下文）
- 未经授权**推送到远程**（包括 `--force`，**永远禁止 force push**）
- **改写历史**（`rebase` / `commit --amend` / `filter-branch`）：仅当该提交**从未推送**且**用户明确要求**

---

## 4. 提交与推送：默认**不做**

- **未经明确授权，不自动 `commit`，不 `push`**。做完改动后**报告 diff**，等用户决定。
- 用户授权提交时：
  - 只暂存**本次任务涉及的文件**（逐个路径 `git add <path>`，不用 `-A`）
  - 提交信息写清"改了什么、为什么"
  - **不包含**用户的其它未提交改动

---

## 5. 本次变更的回滚方法（每次改动都要能说清）

做完改动后，在报告里**明确写出**回滚命令：

| 情况 | 回滚 |
|---|---|
| 改动**未提交** | `git restore -- <file>`（精确到文件） |
| 新建了文件且未提交 | `rm <file>`（确认是你建的） |
| 已提交（未推送） | `git revert <sha>`（**优先 revert，不用 reset**） |
| 已推送 | 只能 `git revert` |

> **验收要求**：任何一次改动，必须能回答"如果这改动是错的，怎么干净地退回去"。
> 答不出来 → 说明改动方式本身就危险。

---

## 6. ❌ 绝对禁止清单

```
git reset --hard          git clean -f / -fd / -fx
git push --force          git push -f          （任何形式）
git rebase（已推送的历史）  git commit --amend（已推送的提交）
git filter-branch / filter-repo
git checkout . / git restore .
git stash drop / stash clear
任何 delete / 删除远程分支
```

若某项**确实必需** → **停下来，说明为什么、影响什么、有没有替代方案，等用户明确授权**。

---

## 7. 验收标准（可检查）

- [ ] 开工前记录过 `git status` / `git diff --stat` 基线
- [ ] 未执行任何第 6 节的禁止命令
- [ ] 未跟踪文件（脚本/日志/证据）**一个都没被删**
- [ ] 用户的未提交修改**完整性未变**（开工前后对比 diff 一致）
- [ ] 未自动 commit / push
- [ ] 报告里写出了**本次改动的回滚命令**
- [ ] 若涉及分支/worktree，已获授权
