# /etc/profile.d/10-cf-db-autoconnect.sh
# 每次「打开终端」时校验一次数据库连接，做到：幂等、不阻塞、不刷屏。
#
# 三条约束：
#   1. 只针对交互式登录 shell 生效，脚本/scp/rsync 等非交互场景直接跳过。
#   2. 用 runuser 切到 cfrun 执行（配置文件的属主就是它），且用 timeout 兜底，
#      最坏等待 12 秒，不会让 SSH 登录卡住。
#   3. 程序内部有 flock 与「新鲜标记」双保险：同一时刻不会跑两个，
#      刚就绪过（默认 300 秒内）就直接返回，所以连开十个终端也只会真正连一次。

# 非交互式 shell 直接返回
case $- in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

CF_PROG=/opt/cashflow/scripts/db_autoconnect.py
CF_PY=/opt/cashflow/venv/bin/python
CF_LOG=/var/log/cashflow/db-autoconnect.log

[ -x "$CF_PY" ] || return 0
[ -f "$CF_PROG" ] || return 0

_cf_db_connect() {
    # 注意必须带 --env-file：以 cfrun 身份执行时是一个全新的环境，
    # 进程里并不继承 systemd 注入的 CF_DB_*，不带的话会被判成“配置缺失”。
    if [ "$(id -u)" = "0" ] && id cfrun >/dev/null 2>&1; then
        timeout 12 runuser -u cfrun -- "$CF_PY" -B "$CF_PROG" \
            --env-file /opt/cashflow/deploy/.env \
            --once --fresh-secs 300 --log-level INFO >/dev/null 2>&1
    else
        timeout 12 "$CF_PY" -B "$CF_PROG" \
            --env-file /opt/cashflow/deploy/.env \
            --once --fresh-secs 300 --log-level INFO >/dev/null 2>&1
    fi
}

# 失败时不静默：给一行可读提示，细节留在日志里（日志里也不含口令）
if ! _cf_db_connect; then
    echo "[cf-db] 数据库连接未就绪，详情见 $CF_LOG" >&2
fi
unset -f _cf_db_connect
