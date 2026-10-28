#!/usr/bin/env bash
# =============================================================================
# healthcheck.sh —— 深度自愈检查（由 cashflow-agent-watchdog.timer 每 5 分钟调用）
#
# systemd 的 Restart=always 只能处理"进程退出"；本脚本处理"进程在但端口不响应"。
# 连续 3 次探测失败才重启，避免瞬时抖动导致无谓重启。
# =============================================================================
set -uo pipefail

PORT="$(grep -E '^  port:' /etc/cashflow-agent/config.yaml 2>/dev/null | awk '{print $2}')"
PORT="${PORT:-8090}"
STATE_DIR=/var/lib/cashflow-agent
LOG_DIR=/var/log/cashflow-agent
FAIL_FILE="$STATE_DIR/watchdog_fail_count"
WLOG="$LOG_DIR/watchdog.log"
MAX_FAIL=3

mkdir -p "$STATE_DIR" "$LOG_DIR"
touch "$WLOG"
echo "[$(date '+%F %T')] 开始健康检查 port=$PORT" >>"$WLOG"

# 1) 单元是否 active
if ! systemctl is-active --quiet cashflow-agent.service; then
  echo "[$(date '+%F %T')] 服务未运行，尝试启动" >>"$WLOG"
  systemctl start cashflow-agent.service >>"$WLOG" 2>&1
  exit 0
fi

# 2) 端口是否响应
if curl -sSf --max-time 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
  echo 0 >"$FAIL_FILE"
  echo "[$(date '+%F %T')] 健康检查通过" >>"$WLOG"
  exit 0
fi

CNT="$(cat "$FAIL_FILE" 2>/dev/null || echo 0)"
CNT=$((CNT + 1))
echo "$CNT" >"$FAIL_FILE"
echo "[$(date '+%F %T')] 健康检查失败（第 $CNT 次）" >>"$WLOG"

if [ "$CNT" -ge "$MAX_FAIL" ]; then
  echo "[$(date '+%F %T')] 连续失败 $CNT 次，执行重启" >>"$WLOG"
  systemctl restart cashflow-agent.service >>"$WLOG" 2>&1
  echo 0 >"$FAIL_FILE"
fi
# 日志自限，避免无限增长
tail -n 2000 "$WLOG" >"$WLOG.tmp" && mv "$WLOG.tmp" "$WLOG"
exit 0
