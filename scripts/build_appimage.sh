#!/usr/bin/env bash
# 把 VRChat 实时同传打成 **AppImage**（Linux：双击即用，不需要装 Python / 依赖）。
#
# 用法：
#     ./scripts/build_appimage.sh              # 构建
#     ./scripts/build_appimage.sh --no-verify  # 只构建，不做冒烟检查
#
# 产物：dist/VRChatLiveTranslate-x86_64.AppImage
#
# ## 打进去什么、不打包什么
#
# | 内容 | 来源 | 说明 |
# |---|---|---|
# | Python 解释器 | uv 的独立 3.11 | **整份拷进去**，不依赖宿主机的 Python（实测可搬运） |
# | 第三方依赖 | `.venv` 的 site-packages | 剔掉 PyInstaller 之类只在打包时用的 |
# | 程序源码 | 仓库 | `vlt/` + `run_gui.py` + `assets/` + `config.example.yaml` + `testdata/` |
#
# **不打进包**：glibc / libGL / libEGL / libwayland / tk / libportaudio ——
# 这些是系统基础库，AppImage 的惯例是依赖宿主机（各发行版版本差异太大，自带反而更容易崩）。
#
# ## 数据写在哪
#
# AppImage 里源码是**只读**的 squashfs 挂载。所以 `vlt/paths.py` 认 `APPIMAGE`/`APPDIR`
# 环境变量，把 config.yaml / logs 写到 `$XDG_DATA_HOME/vrchat-livetranslate`
# （缺省 `~/.local/share/vrchat-livetranslate`）。见 tests/test_paths.py 的 AppImage 用例。
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
APP_ID="vrchat-livetranslate"
APP_NAME="VRChatLiveTranslate"
PYVER="3.11"
OUT_DIR="${REPO}/dist"
WORK="${REPO}/build/appimage"
APPDIR="${WORK}/AppDir"
TOOLS="${REPO}/build/tools"
die() { echo "[X] $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }

VERIFY=1
SLIM=1
for _arg in "$@"; do
    case "$_arg" in
        --no-verify) VERIFY=0 ;;
        --no-slim)   SLIM=0 ;;
        *) die "未知参数：$_arg（可用：--no-verify / --no-slim）" ;;
    esac
done
unset _arg

# ---------------------------------------------------------------- 0. 前置检查
step "0/6 检查前置条件"

VENV_PY="${REPO}/.venv/bin/python"
[ -x "$VENV_PY" ] || die "没有 .venv —— 先跑 ./setup.sh"

# 独立 Python：**必须**是 uv 管理的那份 standalone 解释器。
#
# ⚠️ 不能用 `uv python find` —— 在项目目录里它返回的是 **.venv 的解释器**，
#    而 venv **不可搬运**：`bin/python` 是指向 `~/.local/share/uv/...` 的绝对软链，
#    还带 pyvenv.cfg。打进 AppImage 后换台机器就起不来（本机测却「正常」，很难发现）。
#    所以直接去 uv 的 python 目录里找真身（加 --no-project 也没用，这里实测过）。
HOST_PY=""
if command -v uv >/dev/null 2>&1; then
    HOST_PY="$(find "$HOME/.local/share/uv/python" -maxdepth 3 -type f \
               -name "python${PYVER}" -path "*cpython-${PYVER}*" 2>/dev/null | sort -r | head -1 || true)"
fi
[ -n "$HOST_PY" ] && [ -x "$HOST_PY" ] || die \
    "找不到独立的 Python ${PYVER}。装个 uv（pacman -S uv）后先跑：
       uv python install ${PYVER}"
# 再确认一次「它真的是独立解释器」而不是 venv 的软链
if [ -f "$(dirname "$HOST_PY")/../pyvenv.cfg" ] || [ -L "$HOST_PY" ]; then
    die "找到的 Python 看着像 venv/软链（$HOST_PY）—— 这样打进包不可搬运，请检查"
fi
echo "    Python   : $HOST_PY"
echo "    虚拟环境 : $VENV_PY"

