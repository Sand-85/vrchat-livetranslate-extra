"""打字输入的**译音**：把译文合成成音频，喂给虚拟声卡那条腿。

为什么打字要单独一步 TTS：
    打字走的是**文本翻译**接口（实时模型不接受文本入口，见 `textin.py` 模块注释），
    它只回文本、不回音频 —— 不加这一步，打字内容就永远进不了虚拟声卡、对面听不到。
    语音那条腿的音频是实时模型直出的，这里补的是同格式的替代品。

实测（2026-09）：
- 模型 `qwen3-tts-flash`（也可用 `qwen3-tts-instruct-flash`），返回 `output.audio`
  同时带 `data`(base64) 与 `url`；**优先用 data**，省一次下载且不受 URL 过期影响。
- 音频是 24kHz 单声道 WAV，用 `miniaudio` 解成 **24k 单声道 s16le PCM** ——
  与实时模型译音**同格式**，所以下游可以直接复用 `resample_24k_mono_to_48k_stereo`
  和 `VirtualMic`，不需要任何新管线。
- 音色 `Cherry` 中/英/日都能读（实测），故默认一个音色就够；要换按 config 改。

两代后端（按模型名自动分派，`cosyvoice*` 走后者）：
- **qwen3-tts-***：端点 `/services/aigc/multimodal-generation/generation`，音色是内置名或
  声音设计音色 id。**不可复现** —— 同一句连合三次长度/md5 都不同（`qwen3-tts-flash` 音高 ±3%、
  声音设计音色 `qwen3-tts-vd-*` 音高甚至 ±12%，后者就是用户报「试听和实装出入过大」的根因）。
- **cosyvoice-***：端点 `/services/audio/tts/SpeechSynthesizer`，`parameters.seed` **真生效**：
  同 seed 逐字节一致、换 seed 结果不同 ⇒ 可缓存、可做回归。实测「创建时预览音 vs 事后合成」
  基频只差 2.2%（v3-flash），是「音色稳定 + 可复现」的选择。

延迟（实测 2026-09，同一句 20~30 字）：
- 非流式 `synthesize()`：整段等 1.65~1.89s 才返回 —— 这就是打字腿延迟的大头。
- 流式 `synthesize_stream()`：**首包 0.36~0.42s**、整段仍 1.6~1.7s。下游虚拟声卡是抖动缓冲
  （300ms 起播），拿到前几个分片就能开口，所以引擎默认走流式。
"""
from __future__ import annotations

import base64
import json
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

ENDPOINT = "https://maas.qianwenaiapi.com/api/v1/services/aigc/multimodal-generation/generation"
# cosyvoice 系走另一个端点（响应形态相同：output.audio.data / url）；同一网关，只是路径不同
ENDPOINT_COSYVOICE = "https://maas.qianwenaiapi.com/api/v1/services/audio/tts/SpeechSynthesizer"
DEFAULT_MODEL = "qwen3-tts-flash"
DEFAULT_VOICE = "Cherry"
DEFAULT_TIMEOUT_S = 30.0
# 仅 cosyvoice 生效：固定 seed 让同一句两次合成**逐字节一致**（可复现、可缓存）
DEFAULT_SEED = 1234
SAMPLE_RATE = 24000

# 说话译音用的是 Qwen-Omni 系列音色（Tina/Cindy/…），qwen3-tts-flash **不认这些 id**
# （会 InvalidParameter）。要试听它们只能走非实时 Qwen-Omni —— 同一个 compatible-mode
# 端点（textin.py 已在用、同一把 key、无需 workspace），但音频输出**强制流式**。
OMNI_ENDPOINT = "https://maas.qianwenaiapi.com/compatible-mode/v1/chat/completions"
DEFAULT_OMNI_MODEL = "qwen3.5-omni-flash"
DEFAULT_OMNI_VOICE = "Tina"

# 目标语言码 → DashScope 的 language_type（可选参数；拿不准就不传，服务端自己判）
LANG_NAMES = {
    "zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean",
    "fr": "French", "de": "German", "es": "Spanish", "ru": "Russian",
    "it": "Italian", "pt": "Portuguese",
}

