#!/usr/bin/env bash
# =============================================================================
# V8 全量建表（幂等，可重复执行）
# 文件：deploy/init_v8_tables.sh
#
# 设计原则：不复制表定义。sql/ 下三份 .sql 仍是唯一权威定义，本脚本只负责
#   1) 统一目标库（默认 cfqa_workbench，与线上生产库一致）
#   2) 剥离 init.sql 自带的 CREATE DATABASE / USE cf_workbench
#   3) 按依赖顺序拼接后一次性交给 mysql 执行
#
# 用法（服务器上，需 DDL 权限账号）：
#   sudo bash /opt/cashflow/deploy/init_v8_tables.sh
#   DB=cfqa_workbench ADMIN_USER=root bash init_v8_tables.sh
#   DRY_RUN=1 bash init_v8_tables.sh          # 只打印，不落库
#
# 口令来源（按优先级）：
#   $ADMIN_PW_FILE（默认 /opt/cashflow/deploy/.dbadmin，600，只放口令一行）
#   > 环境变量 MYSQL_PWD > 免密（unix socket / auth_socket，如 root@localhost）
#   口令不出现在命令行与 shell 历史里。
#
# 退出码：0 成功 / 2 前置条件不满足 / 3 建表失败 / 4 校验未通过 / 5 授权失败
# =============================================================================
set -euo pipefail

ROOT="${ROOT:-/opt/cashflow}"
DB="${DB:-cfqa_workbench}"
ADMIN_USER="${ADMIN_USER:-root}"
APP_USER="${APP_USER:-cf_app}"
SQLDIR="${SQLDIR:-$ROOT/sql}"
DRY_RUN="${DRY_RUN:-0}"
ADMIN_PW_FILE="${ADMIN_PW_FILE:-$ROOT/deploy/.dbadmin}"

log() { printf '[init-v8] %s\n' "$*"; }
err() { printf '[init-v8][ERROR] %s\n' "$*" >&2; }

command -v mysql >/dev/null 2>&1 || { err "未找到 mysql 客户端"; exit 2; }
for f in init.sql v8_schema.sql v8_accountability_schema.sql; do
  [[ -r "$SQLDIR/$f" ]] || { err "缺少建表文件：$SQLDIR/$f"; exit 2; }
done

# ---- 1. 拼装 SQL：剥离自带建库/切库，统一指向目标库 --------------------------
TMP_SQL="$(mktemp)"
TMP_CNF="$(mktemp)"
cleanup() { rm -f "$TMP_SQL" "$TMP_CNF"; }
trap cleanup EXIT

{
  # init.sql：跳过 CREATE DATABASE 块（以分号结尾结束），并删掉 USE cf_workbench;
  awk '
    /^CREATE DATABASE /      { skip=1 }
    skip==1 && /;[ \t]*$/    { skip=0; next }
    skip==1                  { next }
    { print }
  ' "$SQLDIR/init.sql"
  printf '\n'
  cat "$SQLDIR/v8_schema.sql"
  printf '\n'
  cat "$SQLDIR/v8_accountability_schema.sql"
} \
| sed -E '/^USE[ \t]+[`'"'"'"]?[A-Za-z0-9_]+[`'"'"'"]?;[ \t]*$/d' \
| sed -E '/^CREATE DATABASE /d' > "$TMP_SQL"

TOTAL=$(grep -cE '^CREATE TABLE IF NOT EXISTS' "$TMP_SQL" || true)
VIEWS=$(grep -cE '^CREATE OR REPLACE VIEW' "$TMP_SQL" || true)
log "目标库=${DB}  管理员=${ADMIN_USER}  待建表=${TOTAL} 张，视图=${VIEWS} 个"

if [[ "$TOTAL" -lt 20 ]]; then
  err "解析出的建表语句仅 ${TOTAL} 条，少于预期的 23 条，怀疑 SQL 预处理有误，中止"
  exit 2
fi

if [[ "$DRY_RUN" == "1" ]]; then
  log "DRY_RUN：前 40 行预览"
  head -40 "$TMP_SQL"
  exit 0
fi

# ---- 2. 准备 mysql 凭据（临时 defaults 文件，600，退出即删）------------------
# 走 unix socket，便于 root 使用 auth_socket 免密；需要口令时从文件读取。
PW=""
if [[ -r "$ADMIN_PW_FILE" ]]; then
  PW="$(tr -d '\r\n' < "$ADMIN_PW_FILE")"
elif [[ -n "${MYSQL_PWD:-}" ]]; then
  PW="$MYSQL_PWD"
else
  log "未提供口令文件，尝试以 ${ADMIN_USER} 免密（unix socket）连接"
fi

umask 077
: > "$TMP_CNF"
chmod 600 "$TMP_CNF"
{
  printf '[client]\nuser=%s\n' "$ADMIN_USER"
  [[ -n "$PW" ]] && printf 'password=%s\n' "$PW"
} > "$TMP_CNF"
unset PW MYSQL_PWD

MYSQL=(mysql --defaults-extra-file="$TMP_CNF" --default-character-set=utf8mb4 --database="$DB")

# ---- 3. 执行 ---------------------------------------------------------------
if ! "${MYSQL[@]}" < "$TMP_SQL"; then
  err "建表执行失败（上面为 MySQL 报错原文）"
  exit 3
fi
log "建表执行完成"

# ---- 4. 校验：期望表名 ⊆ 库中实际表名 --------------------------------------
EXPECT=$(grep -oE '^CREATE TABLE IF NOT EXISTS[ \t]+`?[A-Za-z0-9_]+`?' "$TMP_SQL" \
         | sed -E 's/.*[` ]([A-Za-z0-9_]+)`?$/\1/' | sort -u)

ACTUAL=$("${MYSQL[@]}" -N -B -e 'SHOW TABLES;' | sort -u)

MISSING=""
while IFS= read -r t; do
  [[ -z "$t" ]] && continue
  grep -qxF "$t" <<<"$ACTUAL" || MISSING="$MISSING $t"
done <<<"$EXPECT"

if [[ -n "$MISSING" ]]; then
  err "以下表未建成：$MISSING"
  exit 4
fi

log "校验通过：${TOTAL} 张表全部存在于 ${DB}"
log "库中现有表合计：$(wc -l <<<"$ACTUAL") 张"

# ---- 5. 授权：应用账号只有 DML，不给 DDL、不给 DELETE ------------------------
# 依据 sql/v8_schema.sql 头部口径：留痕只增不改，删除只能由管理流程执行。
log "为应用账号 ${APP_USER} 授权 SELECT/INSERT/UPDATE（不含 DELETE / DDL）"
for h in 127.0.0.1 localhost; do
  "${MYSQL[@]}" -e \
    "GRANT SELECT, INSERT, UPDATE ON \`${DB}\`.* TO '${APP_USER}'@'${h}';" || {
      err "授权 ${APP_USER}@${h} 失败"; exit 5;
    }
done
"${MYSQL[@]}" -e 'FLUSH PRIVILEGES;'

log "授权完成。当前 ${APP_USER} 权限："
"${MYSQL[@]}" -N -B -e "SHOW GRANTS FOR '${APP_USER}'@'127.0.0.1';" | sed 's/^/    /'
exit 0
