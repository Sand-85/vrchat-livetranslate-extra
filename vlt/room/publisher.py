"""房间中继 · 发布器：把会话层的 `TextDelta` 流切成 `RoomClient.publish()` 的上行参数。

调用方是批次 2b 的 `engine._on_text`：每来一条 `TextDelta` 就
`for item in pub.feed(d.source, d.is_final): client.publish(item.utt, item.rev, item.text, item.is_final)`。

为什么单独一层、而不是让 engine 直接调 `publish()`：
    · engine 只该管「收到一条文本」，不该管「这是第几句、第几版、该不该发」——
      分句 / 版本号 / 节流 / 静默兜底全是房间协议自己的口径，混进 engine 就没法离线测了；
    · 本模块**纯逻辑：零 IO、零线程、零全局状态**，时钟（`now_ms`）与句 id
      （`new_utt`）都可注入，`tests/test_room_publisher.py` 不碰网络就能逐条断言。

与 `RoomClient.publish()` 的分工（别在两边各做一份口径）：
    · 本层决定「**要不要**产生一帧」（分句 / 版本号 / 内容去重 / 节流 / 静默兜底）；
    · `RoomClient` 决定「**怎么**发出去」（seq 严格递增兜底、回放补发、限速留痕）。
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable

from .protocol import now_ms as _wall_ms


@dataclass(frozen=True)
class PublishItem:
    """一帧待上行的房间文本（`RoomClient.publish()` 的四个参数，原样对应）。

    text 是**全量快照**，绝不是 diff：接收端按 (src, utt) 覆盖式上屏，
    发 diff 过去会拼出「我在我在测我在测试」这种叠字字幕。
    """

    utt: str          # 句 id
    rev: int          # 句内版本号，递增
    text: str         # 全量快照
    is_final: bool


def new_utt_id() -> str:
    """默认句 id：8 位 hex（`secrets` → 密码学随机源，两个客户端撞不出同一个 id）。"""
    return secrets.token_hex(4)


class SourcePublisher:
    """把一条来源的文本流切成一句一句的房间上行帧。

    状态机口径（每条都有单测，见 `tests/test_room_publisher.py`）：
        1. 只发非空、且与上一次已发快照不同的文本（模型会重复吐同样的字）；
        2. 同一句内 `rev` 递增；封句后必换新 `utt`、`rev` 从 1 重来；
        3. `is_final=True` → 发一条 final（`rev` 再 +1）然后封句；
        4. 静默兜底：距上次 `feed` 超过 `final_gap_s` 且上一句没封 →
           先补一条 final（文本取**上一次已发快照**）再开新句；
        5. partial 节流：距上一帧不足 `min_partial_ms` 的 partial 不发；final 永远发；
        6. `reset()`（切语言 / 会话重建 / 停止）封句清状态，下一次 `feed` 一定是新句。
    """

    def __init__(self, *, final_gap_s: float = 3.0, min_partial_ms: int = 200,
                 duplicate_final_window_ms: int = 800,
                 new_utt: Callable[[], str] | None = None) -> None:
        # ⚠️ final_gap_s 的口径与 `session.base.DEFAULT_FINAL_SILENCE_S` 同源：
        #    必须**大于服务端增量间隔**（实测连续说话时相邻 delta 最大 2.3s）。
        #    调小会在句子中间抢跑，把半句当成最终版发到房间里 —— 对端看到的
        #    就是一句被腰斩的话，而且那句永远不会再被补全（utt 已经换了）。
        self.final_gap_s = float(final_gap_s)
        self.min_partial_ms = int(min_partial_ms)
        # 机械重复 done 的判定窗口（毫秒）：内容相同的 final 只有在**这么短的间隔内**
        # 才算「服务端重复发 done」，超出窗口一律当「人又说了一遍」。
        # ⚠️ 不能只按内容判定：真人会重复说同一句短话（「对」「好」「谢谢」「哈哈」），
        #    那种重复的间隔是秒级；一刀切会把第二遍整句吞掉，对端永远看不到（实测踩到）。
        #    服务端重复 done 是毫秒级，800ms 的窗口足以挡住它。<=0 表示不做这层去重。
        self.duplicate_final_window_ms = int(duplicate_final_window_ms)
        self._new_utt = new_utt or new_utt_id
        self._reset_state()

    # ------------------------------------------------------------ 内部状态

    def _reset_state(self) -> None:
        self._utt = ""            # 当前句 id；空 = 没有开着的句（下一帧惰性开新句）
        self._rev = 0             # 当前句已发出的最高版本号
        self._last_sent = ""      # 上一次已发快照（内容去重的基准）
        self._last_was_final = False   # 上一帧是不是 final（重复 done 的去重基准）
        self._last_feed_ms = -1   # 上次 feed 的墙钟 ms（静默兜底用；-1 = 还没有过）
        self._last_frame_ms = -1  # 上一帧的墙钟 ms（partial 节流用；-1 = 句内第一帧）
        self._last_final_ms = -1  # 上一个 final 的墙钟 ms（挡机械重复 done 用；-1 = 还没有过）

    def reset(self) -> None:
        """封掉当前句并清空内部状态（切语言 / 会话重建 / 停止时调用）。

        不补 final：调用时机是「这条流整个作废」（语言都换了，半句旧文本发到房间里
        只会让人看不懂），清干净、让下一次 `feed` 从新句开始就够了。
        """
        self._reset_state()

    # ------------------------------------------------------------ 主入口

    def feed(self, text: str, is_final: bool, *, now_ms: int | None = None) -> list[PublishItem]:
        """喂一条会话层文本，返回 0~2 条待上行帧（调用方逐条 `publish()` 即可）。

        返回多条只有一种情况：静默兜底补发上一句的 final + 本帧开新句。
        本函数**绝不抛异常、绝不做 IO**；`now_ms=None` 时取墙钟（生产路径），
        测试注入固定值就有确定性。
        """
        now = int(now_ms) if now_ms is not None else _wall_ms()
        snap = text.strip() if isinstance(text, str) else ""
        out: list[PublishItem] = []

        # ⚠️ 必须在静默兜底**之前**更新：`_make()` 拿 `_last_feed_ms` 给 `_last_final_ms`
        #    盖章。晚这一步的话，兜底补出来的那条 final 会把时间戳写成**上一次 feed 的旧值**，
        #    紧接着到达的真 final 就认不出「这一句刚刚发过」→ 同一句发两条 final、对端重复上屏。
        #    （2026-09-28 真机演示复现：一次 `feed(final)` 调用吐了两条 final。）
        prev_feed_ms = self._last_feed_ms
        self._last_feed_ms = now

        # 规则 4：静默兜底分句。服务端有时不发 done（既有教训），上一句会永远开着；
        # 距上次 feed 超过阈值就先替它补一条 final —— 文本取**上一次已发快照**
        # （被节流吃掉的那帧对端根本没见过，当前这帧属于下一句，都不能拿来当定稿）。
        # 上一帧已经是同内容的 final 就不重复补（服务端会重复发 done）。
        # 阈值判断用 prev_feed_ms（本帧的时间戳上面已写进状态，不能再拿来比）。
        if (self._utt and prev_feed_ms >= 0 and self.final_gap_s > 0
                and now - prev_feed_ms > self.final_gap_s * 1000.0):
            if self._last_sent and not self._last_was_final:
                out.append(self._make(self._last_sent, final=True))
            self._seal()

        if not snap:
            return out                    # 规则 1：空文本不上行（开句也推迟到有内容时）

        if is_final:
            # 规则 3：final 永远发（规则 5 的节流吃不掉它，规则 1 的去重也不该吃掉它：
            # final 的文本常常与上一帧 partial 完全相同，但它是「这句到此为止」的
            # 唯一信号，对端要靠它把气泡定稿；漏了它就永远显示成半句）。
            # 唯一去重的情况：**内容相同、且在极短窗口内的重复 final** —— 那才是服务端
            # 重复发 done。超出窗口就是「人又说了一遍」，必须发（见 __init__ 的注释）。
            dup = (snap == self._last_sent and self._last_was_final
                   and self.duplicate_final_window_ms > 0
                   and 0 <= self._last_final_ms
                   and now - self._last_final_ms <= self.duplicate_final_window_ms)
            if not dup:
                out.append(self._make(snap, final=True))
            self._seal()
            return out

        # 规则 5：partial 节流。距上一帧不足 min_partial_ms 就不发（内容变了也不发）——
        # 8 个人同时说话时模型每吐一个字就是一帧，不节流会撞上服务端 20 帧/秒限速，
        # 把 final 一起挤掉。基准是**上一帧**（不是上一次实际发出的帧），
        # 与 protocol.should_send_partial 同一口径。
        # 只在句内节流：封句时基准清零，新句第一帧立刻发（否则兜底分句之后
        # 新气泡要干等一个节流窗口才出现，房间里看着像丢了一句话的开头）。
        if (self.min_partial_ms > 0 and self._utt and self._last_frame_ms >= 0
                and now - self._last_frame_ms < self.min_partial_ms):
            return out

        # 规则 1：内容没变不发（模型会重复吐同样的文本，重发只会白占限速额度）。
        if snap == self._last_sent:
            return out

        out.append(self._make(snap, final=False))
        return out

    # ------------------------------------------------------------ 内部工具

    def _make(self, snap: str, *, final: bool) -> PublishItem:
        """产出一帧：没有开着的句就先开（规则 2：新句必换新 `utt`、`rev` 从 1 重来）。"""
        if not self._utt:
            self._utt = str(self._new_utt() or "") or new_utt_id()
            self._rev = 0
            self._last_frame_ms = -1      # 新句第一帧不受上一句的节流影响
        self._rev += 1
        self._last_frame_ms = self._last_feed_ms
        self._last_sent = snap
        self._last_was_final = final
        if final:
            self._last_final_ms = self._last_feed_ms
        return PublishItem(utt=self._utt, rev=self._rev, text=snap, is_final=final)

    def _seal(self) -> None:
        """封句：下一次 `feed` 一定开新句（新 `utt`、`rev` 从 1 重新开始）。

        故意不清 `_last_sent`：紧跟着重吐同一段文本时（模型 repeat / 静默兜底后
        同一句又回来）仍然按规则 1 去重，不会把同一句话换个 id 再刷一遍。
        """
        self._utt = ""
        self._rev = 0


def should_publish(source: str, enabled: bool, broadcast_source: bool) -> bool:
    """只有「说话人自己麦克风」那条腿该往房间里发。

    `source` 是 Engine 的来源标识：`mic` / `loopback` / `pcm:<路径>`。
    **loopback 那条腿采的是游戏里别人说话的声音**，把它发进房间等于把别人的话
    二次广播（还会引发回环：A 转 B 的话 → B 的房间客户端又转回来），必须排除。
    `pcm:<路径>` 是拿录音文件当自己的输入（调试 / 无麦场景），语义上仍是「我说的」。
    """
    if not enabled or not broadcast_source:
        return False
    src = (source or "").strip().lower()
    if not src or src == "loopback":
        return False
    return src == "mic" or src.startswith("pcm:")
