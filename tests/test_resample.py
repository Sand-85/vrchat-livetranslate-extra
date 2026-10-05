"""to_16k_mono 重采样回归测试。

背景（真机事故，2026-09）：44100Hz 声卡的用户，loopback（听别人说话）那条腿
连续 900 秒「收到译文本 0 条」后被服务端超时掐断，重连后依旧 —— 因为当时
非整数倍采样率走的是「先按 down 抽稀、再当成相邻样点插值」的错误实现，
等于把语音压成约 100Hz 的包络。实测与原始语音相关性 0.005、高频能量只剩 0.5%。

本测试拿「原始 16kHz 信号」当参照物（不是拿实现互相比），所以它验的是
「重采样后还是不是原来那段话」，而不是「有没有跑通」。
"""
import pathlib
import sys

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from vlt.engine import to_16k_mono  # noqa: E402

FAILED = []


def check(cond, msg):
    if cond:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        FAILED.append(msg)


def corr(x, y):
    m = min(len(x), len(y))
    x, y = x[:m].astype(np.float64), y[:m].astype(np.float64)
    if m < 8 or x.std() < 1e-9 or y.std() < 1e-9:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def dominant_hz(x, sr=16000):
    if len(x) < 64 or np.allclose(x, x[0]):
        return 0.0
    w = x - x.mean()
    sp = np.abs(np.fft.rfft(w * np.hanning(len(w))))
    return float(np.fft.rfftfreq(len(w), 1 / sr)[int(np.argmax(sp))])


def hi_energy_ratio(x, sr=16000, cut=2000.0):
    if len(x) < 64 or np.allclose(x, x[0]):
        return 0.0
    w = x - x.mean()
    sp = np.abs(np.fft.rfft(w * np.hanning(len(w))))
    f = np.fft.rfftfreq(len(w), 1 / sr)
    return float(sp[f > cut].sum() / (sp.sum() + 1e-9))


def upsample(origin16k: np.ndarray, rate: int) -> np.ndarray:
    """把 16kHz 波形搬到任意采样率，模拟「声卡上真实存在的信号」。（仅用于造测试输入）"""
    n_out = int(len(origin16k) * rate / 16000)
    pos = np.arange(n_out) * (16000.0 / rate)
    return np.interp(pos, np.arange(len(origin16k)), origin16k.astype(np.float64))


# 一段确定性的「语音状」信号：基频 + 谐波 + 宽带成分 + 缓变包络
rng = np.random.default_rng(20260928)
_t = np.arange(16000) / 16000.0                      # 1 秒
_env = 0.5 + 0.5 * np.sin(2 * np.pi * 2.3 * _t)      # 音节包络
_speech = (_env * (0.6 * np.sin(2 * np.pi * 220 * _t)
                   + 0.3 * np.sin(2 * np.pi * 660 * _t)
                   + 0.25 * np.sin(2 * np.pi * 2600 * _t))
           + 0.05 * rng.standard_normal(len(_t)))
_speech = (np.clip(_speech, -1, 1) * 20000).astype(np.int16)

print("重采样回归：真实语音状信号，经「声卡采样率 → to_16k_mono」后与原始 16kHz 比对")

for rate in (44100, 22050, 88200, 48000, 96000):
    dev = upsample(_speech, rate).astype(np.int16)
    out = np.frombuffer(to_16k_mono(dev.tobytes(), rate, 1), dtype=np.int16)
    want_len = int(len(dev) * 16000 / rate)
    c = corr(out, _speech)
    hi = hi_energy_ratio(out)
    print(f"  [{rate}Hz] 输出 {len(out)} 样点")
    check(abs(len(out) - want_len) <= 2, f"{rate}Hz 输出样点数正确（{len(out)} vs 期望 {want_len}）")
    # 阈值 0.95：实测 44100/22050/88200 修复后 ≥0.99，修复前 ≈0.005
    check(c >= 0.95, f"{rate}Hz 重采样后仍与原始语音高度一致（相关性 {c:.4f} ≥ 0.95）")
    check(hi >= 0.15, f"{rate}Hz 高频成分没被削掉（>2kHz 占比 {hi:.1%} ≥ 15%）")

print("正弦保真：任何采样率下 1000Hz 都必须还是 1000Hz")
for rate in (44100, 22050, 88200, 48000):
    t = np.arange(int(rate * 0.5)) / rate
    tone = (np.sin(2 * np.pi * 1000 * t) * 20000).astype(np.int16)
    out = np.frombuffer(to_16k_mono(tone.tobytes(), rate, 1), dtype=np.int16)
    f = dominant_hz(out)
    check(abs(f - 1000.0) <= 20.0, f"{rate}Hz 下 1000Hz 正弦主频保持（实测 {f:.1f}Hz）")

print("直通与整数倍路径不受影响")
raw = _speech.tobytes()
check(to_16k_mono(raw, 16000, 1) == raw, "16000Hz 直通：字节级完全一致")
for rate in (32000, 48000, 96000):
    dev = upsample(_speech, rate).astype(np.int16)
    out = np.frombuffer(to_16k_mono(dev.tobytes(), rate, 1), dtype=np.int16)
    c = corr(out, _speech)
    check(c >= 0.95, f"{rate}Hz 整数倍抽取正常（相关性 {c:.4f}）")

print("多声道与边界输入")
stereo = upsample(_speech, 44100).astype(np.int16)
stereo = np.repeat(stereo, 2)                         # L/R 相同 → 下混后应与单声道等价
out = np.frombuffer(to_16k_mono(stereo.tobytes(), 44100, 2), dtype=np.int16)
mono = np.frombuffer(to_16k_mono(upsample(_speech, 44100).astype(np.int16).tobytes(), 44100, 1),
                     dtype=np.int16)
check(abs(len(out) - len(mono)) <= 2, f"44100Hz 立体声下混后样点数正确（{len(out)} vs {len(mono)}）")
check(corr(out, mono) >= 0.99, f"44100Hz 立体声下混与单声道一致（相关性 {corr(out, mono):.4f}）")

try:
    check(to_16k_mono(b"", 44100, 1) == b"", "空输入返回空，不抛异常")
    check(to_16k_mono(np.array([5], dtype=np.int16).tobytes(), 44100, 1) == b"", "单样点返回空，不抛异常")
    tiny = (np.sin(np.arange(3) * 0.1) * 1000).astype(np.int16)
    to_16k_mono(tiny.tobytes(), 44100, 1)
    check(True, "极短输入不抛异常")
    check(to_16k_mono(np.full(44100, 32000, dtype=np.int16).tobytes(), 44100, 1) != b"", "满量程输入不炸")
except Exception as e:  # noqa: BLE001
    check(False, f"边界输入不应抛异常，实际：{type(e).__name__}: {e}")

print()
if FAILED:
    print(f"FAILED：{len(FAILED)} 项未通过")
    for m in FAILED:
        print(f"  - {m}")
    sys.exit(1)
print("ALL PASSED（重采样）")