# appimagetool：本机没有就下官方 release 到 build/tools（**不动系统**）
APPIMAGETOOL=""
resolve_appimagetool() {
    if [ -n "${APPIMAGETOOL:-}" ] && [ -x "$APPIMAGETOOL" ]; then return 0; fi
    if command -v appimagetool >/dev/null 2>&1; then APPIMAGETOOL="$(command -v appimagetool)"; return 0; fi
    local extracted="${TOOLS}/squashfs-root/AppRun"
    if [ -x "$extracted" ]; then APPIMAGETOOL="$extracted"; return 0; fi
    echo "    本机没有 appimagetool → 下官方 release 到 ${TOOLS}（不装进系统）"
    mkdir -p "$TOOLS"
    local url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    curl -sSL --max-time 180 -o "${TOOLS}/appimagetool" "$url" || return 1
    chmod +x "${TOOLS}/appimagetool"
    # 不依赖 FUSE：就地解包，用里面的 AppRun
    ( cd "$TOOLS" && ./appimagetool --appimage-extract >/dev/null 2>&1 ) || return 1
    [ -x "$extracted" ] || return 1
    APPIMAGETOOL="$extracted"
}
resolve_appimagetool || die "拿不到 appimagetool（网络不通？也可以手动装 appimagetool 包后重跑）"
echo "    appimagetool: $APPIMAGETOOL"

# ---------------------------------------------------------------- 1. 组装 AppDir
step "1/6 组装 AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/python" "$APPDIR/usr/app" "$OUT_DIR"

# 1a. 独立 Python（整份拷，实测可搬运）
cp -r "$(dirname "$(dirname "$HOST_PY")")/." "$APPDIR/usr/python/"
# uv 的目录名带版本/架构，里面才是 bin/ lib/ —— 兼容两种布局
[ -x "$APPDIR/usr/python/bin/python${PYVER}" ] || {
    find "$APPDIR/usr/python" -maxdepth 3 -type f -name "python${PYVER}" | head -1 | \
        while read -r f; do cp -r "$(dirname "$(dirname "$f")")/." "$APPDIR/usr/python/"; done
}
[ -x "$APPDIR/usr/python/bin/python${PYVER}" ] || die "独立 Python 布局不是我预期的，请检查"
# ★ 关键断言：打进包的必须是**真解释器**，不能是 venv / 软链 ——
#   否则在别的机器上会因为软链指向不存在的路径而起不来（本机却测不出来）。
[ -f "$APPDIR/usr/python/bin/python${PYVER}" ] && [ ! -L "$APPDIR/usr/python/bin/python${PYVER}" ] \
    || die "打进 AppDir 的 python 是软链 —— 不可搬运，构建脚本有问题"
[ ! -e "$APPDIR/usr/python/pyvenv.cfg" ] \
    || die "打进 AppDir 的是 venv（有 pyvenv.cfg）—— 不可搬运，构建脚本有问题"
echo "    Python 解释器已就位（独立解释器，非 venv/软链 ✓）"

# 1b. 依赖（从 venv 拷，剔掉只在打包时用的）
SITE_SRC="$("$VENV_PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
SITE_DST="$APPDIR/usr/python/lib/python${PYVER}/site-packages"
mkdir -p "$SITE_DST"
cp -r "$SITE_SRC/." "$SITE_DST/"
# 只在**打包时**用到的（进了包纯属白占体积）；__pycache__ 也一并清掉，构建时再生成
for junk in PyInstaller pyinstaller _pyinstaller_hooks_contrib pyinstaller_hooks_contrib \
            setuptools pkg_resources pip wheel; do
    rm -rf "${SITE_DST:?}/${junk}" "${SITE_DST:?}/${junk}".* 2>/dev/null || true
