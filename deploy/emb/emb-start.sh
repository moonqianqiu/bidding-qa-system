#!/usr/bin/env bash
# bge-m3 嵌入服务启动（用户级 nohup，风格对齐 ../qwen-start.sh）
set -euo pipefail
BASE="${EMB_BASE:-$HOME/group_4/z/emb}"
MODEL="$BASE/models/bge-m3"
KEY="$BASE/run/api-keys.txt"
LOG="$BASE/logs/emb.log"
PID="$BASE/run/emb.pid"
HOST="${EMB_HOST:-0.0.0.0}"
PORT="${EMB_PORT:-8002}"

mkdir -p "$BASE/logs" "$BASE/run"
if [[ ! -s "$KEY" ]]; then
  echo "Missing API key file: $KEY" >&2
  exit 1
fi
if [[ ! -f "$MODEL/config.json" ]]; then
  echo "Missing model dir (run download-model.sh first): $MODEL" >&2
  exit 1
fi
if [[ -f "$PID" ]] && kill -0 "$(<"$PID")" 2>/dev/null; then
  echo "emb service already running (PID $(<"$PID"))"
  exit 1
fi
cd "$BASE/app"
export HF_HOME="$BASE/models/.hf"
export EMB_API_KEY_FILE="$KEY" EMB_MODEL_PATH="$MODEL" EMB_DEVICE="${EMB_DEVICE:-cuda:0}"
nohup "$BASE/venv/bin/python" -m uvicorn server:app \
  --host "$HOST" --port "$PORT" --workers 1 --log-level info \
  </dev/null >>"$LOG" 2>&1 &
echo $! > "$PID"
echo "Started bge-m3 embeddings on $HOST:$PORT (PID $(<"$PID")); log: $LOG"