_opener = None


def _get_opener():
    """直连 opener（禁用系统代理）——与 textin 同一取舍：国内端点走代理是纯负担。"""
    global _opener
    if _opener is None:
        _opener = build_opener(ProxyHandler({}))
    return _opener


class TtsError(RuntimeError):
    """合成失败（缺 key / 网络 / 参数 / 空音频）。消息给用户看，带原因不带堆栈。"""


def _decode_to_24k_mono(raw: bytes) -> bytes:
    """任意容器（WAV/MP3/…）→ 24kHz 单声道 s16le PCM。"""
    try:
        import miniaudio

        dec = miniaudio.decode(raw, output_format=miniaudio.SampleFormat.SIGNED16,
                               nchannels=1, sample_rate=24000)
        return bytes(dec.samples)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"音频解码失败：{type(exc).__name__}: {exc}") from exc


def _fetch(url: str, timeout: float) -> bytes:
    req = Request(url, headers={"Accept": "*/*"})
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            return r.read()
    except (HTTPError, URLError) as exc:
        raise TtsError(f"下载音频失败：{exc}") from exc


def _validate(text: str, api_key: str) -> str:
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")
    return text


def _build_request(text: str, *, voice: str, model: str, api_key: str, language: str | None,
                   seed: int | None, instruction: str | None = None,
                   speech_rate: float | None = None,
                   streaming: bool = False) -> Request:
    """构造合成请求（两代后端 + 是否流式）。流式靠 SSE 头拿分片。

    `speech_rate` 只对 qwen3-tts 系生效（实测 0.8 → 时长 +21%、1.2 → −19%，单调可控）；
    cosyvoice 端点不接受该参数，故只在 qwen 路径下发。
    """
    model = model or DEFAULT_MODEL
    voice = voice or DEFAULT_VOICE
    if str(model).lower().startswith("cosyvoice"):
        # cosyvoice 系：另一个端点、没有 language_type、多了 seed/instruction
        endpoint = ENDPOINT_COSYVOICE
        payload: dict = {"model": model,
                         "input": {"text": text, "voice": voice,
                                   "format": "wav", "sample_rate": SAMPLE_RATE}}
        if instruction:
            payload["input"]["instruction"] = instruction
        if seed is not None:
            payload["parameters"] = {"seed": int(seed)}
    else:
        endpoint = ENDPOINT
        payload = {"model": model, "input": {"text": text, "voice": voice}}
        lang_name = LANG_NAMES.get((language or "").lower())
        if lang_name:
            payload["input"]["language_type"] = lang_name
        if speech_rate is not None:
            payload.setdefault("parameters", {})["speech_rate"] = float(speech_rate)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if streaming:                                   # 实测：带这个头就是 text/event-stream 分片
        headers["Accept"] = "text/event-stream"
        headers["X-DashScope-SSE"] = "enable"
    return Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   headers=headers, method="POST")


def _raise_if_error(obj: dict) -> None:
    """服务端错误有两种外壳：`{"error":{...}}` 与带 code/message 的扁平形态。"""
    if isinstance(obj.get("error"), dict):
        raise TtsError(str((obj["error"] or {}).get("message") or obj["error"])[:300])
    if obj.get("code") and obj.get("message"):
        raise TtsError(str(obj["message"])[:300])


def _extract_audio(obj: dict, timeout: float) -> bytes | None:
    """取音频原始字节：优先 base64 `data`，退回 `url`（url 有有效期，能不用就不用）。"""
    audio = ((obj.get("output") or {}).get("audio") or {})
    if audio.get("data"):
        try:
            return base64.b64decode(audio["data"])
        except Exception as exc:  # noqa: BLE001
            raise TtsError(f"base64 音频解析失败：{exc}") from exc
    if audio.get("url"):
        return _fetch(str(audio["url"]), timeout)
    return None


def _chunk_to_pcm(chunk: bytes) -> bytes:
    """SSE 分片 → 24k 单声道 s16le。文档说是裸 PCM；万一是容器（RIFF）就解码一次。"""
    if chunk[:4] == b"RIFF":
        return _decode_to_24k_mono(chunk)
    return chunk[: len(chunk) - (len(chunk) % 2)]


