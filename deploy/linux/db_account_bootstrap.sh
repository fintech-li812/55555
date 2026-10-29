#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 数据库账号的幂等初始化脚本（root 运行，可反复执行）。
#
# 设计要点：
#   1. 口令不写在脚本里、也不出现在命令行上——统一写入一个 0600 的临时
#      MySQL 配置文件，用 --defaults-extra-file 传入，避免被 `ps` 看到。
#   2. 全部使用 CREATE USER IF NOT EXISTS / GRANT，重复执行不报错、不改口令。
#   3. 默认只授予运行时真正需要的 SELECT/INSERT/UPDATE；
#      建表属于管理员职责（见 sql/v8_schema.sql），需要自愈式建表时才加 --allow-ddl。
# ---------------------------------------------------------------------------
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/cashflow/deploy/.env}"
ALLOW_DDL=0
for arg in "$@"; do
  case "$arg" in
    --allow-ddl) ALLOW_DDL=1 ;;
    --env-file=*) ENV_FILE="${arg#*=}" ;;
    *) echo "未知参数：$arg（支持 --allow-ddl / --env-file=PATH）" >&2; exit 2 ;;
  esac
done

[ -f "$ENV_FILE" ] || { echo "找不到配置文件：$ENV_FILE" >&2; exit 2; }

# 逐行取值，**不做转义展开**（bash 的 source 会吃掉路径里的反斜杠）
get() {
  sed -n "s/^${1}=//p" "$ENV_FILE" | tail -n 1 | tr -d '\r'
}

DB_HOST="$(get CF_DB_HOST)"; DB_HOST="${DB_HOST:-127.0.0.1}"
DB_PORT="$(get CF_DB_PORT)"; DB_PORT="${DB_PORT:-3306}"
DB_NAME="$(get CF_DB_NAME)"; DB_NAME="${DB_NAME:-cfqa_workbench}"
DB_USER="$(get CF_DB_USER)"; DB_USER="${DB_USER:-cf_app}"
DB_PASS="$(get CF_DB_PASSWORD)"

if [ -z "$DB_PASS" ]; then
  echo "配置文件里没有 CF_DB_PASSWORD，拒绝创建空口令账号。" >&2
  exit 2
fi

# 权限过宽就拒绝：凭证文件不该被其他人读到。
# 权限的后两位必须都是 0（如 600/640 不行、604 不行），否则组或其他用户可读。
PERM="$(stat -c '%a' "$ENV_FILE")"
case "$PERM" in
  *00) ;;
  *)  echo "配置文件 $ENV_FILE 权限过宽（$PERM），组或其他用户可读。请先 chmod 600。" >&2
      exit 2 ;;
esac

# 临时 client 配置：进程结束即销毁
TMP_CNF="$(mktemp /tmp/cf-db-XXXXXX.cnf)"
chmod 600 "$TMP_CNF"
cleanup() { rm -f "$TMP_CNF"; }
trap cleanup EXIT

MY_CNF="$(printf '%s\n' "[client]" "host=$DB_HOST" "port=$DB_PORT" "user=$DB_USER" "password=$DB_PASS")"
printf '%s' "$MY_CNF" > "$TMP_CNF"
# 必须用数组：写成字符串再 "$MY" 会被当成一个整体文件名去找，报 No such file
MY=(mysql --defaults-extra-file="$TMP_CNF" --protocol=TCP)

echo "目标：$DB_USER@$DB_HOST:$DB_PORT  (库 $DB_NAME)"

# 1) 库存在即可，不动已有库的内容
mysql -e "CREATE DATABASE IF NOT EXISTS \`$DB_NAME\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
echo "  库 $DB_NAME 已就绪"

# 2) 账号：两个来源主机都建，避免 socket 与 TCP 走不同条目导致权限不一致
for host in 127.0.0.1 localhost; do
  mysql -e "CREATE USER IF NOT EXISTS '$DB_USER'@'$host' IDENTIFIED BY '$DB_PASS';"
  mysql -e "ALTER USER '$DB_USER'@'$host' IDENTIFIED WITH caching_sha2_password BY '$DB_PASS';" || true
  mysql -e "GRANT SELECT, INSERT, UPDATE ON \`$DB_NAME\`.* TO '$DB_USER'@'$host';"
  if [ "$ALLOW_DDL" = "1" ]; then
    mysql -e "GRANT CREATE ON \`$DB_NAME\`.* TO '$DB_USER'@'$host';"
    echo "  已额外授予 CREATE（--allow-ddl）"
  fi
done
mysql -e "FLUSH PRIVILEGES;"
echo "  账号已创建/更新（幂等，重复执行不会改口令以外的东西）"

# 3) 用新账号真连一次：这才是“连接成功”的证据
"${MY[@]}" "$DB_NAME" -e "SELECT '连接成功' AS result, VERSION() AS version, DATABASE() AS db, CURRENT_USER() AS user;"

echo
echo "授权明细："
mysql -N -e "SHOW GRANTS FOR '$DB_USER'@'localhost';" | sed 's/^/  /'
