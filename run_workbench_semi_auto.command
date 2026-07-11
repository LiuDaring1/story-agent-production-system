#!/bin/zsh
cd "$(dirname "$0")"
if [ -x "/opt/miniconda3/bin/python3" ]; then
  /opt/miniconda3/bin/python3 workbench_semi_auto_app.py
else
  python3 workbench_semi_auto_app.py
fi
