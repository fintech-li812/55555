#!/usr/bin/env bash
# Health probe for the cashflow workbench stack.
#
# Exits non-zero when anything is down so the systemd timer records a failure
# and `systemctl list-units --failed` shows it. Restarting is deliberately left
# to each unit's own Restart=always policy; this script only observes.

FAILED=0

log() { echo "[health] $*"; }

check_unit() {
    if systemctl is-active --quiet "$1"; then
        log "OK   unit   $1"
    else
        log "FAIL unit   $1 ($(systemctl is-active "$1"))"
        FAILED=1
    fi
}

check_http() {
    local name=$1 url=$2 expect=$3
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 -k \
        -H 'Host: workbench.example.com' "$url" 2>/dev/null || echo "000")
    if [ "$code" = "$expect" ]; then
        log "OK   http   $name ($code)"
    else
        log "FAIL http   $name (期望 $expect，实际 $code)"
        FAILED=1
    fi
}

for u in cf-workbench cf-mcp-http cf-mcp-frontend nginx mysql; do
    check_unit "$u"
done

check_http "首页"        "https://127.0.0.1/"                    200
check_http "状态接口"    "https://127.0.0.1/api/status"           200
check_http "控制台"      "https://127.0.0.1/console"              200
check_http "MCP 网关"    "https://127.0.0.1/mcp-frontend"         200

# Disk guard: a full root volume silently breaks MySQL and log rotation.
USED=$(df -P / | awk 'NR==2 {gsub("%","",$5); print $5}')
if [ "$USED" -ge 85 ]; then
    log "WARN 磁盘   根分区已用 ${USED}%"
    FAILED=1
else
    log "OK   磁盘   根分区已用 ${USED}%"
fi

if [ "$FAILED" -eq 0 ]; then
    log "全部检查通过"
else
    log "存在失败项"
fi
exit "$FAILED"
