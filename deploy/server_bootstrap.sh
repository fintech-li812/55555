#!/usr/bin/env bash
# Server bootstrap for the cashflow workbench (Linux).
# Prints environment facts, then prepares storage, service and reverse proxy.
#
# Deliberately does NOT: install MySQL, open public ports, rebind MySQL,
# disable authentication, or print any password.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/CashflowRiskWorkbench}"
OUT=/tmp/cf-server-info.txt
: >"$OUT"
say(){ echo "$*"; echo "$*" >>"$OUT"; }
hr(){ echo "----------------------------------------" >>"$OUT"; }

say "===== 采集时间 ====="; date >>"$OUT"; hr
say "===== 系统 ====="; uname -a >>"$OUT"
grep -E '^(NAME|VERSION_ID)=' /etc/os-release 2>/dev/null >>"$OUT" || true; hr
say "===== CPU / 内存 ====="
echo "核数: $(nproc 2>/dev/null)" >>"$OUT"; free -h >>"$OUT"
df -h / /var 2>/dev/null >>"$OUT" || true; hr
say "===== 网络 ====="
hostname -I 2>/dev/null >>"$OUT" || ip -4 addr show scope global 2>/dev/null | grep -o 'inet [0-9.]*' >>"$OUT" || true; hr

say "===== 组件检测 ====="
PY="$(command -v python3 || true)"; echo "python3: ${PY:-未安装}" >>"$OUT"
if [ -n "$PY" ]; then "$PY" -V >>"$OUT" 2>&1; fi
echo "mysql: $(command -v mysql || echo 未安装)" >>"$OUT"
echo "nginx: $(command -v nginx || echo 未安装)" >>"$OUT"
echo "docker: $(command -v docker || echo 未安装)" >>"$OUT"
systemctl is-active mysql mysqld 2>/dev/null | head -2 >>"$OUT" || true
hr

if [ ! -d "$APP_DIR" ]; then
  say "项目目录不存在：$APP_DIR"
  say "请先把项目放到该目录，或设置 APP_DIR 后重跑。以下跳过部署，仅保留采集结果。"
  exit 0
fi
if [ -z "$PY" ]; then
  say "python3 未安装，无法继续部署。"
  exit 0
fi

say "===== 依赖 ====="
cd "$APP_DIR"
if [ -f mcp-server/requirements.txt ]; then
  "$PY" -m venv .venv 2>>"$OUT" || say "venv 创建失败（可能缺 python3-venv）"
  ./.venv/bin/pip install -q --disable-pip-version-check -r mcp-server/requirements.txt 2>>"$OUT" \
    && echo "依赖安装完成" >>"$OUT" || say "依赖安装失败，见上方输出"
else
  say "未找到 mcp-server/requirements.txt"
fi
hr

say "===== 存储 ====="
if ! command -v mysql >/dev/null 2>&1; then
  say "MySQL 未安装。本脚本不擅自安装数据库（避免破坏既有服务）。"
  say "请安装后重跑本脚本，或改用已有的 MySQL 实例并调整 .env。"
else
  say "MySQL 已就绪，准备建库建表建账号（需要交互输入 root 口令）。"
  ./.venv/bin/python deploy/setup_storage.py 2>&1 | tee -a "$OUT"
fi
hr

say "===== systemd ====="
SVC=/etc/systemd/system/cashflow-workbench.service
if [ -d /etc/systemd/system ]; then
  cat >"$SVC" <<EOF
[Unit]
Description=Cashflow Workbench
After=network.target mysql.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/deploy/serve.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ReadWritePaths=/tmp

[Install]
WantedBy=multi-user.target
EOF
  echo "已写入 $SVC" >>"$OUT"
  systemctl daemon-reload >>"$OUT" 2>&1 || true
  systemctl enable cashflow-workbench >>"$OUT" 2>&1 || true
  systemctl restart cashflow-workbench >>"$OUT" 2>&1 || say "启动失败（数据库未配置时属预期）"
  sleep 2
  systemctl is-active cashflow-workbench >>"$OUT" 2>&1 || true
else
  say "非 systemd 系统，跳过服务单元。"
fi
hr

say "===== Nginx ====="
if command -v nginx >/dev/null 2>&1; then
  cat >/etc/nginx/conf.d/cashflow-workbench.conf <<'EOF'
server {
    listen 80;
    server_name _;
    location / {
        root /opt/CashflowRiskWorkbench/web;
        index 工作台_程序计算版.html;
        try_files $uri $uri/ /工作台_程序计算版.html;
    }
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 30s;
    }
}
EOF
  echo "已写入 nginx 配置（HTTP 80，未开 HTTPS）" >>"$OUT"
  nginx -t >>"$OUT" 2>&1 && systemctl reload nginx >>"$OUT" 2>&1 || say "nginx 配置校验或重载失败"
else
  say "nginx 未安装，跳过反代；应用仍监听 127.0.0.1:8000。"
fi
hr

say "===== 自检 ====="
curl -s -o /dev/null -w "GET / -> %{http_code}\n" --max-time 8 http://127.0.0.1:8000/ >>"$OUT" 2>&1 || say "本地自检请求失败"
curl -s --max-time 8 "http://127.0.0.1:8000/api/status" >>"$OUT" 2>&1 || true
hr
say "完成。请把 $OUT 的内容贴回对话。"
