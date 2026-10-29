#!/usr/bin/env bash
# 一次性验证：并发互斥、失败重试、无重复进程、终端钩子行为
PY=/opt/cashflow/venv/bin/python
PROG=/opt/cashflow/scripts/db_autoconnect.py
E=/opt/cashflow/deploy/.env

echo '=== 1) 并发互斥：A 占住锁重试时，B 应立即返回 4（不叠加进程）==='
runuser -u cfrun -- env CF_DB_PORT=3399 "$PY" -B "$PROG" --env-file "$E" \
    --attempts 8 --delay 1 --max-total 30 >/tmp/instA.log 2>&1 &
APID=$!
sleep 2
BEFORE=$(ps -eo args | grep -c '[d]b_autoconnect.py')
runuser -u cfrun -- env CF_DB_PORT=3399 "$PY" -B "$PROG" --env-file "$E" \
    --attempts 8 --delay 1 --max-total 30 --quiet
BCODE=$?
DURING=$(ps -eo args | grep -c '[d]b_autoconnect.py')
echo "  实例B 退出码=$BCODE    （4 = 识别到已有实例并跳过）"
echo "  B 执行前/执行中的进程数: $BEFORE / $DURING   （相等说明没有叠加第二个进程）"
wait $APID; ACODE=$?
echo "  实例A 退出码=$ACODE    （3 = 重试耗尽后放弃）"
echo '  A 的日志尾部：'
tail -4 /tmp/instA.log | sed 's/^/    /'
rm -f /tmp/instA.log

echo
echo '=== 2) 失败路径：单独跑一次错端口，记录重试次数 ==='
runuser -u cfrun -- env CF_DB_PORT=3399 "$PY" -B "$PROG" --env-file "$E" \
    --attempts 3 --delay 1 --max-total 10 2>&1 | tail -5 | sed 's/^/    /'
echo "  退出码=${PIPESTATUS[0]}"

echo
echo '=== 3) 终端钩子：模拟一次交互式登录 shell ==='
bash -lic 'echo 交互 shell 已启动' 2>&1 | tail -3 | sed 's/^/    /'
echo "  刚跑完是否还在连：$([ -f /run/cashflow/db-ready ] && echo 就绪标记存在 || echo 无标记)"

echo
echo '=== 4) 连续跑 5 次，验证既不重复连接也不产生脏数据 ==='
for i in 1 2 3 4 5; do
  runuser -u cfrun -- "$PY" -B "$PROG" --env-file "$E" --once --fresh-secs 300 --quiet
  printf '%s ' "$?"
done
echo ' <- 五次退出码（全 0）'
echo "  近 10 条日志："
tail -10 /var/log/cashflow/db-autoconnect.log | sed 's/^/    /'
