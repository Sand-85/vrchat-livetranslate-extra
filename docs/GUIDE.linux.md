# VRChat 实时同传 · Linux 使用指南

> **中文** | [English](GUIDE.en.md) | [日本語](GUIDE.ja.md) | [한국어](GUIDE.ko.md) | [Русский](GUIDE.ru.md)
>
> 这份是 **Linux 专用**。Windows 请看 [GUIDE.md](GUIDE.md)。
> 两边**共用同一份 `config.yaml`**，配置语义一致（同一组 `anchor` / `offsets` 换平台不会失效）。

---

## 平台支持矩阵

| 功能 | Windows | Linux |
|---|---|---|
| ① 我说 → **chatbox 气泡** | ✅ | ✅ |
| ② 别人说 → **VR 手腕屏** | ✅ SteamVR overlay | ✅ **自建 OpenXR overlay**（Monado / WiVRn） |
| ③ 我说 → **译音进对方耳朵** | ✅ 需自备虚拟声卡（VoiceMeeter / VB-Cable） | ✅ **不需要自备**（程序运行时自己声明虚拟麦克风） |
| ④ 我说 → **打字替代说话**（含 TTS） | ✅ | ✅ |
| ⑤ 几个人 → **互相看字幕（房间）** | ✅ | ✅ 房间与平台无关，用法见 [GUIDE.md](GUIDE.md#多人房间几个人互相看字幕) |
| 自动更新（发现新版本 → 一键升级） | ✅ exe 自更新 | ✅ **AppImage 自更新**；源码运行只给指引 |
| 图形界面 / 五种界面语言 / 日志导出 / 赞助弹窗 | ✅ | ✅ |

Linux 侧的实现依据（为什么这么做、哪些路试过不通）都在
**[docs/平台约束记录.md](平台约束记录.md)**。

---

## 一、前置条件

1. **Wayland 或 X11 会话都行**（niri / KDE / GNOME / Xorg 均可）。
   - **Wayland**：手腕屏走 **EGL（`XR_MNDX_egl_enable`）** —— niri 的精简 XWayland
     没有可用的 GLX，必须走这条；
   - **X11 / Xorg**：手腕屏走 **GLX（XLIB 图形绑定）**。
     ⚠️ 这条路径**离线已验证、真机待验证**（开发机没有 X11+运行时环境），详见
     [平台约束记录](平台约束记录.md)。
   （界面本身走 Tk/**X11**，Wayland 会话下由 XWayland 提供 —— 桌面发行版默认都有这套基础库。）
2. **PipeWire**（大多数现代发行版默认就是）—— 需要 `pw-dump` / `pw-record` / `pw-cat`。
   > ⚠️ 我们不依赖 PulseAudio 兼容层。实测某些环境下 `pactl` 连不上，而 `pw-dump` 正常，
   > 所以全程用 PipeWire **原生**工具。
3. **OpenXR 运行时**（只有「手腕屏」需要）：**Monado** 或 **WiVRn**，且**头显已连接**。
   - 用 WiVRn：先起服务端（`wivrn-dashboard` 或 `wivrn-server`），再在头显里连上
   - 用 Monado：起 `monado-service`（或经 Envision）
   - 判断是否就绪：`XR_RUNTIME_JSON` 指向一个运行时清单，**或** `~/.config/openxr/1/active_runtime.json`
     存在（系统级路径也可能生效）。`./setup.sh` 只检查「装了哪些运行时」，**装了 ≠ 被选中**
4. **Python 3.11** —— 只用 `setup.sh` 的话这条它会自己处理（有 `uv` 就自动拉一份 3.11）
5. **`libportaudio`**（麦克风采集）：`pacman -S portaudio` / `apt install libportaudio2`
6. **一套中日韩字体**（`pacman -S noto-fonts-cjk` / `apt install fonts-noto-cjk`）。
   ⚠️ **AppImage 自 2026-10 起不再自带字体**，GUI 与手腕屏都靠宿主机 fontconfig 提供字形；
   缺字体时界面会是空壳/豆腐块。
7. **VRChat**：设置里打开 OSC（`OSC enabled: True`），chat bubble visibility 设为 **Everyone**
8. **阿里云百炼 API key**（个人实名认证即可）

---

## 二、安装

### 最快：下现成的 AppImage（不用装 Python 和依赖）

到 **[Releases](https://github.com/nixi-agent/vrchat-livetranslate/releases/latest)** 下载
`VRChatLiveTranslate-x86_64.AppImage`，`chmod +x` 之后双击（或直接运行）即可：

```bash
chmod +x VRChatLiveTranslate-x86_64.AppImage
./VRChatLiveTranslate-x86_64.AppImage
```

- Python 解释器与全部 Python 依赖都打在包里 —— **不需要**装 Python、不需要跑 `./setup.sh`。
  系统层仍由宿主机提供（见前置条件）：**PipeWire**（`pw-*` 命令行）、**portaudio**（麦克风）、
  X11 基础库（XWayland）与一套中日韩字体
- ⚠️ **不再自带字体**：需要宿主机自己有一套中日韩字体（见前置条件第 6 条）
- 配置与日志写在 `~/.local/share/vrchat-livetranslate/`（AppImage 本体放哪都行，只读目录也能跑）
- 仍然要自备一个**阿里云百炼 API key**（见下一节）
- 运行时需要 **Wayland 或 X11 会话**（手腕屏：Wayland 走 EGL、X11 走 GLX）；
  手腕屏还要 OpenXR 运行时（Monado / WiVRn）+ 头显已连

### 或者：源码安装（`./setup.sh`）

想改代码、或不想用 AppImage 的走这条：

```bash
./setup.sh
```

它会：建 Python 3.11 虚拟环境 → 装依赖 → **体检系统依赖**（PipeWire 工具 / OpenXR 运行时 /
libportaudio）→ 提示 API key 怎么配。

脚本**不会**改你的系统配置、**不会**重启任何服务。缺什么它只告诉你缺什么。

<details>
<summary>手动等价命令</summary>

```bash
uv venv --python 3.11 .venv          # 没有 uv 就用 python3.11 -m venv .venv
uv pip install --python .venv/bin/python -r requirements-linux.txt
```

</details>

---

## 三、配置 API key

> 🔑 还没有百炼账号？**[点此开通「阿里云百炼大模型」▸](https://www.aliyun.com/minisite/goods?userCode=q8nma978)**

> 🌏 **海外用户**：默认线路「千问云」面向国内，海外注册不了、直连也不稳。请改用
> **千问云·海外版（Qwen Cloud，<https://www.qwencloud.com/>）**：在界面「⚙ 设置 → 常规 →
> 服务线路」里切换即可（注册不要手机号、不要信用卡，两版 key 不互通）。详见 GUIDE.md 的
> 「海外用户：千问云·海外版」一节。

优先级和 Windows 完全一样（界面里保存的 → 环境变量 → 百炼 CLI）：

| 优先级 | 来源 | 怎么设（Linux） |
|---|---|---|
| 1 | 界面里保存的 key | 图形界面「⚙ 设置 → API key」粘贴保存（存在 `~/.vrchat-livetranslate/api_key.txt`） |
| 2 | 环境变量 | `export DASHSCOPE_API_KEY="sk-你的key"`（建议写进 `~/.zshrc` / `~/.bashrc`） |
| 3 | 百炼 CLI 的配置 | `bl auth login --api-key "sk-你的key"`，读 `~/.bailian/config.json` |

**key 只从这三处读，不写进项目文件、不写进 `config.yaml`。**

> 🔒 仓库带凭据扫描（`scripts/check_no_secrets.py`）。手动全量检查：
> `python scripts/check_no_secrets.py --once`

---

## 四、自检

```bash
./run_selfcheck.sh
```

四步：模块导入 → 读 key（打码打印）→ **离线渲染一帧手腕屏贴图**（不连 VR）→
用自带测试音频跑完整链路（中文 → 英文，打进 chatbox）。

手腕屏的**通透性**（蓝框外是否透明、半透明底板对不对、文字是否实心）只有真机能看，
自检脚本盖不到，单独跑一条：

```bash
python3 -m vlt.output.openxr_overlay --smoke 20 --alpha-test   # 戴着看 20 秒判定图
```

它会读你自己的 `config.yaml`（位置/角度/透明度），所以看到的就是平时的面板参数。

VRChat 开着的话，气泡里应该出现：

```
Hello, I'm Nixi. Today, we're going to test out the real-time simultaneous interpretation feature in VRChat.
```

**先自检再报错。**

---

## 五、使用

| 启动方式 | 命令 | 需要什么 |
|---|---|---|
| 图形界面（推荐） | `./run_gui.sh` | API key |
| 只要 chatbox 气泡 | `./run_chatbox.sh` | VRChat + OSC，**不需要 VR** |
| 手腕屏 | `./run_overlay.sh` | VRChat + **OpenXR 运行时已起、头显已连** |

界面上「输出」那一行勾什么就出什么；手腕屏那条腿起不来只影响它自己，翻译照常。

### Linux 上的音频设备（只有「麦克风」需要选）

Linux 与 Windows 在这里**刻意不一样**：设置里的「音频设备」只有**一个下拉**（`麦克风`），
`VRChat 音频` 与 `译音输出` 两项在 Linux 上不暴露给用户。

- **「VRChat 音频」（别人说话）自动跟随 VRChat**：采集目标是 VRChat 自己的音频输出流
  （PipeWire 里的 `Stream/Output/Audio`，节点名通常就是 `VRChat.exe`），**不是**系统默认输出。
  拿默认输出来抓会把浏览器 / 音乐 / 系统提示音一起当成「游戏内语音」喂给模型，所以**不做**这种回落。
- VRChat 会开**多路**播放流（实测 2 路），本程序会**每一路都抓、相加混音** ——
  只抓一路可能丢声音。日志里会写清抓到几路，例如
  `[loopback] 检测到 VRChat 音频：2 路（…[14061]、…[14002]）`。
- VRChat **没在跑**时，这条腿不会退出，而是显示「等待 VRChat 音频输出」并空转等待；
  VRChat 中途退出、或播放流增减，都会回到等待/重开，重新启动 VRChat 会自动接上，**不用重启本程序**。
- **译音输出固定写到本程序自建的虚拟麦**（`vlt_mic_sink` / `vlt_mic_source`），
  同样不需要选输出设备；「输出」那一行的「译音输出」勾选框照旧是这条腿的总开关。
- `config.yaml` 里的 `capture.loopback_device` 与 `output.audio.device_name` 在 Linux 上**被忽略**
  （键仍保留，Windows 侧照常生效），界面也不再把它们写出来。

### 手腕屏位置怎么调

**两种办法，都会写回 `config.yaml` 并立刻热重载（不用重启）**：

1. **界面上的「微调 ▸」**（在「输出」那一行的右端）：锚点 + **14 个滑块**
   （位置 X/Y/Z、俯仰/偏航/翻滚、大小、弯曲、透明度、译文字号、原文字号、面板高、
   底板不透明度、原文不透明度）—— 戴着看效果边调最快。完整表格见 GUIDE.md 的「手腕屏微调面板」。
2. 直接改 `config.yaml` 的 `overlay.anchor` + `overlay.offsets.<锚点>`，存盘即生效

```yaml
overlay:
  anchor: right_hand         # left_hand | right_hand | tracker | hmd
  offsets:                   # ★ 每个锚点各存一套：切锚点时界面自动载入对应这一份
    right_hand:
      pos: [0.0, 0.06, 0.02] # 相对锚点的偏移（米）
      rot: [-47, -16, 0]     # 欧拉角（度），约定 Rz·Ry·Rx
    left_hand:
      pos: [0.0, 0.06, 0.02]
      rot: [-47, 16, 0]      # 左手 = 右手镜像（见下方说明）
    hmd:
      pos: [0.0, -0.10, -0.35]
      rot: [0, 0, 0]
  offset:                    # 兜底：某个锚点没在 offsets 里单独存过时用它
    width_m: 0.23            # 面板宽度（米）—— 弯曲时按**弧长**算，与 Windows 一致
    curvature: 0.0           # 弯曲：占整圆的比例（0.5 = 半圈 180°；0.1~0.3 好看）。
                             # 改它只弯曲，面板中心不动（两边朝你卷）
```

> ⚠️ **左右手不能照抄**：默认的 `rot` 是为「右手腕」调的（作者实测值），而左右手 grip
> 坐标系沿 X 轴镜像，换到 `left_hand` 要 **`rx` 不变、`ry` 取反、`rz` 取反** ——
> `[-47, -16, 0]` 变成 `[-47, 16, 0]`（位置的 `x` 分量同理）。这才是模板里
> `left_hand` 那份跟 `right_hand` 不一样的原因。每一份都只是**起始值**，
> 最终戴着微调；调好之后各存各的，来回切不会互相覆盖。原因见
> [docs/平台约束记录.md](平台约束记录.md) 第五节。

### 译音（对方直接听到）

**Linux 上不需要自备虚拟声卡** —— 程序运行时会自己声明一对 PipeWire 节点：

- `vlt_mic_sink`：程序往这里写译音
- `vlt_mic_source`：**在 VRChat 里把这个选成麦克风**（显示名 `VLT Mic`）

在界面上勾「译音输出」即可（**不需要、也没有**输出设备下拉 —— 程序固定往 `vlt_mic_sink` 写）。
程序退出时这对节点会被自动销毁。

> 📌 **先起本程序，再起 VRChat**：设备只在程序运行期间存在，VRChat 的设备列表是在启动时枚举的。
> 如果 VRChat 先启动了，去它的音频设置里刷新一下，或者把 `VLT Mic` 设成系统默认输入源。

### 桌面字幕（贴 VRChat 窗口的字幕窗）

**不需要头显**（issue #11）：界面上勾「桌面字幕」，屏幕上会出现一块**无边框、鼠标穿透**的
字幕窗，默认贴在 VRChat 窗口下沿并跟着它走（细节与配置见 GUIDE.md 的「桌面字幕」一节）。

有**三条窗口后端**（两条原生 + 一条 Tk 回落），按会话自动选（日志会写明走的哪条；
`desktop_overlay.backend` 可强制：`auto | native | tk | wayland | x11`）：

| 后端 | 什么时候走 | 透明 | 定位 / 跟随 | 鼠标穿透 |
|---|---|---|---|---|
| **原生 layer-shell 窗**<br>（`vlt/platform/wayland.py`） | Wayland 会话，且合成器实现了 `zwlr_layer_shell_v1`（niri / sway / Hyprland / KWin 6.x / labwc / Cage / gamescope …） | ✅ **逐像素 alpha**：真圆角、底板半透明、整层透明度滑块全生效 | ✅ overlay 层置顶 + anchor/margin 精确定位；跟随用 X11 读 VRChat 几何（与全局坐标一致） | ✅ 协议级：输入区置空 |
| **原生 ARGB 覆盖窗**<br>（`vlt/platform/x11.py`） | X11 会话（纯 Xorg）；Wayland 会话只要带 XWayland（**GNOME/Mutter**、Weston 等没有 layer-shell 的同样吃这条腿） | ✅ **逐像素 alpha**（32 位 visual + 预乘出图）；⚠️ **需要合成器**（picom 等）——没有时自动降级：1 位形状蒙版裁掉透明区（没有黑框；圆角为锯齿、底板是实色），启动日志有一行说明 | ✅ 覆盖窗置顶 + `XMoveWindow` 精确定位；跟随同左 | ✅ X Shape：输入区置空 |
| **Tk 窗**（最后回落） | `backend=tk`；或两条原生腿都建不起来（没有对应协议 / 库 / 32 位 visual） | ⚠️ Windows 靠色键；Linux 用 **1 位形状蒙版**抠掉面板圆角外的键色底（圆角有锯齿；**逐像素半透明仍不行**——那是原生窗的能力） | ⚠️ X11 会话全功能；GNOME/Wayland 下客户端不能自定位 → 跟随不生效 | ✅ X11 输入区置空（X Shape）；GNOME 下未真机验证 |

- 日志会写出走的哪条：`后端=原生`（某条原生腿）或 `后端=Tk`。原生窗建不起来时，后端模块会打一行**具体原因**（没有 layer-shell / 没有 32 位 visual / libwayland 加载失败等）再回落。
- **拖动**：解锁后按住面板拖动。Wayland 后端两种位移源自动切换 —— 支持相对指针的合成器（sway/wlroots 系）用相对增量；niri 系不发相对位移事件，自动改用 `wl_pointer.motion` 的本地坐标差（niri 的 click-grab 冻结焦点坐标，位移精确）。X11 原生窗走 `XGrabPointer` + 绝对坐标（`x_root/y_root`），一步算式，**拖动全程自由跨屏**。
- **跨屏拖动（仅 Wayland 后端）**：拖动过程中面板夹在本屏边缘（中途换面会打断指针 grab），**松手时自动落到指针所在的那块屏**；拖动全程平滑跨屏跟手属后续项。多屏位置取自 xdg-output（wlroots 的 `wl_output.geometry` x/y 恒为 0，不能拿来认屏）。X11 原生窗没有协议层限制，拖动中直接跨屏。
- 找窗/跟随走 `vlt/platform/linux.py`（纯 `ctypes` 直调 libX11/libXext，不新增依赖），
  与 Windows 侧同名同语义；找窗走 `_NET_WM_NAME`（退回 `WM_NAME`），几何经
  `XTranslateCoordinates` 折算到屏幕坐标（多屏 / 负坐标都对）。
- **所有 X 侧操作必须落在「顶层包装窗」上**：Tk 的 `winfo_id()` 是内层客户窗，
  合成器/命中测试看的是外层包装窗（`top_level_hwnd()` 负责换出来）。设错窗口时，
  穿透区里的点击会**直接消失**（上层下层都不收）——这条在 Xvfb + openbox 下做过对照实验。
- 找不到 VRChat 窗口时退回屏幕绝对定位（`config.yaml` 的 `desktop_overlay.pos`），
  日志里会写一行 `没找到窗口 … → 退化成屏幕绝对定位`。

---

## 六、配置：和 Windows 的差异

共用同一份 `config.yaml`，**只有设备相关项不同**：

| 配置项 | Windows | Linux |
|---|---|---|
| `capture.mic_device` | 设备名 | 设备名（`sounddevice` 按名打开） |
| `capture.loopback_device` | WASAPI loopback 设备名 | **忽略** —— 采集目标固定为 VRChat 自己的输出流（自动等待 VRChat 启动） |
| `output.audio.device` / `device_name` | 虚拟声卡回退链（VoiceMeeter / VB-Cable） | **不用管** —— 程序自己声明 `vlt_mic_sink`，`device_name` 被忽略 |
| `overlay.font` | `C:/Windows/Fonts/msyh.ttc` | **留空即可**，自动用 fontconfig 找中日韩字体 |
| `overlay.backend` | `auto` | `auto`（= 自建 OpenXR）/ `null`（禁用） |
| `desktop_overlay.*` | 贴窗跟随 / 鼠标穿透（Win32 扩展样式） | **原生 layer-shell 窗**（逐像素透明 + 协议级穿透；`backend` 可强制）/ Tk 回落（见「桌面字幕」一节） |
| `capture.gate_*`（输入门限） | ✅ 生效 | ✅ **一致** —— 判在 VRChat 多路输出**混音之后**的同一块上（远处小声的玩家一样被拦，hold / preroll / 拦截计数同样成立） |

其它（方向、语言、chatbox 参数、静默闸门、repeat 抑制、字号、配色……）**完全一致**。

> 想确认 VRChat 的音频输出流在不在（Linux 采集的就是它）：
> ```bash
> pw-dump | python3 -c "import json,sys;
> [print(o['id'], (p:=(o.get('info') or {}).get('props') or {}).get('application.name'), p.get('node.name'), p.get('media.class'))
>  for o in json.load(sys.stdin)
>  if o.get('type')=='PipeWire:Interface:Node' and (p:=(o.get('info') or {}).get('props') or {}).get('media.class')=='Stream/Output/Audio']"
> ```

---

## 七、排障

### 手腕屏

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `RuntimeUnavailableError` / `The loader was unable to find or load a runtime` | 没有活跃的 OpenXR 运行时 | 起 Monado 或 WiVRn，并把头显连上；确认 `XR_RUNTIME_JSON` 指向的清单可用，或 `~/.config/openxr/1/active_runtime.json` 存在（只装了运行时、没被选中也会报这个） |
| 会话建立成功，但**手腕上什么都没有** | 会话没到 `FOCUSED`，或锚点没被追踪 | 看日志里的 `会话状态=…` 与 `锚点追踪=…`。**开着 VRChat（或任意 OpenXR 应用）再试** —— overlay 会话的 visible/focused 依赖合成器上报 |
| `锚点 right_hand 追踪=False` | 手柄没被追踪（头显待机 / 手柄没拿起） | 拿起手柄动一下；手柄休眠时 pose 会失效 |
| `无法建立 GL context` / `GL 后端都建不起来` | 会话里没有可用的 GL 后端 | Wayland：确认 `WAYLAND_DISPLAY` 有值；X11：确认 `DISPLAY` 有值且 X server 提供 GLX（`glxinfo` 能跑）。niri 的精简 XWayland 没有 GLX → 走 Wayland 路径即可 |
| 面板位置不对 | 锚点/角度不合适 | 用界面上的「微调 ▸」滑块；注意左右手镜像规则 |
| 面板外面多出一圈**不透明黑边** / 底板看着不透明 | 图层的 alpha 没生效（漏了 `BLEND_TEXTURE_SOURCE_ALPHA_BIT`）；旧版本有此问题 | 用带修复的版本；想确认就 `python3 -m vlt.output.openxr_overlay --smoke 20 --alpha-test`，那张判定图应当「蓝框外全透明、四块灰由淡到实、白字实心」 |
| 面板半透明的地方整体发白 / 白字发光 | 未预乘 alpha 被当成预乘（漏了 `UNPREMULTIPLIED_ALPHA_BIT`） | 同上，用 `--alpha-test` 一眼看出来；两个 flag 在 `layer_alpha_flags()` 里一起给 |
| 面板过一会儿消失 | 见日志 `[overlay:xr][diag]` 心跳行 | 心跳会打出「已上传多少帧 / 上次成功多久前 / 会话状态 / 锚点追踪」，按它判断 |

**开日志**：界面 →「导出日志压缩包」，或看 `logs/` 目录。

### 音频

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 一直显示「等待 VRChat 音频输出」 | VRChat 没在跑（或输出流还没建立） | 起 VRChat。日志里出现 `[loopback] 检测到 VRChat 音频：…` 即已接上，**不用重启本程序** |
| 采不到「别人说话」 | 日志里始终没有「检测到 VRChat 音频」 | 用上面那条 `pw-dump` 命令确认 VRChat 有没有 `Stream/Output/Audio` 节点；没有说明 VRChat 没起来 / 没在放声音 |
| 别人说话里混进了音乐、浏览器声 | ⚠️ 不该发生 | 采集的是 VRChat 自己的输出流，不是默认输出；若真混进来请报 bug，并附 VRChat 节点的 `node.name` |
| 门限「开着」，远处小声的玩家照样被翻 | ⚠️ 不该发生 | 门限判在**多路混音之后**（与 Windows 同口径）。真发生请报 bug，并附启动日志里的 `[gate]` 行 |
| VRChat 的麦克风列表里没有 `VLT Mic` | 设备只在程序运行期间存在 | **先起本程序再起 VRChat**；或在 VRChat 音频设置里刷新设备列表 |
| 对方听不到译音 | 译音腿没开 / VRChat 没选 `VLT Mic` | 界面上勾「译音输出」，并在 VRChat 里把麦克风选成 `VLT Mic` |
| `pactl: Connection refused` | PulseAudio 兼容层没跑/不可达 | **不影响我们** —— 全程用 `pw-*` 原生工具。用 `pw-dump` 验证 PipeWire 本身是否正常 |
| 系统声音突然没了 | ⚠️ 不该发生 | 报告 bug。虚拟声卡**在设计上不可能**被选成默认输出（依据见 `docs/平台约束记录.md` 第一节） |

### 桌面字幕

| 现象 | 原因 | 怎么办 |
|---|---|---|
| 勾了但在屏幕上找不到字幕窗 | 窗口贴到了别处 / 被别的窗口盖住 | 看日志第一行是 `已贴到 'VRChat'` 还是 `没找到窗口 … 退化成屏幕绝对定位`；用 `微调 ▸` 的「解锁拖动」把它拖回来 |
| 点字幕窗时 VRChat 收不到这一下 | 鼠标穿透没设上 | 日志里会有 `鼠标穿透（开）没设上`；报 bug 时附 `xprop -root _NET_SUPPORTED` |
| 圆角外一圈是黑的 / 底板看着不透明 | 走了 Tk 回落路径（日志 `后端=Tk`） | 确认合成器支持 `zwlr_layer_shell_v1`（niri/sway/Hyprland/KDE 支持；GNOME/Weston 不支持）；`desktop_overlay.backend=wayland` 可让日志给出具体原因 |
| 字幕窗被平铺、把桌面布局挤开 | 走了 Tk 回落（原生窗在 overlay 层，不会被平铺） | 同上：看日志确认后端；支持 layer-shell 的合成器里不该出现 |
| 拖不动 / 拖动后落点没变 | 旧版只认 relative-pointer，而 niri 不发相对位移事件 | 升级到含「拖动双源」的版本；仍不对就附日志里 `[desktop:wayland]` 的几行 |
| 泰语字幕显示成一排方块 | 宿主机没装含泰文字形的字体（AppImage **不自带字体**，用的是宿主 fontconfig；CJK 字体不含泰文） | 装一个泰文字体，例如 Debian/Ubuntu：`apt install fonts-thai-tlwg`（用 `fc-list :lang=th` 可确认装上没有）；日志里会有一行「找不到含泰文字形的字体 → …豆腐块」提示 |

### 其它

| 现象 | 怎么办 |
|---|---|
| `找不到 Python 3.11` | 装 `uv`（`pacman -S uv`）让 `setup.sh` 自己拉，或装系统 3.11 |
| 图形界面起不来 | 确认 `tkinter` 可用（Arch 上 `pacman -S tk`） |
| 界面字体发虚/中文显示怪 | 装一套中日韩字体（`pacman -S noto-fonts-cjk`） |

---

## 八、已知限制

1. **手腕屏的 X11 路径真机待验证**。Wayland（EGL_MNDX）是实测通过的常规路径；
   X11/GLX 离线已验证（Xvfb+GLX 下真实建上下文、binding 结构体、后端选择），
   但还没有「X11 显示 + 同一环境跑运行时」的真机验证记录（见平台约束记录的验证状态）。
2. **`anchor: tracker` 未实测**。OpenXR 里 tracker 是**按 role 寻址**的
   （`/user/vive_tracker_htcx/role/...`），和 Windows 侧「第 N 个 GenericTracker」的语义
   不完全一样；`tracker_index` 是这张 role 表的序号（0=右腕 1=左腕 2=右肘 3=左肘
   4=胸 5=腰 6=右脚 7=左脚）。代码写了，但没有硬件验证过。
3. **手腕屏需要 OpenXR 运行时**（Monado / WiVRn）。没有的话这条腿会自己禁用，其它功能不受影响。
4. **VRChat 走 Proton**：虚拟麦克风能否被 VRChat 列出取决于 winepulse，属于需要在你的环境实测的部分。
5. **自更新只对 AppImage 生效**（源码运行不做，只给 `git pull` 指引）。
   - 用 AppImage 时和 Windows 的 exe 一样：发现有新版本 → 点「立即更新」→ 下载 + **sha256 校验** →
     **就地换掉那个 `.AppImage` 文件**并重新打开；点「稍后更新」则在你关闭程序时换好，
     下次打开就是新版。下载物与校验凭据都放在 AppImage 旁边，换好即清理。
   - 更新过程**不会打断你正在进行的翻译**：Linux 的 rename 只换目录项，运行中的旧文件（旧 inode）
     照常工作到你自己关掉它。
   - 前提是那个 AppImage 文件所在的目录**可写**（`~/Applications`、`~/Downloads` 都可以）。
     放在只读位置（如 `/opt`、只读挂载）或用 `--appimage-extract-and-run` 跑时**不会**自更新 ——
     界面会直接给你下载页链接，手动下一个替换掉即可（这是刻意的：不能悄悄改不动的东西）。
   - 只有 `$APPIMAGE`（AppImage 运行时自己设的）指向的文件才认，且不发任何「安装」动作给系统：
     全程只动那一个文件。
6. **桌面字幕优先走原生窗**：Wayland（niri / sway / Hyprland / KWin 6.x 等实现了
   layer-shell 的合成器）走原生 layer-shell 窗 —— 逐像素透明、overlay 层置顶、精确跟随、
   协议级穿透、拖动全部成立（niri 真机实测，见平台约束记录第九节）；**X11 会话**（含带
   XWayland 的 GNOME/Weston）走原生 ARGB 覆盖窗（32 位 visual + 预乘出图，逐像素透明；
   需要合成器 picom 等——没有时**自动降级**用 1 位形状蒙版裁掉透明区：没有黑框，但圆角
   是锯齿、底板是实色，启动日志有一行说明）。两条原生腿都建不起来时最后回落 Tk：
   Linux 的 Tk 同样用 1 位形状蒙版抠掉面板圆角外的键色底（圆角有锯齿；逐像素半透明
   做不到——那是原生窗的事）。

---

## 九、实现说明

想了解「为什么是这样做」「哪些路试过不通」：

**➡️ [docs/平台约束记录.md](平台约束记录.md)**

里面按「约束 → 依据 → 改回去会怎样」的格式，记录了：

- 虚拟声卡为什么不能用 `Audio/Sink`（含那次「用户系统声音突然没了」的实测事故）
- 为什么手腕屏走自建 OpenXR 而不是 WayVR
- OpenXR 那条路上踩过的 6 个坑（事件强转偏移、`sync_actions` 的 FOCUSED 要求、
  `create_reference_space` 的句柄、GLX 失败会**杀进程**（Xlib 默认错误处理器）、
  niri 的 XWayland 没有 GLX、ctypes 签名）
- 手腕屏另外两个「改回去就出怪现象」的坑：图层 alpha 的两个 flag（漏了 = 蓝框外一圈黑边）、
  柱面层的 `pose` 是圆柱的轴而不是面板中心（弄错 = 圆心跑位、面板飘走）
- 构建期平台隔离怎么保证的
- 测试纪律（测试进程不许碰用户的运行时；桌面字幕的原生窗也归这条管 —— 测试进程
  拒绝建原生窗，真协议验收走**私有嵌套**合成器）
- 桌面字幕为什么在 Wayland 上必须走原生 layer-shell 窗、拖动为什么要**双源**
  （relative-pointer + 本地坐标法 —— niri 不发相对位移事件的实测记录）
