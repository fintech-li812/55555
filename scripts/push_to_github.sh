#!/usr/bin/env bash
# =============================================================================
# 把本地项目同步到公开仓库 fintech-li812/cashflow-risk-workbench
#
# 为什么需要这个脚本，而不是直接 git push：
#   远端是一个「归档仓库」，项目位于子目录 CashflowRiskWorkbench/ 下，
#   同级还有 AI_Command_Workspace/（约 7800 文件）、finrisk_workbench_v7/
#   等历史归档。而本地工作目录的根目录就对应远端的那个子目录。
#   两侧目录层级差一级，历史也无关，直接 push 会把远端其余 16000+ 文件
#   全部删掉。因此这里走「克隆 → 合并进子目录 → 推送」的路径。
#
# 安全措施：
#   1. 只导出本地**已跟踪**文件，.gitignore 排除的凭据不会进入远端；
#   2. 推送前打印变更清单并二次确认；
#   3. 推送前后各做一次凭据扫描，发现敏感文件立即中止；
#   4. 全程不删除远端任何既有文件（只新增与覆盖）。
#
# 用法（Git Bash）：
#   bash push_to_github.sh              # 交互式，推送前确认
#   DRY_RUN=1 bash push_to_github.sh    # 只显示将要发生的变更，不推送
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/fintech-li812/cashflow-risk-workbench.git"
SUBDIR="CashflowRiskWorkbench"
LOCAL="$(cd "$(dirname "$0")" && pwd)"

# 临时目录必须与项目同盘且路径尽量短。
# 远端归档里有大量超长路径（AI_Command_Workspace/验收/.../xxx.json），
# Windows 的 260 字符路径上限会让 checkout 失败；用 mktemp 生成的
# C:\Users\...\AppData\Local\Temp\... 会把路径顶爆，导致整个检出中断。
TMP="${SYNC_TMP:-$(dirname "$LOCAL")/_gh_sync_tmp}"
DRY_RUN="${DRY_RUN:-0}"

