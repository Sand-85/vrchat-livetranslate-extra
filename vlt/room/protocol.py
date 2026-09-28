"""房间中继协议 v1：帧编解码 + 房间码 + 乱序自愈 + 覆盖式合并。

**本模块零 IO、零网络、零全局状态** —— 全部是可重复调用的纯函数（`apply_seg` /
`should_accept_final` 只改调用方传进来的容器，不碰模块级变量），因此可以脱离
WebSocket 直接单测（`tests/test_room_protocol.py`）。

协议口径（详见 `.hermes/plans/2026-09-28_112035-room-relay.md` §4）：
    · 传输是 JSON over WebSocket 文本帧，单帧 <1KB
    · **partial（`seg`）是「截至目前的全量快照」，不是 diff** —— 覆盖式，绝不累加
    · `seq` 句内递增，接收方只保留最高 seq（乱序/丢帧自愈）
    · 只对 `final` 回 `ack`；`seg` 不回（丢了下一拍就有更全的）
    · `origin`（这里叫 `src`）标记来源成员，自己发的帧永不再上行（防往返）

编码严、解码宽：`encode_frame` 遇到没登记的字段名直接抛 `ProtocolError`（拼错
字段名是最难查的一类 bug，必须立刻炸）；`decode_frame` 对**未知字段**不报错、
原样保留，由消费方忽略 —— 这样对端将来加字段不需要两端同步升级。
"""
from __future__ import annotations

import json
import secrets
import time
from typing import Any

# ------------------------------------------------------------------ 错误与常量


class ProtocolError(ValueError):
    """协议层错误：坏 JSON、不是对象、缺 `t`、`encode_frame` 传了没登记的字段名。"""


#: Crockford Base32 字符集（32 个），**去掉了易混的 I / L / O / U**
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 房间码长度（8 位 → 32^8 ≈ 1.1e12 种，熟人小圈子够用且好念）
ROOM_CODE_LEN = 8

#: Crockford 的容错映射：口语里念出来的 I/L 其实就是 1，O 就是 0
_CODE_LOOKALIKE = {"I": "1", "L": "1", "O": "0"}

#: 帧类型（`t` 字段的全部合法取值）
FRAME_HELLO = "hello"
FRAME_WELCOME = "welcome"
FRAME_MEMBERS = "members"
FRAME_SEG = "seg"
FRAME_FINAL = "final"
FRAME_ACK = "ack"
FRAME_PING = "ping"
FRAME_PONG = "pong"
FRAME_ERR = "err"

FRAME_KINDS: frozenset[str] = frozenset({
    FRAME_HELLO, FRAME_WELCOME, FRAME_MEMBERS, FRAME_SEG, FRAME_FINAL,
    FRAME_ACK, FRAME_PING, FRAME_PONG, FRAME_ERR,
})

#: 字段表（方案 §4）+ `err` 帧的两个载荷字段（方案只列了 `t: err`，没定载荷）
KNOWN_FIELDS: frozenset[str] = frozenset({
    "t", "room", "tok", "nick", "me", "members",
    "utt", "seq", "text", "lang", "final", "ts", "srv_ts", "src",
    "code", "msg",
})

#: 需要按 int 归一的字段（脏值 → 0，不抛：网络那头是什么都可能出现）
_INT_FIELDS = ("seq", "ts", "srv_ts")

#: 单帧上限（防对端灌垃圾把内存打爆；1KB 是方案口径，留 8 倍余量）
MAX_FRAME_BYTES = 8 * 1024

#: `err` 帧的致命码：收到就别再重连了（重连只会一直被拒）
FATAL_ERR_CODES: frozenset[str] = frozenset({"auth", "room_full", "bad_room", "banned"})


# ------------------------------------------------------------------ 小工具


def now_ms() -> int:
    """当前墙钟毫秒（`ts` 字段用；只做诊断，不参与任何判定）。"""
    return int(time.time() * 1000)


def new_peer_id() -> str:
    """新成员 id：`p_` + 6 位 hex（方案 §4 的 `me` 字段格式）。"""
    return "p_" + secrets.token_hex(3)


# ------------------------------------------------------------------ 房间码


