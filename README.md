# VRChat 实时同传

> **中文** | [English](docs/README.en.md) | [日本語](docs/README.ja.md) | [한국어](docs/README.ko.md) | [Русский](docs/README.ru.md)

[![CI](https://github.com/Sand-85/vrchat-livetranslate-extra/actions/workflows/ci.yml/badge.svg)](https://github.com/Sand-85/vrchat-livetranslate-extra/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Sand-85/vrchat-livetranslate-extra?label=release)](https://github.com/Sand-85/vrchat-livetranslate-extra/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)](docs/GUIDE.md#一前置条件)
[![Python](https://img.shields.io/badge/python-3.11-blue)](docs/GUIDE.md#一前置条件)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

在 VRChat 里做**实时同声传译**：采集麦克风 / 游戏音频 → 千问云实时同传模型 →
译文送到 **chatbox 气泡**、**VR 手腕屏**，可选把译音回灌进虚拟麦克风**让对方直接听见**。

## 📌 关于本仓库

本仓库是 **[nixi-agent/vrchat-livetranslate](https://github.com/nixi-agent/vrchat-livetranslate)**（MIT）的**独立增强版**，由
[@Sand-85](https://github.com/Sand-85) 维护 —— 上游保持原样，这里维护的是一组「让译音真正可用」和 增加更多激进改动 的增强，和上游同步更新且大部分功能互通
**基础全部来自上游原作者 [nixi-agent](https://github.com/nixi-agent)，协议沿用 MIT（见 `LICENSE`，版权归原作者）。**

**本仓库只发布 Windows 版（exe）**，配置、文档与测试都以 Windows 为准 —— 不提供 Linux 构建/安装支持。
（仓库里跟随上游代码一并带来了 Linux 侧的实现与 `docs/GUIDE.linux.md`，那是上游的东西，本仓库不对它做验证或承诺。）

相对上游多出来的部分：

| 能力 | 说明 |
|---|---|
| **流式合成（SSE）** | 打字腿「开口」从 ~1.7s 降到 **~0.52s**；源语言=目标语言时直接跳过翻译请求| 已合并上游
| **语音腿译音音源 A/B** | 设置「译音：音源与音色」里热切换：**A** 实时模型自带音色（延迟最低）/ **B** 本地流式 TTS（音色与打字腿**完全一致**） |
| **语速可调** | `text_input.tts.speech_rate`（实测单调可控），音色语言锁定、cosyvoice 后端等细节见 `docs/GUIDE.md` |
| **修复：整段反复重念** | 多路流式 TTS 并发写同一虚拟声卡导致分片交错 —— 改为单飞队列 + 合并 |已合并上游✅
| **修复：房间首次勾选不生效** | 补建 `room:` 段只写了文件、内存里的配置没刷新 → 表现为「勾了房间没反应、重启一次才好」（已回馈上游） |

> **预编译 exe 在 [Releases](https://github.com/Sand-85/vrchat-livetranslate-extra/releases)**（单文件、免安装）；
> 想从源码跑见 `docs/GUIDE.md`。

> **随上游同步**：当前基线 = 上游 **v0.4.0-beta.1 的内容**（多人房间 · 文字中继）+ v0.3.1 的 44100Hz 重采样修复。
> 本仓库把它作为 **0.4.0 正式版**发布；房间**默认关闭**，不用它完全不受影响。
**Windows 10/11 与 Linux 都支持。** 两边共用同一份 `config.yaml`，配置语义一致。

![界面](assets/gui.png)

---

> 🧭 **这份文档有三种读者**
>
> - **Windows 直接下 exe 的**：不需要 Python、不需要命令行。凡是出现 `.venv\Scripts\python.exe`、
>   `run_*.bat`、`--xxx` 的地方都是**源码安装专用**，跳过即可——你要的功能界面上都有。
> - **Windows 从源码跑的**：下面全部适用。
> - **Linux 用户**：看 **[GUIDE.linux.md](docs/GUIDE.linux.md)**（安装、依赖、虚拟声卡、
>   手腕屏、排障都是 Linux 专用的）；本文下面的 Windows 细节可以跳过。

## 它能做什么

| 功能 | 状态 | 说明 |
|---|---|---|
| ① 我说 → **chatbox 气泡** | ✅ 已实现，默认开启 | 麦克风 → 端到端语音翻译 → 首个增量立即发、之后每 2 秒刷一次快照、句末必发最终版 |
| ② 别人说 → **VR 手腕屏** | ✅ 已实现 | 采集 VRChat 播放输出（WASAPI loopback）→ 译成中文 → 渲染到 SteamVR overlay，**有更新就立刻上屏** |
| ③ 我说 → **译音进对方耳朵** | ✅ 已实现，默认关闭 | 模型直出译音 → 重采样 48kHz → 写进虚拟声卡 → VRChat 麦克风拾取。需自备虚拟声卡（VoiceMeeter / VB-Cable 等） |
| ④ 我说 → **打字替代说话** | ✅ 已实现，默认开启 | 界面底栏输入框，**回车即发**：不想开麦时用键盘代替麦克风，译文走的是和①**完全相同**的下游（气泡 / 手腕屏）；勾了「译音输出」时还会用 **TTS 把译文念出来**送进虚拟声卡（对方能听到） |

### 平台支持

| 功能 | Windows | Linux |
|---|---|---|
| ① chatbox 气泡 | ✅ | ✅ |
| ② VR 手腕屏 | ✅ SteamVR overlay | ✅ 自建 OpenXR overlay（Monado / WiVRn） |
| ③ 译音进对方耳朵 | ✅ 需自备虚拟声卡（VoiceMeeter / VB-Cable） | ✅ **不需要自备**，程序运行时自己声明虚拟麦克风 |
| ④ 打字替代说话（含 TTS） | ✅ | ✅ |

Linux 的安装与用法：**[GUIDE.linux.md](docs/GUIDE.linux.md)** ·
设计依据与「哪些路试过不通」：**[docs/平台约束记录.md](docs/平台约束记录.md)**

---

---

## 📖 使用指南

从**快速上手**到**已知限制**的完整内容（安装、API key、用法、配置、排障、项目结构、开发）都在单独文档里：

- **Windows** → **[使用指南（docs/GUIDE.md）](docs/GUIDE.md)**
- **Linux** → **[Linux 使用指南（docs/GUIDE.linux.md）](docs/GUIDE.linux.md)**
  （安装用 `./setup.sh`，启动用 `./run_gui.sh`）

---

## 🙏 许愿列表（Wishlist）

这里放的是**我想做、但自己不擅长 / 不会 / 不想自己做**的事。你如果看到哪条觉得「这个我能做」，
不用先问我，直接动手就行 —— 做完开个 issue 或者 PR 喊我一声，我把你的成果挂到这条下面。

> 📌 **这个列表会持续更新**：想到新的就往上加，做完的会移走（或者标上 ✅ 和作者）。
> 想第一时间看到新增，点个 Star 或者偶尔回来翻翻即可。

- [ ] **① 一份「怎么用」的教程视频** — 录制者不限，**语言不限**
      先自己把软件装好、完整跑通一遍（照着 [零、最快上手](docs/GUIDE.md#零最快上手下载现成的-exe) 走就行），
      然后录一个面向新手的教程：怎么下载安装、API key 填在哪里、怎么在 VRChat 里真正用起来。
      中文 / English / 日本語 / 한국어 / Русский 都可以；发在哪个平台、多长、什么风格都随你。

- [ ] **② 一份图文版教程** — **语言不限**
      同样是面向新手：用截图 + 文字，把「从下载到第一次翻译成功」的全过程讲清楚。
      博客文章、文档站、PDF、一条长帖都算数。

- [ ] **③ 母语者帮忙校对界面翻译** — 需要 日本語 / 한국어 / Русский / English 的母语者
      界面现在支持五种语言，但日/韩/俄三套词表是我的机翻 + 我这个非母语者手动过了一遍 ——
      **语感、敬体一致性、术语统一这些只有母语者看得出来**。做法很简单：把界面切到你的语言、
      正常用一用，把「翻错 / 不自然 / 看不懂」的地方配上截图开个 issue 就行；
      想直接改也可以：`vlt/locales/<语言>.py` 就是一张「中文原文 → 你的语言」的对照表，
      照着改、提 PR 即可，**不需要碰代码**。

不要求做得完美 —— 能让后来的人少踩一个坑，就算成功。

---

## ☕ 赞助原作者

**请给我报销 token** 🙏 

- ☕ **Ko-fi**（海外 / 信用卡 / PayPal）：

  [![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kcmnixi)

- 国内：微信 / 支付宝扫码

![收款码](assets/sponsor-qrcodes.png)

- 🔑 还没开通千问云？**[点此开通「千问云」▸](https://www.qianwenai.com/)**

> 图形界面顶栏也有「☕ 赞助」按钮，点开就是上面的入口。

---

## 许可

[MIT License](LICENSE) © 2026 Nixi
