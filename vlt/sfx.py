"""通联开关音（每句话开头 / 结尾的「哔」声）。

用户给的一对素材：开头 `on2.wav`（0.23s 845Hz）、结尾 `off2.wav`（0.45s 1325Hz），
只有 TTS 出声（打字腿 / 语音腿 B 模式）才播 —— A 模式用的是实时模型自带的音频，不经过这里。

配置（`config.yaml`）：

    text_input:
      tts:
        open_sfx: assets/sfx/on2.wav      # 相对路径按「打包目录 → 可写目录」依次找；留空 = 不播
        close_sfx: assets/sfx/off2.wav
        sfx_voice: qwen-tts-vc-XXXX-voice-…   # 可选：**只在这条音色上播**；留空 = 任何音色都播
        sfx_gain: 0.35                    # 音量增益（1.0 = 素材原样；0 = 静音）。0~1 之间可随意调

为什么单独一个模块：读 WAV / 重采样 / 转声道这些与引擎无关，放这儿离线可测（`tests/test_radio_sfx.py`），
引擎那边只负责"什么时候推"。
"""
from __future__ import annotations

import logging
import wave
from pathlib import Path

import numpy as np

from .output.virtualmic import resample_24k_mono_to_48k_stereo

log = logging.getLogger(__name__)

TARGET_RATE = 24000        # 先统一到 24k 单声道 s16le，再交给现有 resample_24k_mono_to_48k_stereo
OPEN_KEY = "open_sfx"
CLOSE_KEY = "close_sfx"
VOICE_KEY = "sfx_voice"    # 可选：这对开关音只绑在这条音色上（留空 = 任何音色都播）
GAIN_KEY = "sfx_gain"      # 音量增益（1.0 = 素材原样）

# 默认增益：素材（on2/off2）是**顶满电平**的（RMS -10.8 dBFS、峰值≈0 dBFS），
# 而译音语音的 RMS 只有 -22~-29 dBFS → 原样播会**比说话声响 11~18 dB**（用户实测反馈「太响」）。
# 0.35 倍（≈ -9 dB）让开关音的峰值（≈ -9 dBFS）与 RMS（≈ -20 dBFS）都大致对齐语音。
# ⚠️ 换 `open_sfx`/`close_sfx` 素材时不用改这里 —— 用 `sfx_gain` 现场调。
DEFAULT_GAIN = 0.35


def read_wav_as_24k_mono(path: Path) -> bytes:
    """把任意 WAV（8/16/24/32bit、任意采样率、单/双声道）读成 24k 单声道 s16le 字节。

    双声道取平均；8bit 按无符号（静音=128）解 —— 本项目的素材两种都出现过。
    """
    with wave.open(str(path), "rb") as w:
        sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        if w.getcomptype() != "NONE":
            raise ValueError(f"不支持的压缩格式 {w.getcomptype()}：{path.name}")
        raw = w.readframes(n)

    if sw == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sw == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 3:      # 24bit 小端
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = (b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16))
        v = np.where(v & 0x800000, v - 0x1000000, v)
        x = v.astype(np.float32) / 8388608.0
    elif sw == 4:
        x = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"不支持的位深 {sw * 8}bit：{path.name}")

    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    if sr != TARGET_RATE and len(x):                       # 线性重采样到 24k
        n2 = int(round(len(x) * TARGET_RATE / sr))
        x = np.interp(np.linspace(0, len(x) - 1, n2), np.arange(len(x)), x).astype(np.float32)
    return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()


def _voice_base(voice: str) -> str:
    """取 voice id 的「家族基名」：`qwen-tts-vc-<名>-voice-<时间戳>-<后缀>` → 前面那段。

    为什么要它：同一条音色重建一次，时间戳/后缀会变；按基名比就不会因为重建而突然不播。
    """
    v = (voice or "").strip()
    return v.split("-voice-")[0] if "-voice-" in v else v


