"""`RoomConfig.from_dict` 健壮性验收：缺字段 / 脏值 / 类型错 → **留痕 + 回落默认**，绝不抛。

## 为什么这条是硬要求

`config.yaml` 是用户手改的。写错一个类型就抛异常 = **整个同传起不来**（不只是房间功能没了），
用户看到的是"程序打不开"，而不是"房间那行配置写错了"。所以脏配置必须：
① 打一行 `[room] ⚠️ ...` 说清哪个键、什么值、为什么非法、回落成了什么；② 继续跑。

全部离线，不碰网络、不碰 GUI。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.room.model import (DEFAULT_BACKOFF, DEFAULT_HEARTBEAT_S, DEFAULT_MAX_PEERS,  # noqa: E402
                            DEFAULT_PARTIAL_MIN_MS, NICK_MAX_CHARS, ConnectionState,
                            Peer, RoomConfig, RoomMessage, RoomState)


def _load(raw) -> tuple[RoomConfig, str]:
    """跑一次 from_dict，同时把它打出来的告警抓回来（断言「留痕」而不是只看结果）。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg = RoomConfig.from_dict(raw)
    return cfg, buf.getvalue()


def test_defaults_when_section_missing() -> None:
    """★ `room:` 段整个不存在（None / {}）→ 全默认，且**一行告警都不打**。"""
    for raw in (None, {}):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cfg = RoomConfig.from_dict(raw)
        assert buf.getvalue() == "", f"全默认值时不该有任何告警：{buf.getvalue()!r}"
        assert cfg.enabled is False, "默认必须关（不能影响现有单机用户）"
        assert cfg.server_url == "" and cfg.room_code == "" and cfg.token == ""
        assert cfg.nickname == ""
        assert cfg.broadcast_source is True and cfg.show_remote is True
        assert cfg.max_peers == DEFAULT_MAX_PEERS == 8
        assert cfg.reconnect_backoff == DEFAULT_BACKOFF == (2.0, 5.0, 10.0, 30.0)
        assert cfg.heartbeat_s == DEFAULT_HEARTBEAT_S == 20.0
        assert cfg.source_lang == "zh"
        assert cfg.partial_min_ms == DEFAULT_PARTIAL_MIN_MS == 200
    print("  room 段缺失 → 全默认且不吵 OK")


def test_partial_section_keeps_other_defaults() -> None:
    """只写了两三个键（最常见的情况）→ 其余键仍走默认，不能被带成 None/0。"""
    cfg, out = _load({"enabled": True, "server_url": "wss://room.example.com/ws"})
    assert out == "", f"合法配置不该有告警：{out!r}"
    assert cfg.enabled is True and cfg.server_url == "wss://room.example.com/ws"
    assert cfg.max_peers == DEFAULT_MAX_PEERS
    assert cfg.reconnect_backoff == DEFAULT_BACKOFF
    assert cfg.heartbeat_s == DEFAULT_HEARTBEAT_S
    assert cfg.broadcast_source is True
    print("  只填部分键 → 其余保持默认 OK")


def test_not_a_dict_falls_back() -> None:
    """★ 用户把 `room:` 写成了一行字符串/列表/数字 → 全默认 + 留一条能读懂的痕迹。"""
    for raw in ("wss://x/ws", ["enabled"], 42, True, 3.5):
        cfg, out = _load(raw)
        assert cfg == RoomConfig(), f"{raw!r} 没回落到全默认：{cfg}"
        assert "[room] ⚠️" in out, f"{raw!r} 必须留痕，实际：{out!r}"
        assert "必须是键值对" in out, f"痕迹要说清为什么：{out!r}"
    print("  room 段不是字典 → 全默认 + 留痕 OK")


