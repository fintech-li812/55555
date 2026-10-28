#!/usr/bin/env bash
# =============================================================================
# install.sh —— 部署常驻 agent 服务（幂等）
#
#   sudo bash install.sh --src <源码目录> [--apply] [--force-config]
#
# 不带 --apply 时只做预演（打印将要执行什么），不改动系统。
# 高风险动作保护：
#   - 已存在的配置默认不覆盖，只写 *.new 并提示；
#   - 需要覆盖必须显式加 --force-config，且会先备份为 *.bak.<时间戳>；
#   - 任何 DROP / 删库 / 删用户的操作都不在本脚本内（见 rollback.sh 的 --drop-data）。
# =============================================================================
set -uo pipefail

SRC=""
APPLY=0
FORCE_CONFIG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --src) SRC="$2"; shift 2;;
    --apply) APPLY=1; shift;;
    --force-config) FORCE_CONFIG=1; shift;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "未知参数：$1"; exit 1;;
  esac
done

APP_ROOT=/opt/cashflow-agent
CONF_DIR=/etc/cashflow-agent
STATE_DIR=/var/lib/cashflow-agent
LOG_DIR=/var/log/cashflow-agent
SVC_USER=cfagent
DB_NAME=cf_workbench
DB_USER=cf_agent
RELEASE_TS="$(date +%Y%m%d-%H%M%S)"

