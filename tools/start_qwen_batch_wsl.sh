#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
qwen_env="${JSHI_QWEN_ENV:-/home/lenovo/miniconda3/envs/qwen3-asr}"
export TMPDIR="${TMPDIR:-/home/lenovo/tmp}"
export CC="${CC:-$qwen_env/bin/gcc}"
export CXX="${CXX:-$qwen_env/bin/g++}"
export PYTHONPATH="$project_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$qwen_env/bin/python" -m jshi.voice.qwen_batch_server \
  --model "${JSHI_QWEN_MODEL_PATH:-/home/lenovo/qwen3-asr/models/Qwen3-ASR-1.7B}" \
  --host 0.0.0.0 --port "${JSHI_QWEN_PORT:-8000}" \
  --batch-size "${JSHI_QWEN_BATCH_SIZE:-8}"