done
find "$SITE_DST" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
rm -rf "$SITE_DST"/*.dist-info/RECORD 2>/dev/null || true
echo "    依赖已就位（$(du -sh "$SITE_DST" | cut -f1)）"

# 1c. 程序源码
cp -r "$REPO/vlt" "$APPDIR/usr/app/"
cp "$REPO/run_gui.py" "$REPO/config.example.yaml" "$REPO/LICENSE" "$APPDIR/usr/app/"
cp -r "$REPO/assets" "$APPDIR/usr/app/"
cp -r "$REPO/testdata" "$APPDIR/usr/app/"
find "$APPDIR/usr/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "    源码已就位"

# 1d. ★ 反向排除：删掉 **Windows 独占实现**
#
# 这是 Windows 版 `--exclude-module` 的镜像操作，必须做，而且必须在这里做：
# AppImage 里的 `vlt/` 是**明文 .py**，整份拷进去就等于把 Windows 那套也发出去了。
#   * vlt/platform/win.py        —— WASAPI / Win32（`platform/__init__.py`
#                                   只在 IS_WINDOWS 时才 import 它，删掉安全）
#   * vlt/output/openvr_overlay.py —— SteamVR 手腕屏后端
# 漏删的后果不是「变胖」而是**边界破功**：`scripts/check_platform_purity.py
# --platform linux` 会判红（历史上这条判据从没跑过，所以泄漏一直存在）。
for _win_mod in "vlt/platform/win.py" "vlt/output/openvr_overlay.py"; do
    rm -f "$APPDIR/usr/app/$_win_mod"
    [ -e "$APPDIR/usr/app/$_win_mod" ] && die "反向排除失败：$_win_mod 还在 AppDir 里"
done
find "$APPDIR/usr/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
echo "    已反向排除 Windows 独占实现（vlt/platform/win.py、vlt/output/openvr_overlay.py）"

# 1e. ★ 瘦身：pyopenxr（`xr`）只留 **linux x86_64** 用得到的部分
#
# 为什么值得做：pyopenxr 的 wheel 里带着给 **7 个平台**预编译的 API layer 与 loader ——
# 光 `api_layer/android/` 一份就 56MB（core_validation 24MB + api_dump 22MB），
# 而我们只用 `xr.*` 的接口，从不 enable 调试层。未压缩能省 ~1.3 亿字节。
#
# ⚠️ 保留集不是凭感觉定的，是**由 pyopenxr 自己的代码钉死**的：
#   * `xr/library/__init__.py`   —— linux + x86_64 时 `LoadLibrary(xr.library.x86_64/
#     libopenxr_loader.so)`，而且 `xr/raw_functions.py` 在 **import 时**就 dlopen 它；
#   * `xr/api_layer/__init__.py` —— **import 时**就调 `expose_packaged_api_layers()`，
#     而它要 `xr.api_layer.x86_64` 这个**目录**存在（`importlib.resources.as_file`）。
#   所以这两个必须留；`api_layer/linux/`、顶层 `library/libopenxr_loader.so` 都没有
#   任何代码引用（x86_64 那份才是被选中的）。
_XR="$APPDIR/usr/python/lib/python${PYVER}/site-packages/xr"
if [ "$SLIM" -eq 1 ]; then
    [ -d "$_XR" ] || die "找不到 $_XR —— pyopenxr 的布局变了？瘦身规则要跟着改"
    rm -rf "$_XR/api_layer"/{android,aarch64,win32,windows,linux}
    rm -rf "$_XR/library"/{aarch64,android,win32}
    rm -f  "$_XR/library/openxr_loader.dll"
    # 断言保留集没被误删：错了就在这里炸，而不是发到用户手里才「启动即崩」
    [ -f "$_XR/library/x86_64/libopenxr_loader.so" ] \
        || die "瘦身误删：library/x86_64/libopenxr_loader.so 不见了（import xr 会直接失败）"
    [ -d "$_XR/api_layer/x86_64" ] \
        || die "瘦身误删：api_layer/x86_64 目录不见了（import xr 会直接失败）"
    echo "    ✅ xr 已瘦身：只留 linux x86_64（$(du -sh "$_XR" | cut -f1)）"
else
    echo "    （已按 --no-slim 跳过 xr 瘦身：产物会大 ~30MB）"
fi


# ---------------------------------------------------------------- Xft 版 Tk

# ⚠️ 为什么必须自己编一份 Tk（这不是洁癖，是实测结论）：
#
#   uv 打包的 Python 自带 Tcl/Tk 9.0.4，但那份 **libtcl9tk9.0.so 完全没有 Xft/fontconfig**
#   （`ldd` 只链 libc/libdl/libm/libpthread）。后果是 Tk 只认 X 服务器的核心字体，
#   而现代 Wayland/XWayland 基本不提供核心字体 → **任何中日韩文字都是豆腐块**。
#
#   实测证据（同一份只含一张 Noto CJK 的 fontconfig 配置）：
#       系统 Tk 8.6        → 10 个族，其中**有** Noto Sans CJK
#       uv 的 Tk 9.0       → 65 个族，中日韩 **0 个**（它根本不看 fontconfig）
#   ⇒ 所以「把字体打包进去」本身**不解决问题**；必须先让 Tk 会读 fontconfig。
#
#   修法：编一份 `--enable-xft` 的 Tk，**只替换 `libtcl9tk9.0.so`**。
#   Tcl 不动（uv 那份留着）—— 因为：
#     * 字体是 Tk 的事，与 Tcl 无关；
#     * 自己编的 Tcl 与 uv 的 Tcl 内部符号不一致，会把 `_tkinter` 打成
#       `undefined symbol: TclBN_mp_to_ubin`（实测踩过）。
#
#   替换后实测：689 个族 / 65 个中日韩族，与系统 Tk 完全一致。
#
# 构建依赖（只在**构建机**需要，运行机不需要）：
#     gcc make + libxft/freetype2/fontconfig/X11 协议头的开发文件
#     （Ubuntu 24.04 起 X11 协议头叫 `x11proto-dev`，由 `libx11-dev` 自动带入；
#      别再写 `xorgproto` —— noble 已无此包名）
build_xft_tk() {
    local bundled="$APPDIR/usr/python/lib/libtcl9tk9.0.so"
    [ -f "$bundled" ] || die "AppDir 里没有 libtcl9tk9.0.so，Python 布局可能变了"

    # 版本由 uv 那份 Tcl/Tk 决定 —— 必须同版本，否则 ABI 对不上
    local ver
    ver="$(strings -a "$bundled" 2>/dev/null | grep -oE '^9\.[0-9]+\.[0-9]+' | head -1)"
    [ -n "$ver" ] || die "认不出打包的 Tcl/Tk 版本"
    local tag="core-$(echo "$ver" | tr '.' '-')"      # 9.0.4 → core-9-0-4

    # ⚠️ 缓存目录**必须按 Tcl/Tk 版本分**，复用前还要再校验一次。
    #    历史教训：目录不区分版本时，`uv python install 3.11` 一旦把 uv 自带的
    #    Tcl/Tk 从 9.0.4 升到 9.0.5，脚本会读到 ver=9.0.5，却直接拿缓存里
    #    9.0.4 的 .so 盖到 9.0.5 的 Python 上 —— ABI 不匹配，可能在用户机器上崩，
    #    而 CI 全绿。版本目录 + 复用前校验（版本号 + 真的链了 Xft）把这堵死。
    local cache="${TOOLS}/tk-xft-${ver}"
    local cached_ok=0
    if [ -f "${cache}/libtcl9tk9.0.so" ]; then
        local cver
        cver="$(strings -a "${cache}/libtcl9tk9.0.so" 2>/dev/null \
                | grep -oE '^9\.[0-9]+\.[0-9]+' | head -1)"
        if [ "$cver" = "$ver" ] && ldd "${cache}/libtcl9tk9.0.so" 2>/dev/null | grep -qi Xft; then
            cached_ok=1
        else
            echo "    ⚠️ 缓存里的 Tk 版本/特性不符（要 ${ver}，实际 ${cver:-未知}）→ 重新编译"
        fi
    fi

    if [ "$cached_ok" -eq 0 ]; then
        echo "    编译带 Xft 的 Tcl/Tk ${ver}（首次较慢，之后会缓存）"
        # 源码目录同样按版本分：老版本解出来的 tcl-*/tk-* 留着会让 find 抓到错的源，
        # 老 tarball 也会被 `[ -f ... ]` 当成「已下载」而复用。
        local src="${TOOLS}/tcltk-src-${ver}"
        mkdir -p "$src"
        for pkg in tcl tk; do
            [ -f "${src}/${pkg}.tar.gz" ] ||                 curl -sSL --max-time 300 -o "${src}/${pkg}.tar.gz" \
                     "https://github.com/tcltk/${pkg}/archive/refs/tags/${tag}.tar.gz" \
                || die "下载 ${pkg} ${ver} 源码失败"
            ( cd "$src" && tar xzf "${pkg}.tar.gz" ) || die "解包 ${pkg} 失败"
        done
        local tdir; tdir="$(find "$src" -maxdepth 1 -type d -name 'tcl-*' | head -1)"
        local kdir; kdir="$(find "$src" -maxdepth 1 -type d -name 'tk-*'  | head -1)"
        [ -n "$tdir" ] && [ -n "$kdir" ] || die "找不到解出来的源码目录"
        local pfx="${TOOLS}/tcltk-prefix"
        rm -rf "$pfx"; mkdir -p "$pfx"
        # Tcl 只是 Tk 的构建依赖 —— 编出来**不进包**，所以装到 build/tools 里
        ( cd "${tdir}/unix" && ./configure --prefix="$pfx" --enable-shared --enable-threads >/dev/null 2>&1 \
          && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1 ) \
          || die "编译 Tcl 失败（构建机缺 X11/freetype 开发头文件？）"
        # ★ 关键：--enable-xft
        ( cd "${kdir}/unix" && ./configure --prefix="$pfx" --enable-shared --enable-threads \
            --with-tcl="$pfx/lib" --enable-xft >/dev/null 2>&1 \
          && make -j"$(nproc)" >/dev/null 2>&1 && make install >/dev/null 2>&1 ) \
          || die "编译 Tk 失败"
        [ -f "${pfx}/lib/libtcl9tk9.0.so" ] || die "编完了却没产出 libtcl9tk9.0.so"
        # 自检：编出来的这份确实带 Xft，否则换了也白换
        ldd "${pfx}/lib/libtcl9tk9.0.so" | grep -qi Xft \
            || die "编出来的 Tk 没有链 Xft —— configure 没吃到 --enable-xft"
        mkdir -p "$cache"
        cp "${pfx}/lib/libtcl9tk9.0.so" "${cache}/"
    else
        echo "    用缓存的 Xft 版 Tk（${cache}，Tcl/Tk ${ver}）"
    fi
    cp "${cache}/libtcl9tk9.0.so" "$bundled"
    echo "    ✅ 已换成带 Xft 的 Tk（字体走 fontconfig）"
}

