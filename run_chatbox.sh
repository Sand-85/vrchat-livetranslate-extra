#!/usr/bin/env bash
# 我说中文 → 译文进 VRChat chatbox 气泡（与 run_chatbox.bat 对应）。
#
# 这条腿**完全不依赖 VR**：chatbox 走的是 VRChat 的 OSC（UDP 127.0.0.1:9000），
# 所以 Linux 上不需要 OpenXR 运行时（Monado / WiVRn）也能用。
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PY=".venv/bin/python"

echo "============================================"
echo "  我说中文 -> 译文进 VRChat chatbox 气泡"
echo "============================================"
echo "  需要：VRChat 已运行 + OSC 已开"
echo "  停止：按 Ctrl+C"
echo

if [ -z "${DASHSCOPE_API_KEY:-}" ] \
   && [ ! -f "$HOME/.bailian/config.json" ] \
   && [ ! -f "$HOME/.vrchat-livetranslate/api_key.txt" ]; then
    echo "[X] 没找到 API key。二选一："
    echo "      export DASHSCOPE_API_KEY=\"你的key\""
    echo "      bl auth login --api-key \"你的key\""
    exit 1
fi

echo "目标语言在 config.yaml 的 directions.mine.target_lang 里改（默认 en）"
echo "麦克风设备：改 config.yaml 的 capture.mic_device，或在图形界面的设置里选"
echo

echo "--- 当前设备列表 ---"
"$PY" -m vlt.app --direction mine --mic --sink chatbox --list-devices --dry-run || true
echo "--------------------"
echo
echo "开始采集（默认设备）。Ctrl+C 结束。"
exec "$PY" -m vlt.app --direction mine --mic --sink chatbox "$@"
