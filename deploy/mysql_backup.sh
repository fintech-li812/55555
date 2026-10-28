#!/usr/bin/env bash
# Daily logical backup of the workbench databases.
#
# Runs as root from a systemd timer so it can authenticate over the MySQL
# unix socket without a password. Only dumps databases this project owns
# (cf* prefix) so a stray shared database is never captured by accident.
set -euo pipefail

DEST=/var/backups/cashflow/mysql
KEEP_DAYS=14
STAMP=$(date +%Y%m%d-%H%M%S)

mkdir -p "$DEST"

# Pick up every cf* database. If none exist, fail loudly rather than
# "succeeding" with an empty backup that looks healthy in the log.
mapfile -t DBS < <(mysql -N -B -e "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE 'cf%' ORDER BY schema_name;")

if [ "${#DBS[@]}" -eq 0 ]; then
    echo "[backup] 没有找到 cf* 数据库，拒绝生成空备份" >&2
    exit 1
fi

echo "[backup] 数据库: ${DBS[*]}"

mysqldump --single-transaction --quick --routines --triggers --events \
    --default-character-set=utf8mb4 --databases "${DBS[@]}" \
    | gzip > "$DEST/cashflow-$STAMP.sql.gz"

# A zero-byte archive means mysqldump failed; do not leave it behind as
# evidence that a backup happened.
SIZE=$(stat -c %s "$DEST/cashflow-$STAMP.sql.gz")
if [ "$SIZE" -lt 1024 ]; then
    echo "[backup] 备份文件异常（${SIZE} 字节），已删除" >&2
    rm -f "$DEST/cashflow-$STAMP.sql.gz"
    exit 1
fi

chmod 600 "$DEST/cashflow-$STAMP.sql.gz"
echo "[backup] 完成 $DEST/cashflow-$STAMP.sql.gz (${SIZE} 字节)"

find "$DEST" -name 'cashflow-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "[backup] 已清理 ${KEEP_DAYS} 天前的备份，当前保留 $(find "$DEST" -name 'cashflow-*.sql.gz' | wc -l) 份"
