"""声音设计（qwen-voice-design）的本地客户端：创建 / 列表 / 删除 + 配方库 + 试听缓存。

## 它是什么、为什么这样切

用户可以在应用内**用一句话描述**炼一个属于自己的音色（官方叫「声音设计」），随即试听、
满意就存成当前打字译音的音色。要点全在这几条事实里（2026-10-02 按官方文档复核）：

- **音色挂在「创建它的那个账号」下**：配额按账号算，`action=list` 只返回本账号的音色，
  平台没有音色共享/转移。所以这里有且只有一个正确的做法 —— 用**用户自己的 key** 建，
  绝不代持别人的 key（那会把「谁付费、谁拥有」搞乱）。
- **创建是要花钱的**：Qwen-TTS 声音设计 0.2 元/个（创建失败不计费）；
  北京地域开通后 90 天内前 10 次免费。**钱是用户自己的**，所以：
  界面必须标价并在首次点击时确认（见 GUI 侧），本模块只负责「数」不说「钱」。
- **同一句描述生成的结果不稳定**（官方 FAQ：「建议多次生成后择优使用」）→ 名字相同的
  音色**不保证**是同一个听感，所以这里提供 `find_by_name()` 做**幂等复用**：
  先查后建，避免用户每点一次就多花一次钱、列表里堆一串同名音色。
- **描述只支持中文和英文**（生成的音色能读多语言）；`voice_prompt` 上限 2048 字符；
  `target_model` 创建与合成**必须一致**，否则合成 `InvalidParameter`。
- **单条音色 1 年内未用于任何合成会被系统删除** → 列表里查不到时给出可解释的提示。

## 依赖纪律

只依赖标准库 + `vlt.endpoints`（地址派生）与 `vlt.tts`（同一份「国内端点直连、不走系统代理」
策略）。**不 import tkinter**：GUI 侧只把它当数据源，离线测试可以整体打桩。
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import endpoints
from . import tts                       # 试听要复用它的合成口径（同一份解码/端点纪律）
from .tts import _get_opener          # 与 tts.py 共用「直连、绕开系统代理」的同一份策略

# 创建与合成必须用同一个模型；换模型就得重新炼（官方硬约束）。
DEFAULT_TARGET_MODEL = "qwen3-tts-vd-2026-01-26"        # 声音设计（文字描述）
CLONE_TARGET_MODEL = "qwen3-tts-vc-2026-01-22"          # 声音复刻（音频素材）
DESIGN_MODEL = "qwen-voice-design"                      # 创建/列表/删除用哪个 model 字段
CLONE_MODEL = "qwen-voice-enrollment"
# 两族是**分开的**：列表按 `model` 隔离（用 design 查是看不到复刻音色的，实测过）。
FAMILIES: dict[str, str] = {"design": DESIGN_MODEL, "clone": CLONE_MODEL}
# 自定义音色的接口路径（与 tts 的多模态路径同一个网关，只是路径不同）
CUSTOMIZATION_PATH = "/api/v1/services/audio/tts/customization"

# 复刻素材的硬要求（官方）：10~20s、≥24kHz、单人纯朗读、无背景音/音乐/他人声。
# 我们**本地先查能查的**（时长/采样率/体积），查不出的（有没有背景音、是不是单人）只能提示用户。
MIN_CLONE_SECONDS = 10.0
MAX_CLONE_SECONDS = 20.0
MIN_CLONE_RATE = 24000
MAX_CLONE_BYTES = 10 * 1024 * 1024
# 扩展名 → MIME（data URL 要用；服务端按 MIME 选解码器）
CLONE_MIMES: dict[str, str] = {".wav": "audio/wav", ".mp3": "audio/mpeg",
                               ".m4a": "audio/mp4", ".aac": "audio/aac",
                               ".ogg": "audio/ogg", ".flac": "audio/flac",
                               ".opus": "audio/opus", ".wma": "audio/x-ms-wma"}
CLONE_FILETYPES = (("音频文件", "*.wav *.mp3 *.m4a *.aac *.ogg *.flac *.opus *.wma"),
                   ("全部文件", "*.*"))
CLONE_MODEL_PREFIX = "qwen-tts-vc-"

DEFAULT_TIMEOUT_S = 30.0
NAME_MAX_LEN = 16          # 官方：preferred_name 只允许字母数字下划线，长度 ≤16
PROMPT_MAX_LEN = 2048      # 官方：Qwen-TTS 的 voice_prompt 上限（CosyVoice 是 500）

# 试听用的固定测试文本（用户指定）。三处共用同一句，保证「听的都是同一句」：
#   ① 创建音色时作为 `preview_text`（接口回一份预览音频）；
#   ② 账号里已有的音色没缓存时，现场合成一句来试听（按字符计费，极便宜）；
#   ③ 「过往生成」列表里的回放（缓存落盘后不再花钱）。
TEST_TEXT = "こんにちは、私はSANDです。今から声のテストです。"
# ⚠️ 用「声」不用「音色」：后者被模型念成「オネロ」（ASR 实证）；
#    SAND 用拉丁写法时不同音色会念成 サンド/センブ/サンデー/センド —— 要统一就写片假名 サンド。


class VoiceLabError(RuntimeError):
    """创建/查询音色失败。消息给用户看，带原因不带堆栈（与 tts.TtsError 同口径）。"""


@dataclass(frozen=True)
class VoiceInfo:
    """本账号里的一条自定义音色（`action=list` 的条目）。

    `kind` = 音色族：`"design"`（文字描述炼的）/ `"clone"`（音频素材复刻的）——
    两族**合成时必须用各自的 target_model**，所以这个字段不只是显示用。
    `status` = 服务端状态（复刻有审核，可能是 `UNDEPLOYED`）。
    """

    voice: str
    name: str = ""
    prompt: str = ""
    target_model: str = ""
    created: str = ""
    kind: str = "design"
    status: str = ""


@dataclass
class VoiceCreation:
    """创建（或复用）结果。

    `reused=True` = 没建新音色，命中了账号里同名的旧音色（**用户没花钱**）。
    `preview_wav` = 接口返回的预览音频（可能为空：老音色复用、或服务端没给）。
    """

    voice: str
    name: str
    preview_wav: bytes = b""
    reused: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------- 纯函数（离线可测）


def normalize_name(raw: str) -> str:
    """把用户输入的**音色名**规范成服务端接受的形态（字母数字下划线，≤16）。

    为什么不直接报错拦下：名字只影响用户自己列表里的辨识度，顺手规范掉比教育用户划算。
    规则（官方）：只允许字母、数字、下划线；长度 ≤16。中文名会被换成等长的下划线占位，
    空名回落 `my_voice` —— 让「随手起个中文名」也能生成成功。
    """
    out: list[str] = []
    for ch in str(raw or "").strip():
        if ch.isascii() and (ch.isalnum() or ch == "_"):
            out.append(ch)
        elif out and out[-1] != "_":
            out.append("_")           # 连续非法字符只留一个下划线
    name = "".join(out).strip("_")
    return (name[:NAME_MAX_LEN] or "my_voice")


def normalize_prompt(raw: str) -> str:
    """规范提示词：去首尾空白、压掉多余换行（服务端不吃换行排版）。空 → 抛错。"""
    text = " ".join(str(raw or "").split())
    if not text:
        raise VoiceLabError("请先写一句音色描述（用声学特征描述，例如「年轻女性，音色干净偏薄，语速平稳」）")
    return text


def prompt_looks_like_imitation(text: str) -> bool:
    """粗判「描述里要求模仿某个具体人物」——官方明确不支持且可能涉版权。

    只做**提示**不做拦截（真拦会误伤「像播报员」这类职业比喻，那是允许的）：
    GUI 侧据此给一行提醒，用户自己决定继续与否。
    """
    low = str(text or "").lower()
    markers = ("声优", "配音演员", "cv", "本人", "原声", "模仿", "扮演", "克隆",
               "名人", "明星", "像某", "复刻某")
    return any(m in low for m in markers)


def find_by_name(voices: list[VoiceInfo], name: str) -> VoiceInfo | None:
    """在账号已有音色里按**规范后的名字**找同一条（幂等复用的判据）。

    取**最新**一条（`voice` 里带时间戳，最大的就是最近建的）：用户反复点「生成」时
    复用最近那个，而不是最老的那个。
    """
    want = normalize_name(name)
    hits = [v for v in voices if normalize_name(v.name or _name_of(v.voice)) == want]
    if not hits:
        return None
    return max(hits, key=lambda v: v.voice)


def _name_of(voice: str) -> str:
    """从 voice_id 反推名字：`qwen-tts-vd-<名字>-voice-<时间戳>-<hex>`。

    官方 id 里带 preferred_name，所以老音色即便列表不返回名字也能对上（本仓库实测）。
    """
    parts = str(voice or "").split("-")
    if len(parts) >= 5 and parts[-3] == "voice":
        return "-".join(parts[3:-3])
    return ""


def describe(voices: list[VoiceInfo], label_of=None, tag_of=None) -> list[str]:
    """给界面用的展示行（名字 + id 后 6 位 + 创建日期 + 可选族标签）。

    `label_of(voice_id, fallback)` 可传界面侧的显示名映射（`vlt.voices.display_name`
    或本地登记表）：复刻音色就能显示本地化名字（中文「国民护卫队」/ 其它语言「MetroPolice」）
    而不是一长串 id。
    `tag_of(kind)` 传**界面侧本地化**的族标签（例如复刻显示「复刻」）—— 这里不写死中文，
    否则非中文界面的列表里会冒出汉字（i18n 用例盯着这个）。
    """
    rows: list[str] = []
    for v in voices:
        tail = v.voice[-6:] if len(v.voice) > 6 else v.voice
        when = (v.created or "")[:10]
        label = v.name or _name_of(v.voice) or "(未命名)"
        if label_of is not None:
            label = label_of(v.voice, label) or label
        tag = (tag_of(v.kind) if tag_of is not None else "") or ""
        rows.append(f"{tag}{label}  ·{tail}" + (f"  ·{when}" if when else ""))
    return rows


# ---------------------------------------------------------------- 复刻素材（音频）


@dataclass(frozen=True)
class AudioProbe:
    """用户挑的那份复刻素材**本地能查出来的**信息（查不出的一律不猜）。"""

    path: str
    seconds: float = 0.0
    sample_rate: int = 0
    channels: int = 0
    fmt: str = ""
    size: int = 0
    error: str = ""


def probe_audio(path) -> AudioProbe:                            # noqa: ANN001
    """读素材的时长/采样率/声道（只读元信息，不解码整段）。失败把原因写进 `error`。"""
    import miniaudio                     # 与 tts.py 同一个解码库；只读元信息很轻

    p = Path(str(path or ""))
    if not str(path or "").strip():
        return AudioProbe(path="", error="还没选文件")
    if not p.is_file():
        return AudioProbe(path=str(p), error="文件不存在")
    size = p.stat().st_size
    if size <= 0:
        return AudioProbe(path=str(p), error="文件是空的")
    try:
        info = miniaudio.get_file_info(str(p))
    except Exception as exc:                                    # noqa: BLE001
        return AudioProbe(path=str(p), size=size,
                          error=f"读不出音频信息（{type(exc).__name__}: {exc}）")
    return AudioProbe(path=str(p), seconds=float(info.duration),
                      sample_rate=int(info.sample_rate), channels=int(info.nchannels),
                      fmt=str(getattr(info, "file_format", "")), size=size)


def audio_problems(probe: AudioProbe) -> list[str]:
    """素材的**硬性**问题 —— 能本地拦就别让用户白花 0.01 元再等审核拒。"""
    if probe.error:
        return [probe.error]
    out: list[str] = []
    lo, hi = int(MIN_CLONE_SECONDS), int(MAX_CLONE_SECONDS)
    if probe.seconds < MIN_CLONE_SECONDS:
        out.append(f"太短（{probe.seconds:.1f}s）—— 官方要 {lo}~{hi} 秒")
    elif probe.seconds > MAX_CLONE_SECONDS:
        out.append(f"太长（{probe.seconds:.1f}s）—— 官方要 {lo}~{hi} 秒")
    if probe.sample_rate and probe.sample_rate < MIN_CLONE_RATE:
        out.append(f"采样率偏低（{probe.sample_rate}Hz < {MIN_CLONE_RATE}Hz）")
    if probe.size > MAX_CLONE_BYTES:
        out.append(f"文件太大（{probe.size / 1048576:.1f}MB > {MAX_CLONE_BYTES // 1048576}MB）")
    return out


def audio_warnings(probe: AudioProbe) -> list[str]:
    """能过、但可能影响听感或过审率的（**只提示，不拦** —— 拦了会误伤正常素材）。"""
    out: list[str] = []
    if probe.channels > 1:
        out.append("双声道（官方建议单声道，服务端一般会自行下混）")
    return out


def mime_of(path) -> str:                                       # noqa: ANN001
    """扩展名 → MIME（服务端按 MIME 选解码器）。未知扩展名一律按 wav 报。"""
    return CLONE_MIMES.get(Path(str(path)).suffix.lower(), "audio/wav")


def audio_data_url(path) -> str:                                # noqa: ANN001
    """把素材读成 `data:audio/...;base64,`。

    ⚠️ **必须带 `data:` 前缀**：真机实测，裸 base64 会被服务端当成 URL 直接回 `InvalidURL`。
    """
    p = Path(str(path or ""))
    raw = p.read_bytes()
    if not raw:
        raise VoiceLabError("素材文件是空的")
    if len(raw) > MAX_CLONE_BYTES:
        raise VoiceLabError(f"素材文件太大（{len(raw) / 1048576:.1f}MB）")
    return f"data:{mime_of(p)};base64,{base64.b64encode(raw).decode('ascii')}"


def customization_url(base_url: str, workspace_id: str = "") -> str:
    """自定义音色（声音设计）端点：**与另外两条 HTTP 端点同一个 host 派生口径**。

    ⚠️ 上游 v0.8.0 起 `endpoints.host_of()` **只收 base_url**（「地域 / 业务空间」两个输入框
    已被上游删掉，workspace 不再是地址的一部分）—— 这里也跟着只传 base_url。
    保留 `workspace_id` 形参只为不动调用方（它现在被忽略）。
    """
    try:
        host = endpoints.host_of(base_url)
    except Exception:  # noqa: BLE001 — 解析不出 host（空串/没 scheme）→ 回落国内千问云默认 host，
        # 绝不拼出 `https:///api/v1/...` 这种残废地址（与 endpoints.host_of 的取舍一致）
        host = endpoints.host_of(endpoints.default_base_url(endpoints.PROVIDER_QIANWEN))
    return f"https://{host}{CUSTOMIZATION_PATH}"


# ---------------------------------------------------------------- 网络层


def _post(payload: dict, *, api_key: str, url: str, timeout: float,
          opener=None) -> dict:                                    # noqa: ANN001
    """POST JSON 并返回解析后的 dict；所有失败都翻成**带原因的** VoiceLabError。

    `opener` 只为离线测试注入（生产走 `tts._get_opener()` 的直连策略）。
    错误消息里**绝不回显 api_key**（本仓库纪律），只带 HTTP 码与服务端 message。
    """
    from urllib.request import Request

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(url, data=body, method="POST",
                  headers={"Authorization": f"Bearer {api_key}",
                           "Content-Type": "application/json"})
    op = opener or _get_opener()
    try:
        with op.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                                            # noqa: BLE001
            detail = ""
        raise VoiceLabError(f"HTTP {exc.code}：{detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise VoiceLabError(f"网络不可达：{exc.reason}") from exc
    except Exception as exc:                                         # noqa: BLE001
        raise VoiceLabError(f"{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(raw)
    except Exception as exc:                                         # noqa: BLE001
        raise VoiceLabError(f"响应解析失败：{exc}") from exc


def _check_error(obj: dict) -> None:
    """服务端错误有两种外壳（与 tts.py 同口径）：`{"error":{...}}` 与扁平 code/message。"""
    if isinstance(obj.get("error"), dict):
        raise VoiceLabError(str((obj["error"] or {}).get("message") or obj["error"])[:300])
    if obj.get("code") and obj.get("message"):
        raise VoiceLabError(str(obj["message"])[:300])


def _voice_info(row: dict, family: str) -> VoiceInfo | None:
    """把列表里的一行翻成 `VoiceInfo`（脏数据返回 None，绝不 KeyError）。"""
    if not isinstance(row, dict) or not row.get("voice"):
        return None
    return VoiceInfo(
        voice=str(row.get("voice")),
        name=str(row.get("preferred_name") or row.get("name") or ""),
        prompt=str(row.get("voice_prompt") or row.get("prompt") or ""),
        target_model=str(row.get("target_model") or ""),
        created=str(row.get("gmt_create") or row.get("create_time") or ""),
        kind=family,
        status=str(row.get("status") or ""))


def list_voices(*, api_key: str, base_url: str = "", workspace_id: str = "",
                family: str = "all", url: str = "",
                timeout: float = DEFAULT_TIMEOUT_S, opener=None) -> list[VoiceInfo]:  # noqa: ANN001
    """列出**本账号**的自定义音色。

    ⚠️ 两族**必须分开查**：`action=list` 按 `model` 隔离（用 `qwen-voice-design` 查不到
    复刻音色，反之亦然 —— 真机实测过）。默认 `family="all"` 两族都列，界面上才看得全。
    """
    if not (api_key or "").strip():
        raise VoiceLabError("还没配置 API key（见界面右上角「设置」）")
    fams = tuple(FAMILIES) if family == "all" else (family,)
    endpoint = url or customization_url(base_url, workspace_id)
    voices: list[VoiceInfo] = []
    for fam in fams:
        model = FAMILIES.get(fam)
        if model is None:
            raise VoiceLabError(f"未知音色族：{fam!r}")
        obj = _post({"model": model, "input": {"action": "list", "page_size": 50}},
                    api_key=api_key, url=endpoint, timeout=timeout, opener=opener)
        _check_error(obj)
        out = (obj.get("output") or {})
        rows = out.get("voice_list") or out.get("voices") or []
        for r in rows:
            info = _voice_info(r, fam)
            if info is not None:
                voices.append(info)
    return voices


def create_voice(name: str, prompt: str, *, api_key: str, base_url: str = "",
                 workspace_id: str = "", target_model: str = DEFAULT_TARGET_MODEL,
                 preview_text: str = TEST_TEXT, url: str = "",
                 timeout: float = DEFAULT_TIMEOUT_S, opener=None) -> VoiceCreation:  # noqa: ANN001
    """创建一个音色并取回预览音频（**要花钱**：0.2 元/个；失败不计费）。

    ⚠️ 费用是**用户自己的**，调用方（GUI）负责标价与确认；本函数只管把事做成。
    """
    if not (api_key or "").strip():
        raise VoiceLabError("还没配置 API key（见界面右上角「设置」）")
    nm = normalize_name(name)
    payload = {"model": DESIGN_MODEL,
               "input": {"action": "create", "target_model": target_model,
                         "preferred_name": nm,
                         "voice_prompt": normalize_prompt(prompt),
                         "preview_text": str(preview_text or TEST_TEXT)},
               "parameters": {"sample_rate": 24000, "response_format": "wav"}}
    obj = _post(payload, api_key=api_key, url=url or customization_url(base_url, workspace_id),
                timeout=timeout, opener=opener)
    _check_error(obj)
    out = (obj.get("output") or {})
    voice = str(out.get("voice") or "")
    if not voice:
        raise VoiceLabError("服务端没返回音色 id（创建可能未生效，费用按官方「创建失败不计费」处理）")
    wav = b""
    prev = out.get("preview_audio")
    if isinstance(prev, dict) and prev.get("data"):
        try:
            wav = base64.b64decode(prev["data"])
        except Exception:                                            # noqa: BLE001
            wav = b""
    return VoiceCreation(voice=voice, name=nm, preview_wav=wav, raw=obj)


def create_or_reuse(name: str, prompt: str, *, api_key: str, base_url: str = "",
                    workspace_id: str = "", target_model: str = DEFAULT_TARGET_MODEL,
                    url: str = "", timeout: float = DEFAULT_TIMEOUT_S,
                    opener=None) -> VoiceCreation:                          # noqa: ANN001
    """**先查后建**：账号里已有同名音色就直接复用（不花钱），否则才创建。

    为什么要这一步：名字相同不代表听感相同（官方：同描述结果不稳定），所以「同名」只当作
    「上次已经为这份配方付过钱」的证据，避免用户反复点击时反复计费。想重新炼就换个名字。
    查询失败（网络/权限）**不阻断**创建 —— 但会把失败原因留在异常链里（调用方决定是否继续）。
    """
    nm = normalize_name(name)
    try:
        existing = find_by_name(
            list_voices(api_key=api_key, base_url=base_url, workspace_id=workspace_id,
                        family="design", url=url, timeout=timeout, opener=opener),
            nm)
    except VoiceLabError:
        existing = None
    if existing is not None:
        return VoiceCreation(voice=existing.voice, name=nm, reused=True)
    return create_voice(nm, prompt, api_key=api_key, base_url=base_url,
                        workspace_id=workspace_id, target_model=target_model,
                        url=url, timeout=timeout, opener=opener)


def enroll_voice(name: str, audio_data_url: str, *, api_key: str, base_url: str = "",
                 workspace_id: str = "", target_model: str = CLONE_TARGET_MODEL,
                 url: str = "", timeout: float = DEFAULT_TIMEOUT_S,
                 opener=None) -> VoiceCreation:                          # noqa: ANN001
    """**声音复刻**：拿一段素材音频炼一条音色（`qwen-voice-enrollment`，**0.01 元/次**）。

    `audio_data_url` 必须是 **URL 或 `data:audio/...;base64,`** —— 真机实测：裸 base64 会被
    服务端当 URL 处理，直接回 `InvalidURL`。所以只接受这两种形态，别的一律当场拦下（省一次往返）。

    ⚠️ 复刻**有审核**（列表里的 `status` 可能是 `UNDEPLOYED` = 审核未通过），所以「创建成功」
    不等于「马上能用」；调用方要把 status 如实显示出来，别报成万事大吉。
    """
    if not (api_key or "").strip():
        raise VoiceLabError("还没配置 API key（见界面右上角「设置」）")
    nm = normalize_name(name)
    data = str(audio_data_url or "").strip()
    if not (data.startswith("data:") or data.startswith("http://") or data.startswith("https://")):
        raise VoiceLabError("复刻素材必须是 URL 或 data:audio/...;base64,（裸 base64 会被服务端当 URL 拒绝）")
    payload = {"model": CLONE_MODEL,
               "input": {"action": "create", "target_model": target_model,
                         "preferred_name": nm, "audio": {"data": data}}}
    obj = _post(payload, api_key=api_key, url=url or customization_url(base_url, workspace_id),
                timeout=timeout, opener=opener)
    _check_error(obj)
    out = (obj.get("output") or {})
    voice = str(out.get("voice") or "")
    if not voice:
        raise VoiceLabError("服务端没返回音色 id（复刻可能未生效）")
    return VoiceCreation(voice=voice, name=nm,
                         preview_wav=b"", raw=obj)


def enroll_or_reuse(name: str, audio_data_url: str, *, api_key: str, base_url: str = "",
                    workspace_id: str = "", target_model: str = CLONE_TARGET_MODEL,
                    url: str = "", timeout: float = DEFAULT_TIMEOUT_S,
                    opener=None) -> VoiceCreation:                           # noqa: ANN001
    """**先查后建**（复刻版）：账号里已有同名复刻音色就复用 —— 别为同一份素材反复付 0.01 元。"""
    nm = normalize_name(name)
    try:
        existing = find_by_name(
            list_voices(api_key=api_key, base_url=base_url, workspace_id=workspace_id,
                        family="clone", url=url, timeout=timeout, opener=opener),
            nm)
    except VoiceLabError:
        existing = None
    if existing is not None:
        return VoiceCreation(voice=existing.voice, name=nm, reused=True)
    return enroll_voice(nm, audio_data_url, api_key=api_key, base_url=base_url,
                        workspace_id=workspace_id, target_model=target_model,
                        url=url, timeout=timeout, opener=opener)


def delete_voice(voice: str, *, api_key: str, base_url: str = "", workspace_id: str = "",
                 family: str = "design", target_model: str = DEFAULT_TARGET_MODEL, url: str = "",
                 timeout: float = DEFAULT_TIMEOUT_S, opener=None) -> None:      # noqa: ANN001
    """删除一条自定义音色（清理废弃候选，保持列表干净）。

    ⚠️ 删除也要带上**正确的族**：`action=delete` 的 `model` 与列表/创建同源，
    拿 design 去删复刻音色是删不掉的（而且不报错——只是没删掉）。
    """
    if not (api_key or "").strip():
        raise VoiceLabError("还没配置 API key（见界面右上角「设置」）")
    if not (voice or "").strip():
        raise VoiceLabError("没有指定要删除的音色")
    model = FAMILIES.get(family)
    if model is None:
        raise VoiceLabError(f"未知音色族：{family!r}")
    obj = _post({"model": model,
                 "input": {"action": "delete", "voice": voice,
                           "target_model": target_model}},
                api_key=api_key, url=url or customization_url(base_url, workspace_id),
                timeout=timeout, opener=opener)
    _check_error(obj)


# ---------------------------------------------------------------- 试听缓存（本地存储）


def sample_pcm(voice: str, model: str = "", *, api_key: str, endpoint: str | None = None,
               timeout: float = 60.0, text: str = TEST_TEXT) -> bytes:
    """用测试文本合成一句，供**试听已有音色**（不创建、不再花 0.2 元）。

    为什么需要它：官方只在**创建**时回一份 `preview_audio`；账号里已有的音色（例如用户在
    控制台或用脚本建的）拿不到预览音频 → 想试听只能自己合成一句。按字符计费，
    `TEST_TEXT` 只有 17 字（约 0.002 元），相对「听不到就没法比」这个代价可以忽略。

    `model` 必须与创建该音色时的 `target_model` 一致（列表接口会带回来）——设计音色与内置
    音色不通用，传错必失败。
    """
    if not voice:
        raise VoiceLabError("没有指定音色")
    model = str(model or "").strip() or tts.DEFAULT_MODEL
    try:
        return tts.synthesize(text, voice=voice, model=model, api_key=api_key,
                              endpoint=endpoint, timeout=timeout)
    except tts.TtsError as exc:
        raise VoiceLabError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — 网络/解码异常也要给用户一句人话
        raise VoiceLabError(f"{type(exc).__name__}: {exc}") from exc


def preview_dir(app_dir: Path) -> Path:
    """试听音频的本地存放目录（`<APP_DIR>/voices`）——随配置一起留在用户机器上。

    为什么落盘：接口只在**创建的那一刻**返回 preview_audio；以后想再听同一条，
    要么重新合成（再花钱），要么读这份缓存。所以创建时立刻存下来。
    """
    return Path(app_dir) / "voices"


def save_preview(app_dir: Path, voice: str, wav: bytes) -> Path | None:
    """把预览音频存到本地缓存；空音频不落盘（返回 None，调用方据此提示「无试听」）。"""
    if not wav or not (voice or "").strip():
        return None
    d = preview_dir(app_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{_safe_stem(voice)}.wav"
    path.write_bytes(wav)
    return path


def load_preview(app_dir: Path, voice: str) -> bytes | None:
    """读回本地缓存的试听音频；没有就返回 None（调用方可提示「这条没有缓存，可重新生成」）。"""
    path = preview_dir(app_dir) / f"{_safe_stem(voice)}.wav"
    try:
        return path.read_bytes()
    except OSError:
        return None


def _safe_stem(voice: str) -> str:
    """把 voice_id 变成安全文件名（id 里本来只有字母数字和短横线，再兜一层）。"""
    keep = [c for c in str(voice) if c.isascii() and (c.isalnum() or c in "-_")]
    return "".join(keep)[:80] or "voice"


# ---------------------------------------------------------------- 克隆音色的显示名（本地登记）

# 为什么要这张本地登记表：复刻音色的 id 是一长串（`qwen-tts-vc-<名>-voice-<时间戳>-<后缀>`），
# 列表里看着不像人话。源码里那份 `vlt/voices.CLONED_VOICES` 是**手写**的（每加一条音色都要改代码 ✗），
# 这里改成**克隆完自动记下来**（写用户目录的 labels.json，与试听缓存同一个文件夹）。
# 两者一起用：本地登记优先，其次交给 `voices.display_name`（手写登记表仍然有效）。


def labels_path(app_dir: Path) -> Path:
    """显示名登记表的位置（与试听缓存同目录）。"""
    return preview_dir(app_dir) / "labels.json"


def load_labels(app_dir: Path) -> dict[str, str]:
    """读回「voice id → 显示名」；文件缺失/损坏一律当空表（绝不因此起不来）。"""
    try:
        obj = json.loads(labels_path(app_dir).read_text(encoding="utf-8"))
    except Exception:                                                # noqa: BLE001
        return {}
    if not isinstance(obj, dict):
        return {}
    return {str(k): str(v) for k, v in obj.items() if str(k).strip() and str(v).strip()}


def save_label(app_dir: Path, voice: str, label: str) -> Path | None:
    """记住「这条 id ↔ 这个名字」（下次列表里显示人话，而不是一串 id）。"""
    if not (voice or "").strip() or not (label or "").strip():
        return None
    data = load_labels(app_dir)
    data[str(voice)] = str(label).strip()
    p = labels_path(app_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def label_mapper(labels: dict[str, str]):                            # noqa: ANN201
    """给 `describe(label_of=...)` 用的查表函数：本地登记优先，其次回落到给定的 fallback。

    与 `vlt/voices._registry_label` 同一套「按 `-voice-` 前缀认亲」的口径：
    服务端重建 id 时只有时间戳/后缀变，前缀不变。
    """
    def lookup(voice: str, fallback: str) -> str:
        v = str(voice or "").strip()
        if v in labels:
            return labels[v]
        base = v.split("-voice-")[0]
        if base:
            for vid, label in labels.items():
                if vid.split("-voice-")[0] == base:
                    return label
        return fallback

    return lookup


# ---------------------------------------------------------------- 克隆预设（拼接范本）


@dataclass(frozen=True)
class ClonePreset:
    """一条「克隆预设」= 复刻素材该怎么拼出来的**范本**。

    为什么要它：复刻素材是拼出来的（多条朗读按顺序 + 固定间隔），拼法本身就是可复用的资产 ——
    用户要能看/复制/导出这份范本，交给别人照着录，或自己下次照着再拼一份。
    `key` 是稳定标识（**别改名**：本地登记表按它对齐）。
    """

    key: str
    label: str                                  # 中文显示名
    spec: str                                   # 范本文本（人话：几段、什么顺序、多长、什么规格）
    sample_name: str = ""                       # 期望的样本文件名（本地按名找 / 远端按名拉）
    sample_sha256: str = ""                     # 拉回来的音频必须对得上（防镜像串味/半截文件）
    labels: dict = field(default_factory=dict)  # 其它语言的显示名（缺省回落 label）


# 内置预设：用户 2026-10-03 复刻 MetroPolice 时用的那份拼接范本
# （4 条连续朗读 → 0.2s 间隔拼到 19.7s，44.1kHz 单声道 16bit；实测一次通过、无降级）
BUILTIN_CLONE_PRESETS: tuple[ClonePreset, ...] = (
    ClonePreset(
        key="my_clip_4x",
        label="MetroPolice",
        spec=(
            "素材：4 条连续朗读，44.1kHz 单声道 16bit\n"
            "顺序：standardloyaltycheck(5.69s) → citizensummoned(5.56s) → "
            "loyaltycheckfailure(4.57s) → classifyasdbthisblockready(3.29s)\n"
            "段间：0.2 秒静音（别贴在一起，也别加长）\n"
            "合计：19.7 秒（官方 10~20 秒；留 0.3 秒余量，别贴 20 秒上限）\n"
            "规格：≥24kHz · 单声道 · ≤10MB · 无背景音／音乐／他人声 · 正常语速\n"
            "产出：source_sample_v2.wav\n"
            "注：样本逐字文本（input.text）**别用文件名猜** —— 要么不填，要么拿 ASR 转写样本原样填，"
            "填错会被判 wer_too_high 静默降级。"
        ),
        sample_name="source_sample_v2.wav",
        sample_sha256="93c84fa33ccfd3c5f39cff648fb14bb387a78de1b2cb4568ca2861464f346756",
        labels={},          # 用户点名就叫 MetroPolice（各语言统一用这个英文名）
    ),
)


def _presets_path(app_dir) -> Path:
    return preview_dir(app_dir) / "clone_presets.json"


def load_clone_presets(app_dir) -> dict[str, dict]:
    """本地那份克隆预设（用户可改样本路径/自己加）。坏文件当空，绝不炸。"""
    try:
        obj = json.loads(_presets_path(app_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, dict):
                out[str(k)] = v
    return out


def save_clone_preset(app_dir, key: str, *, label: str = "", spec: str = "",
                      sample_path: str = "") -> Path | None:
    """把一条预设（或对内置预设的本地覆盖，例如「样本在我这儿的位置」）写进本地登记表。"""
    k = str(key or "").strip()
    if not k:
        return None
    data = load_clone_presets(app_dir)
    row = dict(data.get(k) or {})
    if label:
        row["label"] = str(label)
    if spec:
        row["spec"] = str(spec)
    if sample_path:
        row["sample_path"] = str(sample_path)
    data[k] = row
    d = preview_dir(app_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = _presets_path(app_dir)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def all_clone_presets(app_dir) -> list[ClonePreset]:
    """界面要列的全部预设：内置 + 本地（同 key 时本地覆盖显示名/范本）。"""
    local = load_clone_presets(app_dir)
    out: list[ClonePreset] = []
    for p in BUILTIN_CLONE_PRESETS:
        row = local.get(p.key) or {}
        out.append(ClonePreset(key=p.key, label=str(row.get("label") or p.label),
                               spec=str(row.get("spec") or p.spec),
                               sample_name=p.sample_name, labels=dict(p.labels)))
    for k, row in local.items():
        if any(p.key == k for p in BUILTIN_CLONE_PRESETS):
            continue
        out.append(ClonePreset(key=str(k), label=str(row.get("label") or k),
                               spec=str(row.get("spec") or ""),
                               sample_name=str(row.get("sample_name") or ""),
                               labels={}))
    return out


def preset_label(preset: ClonePreset, lang: str = "zh") -> str:
    """预设显示名（按界面语言）。"""
    if lang == "zh" or not preset.labels:
        return preset.label
    return str(preset.labels.get(lang) or preset.label)


def find_preset_sample(app_dir, preset: ClonePreset) -> Path | None:
    """按范本找样本音频：本地登记表里显式指的那份 → 应用数据目录 → 仓库 out/clone（开发机）。"""
    rows = load_clone_presets(app_dir)
    cands: list[Path] = []
    explicit = str((rows.get(preset.key) or {}).get("sample_path") or "")
    if explicit:
        cands.append(Path(explicit))
    name = str(preset.sample_name or "").strip()
    if name:
        cands.append(preview_dir(app_dir) / name)        # ① 已拉取的本地缓存
        try:
            repo = Path(__file__).resolve().parents[1]
            cands.append(repo / "vo_sample" / name)      # ② 跑源码：仓库里就有一份（免下载）
            cands.append(repo / "out" / "clone" / name)  # ③ 开发机上的历史位置
        except Exception:
            pass
    for c in cands:
        try:
            if c.is_file() and c.stat().st_size > 0:
                return c
        except OSError:
            continue
    return None


# ---------------------------------------------------------------- 范本音频的按需拉取
#
# 为什么音频不随包发：用户点名的做法 —— 范本样本**只托管在仓库里**（独立目录 `vo_sample/`），
# 应用需要的时候才去取（启动时查一次更新；试听 / 一键克隆那一刻按需拉）。
# ⚠️ **不在 `assets/` 里**：打包脚本只收 assets/testdata/config.example.yaml，放 assets 会被塞进 exe。
# 拉回来落到 `APP_DIR/voices/`（与试听缓存同目录）→ 之后所有路径都是本地文件，不再联网。
_REPO = "Sand-85/vrchat-livetranslate-extra"
# ⚠️ 顺序是**实测**定的（2026-10-04，本机走代理）：
#   fastly.jsdelivr 1.0s ✓ / cdn.jsdelivr 2.9s ✓ / ghproxy.net 40s ✓ / **raw 每次 60s 超时 ✗**（国内被掐）。
# 所以 raw 放**最后**只当兜底 —— 放前面会让国内用户每次都白等一分钟（第一次真链路验证就是这么撞的）。
PRESET_MIRRORS: tuple[tuple[str, str], ...] = (
    ("fastly.jsdelivr", f"https://fastly.jsdelivr.net/gh/{_REPO}@main/vo_sample/"),
    ("cdn.jsdelivr", f"https://cdn.jsdelivr.net/gh/{_REPO}@main/vo_sample/"),
    ("ghproxy", f"https://ghproxy.net/https://raw.githubusercontent.com/{_REPO}/main/vo_sample/"),
    ("github-raw", f"https://raw.githubusercontent.com/{_REPO}/main/vo_sample/"),
)
PRESET_RAW_BASE = PRESET_MIRRORS[-1][1]        # 兼容旧引用（= 兜底的 github raw）
PRESET_CDN_BASE = PRESET_MIRRORS[1][1]         # 兼容旧引用（= cdn.jsdelivr）
PRESET_FETCH_ATTEMPTS = 2                      # 每个地址试几次（实测有一次偶发断流）
PRESET_MAX_BYTES = 20 * 1024 * 1024
SAMPLE_MANIFEST_NAME = "manifest.json"      # 清单：远端样本的 sha256（启动查更新的依据）


def preset_sample_urls(preset: ClonePreset) -> list[str]:
    """按顺序试的下载地址（镜像顺序见 `PRESET_MIRRORS`：快的在前、raw 兜底）。"""
    name = str(preset.sample_name or "").strip()
    if not name:
        return []
    return [base + name for _label, base in PRESET_MIRRORS]


def preset_cache_path(app_dir, preset: ClonePreset) -> Path:
    """拉回来放哪 —— 与试听缓存同一个目录，`find_preset_sample` 天然认它。"""
    return preview_dir(app_dir) / str(preset.sample_name or "preset_sample.wav")


def _http_get_bytes(url: str, *, timeout: float, opener=None) -> bytes:
    """取一个 URL 的字节（带 UA；有体积上限，别把内存吃光）。"""
    import urllib.request

    req = urllib.request.Request(url, headers={
        "User-Agent": "vrchat-livetranslate-extra/preset-fetch",
        "Accept": "application/octet-stream, audio/*, */*",
    })
    op = opener or urllib.request.urlopen
    with op(req, timeout=timeout) as resp:            # type: ignore[attr-defined]
        data = resp.read(PRESET_MAX_BYTES + 1)
    if not data:
        raise VoiceLabError("拉回来是空的")
    if len(data) > PRESET_MAX_BYTES:
        raise VoiceLabError(f"文件太大（>{PRESET_MAX_BYTES // 1048576}MB），拒绝")
    return data


def fetch_preset_sample(app_dir, preset: ClonePreset, *, opener=None,
                        timeout: float = 60.0) -> Path:
    """把范本音频从仓库拉到本地缓存；已经有（本地/已拉过）就直接返回，不联网。

    失败会抛 `VoiceLabError`（把每个地址的失败原因都带上，界面照实显示）。
    """
    hit = find_preset_sample(app_dir, preset)
    if hit is not None:
        return hit
    urls = preset_sample_urls(preset)
    if not urls:
        raise VoiceLabError("这条预设没写样本文件名，无法拉取")
    # 认可两个 sha：内置那份（随代码走）与清单那份（远端更新过）—— 任一对上就算数
    allowed = {str(preset.sample_sha256 or "").strip().lower()} - {""}
    try:
        rows = dict((fetch_sample_manifest(opener=opener, timeout=min(timeout, 8.0))
                     .get("samples") or {}))
        remote = str((rows.get(str(preset.sample_name or "")) or {}).get("sha256") or "")
        if remote.strip():
            allowed.add(remote.strip().lower())
    except Exception as exc:                                         # noqa: BLE001
        print(f"[voice_lab] 取清单失败（就按内置 sha 校验）：{type(exc).__name__}: {exc}",
              flush=True)
    try:
        raw = _first_verified(urls, allowed, timeout=timeout, opener=opener)
    except VoiceLabError as exc:
        raise VoiceLabError(f"拉取范本音频失败：{exc}") from exc
    dest = preset_cache_path(app_dir, preset)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)
    print(f"[voice_lab] 范本音频已拉取：{dest.name}（{len(raw) / 1024:.0f} KB）", flush=True)
    return dest


def sample_manifest_urls() -> list[str]:
    """清单的候选地址（与样本同一套镜像顺序）。"""
    return [base + SAMPLE_MANIFEST_NAME for _label, base in PRESET_MIRRORS]


def _get_first(urls, *, timeout: float, opener=None,
               attempts: int = PRESET_FETCH_ATTEMPTS) -> bytes:
    """按顺序试这些地址，返回第一个取到的字节；全挂则抛（错误里点名每个地址）。

    每个地址试 `attempts` 次 —— 真链路实测遇到过一次「连接被对端重置」的偶发断流，
    重试一次就过了；重试之间的间隔很短（0.4s），不值得为它搞退避。
    """
    problems: list[str] = []
    for url in urls:
        for attempt in range(max(1, attempts)):
            if attempt:
                time.sleep(0.4)
            try:
                return _http_get_bytes(url, timeout=timeout, opener=opener)
            except Exception as exc:                                 # noqa: BLE001
                last = f"{url} → {type(exc).__name__}: {exc}"
                if attempt == attempts - 1:
                    problems.append(last)
    raise VoiceLabError("；".join(problems or ["没有可用地址"]))


def _first_verified(urls, allowed: set[str], *, timeout: float, opener=None,
                    attempts: int = PRESET_FETCH_ATTEMPTS) -> bytes:
    """按顺序取，**每个地址最多试 attempts 次，每次都要 sha 对得上**才收。

    两层循环都留着是有原因的（真链路实测）：偶发断流 → 同一地址重试就过；
    CDN 缓存串味/半截文件 → 同一地址重试无用，得换下一个镜像。
    """
    problems: list[str] = []
    for url in urls:
        for attempt in range(max(1, attempts)):
            if attempt:
                time.sleep(0.4)
            try:
                raw = _http_get_bytes(url, timeout=timeout, opener=opener)
            except Exception as exc:                                 # noqa: BLE001
                if attempt == attempts - 1:
                    problems.append(f"{url} → {type(exc).__name__}: {exc}")
                continue
            got = hashlib.sha256(raw).hexdigest()
            if not allowed or got in allowed:
                return raw
            if attempt == attempts - 1:
                problems.append(f"{url} → sha256 不符（期望 "
                                f"{'/'.join(sorted(x[:12] for x in allowed))}…，实得 {got[:12]}…）")
    raise VoiceLabError("；".join(problems or ["没有可用地址"]))


def fetch_sample_manifest(*, opener=None, timeout: float = 8.0) -> dict:
    """取远端清单（`vo_sample/manifest.json`）。取不到就抛 —— 调用方决定怎么记日志。"""
    raw = _get_first(sample_manifest_urls(), timeout=timeout, opener=opener)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:                                         # noqa: BLE001
        raise VoiceLabError(f"清单不是合法 JSON：{type(exc).__name__}: {exc}") from exc
    if not isinstance(obj, dict) or not isinstance(obj.get("samples"), dict):
        raise VoiceLabError("清单结构不对（缺 samples 段）")
    return obj


def check_sample_updates(app_dir, presets=None, *, opener=None,
                         timeout: float = 8.0) -> list[str]:
    """启动时查一次：清单里的 sha 与本地（缓存优先，其次仓库副本）不一致 → 把新样本拉到缓存。

    返回这次更新了哪些文件名（空 = 都已是最新）。**只拉到本地缓存**，不动仓库里的那份。
    """
    man = fetch_sample_manifest(opener=opener, timeout=timeout)
    rows = dict(man.get("samples") or {})
    if not rows:
        return []
    todo = list(presets if presets is not None else BUILTIN_CLONE_PRESETS)
    changed: list[str] = []
    for p in todo:
        name = str(getattr(p, "sample_name", "") or "").strip()
        want = str((rows.get(name) or {}).get("sha256") or "").strip().lower()
        if not name or not want:
            continue
        local = find_preset_sample(app_dir, p)
        if local is not None:
            try:
                if hashlib.sha256(local.read_bytes()).hexdigest() == want:
                    continue                                        # 本地就是最新的
            except OSError:
                pass
        urls = preset_sample_urls(p)
        if not urls:
            continue
        try:
            raw = _first_verified(urls, {want}, timeout=timeout, opener=opener)
        except VoiceLabError as exc:
            print(f"[voice_lab] 范本样本 {name} 下载失败：{exc}", flush=True)
            continue
        dest = preset_cache_path(app_dir, p)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        changed.append(name)
        print(f"[voice_lab] 范本样本已更新：{name}（{len(raw) / 1024:.0f} KB）", flush=True)
    return changed


def sample_pcm_from_file(path) -> tuple[bytes, float]:
    """把**任意采样率**的样本音频解成 24kHz 单声道 s16le PCM（本地试听用，不联网、不花钱）。

    为什么放库里而不是界面里：解码/重采样是纯逻辑、能离线测；界面只管播。
    返回 (pcm, 秒数)；解不出来就抛（界面捕获取状态）。
    """
    import miniaudio

    dec = miniaudio.decode_file(str(path), output_format=miniaudio.SampleFormat.SIGNED16,
                                nchannels=1, sample_rate=24000)
    pcm = dec.samples.tobytes()
    if not pcm:
        raise VoiceLabError("样本解出来是空音频")
    return pcm, len(pcm) / 2.0 / 24000.0


# ---------------------------------------------------------------- 配方库


@dataclass(frozen=True)
class Recipe:
    """一份「配方」：名字 + 中文说明 + 描述原文（一键生成用）。"""

    key: str
    label: str
    prompt: str


# 用户预先做好的配方（原文来自他 2026-09 的炼音色记录；同一个描述不保证复现同一听感）。
#
# 2026-10-03：他按日语样本逐条试听后**只留 5 条**（原 1·2·4·6·8），删掉 mix_a / mix_c / vint_2。
# ⚠️ `key` 必须与创建音色时的 `preferred_name` 一致（**别改名**）——「试听配方」靠它去
#    `list_voices` 的结果里对上账号里那条音色。改名就等于对不上、要重新花钱炼一个。
# 删掉的三条描述仍在 `out/vd/{mix,vint}_voices.json` 与 git 历史里，要恢复随时可取。
RECIPES: tuple[Recipe, ...] = (
    Recipe("clear_auto", "清晰·播报感",
           "年轻女性，发音清晰标准，中高音，语速中等，语调平稳专业，像车载导航在播报"),
    Recipe("calm_thin", "冷静·偏薄",
           "年轻女性的声音，音色干净偏薄，情绪起伏极小，语速平稳略慢，几乎没有语调上扬，"
           "带一点电子合成感，像医疗设备在平静地说话"),
    Recipe("mix_bal", "均衡·中高音",
           "年轻女性，音色干净偏薄但有明确的中高音芯，情绪起伏极小，语调平稳专业，"
           "语速中等，咬字清晰标准，带轻微电子合成感，像冷静的播报员在平稳说明"),
    Recipe("vint_1", "起伏·极轻",
           "年轻女性的声音，音色干净偏薄，咬字清晰标准，语速平稳略慢，带一点电子合成感；"
           "整体依然平静，但语调有很轻微的、自然的起伏，句尾偶尔有少许上扬，听感不呆板"),
    Recipe("vint_3", "起伏·明显",
           "年轻女性的声音，音色干净偏薄，咬字清晰标准，语速平稳略慢，带一点电子合成感；"
           "语调起伏更明显，有自然的抑扬顿挫和轻微情绪色彩，但仍然克制、不夸张"),
)


def recipe_by_key(key: str) -> Recipe | None:
    """按 key 取配方（界面下拉用；未知 key 返回 None，绝不 KeyError）。"""
    k = str(key or "").strip().lower()
    for r in RECIPES:
        if r.key == k:
            return r
    return None


def recipe_labels() -> list[str]:
    """配方的展示文本（下拉候选）：`说明（key）` —— 说明在前，用户按听感挑。"""
    return [f"{r.label}（{r.key}）" for r in RECIPES]


def recipe_from_label(label: str) -> Recipe | None:
    """把下拉里的展示文本还原成配方（解析不出返回 None）。"""
    text = str(label or "")
    if "（" in text and text.endswith("）"):
        return recipe_by_key(text[text.rindex("（") + 1:-1])
    return recipe_by_key(text)


def now_stamp() -> str:
    """创建时间戳（日志与文件名用；独立出来便于测试打桩）。"""
    return time.strftime("%Y%m%d%H%M%S")
