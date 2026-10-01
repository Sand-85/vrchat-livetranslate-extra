"""房间文本中继：把各自的 ASR 源文广播给同房的其他人（P0 只做文字，翻译/TTS 只留口子）。

包内模块（**故意不在这里 re-export**：`import vlt.room` 不该顺手把 websockets 拉起来）：
    protocol.py —— 纯函数协议层（零 IO、零网络，好单测）
    model.py    —— RoomConfig / Peer / RoomMessage / RoomState / ConnectionState
    client.py   —— RoomClient：后台线程 + asyncio WebSocket，重连退避、心跳、成员表
    sinks.py    —— Translator / TtsSink 口子（本期只定义，不实现）

用法：`from vlt.room.client import RoomClient`、`from vlt.room.model import RoomConfig`。

⚠️ 接线（engine → publish、远端条目 → 手腕屏、GUI 房间行）是批次 2，本包**不碰**既有模块。
"""

# ⚠️ 下面这行 import 是**必须的**，不是多余的：`sinks` 是「本期只定义口子」的模块，
# 没有任何运行时代码引用它 —— 打包器（PyInstaller）会直接把没人 import 的模块剪掉，
# 而 AppImage 的「磁盘上的 vlt 模块一个不许少」门禁会因此判红（实测：CI 报
# `磁盘上有、包里没有的 vlt 模块：['vlt.room.sinks']`）。它只依赖 typing，
# 不会把 websockets / 网络拉进来，所以放在 `__init__` 里是安全的。
from . import sinks  # noqa: E402,F401