def normalize_room_code(raw: Any) -> str:
    """房间码归一：去空白与连字符、转大写、按 Crockford 规则映射易混字符。

    用户在界面里手输 `abcd-1234` 或把 `1` 打成 `I` 都应该能进房 —— 归一放在这一层，
    上层就不用各自写一遍。`U` 不做映射（它在 Crockford 里就是非法字符）。
    """
    if not isinstance(raw, str):
        return ""
    s = "".join(ch for ch in raw.strip().upper() if ch.isalnum())
    return "".join(_CODE_LOOKALIKE.get(ch, ch) for ch in s)


def new_room_code() -> str:
    """随机生成一个 8 位 Crockford Base32 房间码（`secrets` → 密码学随机源）。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(ROOM_CODE_LEN))


def is_valid_room_code(raw: Any) -> bool:
    """房间码是否合法：归一后长度正好 8 位，且全部字符都在字符集里。"""
    code = normalize_room_code(raw)
    return len(code) == ROOM_CODE_LEN and all(ch in ALPHABET for ch in code)


# ------------------------------------------------------------------ 帧编解码


def encode_frame(kind: str, **fields: Any) -> str:
    """把一帧编成 JSON 文本（紧凑分隔符、中文不转义，尽量省字节）。

    `kind` 必须是 `FRAME_KINDS` 里的一个；字段名必须在 `KNOWN_FIELDS` 里登记过
    （拼错立刻抛，不留到线上才发现对端读不到）。值为 `None` 的字段直接丢掉。
    """
    if not isinstance(kind, str) or kind not in FRAME_KINDS:
        raise ProtocolError(f"未知帧类型：{kind!r}（合法：{sorted(FRAME_KINDS)}）")
    bad = [k for k in fields if k not in KNOWN_FIELDS]
    if bad:
        raise ProtocolError(f"帧 {kind} 带了没登记的字段：{bad}（先在 KNOWN_FIELDS 里加）")
    frame: dict[str, Any] = {"t": kind}
    for key, val in fields.items():
        if val is not None:
            frame[key] = val
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def decode_frame(raw: Any) -> dict[str, Any]:
    """把一帧 JSON 文本解成 dict，并把已知字段归一成该有的类型。

    抛 `ProtocolError` 的情况：不是字符串/字节、坏 JSON、不是 JSON 对象、缺 `t`、
    `t` 不是字符串。
    **未知帧类型不抛**（返回原样，消费方自己忽略）——对端升级不该把老客户端打死。
    **未知字段原样保留**（消费方忽略）。
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError(f"帧不是合法 UTF-8：{exc}") from exc
    if not isinstance(raw, str):
        raise ProtocolError(f"帧必须是文本（str/bytes），收到 {type(raw).__name__}")
    if len(raw.encode("utf-8", "replace")) > MAX_FRAME_BYTES:
        raise ProtocolError(f"帧超过上限 {MAX_FRAME_BYTES}B（实际 {len(raw)} 字符）")
    try:
        frame = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ProtocolError(f"坏 JSON：{exc}（原文前 120 字：{raw[:120]!r}）") from exc
    if not isinstance(frame, dict):
        raise ProtocolError(f"帧必须是 JSON 对象，收到 {type(frame).__name__}")
    kind = frame.get("t")
    if not isinstance(kind, str) or not kind:
        raise ProtocolError(f"帧缺 `t`（帧类型）：{ {k: frame[k] for k in list(frame)[:6]} !r}")

    for key in _INT_FIELDS:
        if key in frame:
            frame[key] = _as_int(frame[key])
    if "text" in frame and not isinstance(frame["text"], str):
        frame["text"] = "" if frame["text"] is None else str(frame["text"])
    if "final" in frame:
        frame["final"] = bool(frame["final"])
    if "members" in frame and not isinstance(frame["members"], list):
        frame["members"] = []
    return frame


def _as_int(val: Any) -> int:
    """尽力把值转成 int；转不动就给 0（`seq` 给 0 意味着「最旧」，会被自愈逻辑挡掉）。"""
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, int):
        return val
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ 乱序自愈


def should_accept(incoming_seq: int, current_seq: int) -> bool:
    """`seq` 乱序自愈：**只接受比当前更高的**版本号。

    UDP-ish 的现实里帧会乱序、会重传（`ack` 没收到就补发），旧快照盖掉新快照
    会让屏幕上的文字「往回退」，用户看到的就是"它在抽风"。这里一律只认更高。
    新句（还没有当前值）请传 `current_seq = -1`。
    """
    return _as_int(incoming_seq) > _as_int(current_seq)


