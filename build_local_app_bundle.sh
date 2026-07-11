#!/usr/bin/env bash
set -euo pipefail

APP_NAME="儿童故事视频合成器"
APP_DIR="dist/${APP_NAME}.app"
RESOURCES_DIR="${APP_DIR}/Contents/Resources/app"
MACOS_DIR="${APP_DIR}/Contents/MacOS"

rm -rf "$APP_DIR"
mkdir -p "$RESOURCES_DIR" "$MACOS_DIR"

cp -R story_video_synthesizer "$RESOURCES_DIR/"
cp desktop_app.py synthesize.py requirements.txt README.md "$RESOURCES_DIR/"

if [ -d "models" ]; then
  cp -R models "$RESOURCES_DIR/"
fi

cat > "${APP_DIR}/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>${APP_NAME}</string>
  <key>CFBundleIdentifier</key>
  <string>com.storyvideo.synthesizer</string>
  <key>CFBundleName</key>
  <string>${APP_NAME}</string>
  <key>CFBundleDisplayName</key>
  <string>${APP_NAME}</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.1.0</string>
  <key>LSMinimumSystemVersion</key>
  <string>11.0</string>
</dict>
</plist>
PLIST

cat > "${MACOS_DIR}/${APP_NAME}" <<'LAUNCHER'
#!/bin/zsh
APP_ROOT="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
cd "$APP_ROOT"
export KMP_DUPLICATE_LIB_OK=TRUE
export OMP_NUM_THREADS=1
export MKL_NUM_THREADS=1
export NUMBA_NUM_THREADS=1
export NUMBA_THREADING_LAYER=workqueue
exec /opt/miniconda3/bin/python3 desktop_app.py
LAUNCHER

chmod +x "${MACOS_DIR}/${APP_NAME}"
echo "已生成：${APP_DIR}"