# ---------------------------------------------------------------- 中日韩字体

# Tk 会读 fontconfig 之后，**打包字体才有意义**（否则它根本看不见）。
# 打包的收益：机器上一套中日韩字体都没装时，界面也不会是豆腐块。
# NotoSansCJK-Regular.ttc 一份就覆盖中日韩（正好够我们五种界面语言），OFL 许可可再分发。
bundle_cjk_font() {
    local dest="$APPDIR/usr/share/fonts"
    local cand=""
    for f in /usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc \
             /usr/share/fonts/noto-cjk/NotoSansCJK-Regular.otf \
             /usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc; do
        [ -f "$f" ] && { cand="$f"; break; }
    done
    if [ -z "$cand" ]; then
        # 构建机没装中日韩字体 → 不阻断构建，但要说清楚（运行机得自己有）
        echo "    ⚠️ 构建机上找不到 NotoSansCJK，**不打包字体**（运行机需自带中日韩字体）"
        return 0
    fi
    mkdir -p "$dest"
    cp "$cand" "$dest/"
    # 许可一起带上（OFL 要求随附）
    for lic in /usr/share/licenses/noto-fonts-cjk/*; do
        [ -f "$lic" ] && { cp "$lic" "$dest/NotoSansCJK-LICENSE.txt"; break; }
    done
    echo "    ✅ 已打包中日韩字体：$(basename "$cand")（$(du -h "$dest" | cut -f1)）"
}

# ---------------------------------------------------------------- 2. 入口 / 桌面项 / 图标
step "2/6 修 Tk 字体 + 打包中日韩字体"
build_xft_tk
bundle_cjk_font

step "2.5/6 写 AppRun / .desktop / 图标"

cat > "$APPDIR/AppRun" <<'RUN'
#!/usr/bin/env bash
# AppImage 入口。AppImage 不沙盒，环境变量原样继承（WAYLAND_DISPLAY / XDG_RUNTIME_DIR /
# DASHSCOPE_API_KEY 等都能正常用）。
set -euo pipefail
HERE="$(dirname "$(readlink -f "$0")")"
PY="$HERE/usr/python/bin/python3.11"
APP="$HERE/usr/app"
export PYTHONPATH="$APP:$HERE/usr/python/lib/python3.11/site-packages${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONUTF8=1

# 让 fontconfig 认识**包内**打包的中日韩字体。
# 配置在运行时生成：AppImage 的挂载路径是随机的，写死在文件里没法用。
# 系统配置用 <include> 拉进来，这样宿主机自己的字体也照常可用。
if [ -d "$HERE/usr/share/fonts" ]; then
    FCCONF="${XDG_CACHE_HOME:-$HOME/.cache}/vrchat-livetranslate/fonts.conf"
    mkdir -p "$(dirname "$FCCONF")" 2>/dev/null || FCCONF="$HERE/usr/share/fonts.conf"
    cat > "$FCCONF" <<EOF
<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "urn:fontconfig:fonts.dtd">
<fontconfig>
  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>
  <dir>$HERE/usr/share/fonts</dir>
</fontconfig>
EOF
    export FONTCONFIG_FILE="$FCCONF"
fi
# 界面用 Tk：某些发行版把 tcl/tk 装在别处，这里不覆盖，交给系统
exec "$PY" "$APP/run_gui.py" "$@"
RUN
chmod +x "$APPDIR/AppRun"

cat > "$APPDIR/${APP_ID}.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=VRChat LiveTranslate
Name[zh_CN]=VRChat 实时同传
Comment=Real-time speech translation for VRChat
Comment[zh_CN]=在 VRChat 里做实时同声传译
Exec=vlt-gui
Icon=${APP_ID}
Categories=AudioVideo;Audio;Utility;
Terminal=false
StartupWMClass=Tk
DESK
cp "$REPO/assets/app.png" "$APPDIR/${APP_ID}.png"

# ---------------------------------------------------------------- 3. 构建前自检
step "3/6 构建前自检（在 AppDir 里直接跑）"
APP_PY="$APPDIR/usr/python/bin/python${PYVER}"
export PYTHONPATH="$APPDIR/usr/app:$SITE_DST"
# ⚠️ `-P` 不能省：`python -c` 会把当前目录放在 sys.path 最前，PYTHONPATH 排在后面，
#    于是从仓库根跑构建时，下面的导入检查读的是**仓库源码**而不是刚组装好的 AppDir。
if "$APP_PY" -P -c "
import vlt.gui, vlt.engine, vlt.output.openxr_overlay, vlt.platform
import numpy, PIL, websockets, yaml, sounddevice, pythonosc, xr, OpenGL
print('    导入检查通过')
" 2>&1 | tail -5; then
    echo "    ✅ 依赖与模块都齐"
else
    die "AppDir 里的导入检查没通过（上面有原因）"
fi

# ---------------------------------------------------------------- 4. 打包
step "4/6 生成 AppImage"
mkdir -p "$OUT_DIR"
ARCH=x86_64 "$APPIMAGETOOL" --no-appstream "$APPDIR" "$OUT_DIR/${APP_NAME}-x86_64.AppImage" 2>&1 | tail -5
OUT_IMG="$OUT_DIR/${APP_NAME}-x86_64.AppImage"
[ -f "$OUT_IMG" ] || die "没产出 AppImage"
chmod +x "$OUT_IMG"
echo "    ✅ $OUT_IMG（$(du -h "$OUT_IMG" | cut -f1)）"

# ---------------------------------------------------------------- 5. 验收（转调独立脚本）
#
# 验收逻辑**不写在这里**：它要能单独对着任意 AppImage 跑（CI 直接调它，不必重新构建），
# 见 scripts/verify_appimage.py。这一步只是构建流程里的自动转调。
if [ "$VERIFY" -eq 1 ]; then
    step "5/6 AppImage 独立验收（平台纯度 + 包内导入 + xr 瘦身 + 离线渲染 + 字体）"
    if [ "$SLIM" -eq 1 ]; then
        "$VENV_PY" "$REPO/scripts/verify_appimage.py" "$OUT_IMG"
    else
        # --no-slim 时别去要求「瘦身已生效」，否则两个开关自相矛盾
        "$VENV_PY" "$REPO/scripts/verify_appimage.py" "$OUT_IMG" --allow-fat
    fi || die "AppImage 验收未通过（上面有明细）"
else
    step "5/6 验收已按 --no-verify 跳过"
    echo "    ⚠️ 跳过 = **未验证**：产物已生成，但平台隔离/导入/渲染都没检查过。"
    echo "       补跑：$VENV_PY scripts/verify_appimage.py $OUT_IMG"
fi

step "6/6 完成"
cat <<EOF
产物：$OUT_IMG

双击即可运行（图形界面）。数据写在：
    \${XDG_DATA_HOME:-~/.local/share}/vrchat-livetranslate/   （config.yaml / logs / out）

注意：
  * 需要 **Wayland 会话**（手腕屏走 Wayland + EGL）
  * 手腕屏还需要 **OpenXR 运行时已起 + 头显已连**（Monado / WiVRn）
  * 译音虚拟声卡由程序运行时自己声明，**不需要**事先装 VB-Cable 之类
  * 详见 GUIDE.linux.md
EOF
