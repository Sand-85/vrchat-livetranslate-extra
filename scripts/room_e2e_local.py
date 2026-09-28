#!/usr/bin/env python3
"""房间文本中继 · 本机无头端到端验收（**唯一**的成功标记是 `ROOM_E2E_OK`）。

用法：
    .venv/Scripts/python.exe scripts/room_e2e_local.py

## 这个脚本存在的理由

`tests/test_room_*.py` 是一堆单元/组件断言，跑绿了也说明不了「A 说的话 B 真能看到」
这条链路是通的。这里只验一件事，但验的是**整条**：

    本机假中继（真 WebSocket，127.0.0.1 随机端口）
      ← RoomClient A（publish 一串 partial + 一个 final）
      → RoomClient B（on_message 收到的 final 文本，必须和 A 发的一字不差）

全程离线：不连百炼、不连 Cloudflare、不需要 API key、不碰麦克风。
带死代理跑也必须绿（客户端不写任何代理逻辑，localhost 天然绕过）。

## 判定口径

* 成功 → stdout 出现 `ROOM_E2E_OK`，退出码 0
* 失败 → stdout 出现 `ROOM_E2E_FAIL: <原因>`，退出码非 0

**只认标记，不要靠人读日志**：日志里会有 `[room]` 的正常噪声。

假中继直接复用 `tests/test_room_client.py` 里的 `FakeRelay` —— 端到端脚本和单测
必须是**同一套**服务端行为，否则两边各自跑绿也说明不了什么。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))       # 复用 FakeRelay / Recorder / make_config

from test_room_client import FakeRelay, Recorder, make_config        # noqa: E402
from vlt.room.client import RoomClient                               # noqa: E402
from vlt.room.model import ConnectionState                           # noqa: E402

OK_MARKER = "ROOM_E2E_OK"
FAIL_MARKER = "ROOM_E2E_FAIL"

# A 说的这句话会被切成 4 帧发出去：3 个 partial（全量快照，不是 diff）+ 1 个 final
UTTERANCE = "u1"
PARTIALS = ["我", "我在", "我在测试"]
FINAL_TEXT = "我在测试翻译功能"
TIMEOUT_S = 20.0


def step(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def wait_until(pred, what: str, timeout: float = TIMEOUT_S) -> None:
    """轮询等条件成立。超时就抛 —— 卡住不说清在等什么，是最难查的那种失败。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.03)
    raise TimeoutError(f"等了 {timeout}s 仍未满足：{what}")


