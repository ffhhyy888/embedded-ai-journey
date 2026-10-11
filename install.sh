#!/bin/sh
# ============================================================
# 新机器一键安装
# ============================================================
# 做三件事：
#   1. 把本仓库里的 skill 复制到 Claude Code 能加载的位置
#      （Claude Code 只从 ~/.claude/skills/ 加载，不会读仓库里的副本）
#   2. 安装 pip 依赖（skeleton/requirements.txt）
#   3. 告诉你还差什么"pip 装不了"的东西
#
# 安全性设计（针对"复制失败会毁掉已装 skill"）：
#   · 先做参数/环境校验，任何一项不满足就**在动手之前**退出
#   · 先在目标目录里做临时副本并**校验内容**，成功后才替换
#   · 替换前把旧版本改名备份；替换失败则**回滚**
#   · 用 trap 保证异常退出时清理临时目录
#   · 不使用可能因空变量指向错误目录的破坏性命令
set -eu

die() { printf '错误: %s\n' "$*" >&2; exit 1; }

# ---------- 环境校验 ----------
[ -n "${HOME:-}" ] || die "环境变量 HOME 为空 —— 无法确定安装位置（请检查 shell 环境）"

REPO="$(cd "$(dirname "$0")" && pwd)"
SKILL_SRC="$REPO/skills/ai-embedded-autolab"
[ -d "$SKILL_SRC" ]     || die "找不到 skill 源目录: $SKILL_SRC"
[ -f "$SKILL_SRC/SKILL.md" ] || die "skill 源目录里没有 SKILL.md: $SKILL_SRC"

SKILLS_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
[ -n "$SKILLS_DIR" ] || die "目标目录为空"
case "$SKILLS_DIR" in
    /*) : ;;                       # 必须是绝对路径，避免相对路径带来意外
    *)  die "目标目录必须是绝对路径（当前: $SKILLS_DIR）" ;;
esac
SKILL_DST="$SKILLS_DIR/ai-embedded-autolab"

echo "== AI 嵌入式研发平台 · 安装 =="
echo "仓库:     $REPO"
echo "skill 源: $SKILL_SRC"
echo "目标:     $SKILL_DST"
echo

# ---------- [1/3] 安装 skill（安全替换）----------
echo "[1/3] 安装 skill"

mkdir -p "$SKILLS_DIR"

TMP="$SKILLS_DIR/.ai-embedded-autolab.tmp.$$"
BAK="$SKILLS_DIR/.ai-embedded-autolab.bak.$$"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT HUP INT TERM

rm -rf "$TMP"
cp -r "$SKILL_SRC" "$TMP" || die "复制到临时目录失败（原安装未受影响）"
[ -f "$TMP/SKILL.md" ] || die "临时副本校验失败：缺 SKILL.md（原安装未受影响）"

if [ -d "$SKILL_DST" ]; then
    mv "$SKILL_DST" "$BAK" || die "备份原有安装失败，已中止（原安装未受影响）"
fi

if mv "$TMP" "$SKILL_DST"; then
    rm -rf "$BAK"
    echo "      ok（已安装/替换）"
else
    [ -d "$BAK" ] && mv "$BAK" "$SKILL_DST"      # 回滚
    die "替换失败，已回滚到原有版本"
fi

# ---------- [2/3] pip 依赖 ----------
echo "[2/3] pip 依赖"
REQ="$REPO/skeleton/requirements.txt"
if [ -f "$REQ" ]; then
    if python -m pip install -q -r "$REQ"; then
        echo "      ok（pyserial / pytest / pyocd / ziglang）"
    else
        echo "      ⚠️ 安装失败 —— 请手动执行："
        echo "      python -m pip install -r \"$REQ\""
    fi
else
    echo "      ⚠️ 找不到 $REQ（跳过）"
fi

# ---------- [3/3] 说明 ----------
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