def test_missing_and_dirty_scalars() -> None:
    """逐字段喂脏值：类型错、越界、NaN、空串 → 回落默认且**每个都留痕**。"""
    dirty = {
        "enabled": "maybe",                    # 不是布尔
        "server_url": 12345,                   # 不是字符串
        "room_code": "ABCD12U4",               # U 是非法字符
        "nickname": None,                      # None
        "broadcast_source": [],                # 不是布尔
        "show_remote": {"a": 1},               # 不是布尔
        "max_peers": "八个",                    # 解析不成数字
        "heartbeat_s": float("nan"),           # NaN
        "partial_min_ms": -5,                  # 越界
        "source_lang": 99,                     # 不是字符串
    }
    cfg, out = _load(dirty)
    assert cfg.enabled is False, f"enabled 脏值没回落：{cfg.enabled!r}"
    assert cfg.server_url == "", f"server_url 脏值没回落：{cfg.server_url!r}"
    assert cfg.room_code == "", f"非法房间码没回落：{cfg.room_code!r}"
    assert cfg.nickname == ""
    assert cfg.broadcast_source is True and cfg.show_remote is True
    assert cfg.max_peers == DEFAULT_MAX_PEERS
    assert cfg.heartbeat_s == DEFAULT_HEARTBEAT_S
    assert cfg.partial_min_ms == DEFAULT_PARTIAL_MIN_MS
    assert cfg.source_lang == "zh"
    warned = [ln for ln in out.splitlines() if "[room] ⚠️" in ln]
    assert len(warned) >= 9, f"脏字段有 {len(dirty)} 个，却只留了 {len(warned)} 条痕：\n{out}"
    for key in ("enabled", "server_url", "room_code", "max_peers", "heartbeat_s",
                "partial_min_ms"):
        assert any(f"room.{key}=" in ln for ln in warned), f"{key} 的告警没点出键名：\n{out}"
    assert "回落默认值" in out, f"告警必须说清回落到了什么：{out!r}"
    print(f"  脏标量全部回落 + 留痕 OK（{len(warned)} 条告警）")


def test_out_of_range_numbers() -> None:
    """越界数字算脏值（不是夹到边界）：写 99 的人显然是搞错了，回落默认更安全。"""
    cases = [
        ({"max_peers": 99}, "max_peers", DEFAULT_MAX_PEERS),
        ({"max_peers": 0}, "max_peers", DEFAULT_MAX_PEERS),
        ({"max_peers": -3}, "max_peers", DEFAULT_MAX_PEERS),
        ({"max_peers": 3.5}, "max_peers", DEFAULT_MAX_PEERS),
        ({"heartbeat_s": -1}, "heartbeat_s", DEFAULT_HEARTBEAT_S),
        ({"heartbeat_s": 10**9}, "heartbeat_s", DEFAULT_HEARTBEAT_S),
        ({"partial_min_ms": 10**6}, "partial_min_ms", DEFAULT_PARTIAL_MIN_MS),
    ]
    for raw, key, want in cases:
        cfg, out = _load(raw)
        got = getattr(cfg, key)
        assert got == want, f"{raw} → {key}={got!r}，应回落 {want!r}"
        assert f"room.{key}=" in out, f"{raw} 没留痕：{out!r}"
    # 合法边界值必须原样保留（别把用户故意调小的值也当脏值）
    ok, out2 = _load({"max_peers": 1, "heartbeat_s": 0, "partial_min_ms": 0})
    assert (ok.max_peers, ok.heartbeat_s, ok.partial_min_ms) == (1, 0.0, 0)
    assert out2 == "", f"合法边界值不该告警：{out2!r}"
    print(f"  越界数字回落 / 合法边界保留 OK（{len(cases)} 例越界）")


def test_string_numbers_and_bools_accepted() -> None:
    """YAML 里写成字符串的数字、写成 yes/on 的布尔 → 尽量接受（用户手改配置很常见）。"""
    cfg, out = _load({"enabled": "yes", "broadcast_source": "off", "max_peers": "6",
                      "heartbeat_s": "12.5", "partial_min_ms": "0"})
    assert out == "", f"可解析的字符串不该告警：{out!r}"
    assert cfg.enabled is True and cfg.broadcast_source is False
    assert cfg.max_peers == 6 and cfg.heartbeat_s == 12.5 and cfg.partial_min_ms == 0
    cfg2, _ = _load({"enabled": "TRUE", "show_remote": 1})
    assert cfg2.enabled is True and cfg2.show_remote is True
    print("  字符串数字 / 各种布尔写法 OK")


def test_room_code_normalization() -> None:
    """房间码归一：小写、带连字符、I/L/O 打错 → 都能救回来；救不回就清空 + 留痕。"""
    for raw, want in (("abcd1234", "ABCD1234"), ("  ABCD-1234  ", "ABCD1234"),
                      ("abcdi234", "ABCD1234"), ("abcdl234", "ABCD1234"),
                      ("abcd0234", "ABCD0234")):
        cfg, out = _load({"room_code": raw})
        assert cfg.room_code == want, f"{raw!r} → {cfg.room_code!r}，应是 {want!r}"
        assert out == "", f"{raw!r} 是合法写法，不该告警：{out!r}"
    for raw in ("ABC", "ABCD12345", "ABCD12U4", "abcd", "房间码是八个字"):
        cfg, out = _load({"room_code": raw})
        assert cfg.room_code == "", f"非法房间码 {raw!r} 没被清空：{cfg.room_code!r}"
        assert "room.room_code=" in out, f"非法房间码 {raw!r} 没留痕：{out!r}"
    print("  房间码归一 / 非法清空 + 留痕 OK")


