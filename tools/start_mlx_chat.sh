#!/bin/sh
set -eu
project=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$project/.venv/bin/python" "$project/tools/start_mlx_chat.py"
