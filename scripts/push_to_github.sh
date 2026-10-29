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
#   AUTO_CONFIRM=1 bash scripts/push_to_github.sh  # 跳过确认（自动化用）
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
AUTO_CONFIRM="${AUTO_CONFIRM:-0}"

# 列出工作区变更文件的规范做法：
#   - core.quotepath=false：否则中文文件名会被 git 转义成 "docs/\350\264\242..."，
#     带引号交给 git add 会直接 fatal，文件被静默漏掉；
#   - sed 去掉 3 个字符的状态前缀；
#   - -uall（= --untracked-files=all）：默认 git 会把整个未跟踪目录折叠成
#     一条 "?? algo/"。折叠后既无法按文件分组（algo/ 会整个落到兜底的 chore），
#     也无法核对「本组应暂存数 vs 实际暂存数」（1 个条目 vs 43 个文件）。
#   - 重命名行 "R  old -> new" 只取新路径。
list_pending() {
  git -c core.quotepath=false status --porcelain -uall | sed 's/^...//' \
    | sed 's/ -> .*$//' | sort -u
}

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

if [ "$DELETED" -gt 0 ] && [ "$AUTO_CONFIRM" != "1" ]; then
  echo ""
  err "检测到 $DELETED 个待删除文件。删除会同步到公开仓库，请确认这是有意为之："
  git -c core.quotepath=false status --porcelain -uall | grep '^D ' | head -10
  printf '确认要删除这些文件？[y/N] '
  read -r CONFIRM_DEL
  [ "$CONFIRM_DEL" = "y" ] || [ "$CONFIRM_DEL" = "Y" ] || die "已取消"
fi

echo ""
log "变更清单："
list_pending | head -40
[ "$TOTAL" -gt 40 ] && echo "  ...（还有 $((TOTAL - 40)) 个）"
echo ""

if [ "$DRY_RUN" = "1" ]; then
  ok "DRY_RUN 模式，不执行推送"
  exit 0
fi

if [ "$AUTO_CONFIRM" != "1" ]; then
  printf '确认推送到公开仓库？[y/N] '
  read -r CONFIRM
  [ "$CONFIRM" = "y" ] || [ "$CONFIRM" = "Y" ] || die "已取消"
fi

# ---- 5. 按文件归属分组提交 --------------------------------------------------
# 为什么要分组而不是打成一个大提交：
# 远端拿到的是扁平的文件快照，如果只提交一次，
# 「改了哪个模块、为什么改」就全丢在一条消息里，事后无法按模块回溯。
# 这里按路径归属拆成若干语义化提交，让公开仓库的历史也具备可读性。
commit_group() {           # commit_group <前缀> <一句话主题> <路径模式...>
  local prefix="$1"; shift
  local subject="$1"; shift
  local pat files=""
  for pat in "$@"; do
    local hit
    # 动态正则里 "\." 会被 awk 当成转义警告，统一写成 "[.]" 更干净；
    # 路径里含空格时 substr 会截断，这里用 sed 剥离前 3 个状态字符。
    hit="$(list_pending | awk -v p="$pat" '$0 ~ p')"
    [ -n "$hit" ] && files="$files
$hit"
  done
  files="$(printf '%s' "$files" | sed '/^$/d' | sort -u)"
  [ -z "$files" ] && return 0
  # 关键：必须先 reset 再按组 add。
  # 前面统计数量时已经 git add -A 暂存了全部文件，而 git commit 不带路径
  # 会把暂存区一并提交——那样第一个分组就会把 70 个文件全带走，
  # 后面各组只能提交到空气。
  git reset -q
  printf '%s\n' "$files" | while IFS= read -r f; do
    git add -A -- "$f" || die "暂存失败（$prefix）：$f"
  done
  # 管道里的 while 在子 shell 里跑，die 的退出码带不回来。
  # 所以必须单独校验「暂存了多少」与「本组有多少」一致，
  # 否则某个文件 add 失败只会打一条 fatal，其余文件照样被提交，
  # 那个文件就静默丢了——而且下次同步还会再报一遍，看起来像幽灵变更。
  WANT="$(printf '%s\n' "$files" | wc -l)"
  STAGED="$(git diff --cached --name-only | wc -l)"
  [ "$STAGED" -eq "$WANT" ] \
    || die "分组 $prefix 只暂存了 $STAGED/$WANT 个文件，已中止"
  git commit -q -m "$prefix: $subject" -m "$(printf '%s\n' "$files" | sed 's/^/  /')" \
    || die "提交失败（$prefix）"
  ok "$(printf '%s\n' "$files" | wc -l) 个文件 → $prefix: $subject"
}

log "分组提交..."
PENDING_BEFORE="$(list_pending | wc -l)"
commit_group "feat(algorithm)" "v8 生产算法包 v1.0：训练/打分分离 + 对偶证书 + 证据只升不降" \
  '^mcp-server/program_v1/src/v8/algorithm/' '^mcp-server/program_v1/src/v8/ra_next[.]py$'
commit_group "feat(config)" "算法开关化：四级覆盖 + Q 参与融合判别可配 + /api/v8/config" \
  '^mcp-server/program_v1/src/v8/config[.]py$' '^mcp-server/program_v1/src/v8/fusion[.]py$' \
  '^mcp-server/program_v1/src/v8/engine[.]py$' '^mcp-server/program_v1/src/v8_server[.]py$' \
  '^deploy/algorithm[.]config[.]json$' '^deploy/env[.]template$'
commit_group "test" "算法包与开关的回归测试" '^mcp-server/program_v1/tests/'
commit_group "feat(web)" "统一工作台：算法与开关选择器 + 生效口径回显" '^web/'
commit_group "docs" "算法生产化审查、v0.3 评审、接入记录、开关说明与方案总结" \
  '^docs/' '^README[.]md$' '^ROADMAP[.]md$' '^TODO[.]md$' '^task[.]md$'
commit_group "chore(deploy)" "部署脚本与环境模板" '^deploy/' '^scripts/'
commit_group "chore(algo)" "ra-next 研究目录产出（不参与生产链路）" '^algo/'
# 兜底：任何没被上面认领的文件也一并提交，避免漏文件导致下次重复出现
REMAIN="$(list_pending)"
if [ -n "$REMAIN" ]; then
  commit_group "chore" "其他更新" '.'
fi
# 提交后必须一个不剩：少提交会静默丢文件，多提交说明分组逻辑有洞
LEFT="$(list_pending | wc -l)"
if [ "$LEFT" -ne 0 ]; then
  err "仍有 $LEFT 个文件未提交，请检查分组规则"
  list_pending | head -10
  die "分组提交不完整，已中止推送"
fi
ok "$PENDING_BEFORE 项变更全部归入分组提交"

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
