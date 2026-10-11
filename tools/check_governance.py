#!/usr/bin/env python3
"""治理规则一致性检查 —— 防止"多份规则互相矛盾"。

为什么需要它
    治理规范是**单一来源**（docs/engineering/），但 CLAUDE.md / SKILL.md / 模板里
    会有**速查摘要**。摘要是人抄的 → 会漂移 → 出现两套互相矛盾的规矩。
    本脚本把这些关键点做机器校验，任何一处漂移都会被抓出来。

它检查什么（都是**确定性**的判断，适合自动化）
    1. 5 份规范文档存在且非空
    2. 规模红线的数值各处一致（1.5 倍 / 200 行）
    3. Git 禁止清单在各处**概念一致**（措辞可不同，含义必须相同）
    4. 分层术语与 `pytest.ini` 的 marker 一致
    5. 退出码语义（PASS/FAIL/ERROR）在规范里定义
    6. 不存在"自动 commit"这类与 GIT_SAFETY 冲突的表述
    7. 文档里引用的规范文件路径**真实存在**

不适合自动化、因此**不在本脚本范围**的：架构判断、代码审查、测试方案选择。

用法:  python tools/check_governance.py
退出码: 0 = 一致；1 = 发现不一致（逐条列出）
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPECDIR = os.path.join("docs", "engineering")
SPECS = ["CHANGE_POLICY.md", "GIT_SAFETY.md", "EMBEDDED_C_STANDARD.md",
         "PYTHON_AUTOMATION_STANDARD.md", "TEST_AND_RELEASE_POLICY.md"]
SUMMARY_DOCS = ["CLAUDE.md", "README.md",
                os.path.join("skills", "ai-embedded-autolab", "SKILL.md"),
                os.path.join("skeleton", "CLAUDE.md.template")]
ALL = SUMMARY_DOCS + [os.path.join(SPECDIR, s) for s in SPECS]

# Git 禁止清单：**按概念**匹配，措辞不同不算矛盾
BAN_CONCEPTS = {
    "reset --hard": r"reset\s+--hard",
    "git clean": r"git\s+clean|clean\s+-f",
    "force push": r"force\s*push|push\s*--force|push\s+-f",
    "改写历史": r"rebase|改写\S*历史|filter-branch",
}

BOLD_RE = re.compile(r"\*\*([^*]*)\*\*")
CODE_RE = re.compile(r"`([^`]*)`")


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def plain(rel):
    """去掉 markdown 装饰（反引号 / 加粗）后再匹配。

    否则「不自动 `commit`」这种写法会被误判成"自动 commit" ——
    措辞与排版差异不是矛盾，检查器不该把它们当成矛盾。
    """
    d = read(rel)
    d = CODE_RE.sub(lambda m: m.group(1), d)   # 去行内代码，保留内容
    d = BOLD_RE.sub(lambda m: m.group(1), d)   # 去加粗，保留内容
    return d


def main():
    bad = []

    # 1) 规范文档存在且非空
    for s in SPECS:
        p = os.path.join(ROOT, SPECDIR, s)
        if not (os.path.isfile(p) and os.path.getsize(p) > 800):
            bad.append("规范文档缺失或过短: %s/%s" % (SPECDIR, s))

    # 2) 规模红线数值一致
    for rel in ALL:
        d = plain(rel)
        if "红线" in d and "1.5" in d and "200" not in d:
            bad.append("%s: 提到 1.5 倍红线，但同处没提 200 行红线" % rel)

    # 3) Git 禁止清单概念一致
    for rel in ["CLAUDE.md", os.path.join(SPECDIR, "GIT_SAFETY.md"),
                os.path.join("skills", "ai-embedded-autolab", "SKILL.md"),
                os.path.join("skeleton", "CLAUDE.md.template")]:
        d = plain(rel)
        miss = [k for k, pat in BAN_CONCEPTS.items() if not re.search(pat, d)]
        if miss:
            bad.append("%s: 禁止清单缺概念 %s" % (rel, miss))

    # 4) 分层术语与 pytest.ini 一致
    ini = read(os.path.join("skeleton", "pytest.ini"))
    for m in ("hardware:", "nohardware:"):
        if m not in ini:
            bad.append("skeleton/pytest.ini: 缺 marker `%s`" % m)
    trp = read(os.path.join(SPECDIR, "TEST_AND_RELEASE_POLICY.md"))
    if "not hardware" not in trp:
        bad.append("%s/TEST_AND_RELEASE_POLICY.md: 未采用 -m \"not hardware\" 选择方式" % SPECDIR)

    # 5) 退出码语义定义齐全
    pas = read(os.path.join(SPECDIR, "PYTHON_AUTOMATION_STANDARD.md"))
    for c in ("PASS", "FAIL", "ERROR"):
        if c not in pas:
            bad.append("%s/PYTHON_AUTOMATION_STANDARD.md: 退出码语义缺 %s" % (SPECDIR, c))

    # 6) 不允许"自动 commit"表述（与 GIT_SAFETY 冲突）；"不自动/未自动"是合法的否定
    for rel in ALL:
        d = plain(rel)
        for m in re.finditer(r"自动\s*commit", d):
            before = d[max(0, m.start() - 2):m.start()]
            if not before.endswith(("不", "未")):
                bad.append("%s: 出现正面表述「自动 commit」（与 GIT_SAFETY 冲突）" % rel)
                break

    # 7) 引用的规范文件必须存在
    for rel in ALL:
        d = read(rel)
        for m in set(re.findall(r"(?:docs/engineering/|engineering/)([A-Z_]+\.md)", d)):
            if not os.path.isfile(os.path.join(ROOT, SPECDIR, m)):
                bad.append("%s: 引用了不存在的规范文件 %s/%s" % (rel, SPECDIR, m))

    if bad:
        print("[FAIL] 治理规则存在 %d 处不一致：" % len(bad))
        for b in bad:
            print("   -", b)
        return 1
    print("[PASS] 治理规则一致（检查了 %d 份文档：数值 / 术语 / 禁止清单 / 引用路径均自洽）"
          % len(ALL))
    return 0


if __name__ == "__main__":
    sys.exit(main())
