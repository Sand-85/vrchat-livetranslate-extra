# VRChat 实时同传 · 使用指南

> **中文** | [English](docs/GUIDE.en.md) | [日本語](docs/GUIDE.ja.md) | [한국어](docs/GUIDE.ko.md) | [Русский](docs/GUIDE.ru.md)

> 🪟 **这份是 Windows 指南。**
> 用 **Linux** 的话看 **[GUIDE.linux.md](docs/GUIDE.linux.md)** —— 安装（`./setup.sh`）、虚拟声卡、
> 手腕屏、排障都是 Linux 专用的；下面这些 Windows 细节（exe / `.bat` / VB-Cable / WASAPI）不适用。
> 两边共用同一份 `config.yaml`，配置语义一致。

[← 返回 README](../README.md)

---

## 零、最快上手：下载现成的 exe

到 **[Releases](https://github.com/nixi-agent/vrchat-livetranslate/releases/latest)** 下载
`VRChatLiveTranslate.exe`（**单文件、免安装、无控制台窗口**），双击即用。

- 仍然要自备一个**千问云 API key**：界面里的「⚙ 设置」粘贴保存
- 配置与日志写在 `%APPDATA%\vrchat-livetranslate`（exe 放在只读目录也能跑）
- 想做成绿色版（配置跟着 exe 走）→ 在 exe 旁边放一个**空的 `portable.txt`**
- 旧版把 `config.yaml` / `logs/` 放在 exe 旁边的话，首次运行会**自动迁移**到新位置，原来的文件不删

### 会自动更新吗？

会**自动检查**，但**绝不自动安装**——升不升、什么时候升，永远你说了算：

- 每次启动后在后台悄悄检查一次新版本；检查失败（比如没网）不打扰你，只写进日志
- 发现新版本会弹窗，三个选择：
  - **立即更新** → 自动下载（约一两分钟），下完点「立即重启并更新」马上换好；
    或者点「稍后更新」，等你哪天关程序时自动换，下次打开就是新版
  - **下次再说** → 这次不弹了，下次启动再看
  - **不再提示这个版本** → 跳过这个版本，出更新的再告诉你
- 下载失败 → 可以重试，也可以点窗口里的「打开下载页自己下」去 Releases 页手动下
- 想在没弹窗的时候看看有没有新版 → 「⚙ 设置 → 软件更新 → 检查更新」
- 更新**不会动你的配置和 API key**；更新成功后下次启动会告诉你一声

**从源码跑的**：不走上面这套，在仓库目录 `git pull` 即可（源码运行时点「立即更新」也会这样提示你）。

### 界面语言

界面支持 **简体中文 / English / 日本語 / 한국어 / Русский** 五种语言：

- **默认跟随 Windows 的显示语言**（认得出日/韩/俄就用对应语言；其它语言按英文接待；中文环境保持中文）
- 想手动改 → 「⚙ 设置 → 界面语言」选一种，**重启程序后生效**（选完会明确告诉你这一点）
- 语言只影响**界面文案**，不影响翻译方向的语种选择（那是另一组下拉）

想看源码 / 自己打包 / 改代码 → 从下面「一」开始。

---

## 一、前置条件（Windows）

1. **Windows 10 / 11**（用到 WASAPI 与 SteamVR；**Linux 见 [GUIDE.linux.md](docs/GUIDE.linux.md)**）
2. **Python 3.11**（3.12 未测；安装时务必勾选 *Add python.exe to PATH*）—— *只用 exe 的话这条不用管*
   <https://www.python.org/downloads/release/python-3119/>
3. **VRChat**：设置里打开 OSC（`OSC enabled: True`），chat bubble visibility 设为 **Everyone**
4. **SteamVR** —— 只有「别人说 → 手腕屏」这个功能需要
5. **千问云 API key**（个人实名认证即可）

## 二、安装（源码）

双击 **`setup.bat`**（自动建 `.venv` + 装依赖，约 1–2 分钟）。

手动等价命令：

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
```

## 三、配置 API key

> 🔑 还没有千问云账号？**[点此开通「千问云」▸](https://www.qianwenai.com/)**

按下面的顺序找一个用，**前面找到就不看后面**：

| 优先级 | 来源 | 怎么设 |
|---|---|---|
| 1 | 界面里保存的 key | 图形界面「⚙ 设置 → API key」粘贴保存（存在 `%USERPROFILE%\.vrchat-livetranslate\api_key.txt`） |
| 2 | 环境变量 | `setx DASHSCOPE_API_KEY "sk-你的key"`（**重开一个命令行窗口才生效**） |
| 3 | 百炼 CLI 的配置 | `bl auth login --api-key "sk-你的key"`（需要 `npm install -g bailian-cli`），读 `%USERPROFILE%\.bailian\config.json` |

**key 只从上面这三处读，不写进项目文件、不写进 `config.yaml`。**

> 🔒 **防误提交**：仓库带凭据扫描。跑一次 `install_secret_guard.bat` 装上 pre-commit 钩子后，
> 任何含 `sk-...` / `gho_...` / `AKIA...` / `Bearer ...` / 硬编码密钥的提交会被直接拦下
> （脚本：`scripts/check_no_secrets.py`）。
> 手动全量检查：`python scripts/check_no_secrets.py --once`
> 为什么需要：key 一旦进了 git 历史，删掉文件也清不掉，只能 rewrite 历史 —— 宁可在提交前拦。

## 四、自检

**用 exe 的**：双击打开界面，确认三件事——

1. 「⚙ 设置」里三个设备下拉不是空的（`麦克风` / `VRChat 音频` / `译音输出`）
2. API key 状态位显示**已配置**（否则点它会跳到开通页）
3. 勾上 `chatbox`，点「开始翻译」，对着麦克风说一句话 → 气泡里出现译文

**源码安装的**：双击 **`run_selfcheck.bat`**。它按顺序做四件事，不需要麦克风、不需要 VRChat 也能跑完前 3 步：

1. 模块导入检查（engine / session / overlay / chatbox / merger）
2. 读一次 API key 并打码打印（确认顺序对）
3. 离线渲染一帧手腕屏贴图（不接管 SteamVR）
4. 用自带测试音频跑一遍完整链路（中文 → 英文，打进 chatbox）

VRChat 开着的话，气泡里应该出现：

```
Hello, I'm Nixi. Today, we're going to test out the real-time simultaneous interpretation feature in VRChat.
```

**先自检再报错。**

---

## 五、使用

### 图形界面（推荐）

**双击 exe**，或源码安装下双击 **`run_gui.bat`**——都是同一个界面（Tkinter，标准库，零额外依赖、秒开）。

| 区域 | 内容 |
|---|---|
| 第一行 | `开始翻译` / `停止翻译`、方向单选（`我说` / `别人说` / `双向同时`）、语言对下拉（源 → 目标）、右侧 `☕ 赞助` 与 `⚙ 设置` |
| 第二行 | `输出:` 勾选 `chatbox` / `手腕屏` / `译音输出`，以及 `微调 ▸` 按钮；最右侧是 API key 状态位（已配置时是纯文本 `API key 已配置`，**未配置时变成可点的「⚠ 未配置 API key · 点此开通千问云 ▸」**） |
| 聊天区 | 右侧蓝色气泡 = 我说的，左侧灰色气泡 = 别人说的；每格两行，**上行原文小字、下行译文大字**；可滚动回看（上限 500 条） |
| 状态栏 | 左侧彩色圆点 + 最新一条状态；右侧统计（`运行中` / `已翻译 N 条` / `首增量 Xms`），两者互不覆盖 |
| 底栏 | **打字输入框**：`打字:` 输入框 + `发送`，**回车即发**（`Esc` 清空）。只在方向含「我说」时可用，其余情况置灰 |
| `☕ 赞助` 弹窗 | Ko-fi 跳转按钮 + 微信 / 支付宝收款码（等比缩放到 240px，可直接扫） |

- **流式上屏**：未 final 的增量**就地重画同一条气泡**，不会每来一个增量就新增一条
- **双向同时**：同一个窗口跑两个翻译方向（麦克风 → 右侧、游戏音频 → 左侧），第二个**错开 300ms 启动**避免抢音频设备；任一侧失败，另一侧照常工作
- **语言镜像**：一个语言对双向对应——选「中文 → 英语」，别人说方向自动变「英语 → 中文」。
  源语言可选 `自动检测` / 中文 / 英语 / 日语 / 韩语 / 法语 / 德语 / 西班牙语 / 俄语（目标语言是同一张表但没有「自动检测」）；
  源语言选 `自动检测` 时对方方向的目标回落中文并在状态栏说明。改动**立即生效**并写回配置，下次启动保留
- **设置弹窗**（`⚙ 设置`）：API key 输入 / 清除、三个音频设备下拉 + `刷新`、**译音菜单**（音源 A/B + 两条腿的音色下拉与试听）、日志区（`导出日志压缩包…`）
- **微调面板**（`微调 ▸`）：手腕屏的锚点 + 12 个滑块，**拖动即热重载、不用重启**（详见「六、配置」）
- **译音菜单（`⚙ 设置 → 译音：音源与音色`）**：一个菜单管两件事 ——
  **① 音源（A/B 热切换，不用重启）**：**A `realtime`（默认）** 语音腿用实时模型自带的译音，延迟最低，音色取下面的「说话译音」；**B `tts`** 语音腿的译音改用本地**流式 TTS** 合成，音色与打字腿**完全一致**（取「打字译音」那一路），代价是要等该句**终版文本**（短句约 +0.5s，连续长句可能等到句末）。切换会**重建一次会话**（算一次连接，受 RPM 10 预算保护），文字输出不受影响；选择写回 `output.audio.mode`，下次启动沿用。
  **② 音色**：两条腿的音色来自**不同模型**、id 不通用（跨模型混用会被服务端拒），所以是两个**可编辑**下拉 + 各自「试听」：「说话译音」写 `session.voice`（实时模型，默认 `Tina`）；「打字译音」写 `text_input.tts.voice`（qwen3-tts，默认 `Cherry`）。表里没有的音色（新出的 / 声音复刻的自定义 id）直接手填即可。
- **打字输入**：不想开麦时用键盘替代麦克风 —— 底栏输入框敲字，**回车即发**。译文走的是和说话**完全相同**的下游（聊天气泡 / 手腕屏 / chatbox）；勾了「译音输出」时**还会出声**：译文经 TTS 合成后写进虚拟声卡，对方能听到（见「六、配置」的 `text_input.tts`）。它替代的是**麦克风**，故只在方向含「我说」时可用（其余情况输入框置灰）

#### 自动化验收（无头，不开窗口）

```bat
:: 源码安装
.venv\Scripts\python.exe -m vlt.gui --self-test
.venv\Scripts\python.exe -m vlt.gui --self-test-dual

:: exe（没有控制台窗口，结果写在日志里；打包脚本就是这么自检的）
VRChatLiveTranslate.exe --self-test
```

成功打印 `GUI_SELFTEST_OK` / `GUI_SELFTEST_DUAL_OK mine=N theirs=N`，失败打印 `..._FAIL: ...` 到 stderr 并以非 0 退出。需要真实 API key。

> 🪵 **崩溃日志**：界面每次启动都会在 `logs/` 下写一个 `gui_<时间戳>.log`，
> 里面含**版本信息**（git HEAD）、Python 环境、以及所有 stdout/stderr（**每行行首带 `HH:MM:SS.mmm` 时间戳**）。
> 已接入四类捕获：原生崩溃（`faulthandler`）、主线程异常、子线程异常、Tk 回调异常。
> 闪退时**把这个文件发给维护者就能定位**——没有它的话窗口一关什么都不剩。
> 单文件写满 2MB 自动切段，日志总量超过 5MB 自动删最旧的；「设置」里可以一键导出成一个**脱敏**压缩包。

### 命令行（仅源码安装）

exe 是无控制台窗口的单文件 GUI，除了上面的 `--self-test` 之外没有命令行用法；命令行只对源码安装有意义。

```bat
:: 我说 → chatbox（真发到 VRChat）
.venv\Scripts\python.exe -m vlt.app --direction mine --mic --sink chatbox

:: 别人说 → 手腕屏（需要 SteamVR 在跑）
.venv\Scripts\python.exe -m vlt.app --direction theirs --loopback --sink overlay

:: 不开 VRChat 也能验链路：用自带测试音频跑一遍
.venv\Scripts\python.exe -m vlt.app --direction mine --pcm testdata\zh_test_16k.pcm --dry-run
```

两个方向要同时翻译 → **开两个命令行窗口**（或用图形界面的「双向同时」，那是一个窗口跑两条会话）。
两个方向各自一条 WebSocket 会话，互不干扰（限流 RPM 10，够用）。

嫌命令长就用现成的 bat：**`run_chatbox.bat`**（我说 → 气泡，会先列一遍设备再启动）、
**`run_overlay.bat`**（别人说 → 手腕屏）。

#### 全部参数

| 参数 | 作用 |
|---|---|
| `--direction mine/theirs` | 用 `config.yaml` 里哪个方向 |
| `--mic` / `--loopback` / `--pcm <file>` | 音源：麦克风 / VRChat 播放输出 / 离线 PCM（三选一，优先级 `--pcm` > `--mic` > `--loopback`） |
| `--sink chatbox/overlay/both` | 输出去向（默认 `chatbox`） |
| `--list-devices` | 列出音频输入/输出设备后退出 |
| `--dry-run` | 不真发 OSC，只打印与落报文 |
| `--overlay-dry-run` | overlay 不接管 SteamVR，把每帧渲染成 PNG 存 `out/overlay_frames/` |
| `--audio-out` | 开启译音输出（覆盖 `config.yaml` 的 `output.audio.enabled`） |
| `--audio-device <名称...>` | 虚拟声卡设备名回退链（覆盖 `config.yaml` 里的默认值） |
| `--no-realtime` | 尽快灌入 PCM（不做实时节流；只对 `--pcm` 有效） |
| `--settle-s <秒>` | 音频推完后等待响应的秒数（默认 8） |
| `--config <path>` | 用别的配置文件 |

> 麦克风采集是**连续运行**的：跑到 `Ctrl+C` 为止（没有时长参数）。
>
> 📌 **选音频设备不用传参数**：在图形界面「⚙ 设置」里选（下拉里第一项「自动检测」= 走回退链），
> 或直接改 `config.yaml` 的 `capture.mic_device` / `capture.loopback_device`。
> 存的是**设备名字符串**而不是索引——索引会随插拔/会话切换整个变掉。

---

## 六、配置（`config.yaml`）

> 📄 **首次运行会自动从 `config.example.yaml` 生成 `config.yaml`**。
> `config.yaml` 是**你自己的配置**（设备选择、语言偏好等）——它已被 gitignore、**不进版本库**，
> 因为设备名这类东西因机器而异，跟着仓库走只会互相污染。想恢复默认：删掉再启动即可。
>
> 它在哪：
> - **用 exe**：`%APPDATA%\vrchat-livetranslate\config.yaml`（放了 `portable.txt` 的绿色版则在 exe 旁边）
> - **源码安装**：仓库根目录的 `config.yaml`
>
> 🛟 **配置写坏不会让你起不来**：YAML 解析失败时会自动备份成 `config.yaml.broken`、
> 从模板重新生成一份并打印清楚原因，然后继续启动。

界面上改的任何东西（方向、输出勾选、语言、设备、手腕屏微调）都会**就地写回**这个文件——
只改那一行，**注释、空行、键顺序全部保留**（写入前还会先验证一遍生成的 YAML 合法，不合法就拒绝写入）。

### 关键键

```yaml
session:
  model: qwen3.8-livetranslate-flash-realtime   # 也可换 qwen3.5-*（事件名按代次自动分派）
  base_url: wss://dashscope.aliyuncs.com/api-ws/v1/realtime
  voice: Tina                 # ⚠️ 必须显式指定，不填会报 Voice 'Chelsie' is not supported
  turn_detection: null        # 留空 = 服务端默认
  final_silence_s: 3.0        # ⚠️ 必须 > 服务端增量间隔（实测最大 2.3s），调小会让最终版在句子中间抢跑
  max_new_sessions_per_minute: 4   # RPM 10 预算：每次 WS 连接算一次请求
  reconnect_backoff: [2, 5, 10, 30]

capture:                      # 空字符串 = 自动检测
  mic_device: ""
  loopback_device: ""
  gate_enabled: true          # 输入门限：只作用于「别人说」（VRChat 输出）那条腿
  gate_db: -45.0              # 低于这个响度（dBFS）的音频不上送 → 滤掉远处小声的玩家
  gate_hold_ms: 500           # 越阈值后回落这段时间内仍继续送（句中停顿/尾音不被切碎）
  gate_preroll_ms: 250        # 开闸时补发越阈值之前的一段（句首不掉字）

directions:
  mine:   { source_lang: zh,   target_lang: en, output_audio: false }   # 我说 → 气泡
  theirs: { source_lang: null, target_lang: zh, output_audio: false }   # 别人说 → 手腕屏（null = 自动识别）

# 专有词库（界面入口：⚙ 设置 → 专有词库）。详细规则见下方「专有词库」一节
glossary:                     # 全局：只放**两个方向都要同一个译名**的词
  "VRChat": "VRChat"          # 译名照抄原文 = 保持原样，不让模型意译
  "逆袭": "Nixi"
# ⚠️ 只在某一个方向成立、或两个方向需要**不同**译名的词条，放对应方向：
#   mine.hotwords:   { "Nekoya": "Nekoya" }   # 我说 → 英文：保持原样
#   theirs.hotwords: { "Nekoya": "猫屋" }     # 别人说 → 中文：用中文名
# （全局那份会同时作用到两个方向，放错了会让反方向的译文里出现另一个名字）

chatbox:
  interval_s: 2.0             # 增量刷新节奏（漏桶 5 条/5 秒 → 2 秒一条余量充足）
  max_chars: 144              # 官方上限（字符，不是字节）

text_input:                   # 打字输入（底栏输入框，回车发送）
  enabled: true               # false = 界面不显示输入行
  model: qwen-mt-flash        # 打字走的**文本**翻译模型，也可换 qwen-mt-plus
                              # ⚠️ 别填 qwen3-livetranslate-flash：实测它的文本接口会原样回吐
  timeout_s: 20               # 单次翻译超时（秒）
  tts:                        # 打字也要出声（译文经 TTS 合成 → 虚拟声卡 → 对方能听到）
    enabled: true             # 需勾选「译音输出」且方向含「我说」；没开译音时这步自动跳过，只出文字
    model: qwen3-tts-flash    # 也可换 qwen3-tts-instruct-flash / qwen3-tts-vd-<日期>（自建音色同模型）
    voice: Cherry             # 多语言音色；自建音色用它的 id，且必须与创建时所用模型配对
    stream: true              # 流式合成（SSE）：首段音频 ~0.5s 起播；false = 等整段（~1.7s）
    speech_rate: 1.0          # 语速（仅 qwen3-tts 生效）：1 = 默认；0.85 约慢 20%，1.2 约快 20%
    seed: null                # 仅 cosyvoice 生效：固定后同句两次合成逐字节一致；null = 随机
    instruction: ""           # 仅 cosyvoice 生效：可选语气/方言提示，如「请用四川话说」
    timeout_s: 30

merger:
  interval_s: 2.0             # 首 delta 立即发，之后每 2 秒一次快照，句末必刷最终版
  carry_over: false           # true = 把上一段最终译文作为前缀保留（连续字幕观感）

overlay:
  interval_s: 0               # 0 = 有更新立刻上屏（本地显示不受 chatbox 限流约束）
  anchor: right_hand          # left_hand | right_hand | tracker | hmd
  size_px: [1024, 320]
  font_size: 42               # 译文字号
  source_font_size: 30        # 原文小字号
  show_source: true           # 双行显示（3.8 默认就返回源文识别结果，零额外成本）

output:
  audio:
    enabled: false            # 译音总开关：与 directions.<X>.output_audio 是「与」关系
    mode: realtime            # 译音音源（设置里可热切换）：realtime = A 实时模型音色（延迟最低）/
                              # tts = B 用本地流式 TTS 合成，音色与打字腿完全一致（每句多等约 0.5s）
    device_name: ""           # 手选的虚拟声卡名（非空时优先于回退链）
```

### 输入门限（滤掉远处说话小声的玩家）

VRChat 把所有人声混成一路输出：远处玩家声音小、本来就听不清，早先照样会被送给模型 ——
白花钱，还容易被翻成乱话。打开门限后按**每 100ms 块的 RMS 响度**判：低于 `gate_db` 的块
**不上送**，模型听不到 = 不翻译。

- **在哪调**：界面「⚙ 设置 → 输入门限」＝ 开关 + 滑块 + 一条**实时电平条**
  （横轴 −70 ~ 0 dBFS；白竖线是门限，蓝色填充 = 当前这段会被翻译）。开始翻译后电平条才有读数；
  拖滑块**立刻生效**（正在跑的那条腿也会当场换阈值），落盘延后 300ms。
- **怎么调**：让远处的人说话，看着电平把滑块拖到「门外」；再让近处的人说话，确认填充能过线。
  参考量级：近处正常说话 −30 ~ −20 dBFS，远处小声常在 −50 以下。
- **三条不切坏句子的保证**：① 越阈值后 `gate_hold_ms` 内即使回落到门限以下也继续送
  （句中停顿、句尾渐弱不被切碎）；② 开闸时补发越阈值前 `gate_preroll_ms` 的音频（句首辅音不掉字）；
  ③ 只**暂停上送**，不改写、不伪造音频（与服务端断句逻辑的节奏假设一致）。
- **只管「别人说」那条腿**：麦克风（自己说话）不走门限 —— 贴着麦说话本来就响，不该被拦。
- 与 `session.silence_gate_*`（长静音闸门）是两条独立机制：前者治「小声」，后者治
  「长时间真静音」（防服务端 repeat 掉线），串联工作、互不抵消。
- 判据用 RMS 不用峰值：单个采样点的尖刺（按键、爆音）能把**峰值**顶满，但只把 100ms 块的 RMS
  抬高约 32dB —— 所以门限治"持续小声"，不治"瞬态噪声"。被爆音频繁误开就把门限调高（如 −35）。

### 专有词库（社团名 / 人名 / 术语）

**什么时候需要它**：在 VRChat 里念社团名、朋友的名字、或者圈内术语时，模型会干两件让人恼火的事——

1. **听错**：语音识别把 `Nekoya` 听成 `Nikoya`（实测）；
2. **意译 / 自创译名**：把专有名词按字面翻掉，或者自己编一个音译。

专有词库就是把这类词**钉死**成你要的写法。

#### 先说清性质：这是「术语干预」，不是字符串替换

词库是**下发给模型的提示**（实时会话的 `corpus.phrases`、打字翻译的 `terms`），模型会尽量按你给的译名走，
但**不是逐字替换**。实测（同一句话，方向＝别人说 → 中文）：

| 词库 | 原文（识别结果） | 译文 |
|---|---|---|
| 无 | `I am from the **Nikoya** Group.` | 我来自 **Nikoya** 集团。 |
| `Nekoya=猫屋` | `I am from the **Nekoya** group.` | 我来自 **猫屋** 集团。 |

两个附带的好消息：

- **它顺带提升识别率**：`Nekoya` 本来被听成 `Nikoya`，写进词库后识别就对了 —— 词库的 key 同时也是**识别热词**；
- **搭配交给模型处理**：`Nekoya Studio is recruiting new members.` 会译成「**猫屋工作室**正在招募新成员。」，
  而不是生硬逐字替换（`Studio` 正常译成「工作室」）。

**边界**（别期待它做不到的事）：

- 模型**自己**音译出来的写法不会被改写。例如 `Nekoya` 被自创音译成「尼可亚」时，词条 `Nekoya=猫屋` 救不回来 ——
  要把「尼可亚」也写成一条（`尼可亚=猫屋`）；
- 识别错得太多也没救（整个名字被听成别的词，词库匹配不上）。

#### 怎么用

`⚙ 设置 → 专有词库`，一行一条 `原文=译名`：

```
VRChat=VRChat
逆袭=Nixi
Nekoya=猫屋
```

- `=` 左边是**原文**（按**源语言**里的实际写法填），右边是你希望的**译名**；
- 右边照抄左边（如 `VRChat=VRChat`）＝「保持原样，不要翻」；
- 左边写中文、右边写英文也完全可以（如 `逆袭=Nixi`）；
- 以 `#` 开头的行是注释、空行忽略。

#### ⚠️ 关键：方向性的词条**不要**放进全局那份

界面里那个文本框编辑的是**全局** `glossary`，它**同时作用到两个方向**。而两个方向的目标语言不同，
同一个词往往需要**不同的译名** —— 这时候必须写进对应方向的 `directions.<方向>.hotwords`：

| 词条该放哪 | 什么时候 | 例子 |
|---|---|---|
| `glossary`（全局，界面可编辑）| 两个方向都要**同一个译名** | `VRChat=VRChat`、`逆袭=Nixi` |
| `directions.mine.hotwords`（手改 config.yaml）| 只对「我说」成立 | `Nekoya=Nekoya`（念英文名，英文译文保持原样）|
| `directions.theirs.hotwords`（手改 config.yaml）| 只对「别人说」成立 | `Nekoya=猫屋`（对方念英文名，中文译文用中文名）|

**为什么必须分**（实测反例）：把 `Nekoya=猫屋` 放进全局后，「我说 → 英文」那条腿也会吃到它 ——
打字说「我来自 Nekoya 社团。」，英文译文变成 `I come from the Cat House club.`，社团名被换掉了。
实时那条腿也复现过同性质的问题（直接把中文译名塞进英文句子）。

> ✅ 界面里那行「**作用方向**」下拉就是在切这两张表：`全局` / `我说` / `别人说`。
> 切到哪个方向，编辑与保存的就是那一张（保存只动那一张，**不会**碰另一张）。
> 手改 config.yaml 也一样有效 —— 打开设置/切换作用方向时读的是**磁盘上的当前内容**，
> 不会被界面上的旧内容覆盖回去。同名词条**以方向级为准**（方向级覆盖全局）。

#### 保存后会怎么生效

| 那条腿 | 词库去哪儿 | 生效时机 |
|---|---|---|
| 说话（实时会话） | `session.translation.corpus.phrases` | 立即**重建会话**（服务端会话级配置，不能热改）|
| 打字（文本翻译） | `translation_options.terms`（Qwen-MT 术语干预） | 下一条打字即生效 |

> ⚠️ 说话那条腿生效需要重建会话，而每次 WebSocket 连接都算一次请求（RPM 预算）。
> 如果一分钟内已经重建过几次，程序会提示「本次未重建会话」——**停止后重新开始翻译**即可用上新词库。
> 打字那条腿不受影响，一直是立刻生效。

### 手腕屏微调面板（12 个滑块，拖动即生效）

| 滑块 | 范围 / 步长 | 滑块 | 范围 / 步长 |
|---|---|---|---|
| 位置 X / Y / Z | −0.30 ~ 0.30 m，0.005 | 大小 | 0.05 ~ 0.80 m，0.01 |
| 俯仰 / 偏航 / 翻滚 | −180 ~ 180°，1 | 弯曲 | 0.0 ~ 0.50，0.01 |
| 透明度 | 0.10 ~ 1.00，0.05 | 译文字号 | 20 ~ 64，1 |
| 原文字号 | 14 ~ 48，1 | 面板高 | 240 ~ 560 px，10 |

外加**锚点**下拉（右手 / 左手 / 前臂 tracker / 头显前固定）与 **tracker 序号**（0–3）。
改动走 200ms 防抖落盘；其中**译文字号 / 原文字号 / 面板高**会触发贴图重渲一帧，其余只重应用变换。

> 实测参考：42/30 字号 + 320px 面板放得下 **2 轮**对话；36/24 + 420px 放得下 **3 轮共 6 行**。

---

## 七、排障

| 现象 | 原因 / 处理 |
|---|---|
| 气泡里没东西 | VRChat 没开 / OSC 没开 / chat bubble visibility 是 Off。`--dry-run` 能看到发送日志说明程序没问题 |
| `[loopback] ❌ 没找到任何 loopback 设备` | VRChat 没在放声音；或**在远程桌面会话里跑**（WASAPI 端点按会话隔离，必须在物理机当前会话） |
| 麦克风采不到声音 | 同上；先在「⚙ 设置」里确认设备下拉里选的是哪个（枚举为空时状态栏会提示「远程会话下枚举为空是正常的」） |
| `Voice 'Chelsie' is not supported` | `session.voice` 没填。保持 `Tina` |
| `Invalid translation parameter` | `session.update` 缺 `translation` 字段（代码里已保证，改代码时注意） |
| `1007 Requests rate limit exceeded` | 撞了 RPM 10。等 1 分钟；别频繁重启（**每次 WS 连接都算一次请求**） |
| 译文停在半句话上 | 静默兜底阈值被调小了。`session.final_silence_s` 必须 > 2.3s，默认 3.0 |
| `[overlay] ⚠️ SteamVR 未运行或不可用` | 正常降级：只有手腕屏不显示，chatbox 不受影响 |
| 手腕屏看不见 | 先确认 SteamVR 在跑；再调「微调」里的位置 / 大小；`--overlay-dry-run` 能出 PNG 说明渲染没问题 |
| 手腕屏过一阵子消失 | 已带两级自愈（连续 3 次失败重建 overlay → 再 3 次失败硬重启 openvr 连接 → 之后每 50 次重试一次）。日志每 30s 有一条 `[overlay][diag] 心跳：…` 可以看「最后成功上传多久前 / 重建次数」 |
| 译音输出没声音 | ①两个开关都要开（输出勾选 + 方向级）；②只对「我说的话」方向有效；③虚拟声卡装了吗、状态栏/日志会明说原因；④**其余功能不受影响** |
| 界面「开始翻译」点了没反应 | 先看终端输出——asyncio 回调里的异常只在控制台打一行 traceback，不进状态栏（用 exe 的话看 `logs/` 里最新的那个 `.log`） |
| 报错看不懂 | **「⚙ 设置 → 导出日志压缩包」**，已自动脱敏，把 zip 发给维护者即可 |

---

## 八、项目结构

```
vlt/
├── app.py                CLI 入口（音频源 → 会话 → 节流 → 输出）
├── engine.py             可编程引擎：会话 + 看门狗重连 + 节流 + chatbox/overlay/译音的启停
├── gui.py                Tkinter 图形界面（含 --self-test 无头验收）
├── config.py             配置加载；凭据解析顺序；写坏自愈
├── textin.py             打字输入：文本翻译（实时模型不接受文本入口，故走 compatible-mode）
├── tts.py                打字出声：译文经 qwen3-tts 合成成音频，喂给虚拟声卡那条腿
├── credentials.py        API key 的保存 / 清除 / 打码
├── devices.py            音频设备枚举与「按名字解析回索引」
├── paths.py              可写目录决策（源码 / exe / 绿色版）+ 旧文件迁移
├── crashlog.py           崩溃捕获 + 日志切段/清理 + 脱敏导出
├── session/
│   ├── base.py           TextDelta / SessionConfig / create_session（按模型代次分派）
│   └── qwen38.py         3.8 与 3.5 的事件分派 + 连接预算 + 静默兜底 + 事件埋点
└── output/
    ├── merger.py         首 delta 立即发 → 2s 快照 → 句末 flush
    ├── chatbox.py        OSC ,sTT + 令牌桶 + 最终版补发队列
    ├── overlay.py        SteamVR 手腕屏：渲染 + 锚点 + 热重载 + 两级自愈
    └── virtualmic.py     译音回灌：24k→48k 重采样 + 抖动缓冲（整句丢弃，绝不切句）

scripts/                  探针与调试工具（probe_* / osc_listen / verify_release）
tests/                    23 个文件、148 个测试函数（离线可跑，CI 逐文件执行）
docs/                     P0.5 / P1 / P2 三份实测结果（协议、延迟、手腕屏）
testdata/                 自带测试音频（中文 8.56s、英文 7.92s，16kHz 单声道 PCM）
assets/                   图标、界面截图与赞助收款码
```

---

## 九、开发

### 跑测试

**不用 pytest**（依赖里没有），每个测试文件都是可独立执行的脚本，与 CI 完全同款：

```bat
:: 全部（逐文件跑，与 CI 完全同款；在 cmd 里直接粘，写进 .bat 文件时把 %t 改成 %%t）
for %t in (tests\test_*.py) do @.venv\Scripts\python.exe %t

:: 单个
.venv\Scripts\python.exe tests\test_virtualmic.py
```

测试**全部离线可跑**（不需要麦克风 / VRChat / SteamVR / 网络），覆盖设备解析、路径决策、
断线重连、日志轮转与脱敏、赞助弹窗、手腕屏自愈、译音缓冲不变式、配置就地写入不破坏注释等。

唯一例外是 `tests/test_engine.py`：它要打一次真实会话，**需要本机已配置 API key**，
CI 里显式跳过（workflow 里有 `::notice::` 说明原因，不做静默跳过）。

### 打包

```bat
build_exe.bat                                              :: 打包 + 打完自检
.venv\Scripts\python.exe scripts\build_exe.py --no-verify  :: 只打包
```

产物 `dist\VRChatLiveTranslate.exe`：PyInstaller **单文件、无控制台窗口**，
内置 `config.example.yaml` / `testdata` / `assets`，约 37 MB。
打完后默认会真跑一次 `exe --self-test`，从日志里找 `GUI_SELFTEST_OK` 才算通过。

### CI / 发布

- **CI**（`.github/workflows/ci.yml`，push 到 main / PR / 手动）：
  语法检查 → 凭据扫描 → 逐文件跑全部离线测试 → 再单独验一次**打包链路**（产物存在且 ≥20MB）
- **Release**（`.github/workflows/release.yml`，推 `v*` tag 触发）：
  先对账 tag 与 `vlt/__init__.py` 的 `__version__`（不一致直接失败）→ 打包 →
  建 Release，附件是 **exe** 与 `SHA256SUMS.txt`：校验值 GitHub 会在附件旁直接显示 `sha256:…`，而那份摘要文件是过渡期给 **v0.2.0 及更早客户端**用的（它们只认这个附件，缺了会静默查不到更新）
- **下载后想自己复核**：`scripts/verify_release.py` 会把 Release 附件拉下来对账
  （SHA256、真跑 `--self-test`、版本行、字节码里搜新功能字符串、图标像素比对）：

  ```bat
  .venv\Scripts\python.exe scripts\verify_release.py v0.2.2 "手腕屏没启动起来"
  ```

---

## 十、已知限制

> 以下均为 **Windows** 侧的限制。Linux 侧的限制请看
> [GUIDE.linux.md](docs/GUIDE.linux.md) 的「八、已知限制」。

- 手腕屏需要 **SteamVR 作为活动合成器**；走厂商原生 OpenXR runtime 时 PC 侧第三方 overlay 不显示
- 输入端**只能拿到游戏混音后的一路立体声**：拿不到逐说话人通道，混音里多人叠话时说话人归属天然不可靠
- WebSocket 链路**无回声消除与降噪** → **戴耳机是硬要求**（外放会把别人的声音采进来，同一句识别两遍）
- 同时翻译两个方向 = 两条 WebSocket 会话，预算按两个方向叠算
- **打字出声是「翻译 + TTS」两次请求**：文字几乎立刻出来，声音要等 TTS 合成回来。
  默认走**流式合成**（`text_input.tts.stream: true`）：首段音频 **~0.5s** 就能起播，
  而整段合成要等 ~1.7s 才开口（把 `stream` 关掉即回到该行为）。
  音色是 TTS 音色（默认 `Cherry`），与语音腿的实时模型音色**不是同一个**。
  条件与第三条腿一致：勾了「译音输出」且虚拟声卡可用，否则自动跳过、只出文字
- **打字替代的是麦克风**：所以只在方向含「我说」时可用，且需要先点「开始翻译」
- **chatbox 只发『我说的话』的译文**：对方说的话的译文不进 chatbox 气泡，看手腕屏或界面聊天区即可
- **输入门限只能按响度过滤，不能按玩家身份或距离过滤**：VRChat 输出里没有逐玩家通道，
  所以近处的人贴着麦小声说（耳语）一样会被滤掉，远处的人吼一嗓子照样通过；
  阈值以上的 BGM / 世界音效 / 枪声也会过门（门限不是「只留人声」）

---

[← 返回 README](../README.md)
