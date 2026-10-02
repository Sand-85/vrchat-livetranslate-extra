"""配置加载：YAML + 环境变量。API key 不落配置文件，只从环境变量或 bl CLI 的配置读。"""
from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .session.base import (SessionConfig, DEFAULT_FINAL_SILENCE_S,
                           DEFAULT_FAST_FINAL_SILENCE_S, DEFAULT_FAST_FINAL_MIC_QUIET_S)

from .paths import APP_DIR, BUNDLE_DIR

ROOT = APP_DIR                                    # 保留别名，兼容既有引用
DEFAULT_CONFIG = APP_DIR / "config.yaml"           # 可写：用户配置
# 入库的是模板；config.yaml 是用户自己的配置（设备名/语言偏好），已被 gitignore。
EXAMPLE_CONFIG = BUNDLE_DIR / "config.example.yaml"  # 只读：随程序分发的模板


def _as_str_map(raw: Any, what: str) -> dict[str, str]:
    """把配置里的「映射表」（专有词库 / 方向级热词）规范成 `dict[str, str]`。

    配置是手写的，写错形状的概率不为零：写成列表、写成标量、或者哪条只有键没有值。
    这些都必须**留痕后丢掉**，绝不能原样下发给服务端 —— 服务端收到非映射的 phrases
    会整条会话被拒（`session.update` 是整体校验，坏一个字段就全军覆没），
    而用户看到的只是「翻译不工作」，根本联想不到是自己那行 YAML 写错了。
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        print(f"[config] ⚠️ {what} 不是映射表（读到 {type(raw).__name__}），已忽略该段；"
              f"正确写法：\n{what}:\n  \"原文\": \"译名\"", flush=True)
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        key, val = str(k or "").strip(), str(v or "").strip()
        if key and val:
            out[key] = val
        else:
            print(f"[config] ⚠️ {what} 里有一条空条目（键或值为空），已跳过：{k!r} → {v!r}",
                  flush=True)
    return out


def ensure_config(path: Path | None = None) -> Path:
    """确保配置文件存在：不存在就从 config.example.yaml 复制一份。

    这样新克隆的仓库（以及给朋友用的时候）开箱即用，而用户的实际配置
    不会进版本库——设备名这类东西因机器而异，跟着仓库走只会互相污染。
    """
    p = Path(path) if path else DEFAULT_CONFIG
    if p.exists() or not EXAMPLE_CONFIG.exists():
        return p
    try:
        p.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[config] 已从 {EXAMPLE_CONFIG.name} 生成 {p.name}")
    except OSError as exc:
        print(f"[config] 生成 {p.name} 失败（将使用内置默认值）：{exc}")
    return p


def load_api_key(explicit: str | None = None) -> str:
    """API key 读取顺序：显式参数 → 界面保存的 → 环境变量 → ~/.bailian/config.json（bl CLI）。

    界面保存的排第二（仅次于显式传参）：用户在界面上填了 key，就是最明确的意图，
    不该被环境变量或 CLI 配置盖掉。来源会打一行日志（**只打码、绝不打明文**），
    否则"为什么连的还是旧 key"根本查不出来。
    """
    if explicit:
        return explicit.strip()
    try:
        from .credentials import load_saved_key, mask_key

        saved = load_saved_key()
        if saved:
            print(f"[config] API key 来源：界面保存（{mask_key(saved)}）", flush=True)
            return saved
    except Exception as exc:  # noqa: BLE001
        print(f"[config] ⚠️ 读取界面保存的 key 失败，继续走其它来源："
              f"{type(exc).__name__}: {exc}", flush=True)
    env = os.environ.get("DASHSCOPE_API_KEY")
    if env:
        return env.strip()
    cfg = Path(os.path.expanduser("~/.bailian/config.json"))
    if cfg.exists():
        data = json.loads(cfg.read_text(encoding="utf-8"))
        for key in ("api_key", "apiKey", "DASHSCOPE_API_KEY"):
            if data.get(key):
                return str(data[key]).strip()
    raise SystemExit("找不到 API key：设 DASHSCOPE_API_KEY，或先跑 `bl auth login --api-key <key>`")


def merge_hotwords(glossary: dict[str, str] | None,
                   hotwords: dict[str, str] | None) -> dict[str, str]:
    """合并「全局专有词库」与「方向级热词」——**全项目唯一的口径**。

    为什么要有这个函数：词库要同时喂给两条腿（实时会话的 `translation.corpus.phrases`
    和打字翻译的 `translation_options.terms`）。若两处各写一遍合并逻辑，迟早会漂移
    （改了一处忘了另一处）——于是「说话时对、打字时不对」这种最难查的 bug 就来了。

    优先级：方向级覆盖全局（同名词条以 `directions.<X>.hotwords` 为准）。
    这样「全局一份常用词库 + 某个方向临时特例」不用把词库复制两遍。
    """
    return {**(glossary or {}), **(hotwords or {})}


@dataclass
class Direction:
    source_lang: str | None = None
    target_lang: str = "en"
    output_audio: bool = False
    hotwords: dict[str, str] = field(default_factory=dict)
    voice: str | None = None

    def to_session_config(self, base: dict[str, Any]) -> SessionConfig:
        return SessionConfig(
            model=base["model"],
            target_lang=self.target_lang,
            source_lang=self.source_lang,
            output_audio=self.output_audio,
            voice=self.voice or base.get("voice") or "Tina",
            # 全局专有词库 + 本方向的覆盖，合并口径只有 merge_hotwords 一处
            hotwords=merge_hotwords(base.get("glossary"), self.hotwords),
            turn_detection=base.get("turn_detection"),
            base_url=base["base_url"],
            workspace_id=base.get("workspace_id") or "",
            api_key=base["api_key"],
            reconnect_backoff=tuple(base.get("reconnect_backoff", (2, 5, 10, 30))),
            max_new_sessions_per_minute=int(base.get("max_new_sessions_per_minute", 4)),
            final_silence_s=float(base.get("final_silence_s", DEFAULT_FINAL_SILENCE_S)),  # 默认值必须 > 服务端增量间隔（实测最大 2.3s），改小会让最终版在句子中间抢跑
            # 快封句（麦克风也静了 → 用户确实说完了）：文字静默到这个值就封，省 ~1.8s；
            # None = 关掉快路径（退回纯 final_silence_s）
            fast_final_silence_s=_opt_float(base.get("fast_final_silence_s",
                                                     DEFAULT_FAST_FINAL_SILENCE_S)),
            fast_final_mic_quiet_s=float(base.get("fast_final_mic_quiet_s",
                                                 DEFAULT_FAST_FINAL_MIC_QUIET_S)),
        )


@dataclass
class AppConfig:
    session_base: dict[str, Any]
    directions: dict[str, Direction]
    chatbox: dict[str, Any]
    merger: dict[str, Any]
    overlay: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] = field(default_factory=dict)
    ui: dict[str, Any] = field(default_factory=dict)      # 界面上次的选择（方向/输出勾选），启动时恢复
    text_input: dict[str, Any] = field(default_factory=dict)   # 打字输入（替代说话）
    # 桌面字幕（PC 桌面模式：贴 VRChat 窗口的叠加窗）。与 `overlay`（头显手腕屏）是两条腿，
    # 视觉参数从 `overlay` 段继承，见 vlt/output/desktop_overlay.py 的 from_dict。
    desktop_overlay: dict[str, Any] = field(default_factory=dict)
    # 房间中继原始配置段。**故意不在这里做字段级校验**：校验统一走
    # `vlt/room/model.py::RoomConfig.from_dict`（脏值回落 + 留痕都做好了），
    # 这里只负责把原始 dict 带出来，避免出现两份口径。
    room: dict[str, Any] = field(default_factory=dict)

    def direction(self, name: str) -> Direction:
        if name not in self.directions:
            raise SystemExit(f"配置里没有方向的 key：{name}（现有：{list(self.directions)}）")
        return self.directions[name]

    def merged_hotwords(self, name: str) -> dict[str, str]:
        """某个方向**实际生效**的专有词库（全局 + 方向级覆盖）。

        引擎两条腿都从这里取值：实时会话走 `Direction.to_session_config`（内部调
        `merge_hotwords`），打字翻译走这里 —— 同一个口径，不会一边有一套。
        """
        d = self.directions.get(name)
        return merge_hotwords(self.session_base.get("glossary"), d.hotwords if d else None)


def _opt_int(value) -> int | None:
    """可选整数字段：None/空串 → None（= 不传该参数）；非法值也回 None（不因此启动失败）。"""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value) -> float | None:
    """可选浮点字段：None/空串 → None（= 不传该参数）；非法值也回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_api_key(api_key: str | None, require_key: bool) -> str:
    """取 key；`require_key=False` 时"还没有 key"不抛错，而是返回空串。

    界面用得上：启动时**不能**因为没填 key 就起不来 —— 那样用户连"去哪填 key"
    的入口都看不到（首次使用、或换台电脑给朋友用，就是死局）。
    调用方（`_start`）会检查空 key 并给出明确指引。
    """
    try:
        return load_api_key(api_key)
    except SystemExit:
        if require_key:
            raise
        print("[config] ⚠️ 尚未配置 API key —— 界面照常启动；"
              "开始翻译前请点主界面右上角的 API key 入口填一个", file=sys.stderr, flush=True)
        return ""


