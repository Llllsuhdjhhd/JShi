#!/bin/sh
set -eu
root=${JSHI_LOCAL_AI_DIR:-"$HOME/local-ai"}
export HF_HOME="$root/hf" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
exec "$root/venv/bin/mlx_audio.server" --host 127.0.0.1 --port 18766 --log-dir "$root/logs"