log(){ echo "[install] $*"; }
die(){ echo "[install][FATAL] $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "必须用 root 执行（仅部署用；常驻进程由 $SVC_USER 运行）"
[ -n "$SRC" ] || die "缺少 --src <源码目录>"
[ -d "$SRC/app" ] || die "$SRC 下找不到 app/，不是合法的源码目录"

if [ "$APPLY" -ne 1 ]; then
  log "预演模式（未加 --apply），以下动作不会执行："
  log "  1. 创建系统账号 $SVC_USER 与目录 $APP_ROOT $CONF_DIR $STATE_DIR $LOG_DIR"
  log "  2. 建立 venv 并安装 requirements.txt"
  log "  3. 发布版本到 $APP_ROOT/releases/$RELEASE_TS 并切换 current 软链"
  log "  4. 生成/更新 $CONF_DIR/config.yaml 与 agent.env（0600）"
  log "  5. 建 MySQL 账号 $DB_USER@localhost（SELECT+INSERT）与 agent_* 表"
  log "  6. 安装 systemd 单元并启动服务"
  log "确认无误后重跑：sudo bash install.sh --src $SRC --apply"
  exit 0
fi

# ---------- 1. 账号与目录 ----------
log "1/6 创建服务账号与目录"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin "$SVC_USER" \
    || die "创建用户 $SVC_USER 失败"
  log "  已创建系统账号 $SVC_USER（nologin，不可交互登录）"
else
  log "  账号 $SVC_USER 已存在，跳过"
fi

install -d -o root -g "$SVC_USER" -m 0750 "$APP_ROOT" "$CONF_DIR"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$STATE_DIR" "$LOG_DIR"
install -d -o root -g "$SVC_USER" -m 0755 "$APP_ROOT/releases"
log "  目录就绪"

# ---------- 2. venv 与依赖 ----------
log "2/6 建立 Python 虚拟环境并安装依赖"
PY="$(command -v python3 || true)"
[ -n "$PY" ] || die "python3 未安装"
if [ ! -x "$APP_ROOT/venv/bin/python" ]; then
  "$PY" -m venv "$APP_ROOT/venv" || die "创建 venv 失败（若报 ensurepip 缺失，需 apt install python3-venv）"
fi
PIP_INDEX="$(pip3 config get global.index-url 2>/dev/null || true)"
PIP_TRUST=""
if [ -n "$PIP_INDEX" ]; then
  HOST="$(echo "$PIP_INDEX" | sed -E 's#^[a-z]+://([^/]+).*#\1#')"
  [ -n "$HOST" ] && PIP_TRUST="--trusted-host $HOST"
  log "  检测到 pip 源 $PIP_INDEX，附加 $PIP_TRUST"
fi
"$APP_ROOT/venv/bin/python" -m pip install --upgrade pip $PIP_TRUST >/dev/null 2>&1 || true
# shellcheck disable=SC2086
"$APP_ROOT/venv/bin/python" -m pip install -r "$SRC/requirements.txt" $PIP_TRUST \
  || die "依赖安装失败（检查 pip 源与网络）"
log "  依赖安装完成"

# ---------- 3. 发布版本 ----------
log "3/6 发布到 $APP_ROOT/releases/$RELEASE_TS"
RELEASE="$APP_ROOT/releases/$RELEASE_TS"
mkdir -p "$RELEASE"
cp -a "$SRC"/app "$SRC"/scripts "$SRC"/sql "$SRC"/systemd "$SRC"/config "$SRC"/tests "$SRC"/requirements.txt "$RELEASE"/ 2>/dev/null
[ -f "$SRC/README.md" ] && cp -a "$SRC/README.md" "$RELEASE"/
chown -R root:"$SVC_USER" "$RELEASE"
chmod -R o-rwx "$RELEASE"
ln -sfn "$RELEASE" "$APP_ROOT/current"
log "  current -> $RELEASE"

# ---------- 4. 配置 ----------
log "4/6 生成配置文件"
install_config(){
  local tpl="$1" dest="$2" mode="$3"
  if [ -f "$dest" ]; then
    if [ "$FORCE_CONFIG" -eq 1 ]; then
      cp -a "$dest" "$dest.bak.$RELEASE_TS"
      install -o root -g "$SVC_USER" -m "$mode" "$tpl" "$dest"
      log "  已覆盖 $dest（原文件备份为 $dest.bak.$RELEASE_TS）"
    else
      install -o root -g "$SVC_USER" -m "$mode" "$tpl" "$dest.new"
      log "  $dest 已存在，不覆盖；新版本写入 $dest.new，请人工核对后替换"
    fi
  else
    install -o root -g "$SVC_USER" -m "$mode" "$tpl" "$dest"
    log "  已生成 $dest"
  fi
}
install_config "$SRC/config/config.yaml.template" "$CONF_DIR/config.yaml" 0640

if [ ! -f "$CONF_DIR/agent.env" ]; then
  DB_PW="$(openssl rand -base64 24 | tr -d '\n')"
  API_TOKEN="$(openssl rand -hex 32)"
  sed -e "s#__CHANGE_ME__#$DB_PW#" "$SRC/config/agent.env.template" > "$CONF_DIR/agent.env.tmp"
  sed -i -e "s#__CHANGE_ME__#$API_TOKEN#" "$CONF_DIR/agent.env.tmp"
  install -o root -g "$SVC_USER" -m 0600 "$CONF_DIR/agent.env.tmp" "$CONF_DIR/agent.env"
  rm -f "$CONF_DIR/agent.env.tmp"
  log "  已生成 $CONF_DIR/agent.env（0600 root:$SVC_USER），含随机数据库口令与接口令牌"
  GEN_NEW_DB=1
else
  log "  $CONF_DIR/agent.env 已存在，保留现有口令与令牌"
  GEN_NEW_DB=0
fi

# ---------- 5. 数据库 ----------
log "5/6 初始化数据库账号与表"
command -v mysql >/dev/null 2>&1 || die "mysql 客户端未安装"
if [ "$GEN_NEW_DB" -eq 1 ]; then
  DB_PW="$(grep '^CF_DB_PASSWORD=' "$CONF_DIR/agent.env" | cut -d= -f2-)"
  SQL_TMP="$(mktemp)"
  sed "s#__CF_AGENT_PASSWORD__#$DB_PW#" "$SRC/sql/init_agent.sql" > "$SQL_TMP"
  mysql -u root < "$SQL_TMP" || die "数据库初始化失败（已中止，未启动服务）"
  rm -f "$SQL_TMP"
  log "  已创建 $DB_USER@localhost 与 agent_* 表"
else
  SQL_TMP="$(mktemp)"
  sed "s#__CF_AGENT_PASSWORD__#__UNCHANGED__#" "$SRC/sql/init_agent.sql" \
    | grep -v "IDENTIFIED BY" > "$SQL_TMP"
  mysql -u root < "$SQL_TMP" || die "数据库建表失败"
  rm -f "$SQL_TMP"
  log "  表结构已是幂等（CREATE TABLE IF NOT EXISTS），账号口令未改动"
fi
# 复核权限：必须只有 SELECT, INSERT
mysql -u root -N -B -e "SHOW GRANTS FOR '$DB_USER'@'localhost';" 2>/dev/null | tee /tmp/cf-agent-grants.txt
grep -qiE 'ALL PRIVILEGES|DROP|DELETE|UPDATE' /tmp/cf-agent-grants.txt \
  && log "  警告：检测到超出预期的权限，请人工复核" \
  || log "  权限复核通过：仅 SELECT + INSERT"

# ---------- 6. systemd ----------
log "6/6 安装 systemd 单元并启动"
install -o root -g root -m 0644 "$SRC/systemd/cashflow-agent.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SRC/systemd/cashflow-agent-watchdog.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SRC/systemd/cashflow-agent-watchdog.timer" /etc/systemd/system/
install -o root -g root -m 0644 "$SRC/systemd/logrotate.conf" /etc/logrotate.d/cashflow-agent
systemctl daemon-reload
systemctl enable --now cashflow-agent.service || die "服务启动失败"
systemctl enable --now cashflow-agent-watchdog.timer >/dev/null 2>&1 || log "  watchdog timer 启用失败（不影响主服务）"

# ---------- 验收 ----------
sleep 3
PORT="$(grep -E '^  port:' "$CONF_DIR/config.yaml" | awk '{print $2}')"
echo
log "===== 部署完成，验收结果 ====="
systemctl is-active cashflow-agent.service | sed 's/^/  服务状态: /'
echo "  健康接口:"
curl -sS --max-time 5 "http://127.0.0.1:${PORT:-8090}/healthz" | head -c 800 | sed 's/^/    /'
echo
echo "  配置文件: $CONF_DIR/config.yaml （0640）"
echo "  敏感配置: $CONF_DIR/agent.env （0600，禁止提交仓库）"
echo "  日志目录: $LOG_DIR"
echo "  回滚命令: bash $APP_ROOT/current/scripts/rollback.sh --list"
log "提示：接口令牌在 $CONF_DIR/agent.env 的 CF_AGENT_API_TOKEN；请勿写入任何仓库或文档。"
