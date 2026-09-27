> [中文](README.md) | **English** | [日本語](README.ja.md) | [한국어](README.ko.md) | [Русский](README.ru.md)

# vrchat-livetranslate

[![CI](https://github.com/nixi-agent/vrchat-livetranslate/actions/workflows/ci.yml/badge.svg)](https://github.com/nixi-agent/vrchat-livetranslate/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/nixi-agent/vrchat-livetranslate?label=release)](https://github.com/nixi-agent/vrchat-livetranslate/releases/latest)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)](GUIDE.en.md#1-prerequisites)
[![Python](https://img.shields.io/badge/python-3.11-blue)](GUIDE.en.md#1-prerequisites)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Real-time simultaneous interpretation inside VRChat: capture your microphone / game audio →
Alibaba Cloud Bailian real-time interpretation model → translation goes to the **chatbox bubble** and a
**VR wrist display**, with an optional path that feeds the translated voice back into a virtual
microphone so the other person **hears it directly**.

![UI](assets/gui.png)

---

> 🧭 **This document has two kinds of readers**
>
> - **Just want the exe**: no Python, no command line needed. Anything mentioning `.venv\Scripts\python.exe`,
>   `run_*.bat`, or `--xxx` flags is **source-install only** — skip it; every feature you need is in the GUI.
> - **Running from source**: everything below applies to you.

## Interface language

The UI is available in **Simplified Chinese**, **English**, **日本語**, **한국어** and
**Русский**. It follows your Windows display language by default, and you can switch it
manually in **⚙ Settings**. **A restart is required for the change to take effect.**

## What it can do

| Feature | Status | Details |
|---|---|---|
| ① I speak → **chatbox bubble** | ✅ Implemented, on by default | Microphone → end-to-end speech translation → first incremental delta sent immediately, then a snapshot every 2 seconds, and the final version always sent at end of sentence |
| ② Others speak → **VR wrist display** | ✅ Implemented | Captures VRChat's playback output (WASAPI loopback) → translates into Chinese → renders to a SteamVR overlay, **pushed to the screen as soon as there's an update** |
| ③ I speak → **translated voice into their ears** | ✅ Implemented, off by default | The model outputs translated audio directly → resampled to 48 kHz → written to a virtual sound card → picked up as your VRChat microphone. Requires your own virtual sound card (VoiceMeeter / VB-Cable etc.) |
| ④ I speak → **type instead of talking** | ✅ Implemented, on by default | Input box in the bottom bar, **Enter sends**: use the keyboard instead of the microphone when you don't want to talk. The translation goes through the **exact same** downstream as ① (bubble / wrist display); with "Audio output" ticked it also **speaks the translation via TTS** into the virtual sound card (the other person hears it) |

---

---

## 📖 Guide

Everything from **quick start** to **known limitations** (install, API key, usage, configuration,
troubleshooting, project structure, development) lives in a separate document:

**➡️ [Guide (GUIDE.en.md)](GUIDE.en.md)**

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

  [![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kcmnixi)

- In China: scan with WeChat / Alipay

![QR codes](assets/sponsor-qrcodes.png)

- 🔑 Haven't signed up for Bailian yet? **[Sign up for Alibaba Cloud Bailian ▸](https://www.aliyun.com/minisite/goods?userCode=q8nma978)**

> The GUI's top bar also has a "☕ Sponsor" button — it opens the same links.

---

## License

[MIT License](LICENSE) © 2026 Nixi
