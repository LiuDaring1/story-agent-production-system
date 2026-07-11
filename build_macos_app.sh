#!/usr/bin/env bash
set -euo pipefail

APP_NAME="儿童故事视频合成器"
EXTRA_ARGS=()

export PYINSTALLER_CONFIG_DIR="$PWD/.pyinstaller-cache"

if [ -d "models" ]; then
  EXTRA_ARGS+=(--add-data "models:models")
fi

if command -v ffmpeg >/dev/null 2>&1; then
  EXTRA_ARGS+=(--add-binary "$(command -v ffmpeg):.")
fi

if command -v ffprobe >/dev/null 2>&1; then
  EXTRA_ARGS+=(--add-binary "$(command -v ffprobe):.")
fi

python3 -m PyInstaller \
  --distpath dist_pyinstaller_full \
  --workpath build_pyinstaller_full \
  --windowed \
  --name "$APP_NAME" \
  --hidden-import synthesize \
  --collect-all whisper \
  --collect-all tiktoken \
  "${EXTRA_ARGS[@]}" \
  desktop_app.py