log()  { printf '\033[1;34m[push]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; }
die()  { err "$*"; exit 1; }

cleanup() {
  [ -d "$TMP" ] || return 0
  rm -rf "$TMP" 2>/dev/null || log "临时目录未能自动清理，可手动删除：$TMP"
  return 0
}
trap cleanup EXIT

# ---- 0. 前置检查 ------------------------------------------------------------
command -v git >/dev/null || die "未找到 git"

cd "$LOCAL"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$LOCAL 不是 git 仓库"

# 本地若有未提交改动，先提示（不强制，允许提交后再同步）
if [ -n "$(git status --porcelain)" ]; then
  err "本地有未提交的改动，请先 commit 再同步："
  git status --short | head -20
  die "已中止"
fi

log "本地仓库：$LOCAL"
log "目标仓库：$REPO_URL（子目录 $SUBDIR/）"

# ---- 1. 克隆远端到临时目录 --------------------------------------------------
log "克隆远端仓库到 $TMP ..."
rm -rf "$TMP" 2>/dev/null || true
git clone "$REPO_URL" "$TMP" 2>&1 | tail -3 || die "克隆失败，检查网络与权限"
cd "$TMP"
BEFORE="$(git ls-files | wc -l)"
ok "远端现有 $BEFORE 个文件"

# 检出完整性核验：远端归档含超长路径，Windows 下 checkout 可能部分失败。
# 此时工作区会相对 HEAD 出现「缺失」，后续 add -A 会把它们当成删除，
# 一旦推送就是批量删文件。所以这里必须在合并前就拦住。
DIRTY="$(git status --porcelain | wc -l)"
if [ "$DIRTY" -ne 0 ]; then
  err "克隆后工作区不干净（$DIRTY 项），说明检出未完整完成。"
  err "常见原因是 Windows 的 260 字符路径上限。请改用更短的路径："
  err "  SYNC_TMP=/d/sync bash push_to_github.sh"
  git status --porcelain | head -10
  die "已中止，未做任何推送"
fi
ok "检出完整，工作区干净"

# ---- 2. 导出本地已跟踪文件，合并进子目录 -----------------------------------
log "导出本地已跟踪文件到 $SUBDIR/ ..."
mkdir -p "$TMP/$SUBDIR"
git -C "$LOCAL" archive --format=tar HEAD | tar -x -C "$TMP/$SUBDIR" \
  || die "导出失败"
ok "导出完成"

# ---- 3. 凭据扫描（第一道）--------------------------------------------------
log "扫描导出的文件是否混入凭据..."
LEAKS="$(find "$TMP/$SUBDIR" -type f \
  \( -name '.env' -o -name '*.env' -o -name '.sshpw' -o -name '*.pem' \
     -o -name '*.key' -o -name '*.p12' -o -name '*.pfx' -o -name 'id_rsa*' \) \
  -not -name '*.template' 2>/dev/null || true)"
if [ -n "$LEAKS" ]; then
  err "发现敏感文件，已中止："
  echo "$LEAKS"
  die "请检查 .gitignore 后再试"
fi

CONTENT_LEAKS="$(grep -rInE \
  "(PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_TOKEN|WEBHOOK)\s*[:=]\s*['\"][^'\"]{6,}" \
  "$TMP/$SUBDIR" 2>/dev/null | head -5 || true)"
if [ -n "$CONTENT_LEAKS" ]; then
  err "文件内容中发现疑似明文凭据，已中止："
  echo "$CONTENT_LEAKS"
  die "请清理后再推送"
fi
ok "未发现敏感文件"

# ---- 4. 展示变更并确认 ------------------------------------------------------
cd "$TMP"
git add -A
ADDED="$(git status --porcelain | grep -c '^A ' || true)"
MODIFIED="$(git status --porcelain | grep -c '^M ' || true)"
DELETED="$(git status --porcelain | grep -c '^D ' || true)"
TOTAL="$((ADDED + MODIFIED + DELETED))"

echo ""
log "变更摘要：新增 $ADDED / 修改 $MODIFIED / 删除 $DELETED（合计 $TOTAL）"

if [ "$DELETED" -gt 0 ]; then
  err "检测到 $DELETED 个待删除文件。正常同步不应删除远端文件，"
  err "请确认是否误操作："
  git status --porcelain | grep '^D ' | head -10
  die "已中止"
fi

if [ "$TOTAL" -eq 0 ]; then
  ok "远端已是最新，无需推送"
  exit 0
fi

echo ""
log "变更清单："
git status --porcelain | sed 's/^...//' | sed "s|^$SUBDIR/||" | head -40
[ "$TOTAL" -gt 40 ] && echo "  ...（还有 $((TOTAL - 40)) 个）"
echo ""

if [ "$DRY_RUN" = "1" ]; then
  ok "DRY_RUN 模式，不执行推送"
  exit 0
fi

printf '确认推送到公开仓库？[y/N] '
read -r CONFIRM
[ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || die "已取消"

# ---- 5. 提交并推送 ----------------------------------------------------------
log "提交..."
git commit -q -m "Sync project updates from local workbench ($(date +%Y-%m-%d))" \
  || die "提交失败"

log "推送到 main ..."
git push origin main || die "推送失败"

# ---- 6. 推送后核验 ----------------------------------------------------------
git fetch -q origin
AFTER="$(git ls-tree -r --name-only origin/main | wc -l)"
REMOTE_LEAKS="$(git ls-tree -r --name-only origin/main \
  | grep -iE '(^|/)\.env$|sshpw|\.pem$|\.key$|mysqldata|id_rsa' || true)"
if [ -n "$REMOTE_LEAKS" ]; then
  err "警告：远端出现敏感文件，请立即处理："
  echo "$REMOTE_LEAKS"
else
  ok "推送完成：远端文件数 $BEFORE → $AFTER，未发现凭据"
fi
ok "最新提交：$(git log --oneline origin/main -1)"
