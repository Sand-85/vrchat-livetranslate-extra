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
import json
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import endpoints
from . import tts                       # 试听要复用它的合成口径（同一份解码/端点纪律）
from .tts import _get_opener          # 与 tts.py 共用「直连、绕开系统代理」的同一份策略

# 创建与合成必须用同一个模型；换模型就得重新炼（官方硬约束）。
DEFAULT_TARGET_MODEL = "qwen3-tts-vd-2026-01-26"
DESIGN_MODEL = "qwen-voice-design"
# 自定义音色的接口路径（与 tts 的多模态路径同一个网关，只是路径不同）
CUSTOMIZATION_PATH = "/api/v1/services/audio/tts/customization"

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
    """本账号里的一条自定义音色（`action=list` 的条目）。"""

    voice: str
    name: str = ""
    prompt: str = ""
    target_model: str = ""
    created: str = ""


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


def describe(voices: list[VoiceInfo], label_of=None) -> list[str]:
    """给界面用的展示行（名字 + 语音 id 后 6 位 + 创建日期），避免让用户看一串长 id。

    `label_of(voice_id, fallback)` 可传界面侧的显示名映射（`vlt.voices.display_name`）：
    复刻音色就能显示本地化名字（中文「国民护卫队」/ 其它语言「MetroPolice」）而不是一长串 id。
    """
    rows: list[str] = []
    for v in voices:
        tail = v.voice[-6:] if len(v.voice) > 6 else v.voice
        when = (v.created or "")[:10]
        label = v.name or _name_of(v.voice) or "(未命名)"
        if label_of is not None:
            label = label_of(v.voice, label) or label
        rows.append(f"{label}  ·{tail}" + (f"  ·{when}" if when else ""))
    return rows


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


def list_voices(*, api_key: str, base_url: str = "", workspace_id: str = "",
                target_model: str = DEFAULT_TARGET_MODEL, url: str = "",
                timeout: float = DEFAULT_TIMEOUT_S, opener=None) -> list[VoiceInfo]:  # noqa: ANN001
    """列出**本账号**的自定义音色（列表按 target_model 隔离，故要带上模型名）。"""
    if not (api_key or "").strip():
        raise VoiceLabError("还没配置 API key（见界面右上角「设置」）")
    obj = _post({"model": DESIGN_MODEL,
                 "input": {"action": "list", "target_model": target_model,
                           "page_size": 50}},
                api_key=api_key, url=url or customization_url(base_url, workspace_id),
                timeout=timeout, opener=opener)
    _check_error(obj)
    out = (obj.get("output") or {})
    rows = out.get("voice_list") or out.get("voices") or []
    voices: list[VoiceInfo] = []
    for r in rows:
        if not isinstance(r, dict) or not r.get("voice"):
            continue
        voices.append(VoiceInfo(
            voice=str(r.get("voice")),
            name=str(r.get("preferred_name") or r.get("name") or ""),
            prompt=str(r.get("voice_prompt") or r.get("prompt") or ""),
            target_model=str(r.get("target_model") or ""),
            created=str(r.get("gmt_create") or r.get("create_time") or "")))
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
                        target_model=target_model, url=url, timeout=timeout, opener=opener),
            nm)
    except VoiceLabError:
        existing = None
    if existing is not None:
        return VoiceCreation(voice=existing.voice, name=nm, reused=True)
    return create_voice(nm, prompt, api_key=api_key, base_url=base_url,
                        workspace_id=workspace_id, target_model=target_model,
                        url=url, timeout=timeout, opener=opener)


def delete_voice(voice: str, *, api_key: str, base_url: str = "", workspace_id: str = "",
                 target_model: str = DEFAULT_TARGET_MODEL, url: str = "",
                 timeout: float = DEFAULT_TIMEOUT_S, opener=None) -> None:      # noqa: ANN001
    """删除一条自定义音色（清理废弃候选，保持列表干净）。"""
    if not (api_key or "").strip():
        raise VoiceLabError("还没配置 API key（见界面右上角「设置」）")
    if not (voice or "").strip():
        raise VoiceLabError("没有指定要删除的音色")
    obj = _post({"model": DESIGN_MODEL,
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