def test_nickname_truncated() -> None:
    """昵称超过 16 字符 → 截断（协议口径 ≤16），并留一行痕迹。"""
    long = "一二三四五六七八九十一二三四五六七八"
    cfg, out = _load({"nickname": long})
    assert len(cfg.nickname) == NICK_MAX_CHARS == 16, f"没截断：{len(cfg.nickname)}"
    assert cfg.nickname == long[:16]
    assert "截断" in out, f"截断要留痕：{out!r}"
    cfg2, out2 = _load({"nickname": "  小明  "})
    assert cfg2.nickname == "小明" and out2 == "", "合法昵称不该被动、也不该告警"
    print("  昵称截断到 16 字符 OK")


def test_backoff_variants() -> None:
    """退避表：列表/元组/单个数字/逗号字符串都收；脏档丢掉；全脏回落默认。"""
    cfg, out = _load({"reconnect_backoff": [1, 2, 3, 4]})
    assert cfg.reconnect_backoff == (1.0, 2.0, 3.0, 4.0) and out == ""
    cfg, out = _load({"reconnect_backoff": (0.05, 0.1)})
    assert cfg.reconnect_backoff == (0.05, 0.1) and out == "", f"小数值退避被拒了：{out}"
    cfg, out = _load({"reconnect_backoff": 5})
    assert cfg.reconnect_backoff == (5.0,), f"单个数字没被当成一档：{cfg.reconnect_backoff}"
    cfg, out = _load({"reconnect_backoff": "2, 5, 10"})
    assert cfg.reconnect_backoff == (2.0, 5.0, 10.0), cfg.reconnect_backoff
    cfg, out = _load({"reconnect_backoff": [2, "五", -1, 10**9, 30]})
    assert cfg.reconnect_backoff == (2.0, 30.0), f"脏档没被丢掉：{cfg.reconnect_backoff}"
    assert out.count("[room] ⚠️") == 3, f"3 个脏档应留 3 条痕：\n{out}"
    for raw in ([], "   ", None, "abc", {"a": 1}, [True], [float("nan")]):
        cfg, out = _load({"reconnect_backoff": raw})
        assert cfg.reconnect_backoff == DEFAULT_BACKOFF, f"{raw!r} → {cfg.reconnect_backoff}"
    # 退避表的取用：超出档位就停在最后一档
    c = RoomConfig(reconnect_backoff=(2.0, 5.0, 10.0, 30.0))
    assert [c.backoff_delay(i) for i in range(6)] == [2.0, 5.0, 10.0, 30.0, 30.0, 30.0]
    assert RoomConfig(reconnect_backoff=()).backoff_delay(0) == 2.0, "空退避表要有兜底"
    print("  退避表各种写法 + 取档 OK")


def test_to_dict_roundtrip() -> None:
    """`to_dict()` 必须是 YAML 可序列化的纯 dict，且回灌 `from_dict` 结果一致。"""
    import dataclasses

    import yaml

    cfg = RoomConfig(enabled=True, server_url="wss://room.example.com/ws",
                     room_code="ABCD1234", token="tok", nickname="小明",
                     broadcast_source=False, show_remote=False, max_peers=4,
                     reconnect_backoff=(1.0, 2.0), heartbeat_s=5.0,
                     source_lang="zh", partial_min_ms=100)
    d = cfg.to_dict()
    assert isinstance(d, dict)
    assert set(d) == {f.name for f in dataclasses.fields(cfg)}, f"键对不上：{sorted(d)}"
    assert isinstance(d["reconnect_backoff"], list), "tuple 必须转 list（YAML 写不出 tuple）"
    text = yaml.safe_dump(d, allow_unicode=True)
    back, out = _load(yaml.safe_load(text))
    assert out == "", f"自己写出去的配置回灌时不该告警：{out!r}"
    assert back == cfg, f"回灌后不一致：\n{back}\n!=\n{cfg}"
    print(f"  to_dict / YAML 回灌一致 OK（{len(d)} 个键）")