def main() -> int:
    relay = FakeRelay()
    relay.start(timeout=15.0)
    step(f"假中继已起：{relay.url}（房间 {relay.room_code}）")

    rec_a, rec_b = Recorder(), Recorder()
    a = RoomClient(make_config(relay, "小明"), rec_a.on_message, rec_a.on_status)
    b = RoomClient(make_config(relay, "阿华"), rec_b.on_message, rec_b.on_status)
    leftover: list[str] = []
    try:
        a.start()
        b.start()
        step("两端 RoomClient 已启动，等进房…")
        wait_until(lambda: a.state().conn is ConnectionState.ONLINE,
                   f"A 上线（状态：{a.state().summary}；最后错误：{a.state().last_error!r}）")
        wait_until(lambda: b.state().conn is ConnectionState.ONLINE,
                   f"B 上线（状态：{b.state().summary}；最后错误：{b.state().last_error!r}）")
        wait_until(lambda: a.state().peer_count == 2 and b.state().peer_count == 2,
                   f"两端成员表都该是 2 人（A={a.state().peer_count} B={b.state().peer_count}）")
        step(f"进房成功：A={a.state().me}、B={b.state().me}，互相看得见")

        # ---- A 说一句：partial 逐字长出来，最后定稿 ----
        for i, text in enumerate(PARTIALS, start=1):
            a.publish(UTTERANCE, i, text, is_final=False)
        a.publish(UTTERANCE, len(PARTIALS) + 1, FINAL_TEXT, is_final=True)
        step(f"A 已发出 {len(PARTIALS)} 个 partial + 1 个 final：{FINAL_TEXT!r}")

        # ---- B 必须收到一模一样的 final ----
        wait_until(lambda: rec_b.final_map().get(UTTERANCE) is not None,
                   f"B 应收到 utt={UTTERANCE} 的 final（B 收到的帧：{rec_b.texts()}）")
        got = rec_b.final_map()[UTTERANCE]
        if got != FINAL_TEXT:
            raise AssertionError(f"★ B 收到的 final 和 A 发的不一致：A={FINAL_TEXT!r} B={got!r}")
        step(f"B 收到 final：{got!r}（与 A 一致）")

        finals = rec_b.finals()
        if len(finals) != 1:
            raise AssertionError(f"final 应恰好 1 条，实际 {len(finals)} 条：{rec_b.texts()}")
        speaker = finals[0]
        if speaker.peer_id != a.state().me:
            raise AssertionError(f"说话人对不上：帧里 src={speaker.peer_id}，A 的 id={a.state().me}")
        if speaker.nick != "小明":
            raise AssertionError(f"昵称对不上：帧里 nick={speaker.nick!r}，应为 '小明'")
        step(f"说话人归属正确：{speaker.nick}({speaker.peer_id})，seq={speaker.seq}，"
             f"lang={speaker.lang}，srv_ts={'有' if speaker.srv_ts else '无'}")

        # ---- A 不该收到自己的回声（服务端不回发给说话人，客户端因此不用做回声消除）----
        if rec_a.msgs:
            raise AssertionError(f"★ A 收到了回声（服务端不该回发给说话人）：{rec_a.texts()}")
        partials_seen = [m.text for m in rec_b.msgs if not m.is_final]
        if not partials_seen:
            raise AssertionError("B 一个 partial 都没收到（扇出漏了，只剩 final 到得了）")
        step(f"B 看到的 partial 轨迹：{partials_seen}")

        # ---- final 必须被 ack：否则回放表一直挂着，重连会重发 ----
        wait_until(lambda: a.state().pending_replay == 0,
                   f"final 应被服务端 ack（回放表还挂着 {a.state().pending_replay} 条）")
        step("final 已被 ack，回放表清空")

        for name, err in (("A", a.state().last_error), ("B", b.state().last_error)):
            if err:
                raise AssertionError(f"{name} 全程出现了错误：{err!r}")
        for name, rec in (("A", a.state().reconnects), ("B", b.state().reconnects)):
            if rec:
                raise AssertionError(f"{name} 不该重连，实际重连 {rec} 次")
        step(f"统计：A 发 {a.state().sent_frames} 帧 / 收 {a.state().recv_frames} 帧，"
             f"B 发 {b.state().sent_frames} 帧 / 收 {b.state().recv_frames} 帧")
    finally:
        # 无论成败都要收干净：线程和假中继都不能留在进程里（留了就是僵尸）
        a.stop(timeout=5.0)
        b.stop(timeout=5.0)
        relay.stop(timeout=5.0)
        leftover = [n for n, t in (("A", a._thread), ("B", b._thread))
                    if t is not None and t.is_alive()]
        # 不在 finally 里 raise：那会盖掉正在传播的原始异常，真原因就查不到了
        step("两端线程与假中继都已收干净" if not leftover
             else f"⚠️ stop() 之后线程还活着：{leftover}")
    if leftover:
        raise AssertionError(f"★ stop() 之后线程还活着：{leftover}")

    print(OK_MARKER, flush=True)
    return 0


if __name__ == "__main__":
    # ⚠️ 别写成 `sys.exit(main())`：SystemExit(0) 会被下面的 except 抓住，
    # 于是成功之后又多打一行 ROOM_E2E_FAIL —— 两个标记同时出现就没法只认标记了。
    exit_code = 1
    try:
        exit_code = main()
    except BaseException as exc:                       # noqa: BLE001
        # KeyboardInterrupt 也要留标记：控制器只看标记和退出码，不看栈
        print(f"{FAIL_MARKER}: {type(exc).__name__}: {exc}", flush=True)
        exit_code = 1
    sys.exit(exit_code)