def bound_to_current_voice(tts_cfg: dict | None) -> bool:
    """这对开关音现在该不该播（按**当前音色**判断）。

    - `sfx_voice` 留空 → 不绑定，任何音色都播（老配置/老行为）；
    - 填了 voice id（或它的家族基名）→ 只有当前 `voice` 与之相符时才播。
    """
    cfg = tts_cfg or {}
    want = str(cfg.get(VOICE_KEY) or "").strip()
    if not want:
        return True
    cur = str(cfg.get("voice") or "").strip()
    if not cur:
        return False
    return cur == want or _voice_base(cur) == _voice_base(want)


def _resolve(name: str, bases: tuple[Path, ...]) -> Path | None:
    """把配置里的路径解析成真实文件：绝对路径直接用；相对路径按 bases 依次找。"""
    raw = (name or "").strip()
    if not raw:
        return None
    p = Path(raw)
    if p.is_absolute():
        return p if p.exists() else None
    for base in bases:
        cand = base / raw
        if cand.exists():
            return cand
    return None


def apply_gain(pcm: bytes, gain: float) -> bytes:
    """对 24k 单声道 s16le PCM 做幅度缩放（**削波安全**：先缩放再裁，绝不让 int16 溢出回绕）。

    `gain=1.0` 原样返回（零拷贝路径）；`0.0` = 静音；非有限值/负数按 1.0（不该因此把整句话静掉）。
    """
    if not pcm:
        return pcm
    g = float(gain)
    if not np.isfinite(g) or g < 0:
        return pcm
    if g == 1.0:
        return pcm
    x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * g
    return np.clip(x, -32768.0, 32767.0).astype(np.int16).tobytes()


def gain_of(tts_cfg: dict | None) -> float:
    """读增益：缺省/空 → `DEFAULT_GAIN`；**非法值留痕**再回落默认（本仓库的既有口径）。"""
    raw = (tts_cfg or {}).get(GAIN_KEY)
    if raw is None or raw == "":
        return DEFAULT_GAIN
    try:
        g = float(raw)
    except (TypeError, ValueError):
        print(f"[sfx] ⚠️ {GAIN_KEY}={raw!r} 不是数字 → 回落默认增益 {DEFAULT_GAIN}",
              flush=True)
        return DEFAULT_GAIN
    if g < 0:
        print(f"[sfx] ⚠️ {GAIN_KEY}={raw!r} 是负数 → 回落默认增益 {DEFAULT_GAIN}", flush=True)
        return DEFAULT_GAIN
    return g


def load_pair(tts_cfg: dict | None, bases: tuple[Path, ...], *,
              gain: float | None = None) -> tuple[bytes, bytes]:
    """读一对开关音 → `(开头音, 结尾音)` 的 48k 立体声 s16le 字节；未配置/找不到就是空字节。

    `gain=None` 用配置里的值（`sfx_gain`）；显式传 `gain=1.0` 可拿到**未加增益**的原始波形
    —— 引擎就是这么缓存的：缓存原始波形、每次出声再乘当前增益，于是调音量**不用重启**。

    **禁静默降级**：配置了却读不出来要留痕（不然用户只会看到「没声音」，查不出原因）。
    """
    cfg = tts_cfg or {}
    gain = gain_of(cfg) if gain is None else float(gain)
    out: list[bytes] = []
    for key in (OPEN_KEY, CLOSE_KEY):
        want = str(cfg.get(key) or "").strip()
        if not want:
            out.append(b"")
            continue
        path = _resolve(want, bases)
        if path is None:
            log.warning("[sfx] 配置了 %s=%r 但找不到文件（找过 %s）→ 这一侧不播",
                        key, want, "、".join(str(b) for b in bases))
            print(f"[sfx] ⚠️ {key}={want!r} 找不到文件 → 这一侧不播", flush=True)
            out.append(b"")
            continue
        try:
            out.append(resample_24k_mono_to_48k_stereo(
                apply_gain(read_wav_as_24k_mono(path), gain)))
        except Exception as exc:  # noqa: BLE001
            log.warning("[sfx] %s 读取失败：%s", path, exc)
            print(f"[sfx] ⚠️ {path.name} 读取失败：{type(exc).__name__}: {exc} → 这一侧不播", flush=True)
            out.append(b"")
    return out[0], out[1]