def test_unavailable_reason() -> None:
    """★ 不可用原因必须是人能读懂的一句话（「连不上又不说为什么」是反复踩的坑）。"""
    assert "server_url" in RoomConfig().unavailable_reason()
    assert "ws://" in RoomConfig(server_url="https://x/ws").unavailable_reason()
    assert "room_code" in RoomConfig(server_url="wss://x/ws").unavailable_reason()
    assert RoomConfig(server_url="wss://x/ws", room_code="ABCD1234").unavailable_reason() is None
    assert RoomConfig(server_url="ws://127.0.0.1:8765/ws",
                      room_code="ABCD1234").unavailable_reason() is None
    print("  unavailable_reason 可读 OK")


def test_effective_nickname() -> None:
    """昵称留空 → 取系统用户名（方案 §12 的推荐口径）；取不到也有兜底。"""
    cfg = RoomConfig(nickname="小明")
    assert cfg.effective_nickname == "小明"
    got = RoomConfig(nickname="").effective_nickname
    assert got and len(got) <= NICK_MAX_CHARS, f"空昵称没兜住：{got!r}"
    print(f"  昵称兜底 OK（本机取到 {got!r}）")


def test_with_overrides_and_state_snapshot() -> None:
    """`with_overrides` 不改原对象；`RoomState.summary` 每个连接态都有文案。"""
    base = RoomConfig(room_code="ABCD1234")
    other = base.with_overrides(room_code="ZZZZ9999", enabled=True)
    assert base.room_code == "ABCD1234" and base.enabled is False, "原对象被改了"
    assert other.room_code == "ZZZZ9999" and other.enabled is True

    for conn in ConnectionState:
        s = RoomState(conn=conn, room_code="ABCD1234",
                      peers=(Peer("p_a", "小明", True), Peer("p_b", "阿华", False)),
                      last_error="auth：令牌不对", reconnects=3)
        text = s.summary
        assert "ABCD1234" in text, f"{conn} 的状态文案没带房间码：{text}"
        assert text.strip(), f"{conn} 的状态文案是空的"
    online = RoomState(conn=ConnectionState.ONLINE,
                       peers=(Peer("p_a", "小明", True), Peer("p_b", "阿华", False)))
    assert online.is_online and online.peer_count == 1, f"在线人数只算 online：{online.peer_count}"
    assert "1 人" in online.summary
    assert RoomState().summary and not RoomState().is_online
    print(f"  with_overrides / RoomState.summary OK（{len(list(ConnectionState))} 个连接态）")


def test_peer_and_message_models() -> None:
    """`Peer.from_dict` 对脏数据返回 None（调用方跳过），`RoomMessage.display` 有译文优先。"""
    p = Peer.from_dict({"id": "p_ab12cd", "nick": " 小明 ", "online": False})
    assert p == Peer("p_ab12cd", "小明", False)
    assert p.display == "小明"
    assert Peer("p_x", "").display == "p_x", "没昵称时退到成员 id"
    for bad in (None, {}, {"id": ""}, {"id": 42}, "p_ab12cd", [], {"nick": "小明"}):
        assert Peer.from_dict(bad) is None, f"脏成员数据没被跳过：{bad!r}"

    m = RoomMessage(peer_id="p_a", nick="小明", utt="u1", seq=2, text="原文",
                    lang="zh", is_final=True, srv_ts=99)
    assert m.display == "原文", "本期没翻译 → display 就是原文"
    assert RoomMessage("p_a", "小明", "u1", 1, "原文", translated="译文").display == "译文"
    assert RoomMessage("p_a", "小明", "u1", 1, "  原文  ").text.strip() == "原文"
    print("  Peer / RoomMessage 模型 OK")


if __name__ == "__main__":
    print("test_room_model:")
    test_defaults_when_section_missing()
    test_partial_section_keeps_other_defaults()
    test_not_a_dict_falls_back()
    test_missing_and_dirty_scalars()
    test_out_of_range_numbers()
    test_string_numbers_and_bools_accepted()
    test_room_code_normalization()
    test_nickname_truncated()
    test_backoff_variants()
    test_to_dict_roundtrip()
    test_unavailable_reason()
    test_effective_nickname()
    test_with_overrides_and_state_snapshot()
    test_peer_and_message_models()
    print("ALL PASSED")
