#!/usr/bin/env bash
# Weekly rolling-forecast run.
#
# Produces a timestamped result file, points a stable "latest" symlink at it,
# prunes results older than the retention window, and mirrors the log line to
# /var/log/rollcast/. Exit status is the pipeline's exit status, so a failure
# is visible to systemd rather than being masked by the wrapper.
set -uo pipefail

BASE=/opt/cashflow
OUT=/var/lib/rollcast/out
LOGDIR=/var/log/rollcast
JOB="$BASE/rollcast/jobs/weekly.json"
CONFIG="$BASE/rollcast/config/production.json"
KEEP_DAYS=${ROLLCAST_KEEP_DAYS:-120}

mkdir -p "$OUT" "$LOGDIR"

if [ ! -f "$JOB" ]; then
    echo "[rollcast] 作业文件缺失: $JOB" >&2
    exit 4
fi

STAMP=$(date +%Y%m%d-%H%M%S)
TARGET="$OUT/run-$STAMP.json"
LOGFILE="$LOGDIR/rollcast-$STAMP.log"

echo "[rollcast] 开始 $STAMP  job=$JOB" | tee -a "$LOGFILE"

"$BASE/venv/bin/python" -B -u -m rollcast.cli \
    "$JOB" \
    --output "$TARGET" \
    --config "$CONFIG" \
    --persist --json-logs 2>&1 | tee -a "$LOGFILE"
STATUS=${PIPESTATUS[0]}

# Point the stable symlink at this run only when it produced a result, so
# `latest.json` never refers to a half-written file.
if [ "$STATUS" -eq 0 ] || [ "$STATUS" -eq 2 ]; then
    ln -sfn "run-$STAMP.json" "$OUT/latest.json"
fi

find "$OUT" -name 'run-*.json' -mtime +"$KEEP_DAYS" -delete
find "$LOGDIR" -name 'rollcast-*.log' -mtime +"$KEEP_DAYS" -delete

echo "[rollcast] 结束 status=$STATUS  out=$TARGET" | tee -a "$LOGFILE"
exit "$STATUS"
