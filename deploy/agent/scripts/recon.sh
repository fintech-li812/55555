#!/usr/bin/env bash
# =============================================================================
# recon.sh —— 只读环境采集。不安装、不修改、不删任何东西，不打印任何口令。
# 用法： sudo bash recon.sh [输出文件]
# =============================================================================
set -uo pipefail

OUT="${1:-/tmp/cf-agent-recon.txt}"
: >"$OUT"
say(){ echo "$*"; echo "$*" >>"$OUT"; }
hr(){ echo "----------------------------------------" >>"$OUT"; }

say "===== 采集时间 ====="; date >>"$OUT"; hr

say "===== 1. 系统 ====="
uname -a >>"$OUT"
grep -E '^(NAME|VERSION|VERSION_ID|ID)=' /etc/os-release 2>/dev/null >>"$OUT" || echo "无 /etc/os-release" >>"$OUT"
echo "init: $(ps -p 1 -o comm= 2>/dev/null)" >>"$OUT"
echo "systemd: $(systemctl --version 2>/dev/null | head -1 || echo 未安装)" >>"$OUT"
hr

say "===== 2. 资源 ====="
echo "CPU 核数: $(nproc 2>/dev/null)" >>"$OUT"
free -h >>"$OUT"
echo "--- swap ---" >>"$OUT"; swapon --show 2>/dev/null >>"$OUT" || echo "无 swap" >>"$OUT"
echo "--- 磁盘 ---" >>"$OUT"; df -h / /var /opt 2>/dev/null >>"$OUT"
hr

say "===== 3. 运行时 ====="
PY="$(command -v python3 || true)"
echo "python3: ${PY:-未安装}" >>"$OUT"
[ -n "$PY" ] && "$PY" -V >>"$OUT" 2>&1
echo "pip3: $(command -v pip3 || echo 未安装)" >>"$OUT"
echo "venv 模块: $("$PY" -c 'import venv;print("可用")' 2>/dev/null || echo 不可用)" >>"$OUT"
echo "node: $(command -v node || echo 未安装) $("node" -v 2>/dev/null)" >>"$OUT"
echo "npm: $(command -v npm || echo 未安装)" >>"$OUT"
echo "docker: $(command -v docker || echo 未安装) $("docker" --version 2>/dev/null)" >>"$OUT"
echo "nginx: $(command -v nginx || echo 未安装)" >>"$OUT"
echo "curl: $(command -v curl || echo 未安装)" >>"$OUT"
hr

say "===== 4. MySQL ====="
echo "mysql 客户端: $(command -v mysql || echo 未安装)" >>"$OUT"
if command -v mysql >/dev/null 2>&1; then
  echo "服务端版本: $(mysql -u root -N -B -e 'SELECT VERSION();' 2>/dev/null || echo 'root 免密不可用')" >>"$OUT"
  echo "--- 监听（关键：不得出现 0.0.0.0:3306）---" >>"$OUT"
  ss -lntp 2>/dev/null | grep -E ':3306|:33060' >>"$OUT" || echo "未监听或 ss 不可用" >>"$OUT"
  echo "--- 数据库清单 ---" >>"$OUT"
  mysql -u root -N -B -e 'SHOW DATABASES;' 2>/dev/null >>"$OUT" || echo "无法列举" >>"$OUT"
  echo "--- 账号与 host（不打印口令）---" >>"$OUT"
  mysql -u root -N -B -e "SELECT user, host FROM mysql.user;" 2>/dev/null >>"$OUT" || echo "无法列举" >>"$OUT"
  echo "--- cf_workbench 现有表 ---" >>"$OUT"
  mysql -u root -N -B -e "SELECT table_name FROM information_schema.tables WHERE table_schema='cf_workbench';" 2>/dev/null >>"$OUT" || echo "无法列举" >>"$OUT"
fi
hr

say "===== 5. 端口占用（全部监听）====="
ss -lntup 2>/dev/null | head -40 >>"$OUT" || netstat -lntup 2>/dev/null | head -40 >>"$OUT"
hr

say "===== 6. 目标目录现状 ====="
for d in /opt/cashflow-agent /etc/cashflow-agent /var/lib/cashflow-agent /var/log/cashflow-agent /opt/CashflowRiskWorkbench; do
  if [ -e "$d" ]; then
    echo "[存在] $d" >>"$OUT"; ls -ld "$d" >>"$OUT"
    ls -l "$d" 2>/dev/null | head -12 >>"$OUT"
  else
    echo "[不存在] $d" >>"$OUT"
  fi
done
echo "--- 服务账号 ---" >>"$OUT"
id cfagent 2>/dev/null >>"$OUT" || echo "cfagent 用户不存在" >>"$OUT"
hr

say "===== 7. 已存在的同名 systemd 单元 ====="
systemctl list-units --all 2>/dev/null | grep -i -E 'cashflow|cfagent' >>"$OUT" || echo "无" >>"$OUT"
ls -l /etc/systemd/system/ 2>/dev/null | grep -i -E 'cashflow|cfagent' >>"$OUT" || echo "无单元文件" >>"$OUT"
hr

say "===== 8. pip 源与依赖可达性 ====="
pip3 config list 2>/dev/null >>"$OUT" || echo "无 pip 配置" >>"$OUT"
echo "--- 连通性探测（不安装任何东西）---" >>"$OUT"
IDX="$(pip3 config get global.index-url 2>/dev/null || echo https://pypi.org/simple)"
echo "index-url: $IDX" >>"$OUT"
timeout 12 curl -sS -o /dev/null -w "index HTTP %{http_code}\n" "$IDX" 2>&1 >>"$OUT" || echo "index 不可达" >>"$OUT"
hr

say "===== 9. 结论（自动判定）====="
[ -n "$PY" ] && echo "python3 可用：$("$PY" -V 2>&1)" >>"$OUT" || echo "python3 缺失：需先安装" >>"$OUT"
if ss -lnt 2>/dev/null | grep -q '0.0.0.0:3306'; then echo "警告：MySQL 正在监听公网地址！" >>"$OUT"; else echo "MySQL 未监听公网：OK" >>"$OUT"; fi
if [ "$(id -u)" -eq 0 ]; then echo "当前以 root 执行：OK（仅部署用，常驻进程不用 root）" >>"$OUT"; else echo "当前非 root：部分采集可能受限" >>"$OUT"; fi
hr

echo
echo "采集完成：$OUT"
echo "本脚本未做任何修改。"
