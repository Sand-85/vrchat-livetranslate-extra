"""房间按钮 · 真机联网端到端（真 Tk 窗口 + 真 RoomClient + 真 WebSocket）。

**作用**：不验「能连上服务器」本身，验的是**用户点下去会发生什么**：
    点「连接房间」→ 真的连上线上房间服务 → 按钮自己变成「断开连接」
    再点一下 → 真的断开 → 按钮自己变回「连接房间」

**需要的环境**：
- **真实桌面会话**（要能开 Tk 窗口）。
- **联网可达线上房间服务**（`wss://vlt-room.kcm-nixi.cn/ws`）：断网 / 服务不可达时，
  脚本会在超时后如实报「等不到」并以失败退出，**不是 hang**。
- 不需要麦克风/头显。

**跑法**（在仓库根目录）：
    ./.venv/Scripts/python.exe scripts/verify/verify_room_button_live.py

**会不会写盘**：只写**临时目录**（`tempfile.mkdtemp` 下的 `config.yaml`），
**绝不碰真实 `config.yaml`**。

⚠️ 这是**联网**验收，不是离线测试：别把它塞进 CI（CI 环境没有真实桌面，也没有到该服务的网络）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]        # scripts/verify/ -> 仓库根
sys.path.insert(0, str(REPO))
os.environ.pop("DASHSCOPE_API_KEY", None)

_tmp = Path(tempfile.mkdtemp(prefix="vlt-room-live-"))
CFG = _tmp / "config.yaml"
CFG.write_text("session:\n  api_key: ''\nui:\n  lang: zh\n", encoding="utf-8", newline="\n")

import vlt.config as _config        # noqa: E402
import vlt.gui as gui_mod           # noqa: E402
_config.DEFAULT_CONFIG = CFG
gui_mod.DEFAULT_CONFIG = CFG

import vlt.i18n as _i18n            # noqa: E402
_i18n.detect_system_language = lambda: "zh"

from vlt.gui import TranslationGUI  # noqa: E402
from vlt.i18n import t              # noqa: E402

ROOM_CODE = "TESTTEST"
gui = TranslationGUI()
root = gui._root
gui._room_code_var.set(ROOM_CODE)
gui._room_nick_var.set("vrc-check")
print(f"房间码={ROOM_CODE} 昵称=vrc-check  服务端={gui._room_cfg.server_url!r}", flush=True)


def pump(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update()
        time.sleep(0.05)


results = {}


def check(tag, cond, detail=""):
    results[tag] = bool(cond)
    print(f"  [{'OK ' if cond else '★FAIL'}] {tag}{(' — ' + detail) if detail else ''}", flush=True)


def wait_for(pred, what, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        root.update()
        if pred():
            return True
        time.sleep(0.1)
    print(f"  ★ 等不到：{what}", flush=True)
    return False


try:
    print("=== ① 点「连接房间」→ 真连线上服务 ===", flush=True)
    check("点之前是「连接房间」", gui._room_btn.cget("text") == t("连接房间"),
          repr(gui._room_btn.cget("text")))
    gui._on_room_button()
    check("点了之后建了 RoomClient", gui._room is not None)
    seen = []
    end = time.monotonic() + 25
    while time.monotonic() < end:
        root.update()
        st = None
        if gui._room is not None:
            try:
                st = gui._room.state()
            except Exception:
                st = None
        if st is not None and (not seen or seen[-1] != st.conn.value):
            seen.append(st.conn.value)
            print(f"    连接态 → {st.conn.value}（{st.peer_count} 人）", flush=True)
        if gui._room_btn.cget("text") == t("断开连接"):
            break
        time.sleep(0.1)
    print(f"    观察到的连接态序列：{seen}", flush=True)
    check("按钮自己变成「断开连接」", gui._room_btn.cget("text") == t("断开连接"),
          repr(gui._room_btn.cget("text")))
    check("状态标签显示已连接", t("已连接") in str(gui._room_status.cget("text")),
          repr(gui._room_status.cget("text")))
    check("真的走到了 online", "online" in seen, str(seen))

    print("=== ② 再点一下 → 真断开 ===", flush=True)
    gui._on_room_button()
    ok = wait_for(lambda: gui._room is None and gui._room_btn.cget("text") == t("连接房间"),
                  "断开后按钮变回「连接房间」")
    check("client 已释放", gui._room is None)
    check("按钮变回「连接房间」", gui._room_btn.cget("text") == t("连接房间"),
          repr(gui._room_btn.cget("text")))
    check("整个断开流程在 20s 内完成", ok)
finally:
    print("=== 汇总 ===", flush=True)
    for k, v in results.items():
        print(f"    {'OK  ' if v else '★FAIL'} {k}")
    print("DONE" if results and all(results.values()) else "SOME FAILED")
    try:
        gui._stop_room()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass
