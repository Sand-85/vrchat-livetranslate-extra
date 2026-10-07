#!/usr/bin/env bash
# VRChat 实时同传 —— Linux 环境安装
#
# 做的事：建 Python 3.11 虚拟环境 → 装依赖 → 体检系统层依赖（PipeWire / OpenXR）→ 提示下一步
#
# 为什么单独钉 3.11：项目在 Windows 侧就是 3.11 验证的；本机若是更新的版本
# （如 3.14）不一定每个依赖都有 wheel。用独立 venv 而不是系统解释器，
# 也不污染系统 Python。
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

VENV=".venv"
PY="$VENV/bin/python"

echo "============================================"
echo "  VRChat 实时同传 - Linux 环境安装"
echo "============================================"
echo

# ---------------------------------------------------------------- 1. 建 venv
if [ -x "$PY" ]; then
    echo "[1/4] 已有虚拟环境，跳过创建（$( "$PY" -V )）"
else
    echo "[1/4] 创建 Python 3.11 虚拟环境 ..."
    if command -v uv >/dev/null 2>&1; then
        # uv 会自动拉一份 python-build-standalone 的 3.11，不需要系统预装
        uv venv --python 3.11 "$VENV" || { echo "[X] uv 创建虚拟环境失败"; exit 1; }
    elif command -v python3.11 >/dev/null 2>&1; then
        python3.11 -m venv "$VENV" || { echo "[X] 创建虚拟环境失败"; exit 1; }
    else
        echo "[X] 找不到 Python 3.11，也没有 uv。二选一："
        echo "      pacman -S python311          （或用 AUR 的对应包）"
        echo "      或装 uv： pacman -S uv"
        exit 1
    fi
fi
echo

# ---------------------------------------------------------------- 2. 装依赖
echo "[2/4] 安装依赖 ..."
if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PY" -r requirements-linux.txt
else
    # uv 建的 venv 不带 pip，所以优先用 uv；没 uv 就要求普通 venv（带 pip）
    "$PY" -m pip install -r requirements-linux.txt
fi || { echo "[X] 依赖安装失败"; exit 1; }
echo

# ---------------------------------------------------------------- 3. 系统层体检
echo "[3/4] 系统依赖体检 ..."
missing=0

check_cmd() {  # 名字 用途 安装提示
    if command -v "$1" >/dev/null 2>&1; then
        echo "      OK   $1（$2）"
    else
        echo "      缺   $1（$2）→ $3"
        missing=1
    fi
}

check_cmd pw-dump  "枚举音频设备"     "pacman -S pipewire"
check_cmd pw-record "采集系统声 / 麦克风" "pacman -S pipewire"
check_cmd pw-cat   "写入虚拟声卡（译音输出）"   "pacman -S pipewire"

# 手腕屏 → 自建 OpenXR overlay：需要 pyopenxr（python 侧）+ OpenXR 运行时 + EGL/Wayland
if "$PY" -c "import xr" >/dev/null 2>&1; then
    echo "      OK   pyopenxr（手腕屏）"
else
    echo "      缺   pyopenxr（手腕屏）→ 重跑本脚本，或 pip install -r requirements-linux.txt"
    missing=1
fi

if ls /usr/share/openxr/1/*.json >/dev/null 2>&1 \
   || [ -f "$HOME/.config/openxr/1/active_runtime.json" ] \
   || [ -n "${XR_RUNTIME_JSON:-}" ]; then
    echo "      OK   OpenXR 运行时（Monado / WiVRn）"
    # ⚠️ 上面这条判据只能说明「装了运行时」：清单在 /usr/share/openxr/1/ 只代表有这个运行时，
    #    真正生效的是「活跃运行时」指针（XR_RUNTIME_JSON 或 ~/.config/openxr/1/active_runtime.json）。
    if [ -z "${XR_RUNTIME_JSON:-}" ] && [ ! -f "$HOME/.config/openxr/1/active_runtime.json" ]; then
        echo "           ⚠️ 但没看到「活跃运行时」指针（XR_RUNTIME_JSON / ~/.config/openxr/1/active_runtime.json）"
        echo "              → 手腕屏可能报 RuntimeUnavailableError；用 wivrn-dashboard 或 Envision 选中一个运行时"
    fi
else
    echo "      缺   OpenXR 运行时（手腕屏，可选）"
    echo "           → Monado：pacman -S monado    WiVRn：AUR wivrn-server"
    echo "             （不装的话只用 chatbox 也行）"
fi

if [ -e /usr/lib/libEGL.so.1 ] && [ -e /usr/lib/libwayland-client.so.0 ]; then
    echo "      OK   libEGL / libwayland-client（手腕屏 GL 绑定）"
else
    echo "      缺   libEGL / libwayland-client → pacman -S mesa libglvnd wayland"
    missing=1
fi

# 麦克风采集自 2026-10 走 PipeWire 原生 `pw-record`（与 loopback 同一条通路）：
# 不再需要 sounddevice/libportaudio，也不依赖可选包 pipewire-jack / pipewire-alsa。
echo "      ℹ️   麦克风走 PipeWire 原生 pw-record（上面 pw-* 检查已覆盖；无需 portaudio/JACK）"
echo

# ---------------------------------------------------------------- 4. API key
echo "[4/4] API key ..."
if [ -n "${DASHSCOPE_API_KEY:-}" ]; then
    echo "      OK   读到环境变量 DASHSCOPE_API_KEY"
elif [ -f "$HOME/.bailian/config.json" ]; then
    echo "      OK   读到百炼 CLI 配置 ~/.bailian/config.json"
elif [ -f "$HOME/.vrchat-livetranslate/api_key.txt" ]; then
    echo "      OK   读到此前的界面保存的 key"
else
    echo "      还没配置。二选一："
    echo "        export DASHSCOPE_API_KEY=\"你的key\"     # 建议写进 ~/.zshrc"
    echo "        bl auth login --api-key \"你的key\""
    echo "      （也可以先不配，启动界面后在「设置」里填）"
fi
echo

echo "============================================"
if [ "$missing" -eq 1 ]; then
    echo "  安装完成，但上面标「缺」的系统依赖要补一下"
else
    echo "  安装完成"
fi
echo "============================================"
echo "下一步："
echo "  1) 图形界面：  ./run_gui.sh"
echo "  2) 自检：      ./run_selfcheck.sh"
echo "  3) 只要文本：  ./run_chatbox.sh"
echo "  4) 手腕屏：    ./run_overlay.sh"
