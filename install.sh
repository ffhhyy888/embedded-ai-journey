#!/bin/sh
# ============================================================
# 新机器一键安装
# ============================================================
# 做三件事：
#   1. 把本仓库里的 skill 复制到 Claude Code 能加载的位置
#      （Claude Code 只从 ~/.claude/skills/ 加载，不会读仓库里的副本）
#   2. 安装 pip 依赖（skeleton/requirements.txt）
#   3. 告诉你还差什么"pip 装不了"的东西
set -e

REPO="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$REPO/skills/ai-embedded-autolab"
SKILL_DST="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}/ai-embedded-autolab"

echo "== AI 嵌入式研发平台 · 安装 =="
echo "仓库: $REPO"
echo

echo "[1/3] 安装 skill"
echo "      $SKILL_SRC  ->  $SKILL_DST"
mkdir -p "$(dirname "$SKILL_DST")"
rm -rf "$SKILL_DST"
cp -r "$SKILL_SRC" "$SKILL_DST"
echo "      ok"

echo "[2/3] pip 依赖"
if python -m pip install -q -r "$REPO/skeleton/requirements.txt"; then
    echo "      ok（pyserial / pytest / pyocd / ziglang）"
else
    echo "      ⚠️ 失败。请手动执行："
    echo "      python -m pip install -r $REPO/skeleton/requirements.txt"
fi

echo "[3/3] 需要人工安装的（pip 装不了）"
cat <<'EOF'
      · Keil MDK / ARMCC AC5 —— 系统级安装 + 授权，无法 pip 安装
        装好后按需设置环境变量：
          AUTOLAB_UVISION   ->  ...\UV4\uVision.com
          AUTOLAB_FROMELF   ->  ...\ARMCC\bin\fromelf.exe
      · 其它芯片家族请用对应的厂商工具链（见 adapters/<家族>/）
EOF

echo
echo "完成。下一步："
echo "  1) cd 到你的工程目录"
echo "  2) python tools/preflight.py        # 环境自检，会告诉你还差什么"
