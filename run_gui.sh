#!/usr/bin/env bash
# 启动图形界面（与 run_gui.bat 对应）。
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
exec .venv/bin/python -m vlt.gui "$@"
