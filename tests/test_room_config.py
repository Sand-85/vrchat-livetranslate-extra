"""房间配置段验收：`load_config()` 带出 `room` 段 + 脏值交给 `RoomConfig.from_dict` 回落留痕。

## 口径（BRIEF 批次 2a · R2）

`vlt/config.py` **不做字段级校验**，只把 `raw.get("room") or {}` 原样带出来 ——
校验统一走 `vlt/room/model.py::RoomConfig.from_dict`（脏值回落 + 留痕它都做好了）。
两边各做一份校验 = 两份口径 = 用户改配置时永远猜不到哪条生效。

全部离线：只在 `out/` 下写临时 YAML，不碰网络、不碰 GUI、**不动仓库根的 config.yaml**
（那是用户自己的配置，测试改坏了就是真坏了）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

import yaml

# 干净环境（CI）上没有 API key，而 load_config() 默认 require_key=True 会 SystemExit。
# 给一个**拼接出来的假 key**（不触发仓库的凭据扫描）：本文件只验配置读写，跟 key 真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "roomcfgtest0123456789abcdef")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt.config import load_config                              # noqa: E402
from vlt.room.model import DEFAULT_MAX_PEERS, RoomConfig        # noqa: E402

TMP = ROOT / "out" / "room_cfg_test"
EXAMPLE = ROOT / "config.example.yaml"


def write_cfg(name: str, text: str) -> Path:
    TMP.mkdir(parents=True, exist_ok=True)
    p = TMP / name
    p.write_text(text, encoding="utf-8")
    return p


def test_example_template_has_room_section() -> None:
    """模板自检：`config.example.yaml` 必须有 `room:` 段、字段与 `RoomConfig` 对齐、默认关。

    新用户的第一份 config.yaml 就是从这个模板复制出来的 —— 模板缺段，
    界面上就永远没有房间那一行可填。
    """
    raw = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    room = raw.get("room")
    assert isinstance(room, dict), f"★ 模板里没有 room 段（或不是键值对）：{room!r}"
    want = {"enabled", "server_url", "room_code", "nickname", "token", "broadcast_source",
            "show_remote", "max_peers", "reconnect_backoff", "heartbeat_s"}
    assert want <= set(room), f"★ 模板缺字段：{sorted(want - set(room))}"
    known = set(RoomConfig().__dataclass_fields__)
    unknown = set(room) - known
    assert not unknown, f"★ 模板里有 RoomConfig 不认识的字段（写错了没人管）：{sorted(unknown)}"
    assert room["enabled"] is False, "★ 模板默认必须是关（不能影响现有单机用户）"
    assert str(room["server_url"]).startswith("wss://"), room["server_url"]
    assert room["room_code"] == "" and room["nickname"] == "" and room["token"] == ""
    assert room["broadcast_source"] is True and room["show_remote"] is True
    assert room["max_peers"] == 8 and room["heartbeat_s"] == 20
    assert list(room["reconnect_backoff"]) == [2, 5, 10, 30]
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "多人各自跑 VLT 时互相看字幕" in text, "room 段该带一句中文说明它是干什么的"
    # 模板自己必须能过 from_dict 且一行告警都不打（模板带脏值 = 每个新用户都吃一条警告）
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cfg = RoomConfig.from_dict(room)
    assert buf.getvalue() == "", f"★ 模板里的 room 段触发了告警：{buf.getvalue()!r}"
    assert cfg.server_url == room["server_url"] and cfg.max_peers == 8
    assert cfg.reconnect_backoff == (2.0, 5.0, 10.0, 30.0)
    assert cfg.unavailable_reason(), "模板默认值（空房间码）应判为不可用，而不是静默连不上"
    print(f"  模板 room 段：{len(room)} 个字段与 RoomConfig 对齐、默认关、from_dict 零告警 OK")


def test_load_config_reads_room_section() -> None:
    """① `load_config()` 能读出 `room` 段：原样带出（**不做字段级校验**）。"""
    p = write_cfg("with_room.yaml",
                  "session:\n"
                  "  model: qwen3.8-livetranslate-flash-realtime\n"
                  "room:\n"
                  "  enabled: true\n"
                  "  server_url: wss://vlt-room.example.cn/ws\n"
                  "  room_code: test-1234\n"      # 故意写小写 + 连字符：config.py 不该管归一
                  "  nickname: 小明\n"
                  "  token: abc\n"
                  "  broadcast_source: false\n"
                  "  show_remote: true\n"
                  "  max_peers: 4\n"
                  "  reconnect_backoff: [1, 2]\n"
                  "  heartbeat_s: 15\n"
                  "  source_lang: zh\n"
                  "  partial_min_ms: 120\n")
    cfg = load_config(p)
    assert isinstance(cfg.room, dict) and cfg.room, f"★ room 段没带出来：{cfg.room!r}"
    assert cfg.room["enabled"] is True and cfg.room["nickname"] == "小明"
    assert cfg.room["room_code"] == "test-1234", "config.py 只带原始值，归一是 from_dict 的事"
    assert cfg.room["broadcast_source"] is False and cfg.room["max_peers"] == 4
    assert cfg.room["reconnect_backoff"] == [1, 2] and cfg.room["heartbeat_s"] == 15
    # 交给 RoomConfig 才是「生效值」：归一 + 校验都在那边
    rc = RoomConfig.from_dict(cfg.room)
    assert rc.enabled is True and rc.room_code == "TEST1234", rc.room_code
    assert rc.nickname == "小明" and rc.broadcast_source is False
    assert rc.max_peers == 4 and rc.heartbeat_s == 15.0 and rc.partial_min_ms == 120
    assert rc.reconnect_backoff == (1.0, 2.0) and rc.unavailable_reason() is None
    # 其他段不受影响（新字段不许把既有配置挤掉）
    assert cfg.session_base["model"] == "qwen3.8-livetranslate-flash-realtime"
    assert cfg.directions and cfg.text_input["enabled"] is True
    print(f"  ① load_config 读出 room 段 OK（{len(cfg.room)} 键 → RoomConfig 生效值一致）")


def test_load_config_without_room_section() -> None:
    """② 缺 `room` 段时**不崩**且得到空 dict（老用户的 config.yaml 就是这样的）。"""
    p = write_cfg("no_room.yaml",
                  "session:\n"
                  "  model: qwen3.8-livetranslate-flash-realtime\n"
                  "overlay:\n"
                  "  enabled: true\n")
    cfg = load_config(p)                            # 不该抛
    assert cfg.room == {}, f"★ 缺段时应得到空 dict：{cfg.room!r}"
    assert isinstance(cfg.room, dict)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = RoomConfig.from_dict(cfg.room)
    assert buf.getvalue() == "", f"空段不该吵：{buf.getvalue()!r}"
    assert rc.enabled is False, "★ 缺段时必须默认关（不能凭空连上什么房间）"
    assert rc.unavailable_reason(), "缺段时应判为不可用"

    # `room:` 写了个空值 / 写成一行字符串（用户手改最常见的两种坏法）也不许崩
    p2 = write_cfg("room_null.yaml", "room:\n")
    assert load_config(p2).room == {}
    p3 = write_cfg("room_str.yaml", "room: 我不是字典\n")
    got = load_config(p3).room
    assert isinstance(got, dict), f"★ room 段不是字典时 config.py 要保证类型：{got!r}"
    assert RoomConfig.from_dict(load_config(p3).room).enabled is False
    print("  ② 缺 room 段 / 空值 / 写成字符串 → 不崩、得到空 dict、默认关 OK")


def test_dirty_room_values_fall_back_with_trace() -> None:
    """③ 脏值交给 `RoomConfig.from_dict`：回落默认**且留下可读痕迹**。

    房间码 `FULLFULL` 含非法字符 `U`（Crockford Base32 去掉了 I/L/O/U —— 手输时
    U 和 V、0 和 O 分不清）；`max_peers: "abc"` 根本不是数字。
    这两种都必须：一行 `[room] ⚠️ ...` 说清哪个键、什么值、回落成了什么，然后继续跑。
    """
    dirty = {"enabled": True, "server_url": "wss://x/ws", "room_code": "FULLFULL",  # YAML 1.1 把裸 yes 解成 True
             "max_peers": "abc", "heartbeat_s": -5, "reconnect_backoff": "2, xx, 5"}
    p = write_cfg("dirty.yaml",
                  "room:\n"
                  "  enabled: yes\n"
                  "  server_url: wss://x/ws\n"
                  '  room_code: "FULLFULL"\n'
                  '  max_peers: "abc"\n'
                  "  heartbeat_s: -5\n"
                  '  reconnect_backoff: "2, xx, 5"\n')
    cfg = load_config(p)
    assert cfg.room == dirty, f"★ config.py 不该偷偷改值（校验是 from_dict 的事）：{cfg.room}"
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = RoomConfig.from_dict(cfg.room)
    trace = buf.getvalue()
    assert rc.room_code == "", f"★ 非法房间码没清空：{rc.room_code!r}"
    assert rc.max_peers == DEFAULT_MAX_PEERS == 8, f"★ max_peers 没回落：{rc.max_peers}"
    assert rc.enabled is True, "'yes' 应当认成 true"
    assert rc.heartbeat_s == 20.0, f"★ 越界 heartbeat_s 没回落：{rc.heartbeat_s}"
    assert rc.reconnect_backoff == (2.0, 5.0), f"退避表脏项该丢掉：{rc.reconnect_backoff}"
    # 痕迹必须可读：带上键名、原值、回落结果，且走统一的 [room] 前缀
    for want in ("[room]", "⚠️", "room_code", "FULLFULL", "max_peers", "abc", "回落"):
        assert want in trace, f"★ 痕迹里缺 {want!r}：\n{trace}"
    assert trace.count("[room] ⚠️") >= 2, f"两个脏键至少两行告警：\n{trace}"
    # 非法房间码 → 判为不可用，且原因人能读懂（不许静默连不上）
    reason = rc.unavailable_reason()
    assert reason and "room_code" in reason, f"★ 不可用原因不可读：{reason!r}"
    print("  ③ 脏值回落 + 留痕 OK：")
    for line in trace.strip().splitlines():
        print("     " + line)
    print(f"     → room_code={rc.room_code!r} max_peers={rc.max_peers} "
          f"heartbeat_s={rc.heartbeat_s} backoff={rc.reconnect_backoff}")


def test_room_defaults_do_not_disturb_existing_sections() -> None:
    """新字段不许改变既有 `AppConfig` 的形状（gui/engine 都在按位置之外的关键字取用）。"""
    p = write_cfg("full_example.yaml", EXAMPLE.read_text(encoding="utf-8"))
    cfg = load_config(p)
    for name in ("session_base", "directions", "chatbox", "merger", "overlay", "output",
                 "ui", "text_input", "room"):
        assert hasattr(cfg, name), f"★ AppConfig 少了字段 {name}"
    assert cfg.overlay.get("enabled") is True and cfg.output["audio"]["sample_rate"] == 48000
    assert cfg.direction("mine").target_lang == "en"
    assert set(cfg.room) >= {"enabled", "server_url", "room_code"}, cfg.room
    print(f"  AppConfig 既有字段完好 + room 段随模板带出 OK（{len(cfg.room)} 键）")


if __name__ == "__main__":
    print("test_room_config:")
    test_example_template_has_room_section()
    test_load_config_reads_room_section()
    test_load_config_without_room_section()
    test_dirty_room_values_fall_back_with_trace()
    test_room_defaults_do_not_disturb_existing_sections()
    print("ALL PASSED")