def load_config(path: str | Path | None = None, api_key: str | None = None,
                require_key: bool = True) -> AppConfig:
    p = ensure_config(Path(path) if path else None)   # 缺文件时从 config.example.yaml 生成
    raw: dict[str, Any] = {}
    if p.exists():
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            # 配置文件被写坏了（实测：残留的孤立序列项会让整个文件非法，
            # 比如 `pos: [...]` 下面还留着旧版的 `- 0.0`）→ 备份 + 从模板重建，
            # 否则程序下次连启动都起不来。**绝不静默**：打印清楚，并保留坏文件供排查。
            broken = p.with_name(p.name + ".broken")
            try:
                shutil.copyfile(p, broken)
            except OSError as exc2:  # noqa: BLE001
                broken = None
                print(f"[config] 备份损坏的配置也失败了：{exc2}", file=sys.stderr)
            print(f"[config] ❌ 配置文件解析失败：{exc}\n"
                  f"[config]    （这是「配置被写坏」的表现，不是你的错）\n"
                  f"[config]    已备份到 {broken}，并从 {EXAMPLE_CONFIG.name} 重新生成默认配置 ——"
                  f"设备/语言等选择需要重设一次",
                  file=sys.stderr)
            if EXAMPLE_CONFIG.exists():
                shutil.copyfile(EXAMPLE_CONFIG, p)
                raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    s = raw.get("session", {}) or {}
    session_base = {
        "model": s.get("model", "qwen3.8-livetranslate-flash-realtime"),
        "base_url": s.get("base_url", "wss://maas.qianwenaiapi.com/api-ws/v1/realtime"),
        "voice": s.get("voice", "Tina"),
        "turn_detection": s.get("turn_detection"),
        "workspace_id": s.get("workspace_id") or "",
        "reconnect_backoff": s.get("reconnect_backoff", [2, 5, 10, 30]),
        "max_new_sessions_per_minute": s.get("max_new_sessions_per_minute", 4),
        "final_silence_s": float(s.get("final_silence_s", DEFAULT_FINAL_SILENCE_S)),  # 默认值必须 > 服务端增量间隔（实测最大 2.3s），改小会让最终版在句子中间抢跑
        # 快封句：麦克风也静了 → 文字静默 fast_final_silence_s 就封（省 ~1.8s）；None = 关掉
        "fast_final_silence_s": _opt_float(s.get("fast_final_silence_s",
                                                DEFAULT_FAST_FINAL_SILENCE_S)),
        "fast_final_mic_quiet_s": float(s.get("fast_final_mic_quiet_s",
                                             DEFAULT_FAST_FINAL_MIC_QUIET_S)),
        # 长静音闸门 + 本地 repeat 抑制：原样透传，取值校验在 engine 里做
        # （非法值会**留痕**并回落默认值，见 engine.silence_gate_settings / repeat_guard_settings）
        "silence_gate_enabled": s.get("silence_gate_enabled", True),
        "silence_gate_after_s": s.get("silence_gate_after_s", 30),
        "silence_gate_preroll_s": s.get("silence_gate_preroll_s", 1.0),
        "repeat_guard_enabled": s.get("repeat_guard_enabled", True),
        "repeat_guard_hits": s.get("repeat_guard_hits", 3),
        "repeat_guard_ratio": s.get("repeat_guard_ratio", 0.9),
        # 全局专有词库放在这个公共底座里：`to_session_config` 只有这一个入参，
        # 而词库对两条腿（实时会话 / 打字翻译）是同一份 —— 放在这里两处都拿得到。
        "glossary": _as_str_map(raw.get("glossary"), "glossary（专有词库）"),
        "api_key": _resolve_api_key(api_key, require_key),
    }
    directions = {}
    for name, d in (raw.get("directions") or {}).items():
        directions[name] = Direction(
            source_lang=(d or {}).get("source_lang"),
            target_lang=(d or {}).get("target_lang", "en"),
            output_audio=bool((d or {}).get("output_audio", False)),
            hotwords=_as_str_map((d or {}).get("hotwords"), f"directions.{name}.hotwords（方向级热词）"),
            voice=(d or {}).get("voice"),
        )
    if not directions:
        directions = {"mine": Direction(source_lang="zh", target_lang="en")}
    raw_output = raw.get("output") or {}
    raw_audio = raw_output.get("audio") or {}
    raw_capture = raw.get("capture") or {}
    raw_textin = raw.get("text_input") or {}
    # room 段原样带出（脏值交给 RoomConfig.from_dict 回落 + 留痕）；
    # 用户把 `room:` 写成一行字符串之类时这里只保证类型是 dict，不做字段级校验。
    raw_room = raw.get("room") or {}
    if not isinstance(raw_room, dict):
        raw_room = {}
    output = {
        "audio": {
            "enabled": bool(raw_audio.get("enabled", False)),
            # 译音**音源**（A/B 热切换）：realtime = 实时模型自带音频；tts = 本地流式 TTS
            # （音色与打字腿一致）。开关在界面「设置」里，改完立即生效。
            "mode": ("tts" if str(raw_audio.get("mode") or "realtime").strip().lower()
                     in ("tts", "b", "typing", "same") else "realtime"),
            "device": raw_audio.get("device") or ["voicemeeter input", "voicemeeter aux input", "cable input", "vb-audio"],
            "device_name": str(raw_audio.get("device_name") or ""),
            "sample_rate": int(raw_audio.get("sample_rate", 48000)),
            "buffer_ms": int(raw_audio.get("buffer_ms", 300)),
            "max_buffer_ms": int(raw_audio.get("max_buffer_ms", 2000)),
        },
        "capture": {
            "mic_device": str(raw_capture.get("mic_device") or ""),
            "loopback_device": str(raw_capture.get("loopback_device") or ""),
            # 输入门限（只作用于 loopback = VRChat 输出「别人说话」那条腿）：
            # 原样透传，取值校验在 engine.input_gate_settings 里做（非法值留痕 + 回落默认值）。
            # ⚠️ 默认值必须与 engine 的 INPUT_GATE_DEFAULT_* 一致（这里不能 import engine：
            #    engine 反向 import 本模块，会成环）。
            "gate_enabled": raw_capture.get("gate_enabled", True),
            "gate_db": raw_capture.get("gate_db", -45.0),
            "gate_hold_ms": raw_capture.get("gate_hold_ms", 500),
            "gate_preroll_ms": raw_capture.get("gate_preroll_ms", 250),
        },
    }
    return AppConfig(
        session_base=session_base,
        directions=directions,
        chatbox=raw.get("chatbox") or {},
        merger=raw.get("merger") or {},
        overlay=raw.get("overlay") or {},
        output=output,
        ui=raw.get("ui") or {},
        room=raw_room,
        desktop_overlay=raw.get("desktop_overlay") or {},
        text_input={
            "enabled": bool(raw_textin.get("enabled", True)),
            # 默认 qwen-mt-flash：实测 qwen3-livetranslate-flash 的**文本**接口会原样回吐
            # （中文进中文出，换个句子又正常），不能依赖；mt 系列稳定且能自动识别源语言。
            "model": str(raw_textin.get("model") or "qwen-mt-flash"),
            "timeout_s": float(raw_textin.get("timeout_s", 20.0)),
            # 打字也要出声：文本翻译不回音频，这一步单独用 TTS 合成后喂虚拟声卡
            "tts": {
                "enabled": bool((raw_textin.get("tts") or {}).get("enabled", True)),
                # qwen3-tts-*（内置/设计音色，不可复现）或 cosyvoice-*（seed 可复现）
                "model": str((raw_textin.get("tts") or {}).get("model") or "qwen3-tts-flash"),
                "voice": str((raw_textin.get("tts") or {}).get("voice") or "Cherry"),
                # 仅 cosyvoice 生效：固定 seed → 同一句两次合成逐字节一致；写 null 则不传（随机）
                "seed": _opt_int((raw_textin.get("tts") or {}).get("seed", 1234)),
                # 可选语气/风格提示（如「请用四川话说」）；空则不传
                "instruction": str((raw_textin.get("tts") or {}).get("instruction") or ""),
                # 流式合成（SSE）：首包 ~0.4s 就能起播，整段要等 1.6~1.9s → 默认开
                "stream": bool((raw_textin.get("tts") or {}).get("stream", True)),
                # 语速（仅 qwen3-tts 生效）：1 = 默认；0.8 约慢 20%，1.2 约快 20%（实测单调）
                "speech_rate": _opt_float((raw_textin.get("tts") or {}).get("speech_rate")),
                "timeout_s": float((raw_textin.get("tts") or {}).get("timeout_s", 30.0)),
            },
        },
    )