def merge_segment(current: str, incoming: str) -> str:
    """partial 合并 = **覆盖**（`incoming` 是全量快照，绝不拼接）。

    这是本功能最容易写错的一行：把 partial 当 diff 累加，屏幕会变成
    「你你你好你你好」。只有 `incoming` 为空（对端发了个空快照）时保留旧值，
    避免闪一下空白。
    """
    inc = (incoming or "").strip() if isinstance(incoming, str) else ""
    if inc:
        return inc
    return current if isinstance(current, str) else ""


def should_accept_final(seen_finals: set[str], key: str) -> bool:
    """final 幂等去重：同一句只认第一次，重复的 final 直接丢。

    `ack` 丢了客户端会补发 final，服务端也可能重投 —— 不去重就会在手腕屏上
    看到同一句话叠好几条。`key` 建议用 `f"{src}:{utt}"`（跨说话人不撞）。
    会把 `key` 记进 `seen_finals`（调用方持有并负责限量，见 `_prune`）。
    """
    if not isinstance(key, str) or not key:
        return False
    if key in seen_finals:
        return False
    seen_finals.add(key)
    return True


def apply_seg(store: dict[str, dict[str, Any]], seen_finals: set[str],
              frame: dict[str, Any]) -> dict[str, Any] | None:
    """把一帧 `seg`/`final` 落到「每 (src, utt) 一条」的存储里，返回改动后的最新条目。

    三条纪律一次做完，客户端和测试共用同一份实现：
      ① `final` 是这句话的终态 → 只认第一次（重传/重投幂等），且**不比 `seq`**
         （ack 丢了补发的 final 可能带着比后到 partial 更旧的 seq，比 seq 会把定稿丢掉）
      ② `seg`（partial）走 `seq` 只认更高（乱序/丢帧自愈），已定稿的句子不再接受迟到 partial
      ③ `text` 一律覆盖式合并（绝不累加）
    被这三条挡掉时返回 `None` —— 调用方据此决定「要不要刷新界面」。
    """
    src = frame.get("src") or ""
    utt = frame.get("utt") or ""
    if not src or not utt:
        return None
    key = f"{src}:{utt}"
    is_final = bool(frame.get("final")) or frame.get("t") == FRAME_FINAL
    cur = store.get(key)
    cur_seq = -1 if cur is None else _as_int(cur.get("seq", -1))
    incoming_seq = _as_int(frame.get("seq", 0))
    if is_final:
        if not should_accept_final(seen_finals, key):
            return None                      # 这句的 final 已经收过了
    else:
        if cur is not None and cur.get("final"):
            return None                      # 已定稿，迟到的 partial 不许把文字往回改
        if cur is not None and not should_accept(incoming_seq, cur_seq):
            return None                      # 乱序/重复的旧快照
    entry = dict(cur or {})
    entry.update({
        "src": src,
        "utt": utt,
        "seq": max(incoming_seq, cur_seq),
        "text": merge_segment(entry.get("text", ""), frame.get("text", "")),
        "lang": frame.get("lang") or entry.get("lang") or "zh",
        "final": is_final or bool(entry.get("final")),
        "srv_ts": _as_int(frame.get("srv_ts", 0)),
    })
    store[key] = entry
    return entry


# ------------------------------------------------------------------ partial 节流


def should_send_partial(prev_text: str, prev_ms: int, text: str, now: int,
                        min_interval_ms: int = 200) -> bool:
    """partial 节流：**内容没变不发**，距上一帧不足 `min_interval_ms` 不发。

    方案 §11 的口径是 ≤1 帧/200ms。不节流的话，模型每吐一个字就是一帧，
    房间里 8 个人同时说话 = 每秒几十帧，服务端限速会把 final 一起挤掉。
    `min_interval_ms <= 0` 表示不限流（测试里要确定性时用）。
    **final 永远不走这里**（句末必达，不能被节流吃掉）。
    """
    text = (text or "").strip() if isinstance(text, str) else ""
    if not text:
        return False
    if isinstance(prev_text, str) and prev_text.strip() == text:
        return False
    if min_interval_ms > 0 and _as_int(now) - _as_int(prev_ms) < min_interval_ms:
        return False
    return True
