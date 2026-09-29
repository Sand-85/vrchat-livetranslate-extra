#!/usr/bin/env bash
# 别人说话 → 中文显示在手腕屏（与 run_overlay.bat 对应）。
#
# Linux 上的手腕屏由本程序**自建 OpenXR overlay** 承担：直接作为 XR_EXTX_overlay
# 会话连 Monado / WiVRn 的合成器，不需要 WayVR 之类的第三方管理器、不写配置、不重启。
# 需要：
#   1) 装了 pyopenxr（./setup.sh 会装，见 requirements-linux.txt）
#   2) OpenXR 运行时在跑（Monado 或 WiVRn），且头显已连接
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PY=".venv/bin/python"

echo "============================================"
echo "  别人说话 -> 中文显示在手腕屏"
echo "============================================"
echo "  停止：按 Ctrl+C"
echo

if ! "$PY" -c "import xr" >/dev/null 2>&1; then
    echo "[!] 没装 pyopenxr —— 手腕屏起不来（其它腿不受影响）。"
    echo "    重跑 ./setup.sh，或 pip install -r requirements-linux.txt"
    echo "    或改用 ./run_chatbox.sh（纯文本气泡，不需要 VR）"
    echo
fi

echo "说明：手腕屏需要 OpenXR 运行时（Monado / WiVRn）在跑、且头显已连接；"
echo "      采集的是**系统播放输出**（PipeWire 的 sink monitor），"
echo "      所以 VRChat 的声音必须真的在放（静音/未开始播放就采不到）。"
echo

exec "$PY" -m vlt.app --direction theirs --loopback --sink overlay "$@"
