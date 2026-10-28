#!/usr/bin/env bash
# =============================================================================
# rollback.sh —— 回滚（默认只切版本，绝不删数据）
#
#   bash rollback.sh --list
#   sudo bash rollback.sh --to <release目录名或版本时间戳>
#   sudo bash rollback.sh --stop                 # 停服务但不卸载
#   sudo bash rollback.sh --drop-data            # 危险：删除 agent_* 表与 cf_agent 账号
#
# 二次确认：--drop-data 必须交互式输入 "DELETE DATA" 才会执行，否则一律拒绝。
# =============================================================================
set -uo pipefail

APP_ROOT=/opt/cashflow-agent
SVC=cashflow-agent.service
MODE=""
TARGET=""

while [ $# -gt 0 ]; do
  case "$1" in
    --list) MODE=list; shift;;
    --to) MODE=to; TARGET="$2"; shift 2;;
    --stop) MODE=stop; shift;;
    --drop-data) MODE=dropdata; shift;;
    -h|--help) sed -n '2,16p' "$0"; exit 0;;
    *) echo "未知参数：$1"; exit 1;;
  esac
done

case "$MODE" in
  list)
    echo "可用版本（current -> $(readlink -f "$APP_ROOT/current" 2>/dev/null || echo 无)）："
    ls -1 "$APP_ROOT/releases" 2>/dev/null | sed 's/^/  /' || echo "  无历史版本"
    exit 0;;
  stop)
    [ "$(id -u)" -eq 0 ] || { echo "需要 root"; exit 1; }
    systemctl stop "$SVC" && echo "服务已停止（数据保留）"
    exit 0;;
esac

[ "$(id -u)" -eq 0 ] || { echo "需要 root 执行"; exit 1; }

# ---------- 危险：删数据 ----------
if [ "$MODE" = "dropdata" ]; then
  echo "=============================================================="
  echo "  危险操作：将删除 cf_workbench 中全部 agent_* 表，并删除数据库账号 cf_agent。"
  echo "  此操作不可回滚。如需确认，请原样输入：DELETE DATA"
  echo "=============================================================="
  read -r -p "请输入确认短语 > " PHRASE
  [ "$PHRASE" = "DELETE DATA" ] || { echo "确认短语不匹配，已取消。未做任何改动。"; exit 1; }
  mysql -u root -e "USE cf_workbench; DROP TABLE IF EXISTS agent_card, agent_chat_log, agent_report, agent_alert, agent_inspection_item, agent_inspection;"
  mysql -u root -e "DROP USER IF EXISTS 'cf_agent'@'localhost';"
  systemctl stop "$SVC" 2>/dev/null || true
  systemctl disable "$SVC" 2>/dev/null || true
  echo "已删除 agent 相关表与账号；/opt/cashflow-agent 目录保留。"
  exit 0
fi

# ---------- 版本回滚 ----------
[ "$MODE" = "to" ] && [ -n "$TARGET" ] || { echo "用法见 --help"; exit 1; }

# 允许传完整路径或时间戳
if [ -d "$TARGET" ]; then REL="$TARGET"; else REL="$APP_ROOT/releases/$TARGET"; fi
[ -d "$REL" ] || { echo "目标版本不存在：$REL （用 --list 查看）"; exit 1; }

CUR="$(readlink -f "$APP_ROOT/current" 2>/dev/null || echo "")"
echo "当前版本: ${CUR:-无}"
echo "目标版本: $REL"
read -r -p "确认回滚？请输入 rollback > " PHRASE
[ "$PHRASE" = "rollback" ] || { echo "未确认，已取消。未做任何改动。"; exit 1; }

systemctl stop "$SVC" 2>/dev/null || true
ln -sfn "$REL" "$APP_ROOT/current"
systemctl start "$SVC"
sleep 3
echo "回滚完成："
systemctl is-active "$SVC" | sed 's/^/  服务状态: /'
PORT="$(grep -E '^  port:' /etc/cashflow-agent/config.yaml 2>/dev/null | awk '{print $2}')"
curl -sS --max-time 5 "http://127.0.0.1:${PORT:-8090}/healthz" | head -c 400 | sed 's/^/  健康: /'
echo
echo "数据未被删除（agent_* 表完整保留）。"
