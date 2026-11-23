#!/usr/bin/env bash
# bge-m3 嵌入服务停止（SIGTERM → 30s 宽限，风格对齐 ../stop.sh）
set -euo pipefail
BASE="${EMB_BASE:-$HOME/group_4/z/emb}"
PID_FILE="$BASE/run/emb.pid"

if [[ ! -f "$PID_FILE" ]]; then
  echo "No emb PID file found."
  exit 0
fi

PID="$(<"$PID_FILE")"
if [[ ! "$PID" =~ ^[0-9]+$ ]]; then
  echo "Invalid PID in $PID_FILE; refusing to stop anything." >&2
  exit 1
fi
if ! kill -0 "$PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "Removed stale PID file; emb service is not running."
  exit 0
fi

CMD="$(ps -p "$PID" -o args=)"
if [[ "$CMD" != *"$BASE/venv/bin/python"* || "$CMD" != *"uvicorn"* ]]; then
  echo "PID $PID is not the emb service in $BASE; refusing to stop it." >&2
  exit 1
fi

kill -TERM "$PID"
for _ in {1..30}; do
  if ! kill -0 "$PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "Stopped emb service (PID $PID)."
    exit 0
  fi
  sleep 1
done

echo "emb service (PID $PID) did not exit within 30 seconds; PID file retained." >&2
exit 1
