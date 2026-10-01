> [中文](../README.md) | **English** | [日本語](README.ja.md) | [한국어](README.ko.md) | [Русский](README.ru.md)

# VRChat Live Interpretation

[![CI](https://github.com/Sand-85/vrchat-livetranslate-extra/actions/workflows/ci.yml/badge.svg)](https://github.com/Sand-85/vrchat-livetranslate-extra/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Sand-85/vrchat-livetranslate-extra?label=release)](https://github.com/Sand-85/vrchat-livetranslate-extra/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)](GUIDE.en.md#1-prerequisites-windows)
[![Python](https://img.shields.io/badge/python-3.11-blue)](GUIDE.en.md#1-prerequisites-windows)
[![License](https://img.shields.io/badge/license-MIT-green)](../LICENSE)

Real-time simultaneous interpretation inside VRChat: capture your microphone / game audio →
Qwen Cloud real-time interpretation model → translation goes to the **chatbox bubble** and a
**VR wrist display**, with an optional path that feeds the translated voice back into a virtual
microphone so the other person **hears it directly**.

## 📌 About this repository

This repository is a **standalone enhanced build** of
**[nixi-agent/vrchat-livetranslate](https://github.com/nixi-agent/vrchat-livetranslate)** (MIT),
maintained by [@Sand-85](https://github.com/Sand-85) — upstream stays untouched; what is maintained
here is a set of enhancements that "make the translated voice actually usable" plus further,
more aggressive changes. It is kept in sync with upstream, and most features work on both sides.
**Everything here is built on the original author's work [nixi-agent](https://github.com/nixi-agent);
the licence stays MIT (see `LICENSE`, copyright belongs to the original author).**

**This repository ships the Windows build only (exe)** — configuration, documentation and tests all
target Windows; no Linux build or installation support is provided.
(The Linux-side implementation and `docs/GUIDE.linux.md` come along with upstream's code — that is
upstream's work; this repository neither verifies nor promises anything about it.)

What this repository adds on top of upstream:

| Capability | Details |
|---|---|
| **A/B voice source for the speech leg** | Hot-switch in Settings → "Voice: source and timbre": **A** = the realtime model's own voice (lowest latency) / **B** = local streaming TTS (timbre **identical** to the typing leg) |
| **Adjustable speech rate** | `text_input.tts.speech_rate` (measured, monotonic), voice-language locking, the cosyvoice backend and other details are in `docs/GUIDE.md` |
| **Fix: ticking "Room" did nothing on first use** | The `room:` section was only written to the file, the in-memory config was never refreshed → symptom: "the room checkbox does nothing, it works only after a restart" (first submitted here, since merged upstream ✅) |

> **Prebuilt exe is on [Releases](https://github.com/Sand-85/vrchat-livetranslate-extra/releases)**
> (single file, no installation); to run from source see `docs/GUIDE.md`.

> **Synced with upstream**: current baseline = upstream **v0.6.0** — upstream merged the **Room**
> (multi-user text relay) into main (#41, including the "first tick takes effect immediately" fix we
> had submitted as #5), and both our "stop no longer freezes the UI" fix and the author's follow-ups
> are in it (#38 / #40).
> This repository's matching version is **v0.6.1**; **both lines now speak the same room protocol**,
> the Room stays off by default, and ignoring it changes nothing.
> Beyond 0.5.x this upstream release also brings: the Room entry moved to **`⚙ Settings → Room`**
> (connect/disconnect button + "generate random" room code), desktop subtitles showing each room
> member's nickname, a right-click menu in the text input (cut / copy / paste / select all), and a
> packaging fix that keeps `vlt/room/sinks` in the built artifact (AppImage module accounting gate).
> The 0.5.x batch is in the package too: **desktop subtitles**, wrist-display pose stored per anchor,
> live input-gate level.
**This repository ships the Windows build only (exe)**: the Linux column in the platform table below
is a capability **upstream's code already has**; this repository does not build, verify or promise it
(see above). Both lines share the same `config.yaml` semantics.

![UI](../assets/gui.png)

---

> 🧭 **This document has three kinds of readers**
>
> - **Windows, just want the exe**: no Python, no command line needed. Anything mentioning
>   `.venv\Scripts\python.exe`, `run_*.bat`, or `--xxx` flags is **source-install only** — skip it;
>   every feature you need is in the GUI.
> - **Windows, running from source**: everything below applies to you.
> - **Linux users**: see **[GUIDE.linux.md](GUIDE.linux.md)** (install, dependencies, virtual mic,
>   wrist display and troubleshooting are all Linux-specific); you can skip the Windows details below.

## What it can do

| Feature | Status | Details |
|---|---|---|
| ① I speak → **chatbox bubble** | ✅ Implemented, on by default | Microphone → end-to-end speech translation → first incremental delta sent immediately, then a snapshot every 2 seconds, and the final version always sent at end of sentence |
| ② Others speak → **VR wrist display** | ✅ Implemented | Captures VRChat's playback output (WASAPI loopback) → translates into Chinese → renders to a SteamVR overlay, **pushed to the screen as soon as there's an update** |
| ③ I speak → **translated voice into their ears** | ✅ Implemented, off by default | The model outputs translated audio directly → resampled to 48 kHz → written to a virtual sound card → picked up as your VRChat microphone. Requires your own virtual sound card (VoiceMeeter / VB-Cable etc.) |
| ④ I speak → **type instead of talking** | ✅ Implemented, on by default | Input box in the bottom bar, **Enter sends**: use the keyboard instead of the microphone when you don't want to talk. The translation goes through the **exact same** downstream as ① (bubble / wrist display); with "Audio output" ticked it also **speaks the translation via TTS** into the virtual sound card (the other person hears it) |
| ⑤ A few people → **see each other's subtitles (room)** | ✅ Done, off by default | Everyone runs their own copy and enters the **same room code** to see **what the others are saying** on their own wrist overlay / desktop subtitles. Only your own speech is broadcast (game audio is never relayed). Entry: `⚙ Settings → Room` |

### Platform support

| Feature | Windows | Linux |
|---|---|---|
| ① chatbox bubble | ✅ | ✅ |
| ② VR wrist display | ✅ SteamVR overlay | ✅ Built-in OpenXR overlay (Monado / WiVRn) |
| ③ Translated voice into their ears | ✅ Needs your own virtual sound card (VoiceMeeter / VB-Cable) | ✅ **None needed** — the app declares a virtual mic at runtime |
| ④ Type instead of talking (with TTS) | ✅ | ✅ |
| ⑤ Room (see each other's subtitles) | ✅ | ✅ |

Linux install & usage: **[GUIDE.linux.md](GUIDE.linux.md)** ·
Design rationale and dead ends: **[docs/平台约束记录.md](平台约束记录.md)** (Chinese)

---

## 📖 Guide

Everything from **quick start** to **known limitations** (install, API key, usage, configuration,
troubleshooting, project structure, development) lives in separate documents:

- **Windows** → **[Guide (GUIDE.en.md)](GUIDE.en.md)**
- **Linux** → **[Linux guide (GUIDE.linux.md)](GUIDE.linux.md)**
  (install with `./setup.sh`, launch with `./run_gui.sh`)

---

## 🙏 Wishlist

This is where I put the things **I want to see done but I'm not good at / don't know how to /
don't want to do myself**. If one of them looks like something you'd enjoy, go right ahead — no
need to ask first. When it's done, open an issue or a PR and I'll link your work under that item.

> 📌 **This list keeps growing.** New wishes get added as they come to me; finished ones get removed
> (or marked ✅ with the author credited). Star the repo, or just drop by every now and then.

- [ ] **① A "how do I use this" tutorial video** — any creator, **any language**
      Get the app installed and working end to end first (just follow
      [0. Fastest start](GUIDE.en.md#0-fastest-start-download-the-ready-made-exe)), then record a
      beginner-friendly walkthrough: how to download and install it, where the API key goes, and how
      to actually use it inside VRChat. Chinese / English / 日本語 / 한국어 / Русский all welcome — any
      platform, any length, any style.

- [ ] **② A written tutorial with screenshots** — **any language**
      Same audience: walk a newcomer through everything from "download" to "first successful
      translation" using screenshots plus text. A blog post, a docs page, a PDF or a long thread all
      count.

- [ ] **③ Native speakers to proofread the UI translations** — 日本語 / 한국어 / Русский / English
      The UI ships in five languages, but the Japanese, Korean and Russian word lists were only ever
      done by machine plus my own (non-native) pass — **naturalness, politeness level and consistent
      terminology are exactly what only a native speaker can judge**. All it takes: switch the UI to
      your language, use it for a bit, and file an issue with screenshots of anything that reads
      wrong. Want to fix it directly instead? `vlt/locales/<lang>.py` is a plain
      "Chinese original → your language" table — edit it and open a PR, **no code involved**.

It doesn't have to be perfect — sparing one person a single footgun already counts as a win.

---

## ☕ Sponsor

**Help cover my token bill** 🙏

- ☕ **Ko-fi** (international / credit card / PayPal):
  - Original author (upstream):
    [![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kcmnixi)
  - This fork (Extra):
    [![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/sand85)

- In China: scan with WeChat / Alipay
  - Original author:
    ![QR codes](../assets/sponsor-qrcodes.png)
  - This fork (Extra):
    ![QR codes](../assets/sponsor-qrcodes-Sand.png)

- 🔑 Haven't signed up for Qwen Cloud yet? **[Sign up for Qwen Cloud ▸](https://www.qianwenai.com/)**

> The GUI's top bar also has a "☕ Sponsor" button — it opens the same links.

---

## License

[MIT License](../LICENSE) © 2026 Nixi