def _http_error_detail(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", "replace")[:300]
    except Exception:  # noqa: BLE001
        return ""


def synthesize(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    seed: int | None = DEFAULT_SEED,
    instruction: str | None = None,
    speech_rate: float | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """整段合成：等到全部音频生成完才返回（首字延迟 ≈ 整段耗时，1.6~1.9s）。

    同步函数（调用方丢线程池里跑）；`language` 是目标语言码（zh/en/ja…），
    会映射成 service 的 `language_type`，拿不准就不传。
    `seed`/`instruction` 只对 `cosyvoice*` 生效（`seed=None` = 不传，即随机）。
    """
    text = _validate(text, api_key)
    req = _build_request(text, voice=voice, model=model, api_key=api_key, language=language,
                         seed=seed, instruction=instruction, speech_rate=speech_rate)
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except HTTPError as exc:
        raise TtsError(f"HTTP {exc.code}：{_http_error_detail(exc) or exc.reason}") from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    try:
        resp = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"响应解析失败：{exc}") from exc
    _raise_if_error(resp)
    raw = _extract_audio(resp, timeout)
    if not raw:
        raise TtsError("服务端没返回音频")
    return _decode_to_24k_mono(raw)


def synthesize_stream(
    text: str,
    *,
    voice: str = DEFAULT_VOICE,
    model: str = DEFAULT_MODEL,
    api_key: str = "",
    language: str | None = None,
    seed: int | None = DEFAULT_SEED,
    instruction: str | None = None,
    speech_rate: float | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Iterator[bytes]:
    """流式合成（SSE）：边生成边 yield 24k 单声道 s16le 的 PCM 分片。

    为什么值得：首包 0.36~0.42s vs 整段 1.6~1.9s。虚拟声卡是抖动缓冲，拿到前几个
    分片就能开口，所以引擎默认走这条路 —— 打字后「开口」从 ~1.9s 压到 ~0.4s。

    退回策略（调用方不必写两套逻辑）：
    - 服务端没给 event-stream（或一个分片都没拿到）→ 退回整段 `synthesize()`，yield 一整块；
    - 中途失败 → **保留已经 yield 的分片**（宁可少说半句，也不要整句消失）。
    """
    text = _validate(text, api_key)
    req = _build_request(text, voice=voice, model=model, api_key=api_key, language=language,
                         seed=seed, instruction=instruction, speech_rate=speech_rate,
                         streaming=True)
    got = 0
    acc = bytearray()          # 已发出的音频（用于识别末尾的"整段汇总"分片）
    try:
        with _get_opener().open(req, timeout=timeout) as resp:
            ctype = str(resp.headers.get("Content-Type", "") or "")
            if "event-stream" not in ctype:              # 服务端降级成了整段响应
                body = resp.read().decode("utf-8", "replace")
                try:
                    obj = json.loads(body)
                except Exception as exc:  # noqa: BLE001
                    raise TtsError(f"响应解析失败：{exc}") from exc
                _raise_if_error(obj)
                raw = _extract_audio(obj, timeout)
                if raw:
                    got += 1
                    yield _decode_to_24k_mono(raw)
            else:
                for line in resp:
                    line = line.strip()
                    if not line.startswith(b"data:"):
                        continue                              # 心跳 / 空行 / event: 行
                    data = line[5:].strip()
                    if not data or data == b"[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except Exception:                          # noqa: BLE001
                        continue                               # 非 JSON 的分片直接跳过
                    _raise_if_error(obj)
                    raw = _extract_audio(obj, timeout)
                    if not raw:
                        continue
                    pcm = _chunk_to_pcm(raw)
                    if not pcm:
                        continue
                    # ⚠️ 服务端在流末尾还会补发一片「整段汇总」（实测与前面所有分片**逐字节相同**）：
                    # 直接吃掉它，否则虚拟声卡会把整句念两遍。真分片不可能与已累计音频等长同内容。
                    if acc and len(pcm) == len(acc) and pcm == bytes(acc):
                        continue
                    got += 1
                    acc += pcm
                    yield pcm
    except TtsError:
        if got:
            return                                             # 已唱出去的部分不撤
        raise
    except HTTPError as exc:
        if got:
            return
        # 可能只是服务端不认流式（头被拒）→ 先试整段兜底，兜底也失败再报这个错
        err = TtsError(f"HTTP {exc.code}：{_http_error_detail(exc) or exc.reason}")
        try:
            yield synthesize(text, voice=voice, model=model, api_key=api_key,
                             language=language, seed=seed, instruction=instruction,
                             speech_rate=speech_rate, timeout=timeout)
        except TtsError:
            raise err from exc
        return
    except URLError as exc:
        if got:
            return
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        if got:
            return
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    if not got:                                                # 流式没给东西 → 整段兜底
        yield synthesize(text, voice=voice, model=model, api_key=api_key, language=language,
                         seed=seed, instruction=instruction, speech_rate=speech_rate,
                         timeout=timeout)


def synthesize_omni(
    text: str,
    *,
    voice: str = DEFAULT_OMNI_VOICE,
    model: str = DEFAULT_OMNI_MODEL,
    api_key: str = "",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """用**非实时 Qwen-Omni** 合成一段文本 → 24kHz 单声道 s16le PCM（与 `synthesize` 同格式）。

    为何单独一条路：说话译音的音色（Tina/Cindy/Liora Mira…）属于 Qwen-Omni 系列，
    `qwen3-tts-flash` 不支持（跨模型混用会 InvalidParameter），要试听只能走 Omni。

    实现要点（均有官方文档依据）：
    - Omni 是对话模型，音频输出**必须** `stream=True`；自己解 SSE，把分片的
      `choices[0].delta.audio.data`（base64）**拼接后一次解码**（官方示例就是这么干的）。
    - 它不是逐字 TTS：下一条指令让它朗读样例句，个别措辞可能略有出入 —— 试听音色足够。
    - 回的是 24k 单声道音频（可能裸 PCM、也可能带 WAV 头），统一过一遍解码器，
      解不动就当裸 s16le PCM 直接用（本就是目标格式）。
    """
    text = (text or "").strip()
    if not text:
        raise TtsError("内容为空")
    if not (api_key or "").strip():
        raise TtsError("还没配置 API key（见界面右上角「设置」）")

    payload = {
        "model": model or DEFAULT_OMNI_MODEL,
        "messages": [{"role": "user",
                      "content": f"请逐字朗读下面引号内的这句话，只朗读、不要回答或补充任何内容：「{text}」"}],
        "modalities": ["text", "audio"],
        "audio": {"voice": voice or DEFAULT_OMNI_VOICE, "format": "wav"},
        "stream": True,                             # ⚠️ Omni 音频输出必须流式
        "stream_options": {"include_usage": True},
    }
    req = Request(OMNI_ENDPOINT, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json",
                           "Accept": "text/event-stream"}, method="POST")
    b64: list[str] = []
    try:
        with _get_opener().open(req, timeout=timeout) as r:
            for raw_line in r:                        # 逐行读 SSE
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:  # noqa: BLE001 — 心跳/不完整帧直接略过
                    continue
                if isinstance(obj.get("error"), dict):
                    err = obj["error"]
                    raise TtsError(str(err.get("message") or err)[:300])
                choices = obj.get("choices") or []
                if not choices:
                    continue
                aud = (choices[0].get("delta") or {}).get("audio") or {}
                if aud.get("data"):
                    b64.append(aud["data"])
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise TtsError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except URLError as exc:
        raise TtsError(f"网络不可达：{exc.reason}") from exc
    except TtsError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"{type(exc).__name__}: {exc}") from exc

    if not b64:
        raise TtsError("服务端没返回音频")
    try:
        raw = base64.b64decode("".join(b64))
    except Exception as exc:  # noqa: BLE001
        raise TtsError(f"base64 音频解析失败：{exc}") from exc
    try:
        return _decode_to_24k_mono(raw)
    except TtsError:
        return raw                                  # 已是裸 24k 单声道 s16le PCM，直接用
