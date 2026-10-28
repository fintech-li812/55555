#!/usr/bin/env bash
# =============================================================================
# 把本地项目同步到公开仓库 fintech-li812/55555
#
# 为什么走「导出到临时仓库 → 提交 → 推送」，而不是直接 git push：
#   本地工作目录的 git 历史与远端仓库的历史互不相干（本地是长期积累的独立历史，
#   且历史提交里含有脱敏前的服务器地址）。直接 push 要么因历史冲突被拒，
#   要么需要 --force 覆盖，而 force 会把脱敏前的历史一并带到公开仓库。
#   这里改成「远端历史纯净地向前累积，每次同步只增加一个新提交」，
#   既不会泄露旧历史，也不需要任何 force 操作。
#
# 安全措施：
#   1. 只导出本地**已跟踪**文件，.gitignore 排除的凭据不会进入远端；
#   2. 推送前打印变更清单并二次确认；
#   3. 推送前后各做一次凭据扫描，发现敏感文件、真实地址或明文口令立即中止；
#   4. 删除单独拦截，需二次确认（删除会同步到公开仓库）。
#
# 用法（Git Bash，在本项目任意位置执行）：
#   bash scripts/push_to_github.sh              # 交互式，推送前确认
#   DRY_RUN=1 bash scripts/push_to_github.sh    # 只显示将要发生的变更，不推送
#
# 仓库若改名，同步修改下面的 REPO_URL 即可。
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/fintech-li812/55555.git"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)"
# git.exe 是 Windows 原生程序，不认识 MSYS 的 /d 挂载：把 /d/foo 交给它，
# 它不会当成 D:\foo，而是解析到 Git 自己的安装目录下，于是「目录已存在且非空」
# 之类的误判就来了。这里统一转成 D:/ 风格再传给 git（MSYS 的 cd 同样认得）。
LOCAL_W="$(cd "$LOCAL" && pwd -W)"
# 临时目录必须与项目同盘且路径尽量短：Windows 的 260 字符路径上限会让
# 深层路径的 checkout 失败，mktemp 生成的长路径会把路径顶爆。
TMP="${SYNC_TMP:-$(dirname "$LOCAL_W")/_gh_sync}"
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

if [ -n "$(git status --porcelain)" ]; then
  err "本地有未提交的改动。本脚本只导出**已跟踪**内容，未提交的改动不会同步。"
  err "请先 commit，或先确认这些改动确实不需要同步："
  git status --short | head -20
  die "已中止"
fi

log "本地仓库：$LOCAL"
log "目标仓库：$REPO_URL"

# ---- 1. 克隆远端到临时目录 --------------------------------------------------
log "克隆远端仓库到 $TMP ..."
rm -rf "$TMP" 2>/dev/null || true
# 必须先建目录再 clone：clone 到「尚不存在」的目标路径时，在部分受限环境里
# 无法自行创建该目录（会留下 Cloning into 的提示但目录不存在，后续 cd 直接失败）。
mkdir -p "$TMP" || die "无法创建临时目录 $TMP"
git clone "$REPO_URL" "$TMP" 2>&1 | tail -3 || die "克隆失败，检查网络与权限"
cd "$TMP"
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
BEFORE="$(git ls-files | wc -l)"
ok "远端现有 $BEFORE 个文件（分支 $BRANCH）"

# ---- 2. 用本地已跟踪文件覆盖工作区 -----------------------------------------
# 先清空工作区文件（保留 .git），再导出，这样本地删掉的文件在远端也会消失。
log "导出本地已跟踪文件..."
git ls-files -z | xargs -0 rm -f 2>/dev/null || true
git -C "$LOCAL_W" archive --format=tar HEAD | tar -x -C "$TMP" || die "导出失败"
ok "导出完成（$(find . -type f -not -path './.git/*' | wc -l) 个文件）"

# ---- 3. 凭据扫描 ------------------------------------------------------------
log "扫描导出内容..."
LEAKS="$(find . -type f -not -path './.git/*' \
  \( -name '.env' -o -name '*.env' -o -name '.sshpw' -o -name '*.sshpw' \
     -o -name '*.pem' -o -name '*.key' -o -name '*.p12' -o -name '*.pfx' \
     -o -name '*.crt' -o -name '*.cer' -o -name 'id_rsa*' \) 2>/dev/null || true)"
if [ -n "$LEAKS" ]; then
  err "发现敏感文件，已中止："
  echo "$LEAKS"
  die "请检查 .gitignore 后再试"
fi

ADDR_LEAKS="$(grep -rIl -E '39\.104\.201\.10|jdjrkj\.online' . \
  --exclude-dir=.git 2>/dev/null || true)"
if [ -n "$ADDR_LEAKS" ]; then
  err "发现生产服务器地址或域名，已中止："
  echo "$ADDR_LEAKS"
  die "公开仓库不保留真实地址，请改用占位符"
fi

CONTENT_LEAKS="$(grep -rInE \
  "(PASSWORD|PASSWD|SECRET|API_KEY|ACCESS_TOKEN|WEBHOOK)\s*[:=]\s*['\"][^'\"]{8,}" \
  . --exclude-dir=.git --include=*.md --include=*.py --include=*.sh \
    --include=*.conf --include=*.json --include=*.service 2>/dev/null \
  | grep -v '\.template' | head -5 || true)"
if [ -n "$CONTENT_LEAKS" ]; then
  err "文件内容中发现疑似明文凭据，已中止："
  echo "$CONTENT_LEAKS"
  die "请清理后再推送"
fi
ok "未发现敏感文件、真实地址与明文口令"

# ---- 4. 展示变更并确认 ------------------------------------------------------
git add -A
ADDED="$(git status --porcelain | grep -c '^A ' || true)"
MODIFIED="$(git status --porcelain | grep -c '^M ' || true)"
DELETED="$(git status --porcelain | grep -c '^D ' || true)"
TOTAL="$((ADDED + MODIFIED + DELETED))"

echo ""
log "变更摘要：新增 $ADDED / 修改 $MODIFIED / 删除 $DELETED（合计 $TOTAL）"

if [ "$TOTAL" -eq 0 ]; then
  ok "远端已是最新，无需推送"
  exit 0
fi

if [ "$DELETED" -gt 0 ]; then
  echo ""
  err "检测到 $DELETED 个待删除文件。删除会同步到公开仓库，请确认这是有意为之："
  git status --porcelain | grep '^D ' | head -10
  printf '确认要删除这些文件？[y/N] '
  read -r CONFIRM_DEL
  [ "$CONFIRM_DEL" = "y" ] || [ "$CONFIRM_DEL" = "Y" ] || die "已取消"
fi

echo ""
log "变更清单："
git status --porcelain | sed 's/^...//' | head -40
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

log "推送到 $BRANCH ..."
git push origin "$BRANCH" || die "推送失败"

# ---- 6. 推送后核验 ----------------------------------------------------------
git fetch -q origin
AFTER="$(git ls-tree -r --name-only "origin/$BRANCH" | wc -l)"
REMOTE_LEAKS="$(git ls-tree -r --name-only "origin/$BRANCH" \
  | grep -iE '(^|/)\.env$|sshpw|\.pem$|\.key$|mysqldata|id_rsa' || true)"
if [ -n "$REMOTE_LEAKS" ]; then
  err "警告：远端出现敏感文件，请立即处理："
  echo "$REMOTE_LEAKS"
else
  ok "推送完成：远端文件数 $BEFORE → $AFTER，未发现凭据"
fi
ok "最新提交：$(git log --oneline "origin/$BRANCH" -1)"
