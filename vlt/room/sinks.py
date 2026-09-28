"""房间链路的两个**口子**：翻译层与 TTS 层。本期只定义接口，不做任何真实实现。

为什么要现在就把口子留出来（方案 §1.3 / §6.2）：
    · P0 场景是「一群中国人一起玩」，大家说的都是中文，广播的就是各自的 ASR 源文，
      翻译用不上；但将来会有混语房间，那时候接收侧要能流式翻译远端文本。
    · TTS 是给「没装 VLT 的听众」用的，用户明确说「先留口子」。
    两个能力都得挂在**同一个位置**（收到远端帧之后、上屏之前），所以先把接口钉死，
    将来加实现不用改 `RoomClient` 的签名，也不用改手腕屏的渲染签名。

⚠️ 纪律：
    · 不许在这里写真的翻译 / TTS 代码（那是批次 4）。
    · **界面上不许出现「本功能将在下个版本开放」这类占位文案**（会把未完成品发出去）。
      口子只存在于代码接口与配置文件里。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Translator(Protocol):
    """把远端文本从源语言翻到本机目标语。

    实现方要保证：`text` 是**全量快照**（不是 diff），返回值也必须是全量快照 ——
    上游（手腕屏）拿到就直接覆盖，不做拼接。做不到这一点就会写出「叠字」bug。
    """

    def translate(self, text: str, src_lang: str, tgt_lang: str) -> str:
        """翻译一段文本；同语言或翻不动时应原样返回（不许抛，不许返回 None）。"""
        ...


class IdentityTranslator:
    """本期默认实现：**直通**（同语言房间，原文即译文）。

    单独做一个类而不是写 `if translator is None`，是为了让调用方永远拿到一个
    可用对象 —— 少一处 None 判断，就少一处「忘了判 → 崩在用户脸上」。
    """

    __slots__ = ()

    def translate(self, text: str, src_lang: str, tgt_lang: str) -> str:
        return text or ""

    def __repr__(self) -> str:                      # 日志里能看出用的是直通
        return "<IdentityTranslator 直通（不翻译）>"


@runtime_checkable
class TtsSink(Protocol):
    """把远端文本送去合成语音（给没装 VLT 的听众听）。本期不实现。"""

    def feed(self, text: str, is_final: bool, speaker: str) -> None:
        """喂一段文本。`is_final=True` 表示这句已定稿，可以真正开始合成。

        实现方**必须自己节流/丢弃**：partial 是高频全量快照，逐帧合成会打成一片噪音。
        """
        ...


def make_translator(name: str = "identity") -> Translator:
    """按名字取一个 `Translator`（将来在这里登记真实实现）。

    现在只有 `identity`。名字不认识时**不抛**，回落直通并留一行痕迹由调用方打 ——
    房间链路失败绝不能拖垮现有翻译/chatbox/手腕屏（方案 §11）。
    """
    if name and name != "identity":
        return IdentityTranslator()
    return IdentityTranslator()
