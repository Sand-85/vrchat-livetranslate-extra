"""平台抽象层。

共享代码只从这里拿平台能力，**不要**直接 `import vlt.platform.win` /
`vlt.platform.linux` —— 那是平台独占模块，打包时会被 `--exclude-module` 剔除，
直接 import 会让另一个平台的产物在启动时就 ImportError。

    ✅ from ..platform import IS_WINDOWS, open_path
    ✅ from ..platform import device_backend
    ❌ from ..platform import linux          # 只有 linux.py 自己内部可以这么写

平台模块的加载是**惰性的**：只有真正用到时才 import，所以缺哪一侧的实现都
不会影响「另一侧 + 共享代码」的启动。这也让 Windows 产物的字节码里
根本不含 Linux 实现的任何字样（结构性保证，见 vlt/platform/base.py 的说明）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")

__all__ = [
    "IS_WINDOWS", "IS_LINUX",
    "backend", "device_backend",
    "open_path", "find_cjk_font", "detect_ui_language",
]

_backend: Any = None


def backend() -> Any:
    """返回当前平台的实现模块（`vlt/platform/win.py` 或 `vlt/platform/linux.py`）。

    惰性导入 + 缓存。不支持的平台在这里就报清楚，而不是等某处神秘 AttributeError。
    """
    global _backend
    if _backend is not None:
        return _backend

    if IS_WINDOWS:
        from . import win as mod
    elif IS_LINUX:
        from . import linux as mod
    else:
        raise RuntimeError(
            f"不支持的平台：{sys.platform!r}（本项目只做 Windows 与 Linux）")

    _backend = mod
    return mod


def device_backend() -> Any:
    """设备枚举后端（`query_devices` / `query_loopback_devices`）。"""
    return backend()


def capture_backend() -> Any:
    """采集后端（`open_mic` / `open_loopback` → `AudioSource`）。

    与 `device_backend()` 取的是同一个模块，分开命名只是为了让调用点自解释：
    「我要枚举设备」还是「我要开一路采集」。
    """
    return backend()


class _NullWristOverlay:
    """`overlay.backend: null` 时用的空实现（接口与真后端一致，永远不可用）。"""

    available = False

    def __init__(self, *_a: Any, **_kw: Any) -> None:
        self.cfg = None

    def start(self) -> bool:
        return False

    def update(self, *_a: Any, **_kw: Any) -> None: ...

    def update_entries(self, *_a: Any, **_kw: Any) -> None: ...

    def tick(self) -> None: ...

    def close(self) -> None: ...


def create_wrist_overlay(cfg: Any, config_path: Any = None, dry_run: bool = False) -> Any:
    """手腕屏后端（`start/update/update_entries/tick/close/available` 两端一致）。

    `cfg.backend`：
      * `auto`（默认）→ 按平台选（Windows: pyopenvr；Linux: 自建 OpenXR）
      * `null`        → 禁用（配置层的总开关，不用改 `enabled` 也能关掉）

    ⚠️ 不提供强行指定 openvr/openxr —— 那会让平台隔离破功：
        Windows 产物里会出现 pyopenxr 的字样。真要强制，请在对应平台的
        模块里做，而不是让共享代码引用另一个平台的实现。
    """
    backend_name = str(getattr(cfg, "backend", "auto") or "auto").lower()
    if backend_name == "null":
        return _NullWristOverlay(cfg)
    if backend_name not in ("auto", ""):
        import logging
        logging.getLogger(__name__).warning(
            "[overlay] 配置里的 backend=%r 不支持（只认 auto/null）→ 按 auto 处理", backend_name)
    return backend().create_wrist_overlay(cfg, config_path=config_path, dry_run=dry_run)


def open_audio_out(audio_cfg: dict, on_status: Any) -> Any:
    """译音输出（虚拟声卡 + 写入端）。失败返回 None，由调用方决定怎么提示。

    目前**只有 Linux 需要这个入口**：它得先「运行时声明」一对 PipeWire 节点
    （无配置文件、无重启、不改任何全局状态），再把 PCM 写进去。

    Windows 侧不需要声明 —— 虚拟声卡（VB-Cable / VoiceMeeter）是用户事先装好的设备，
    引擎直接按设备名用 PortAudio 打开即可，那条路径留在 `vlt/engine.py` 里没动。
    """
    return backend().open_audio_out(audio_cfg, on_status)


def open_path(p: str | Path) -> None:
    """用系统默认程序打开一个文件/目录。失败抛异常，由调用方决定怎么提示。

    Windows：`os.startfile`（不产生子进程）；Linux：`xdg-open` 分离启动。
    打不开**不许静默** —— 现有 `gui._on_open_log_folder` 的做法是状态栏 + 日志都留痕。
    """
    target = str(p)
    if IS_WINDOWS:
        import os
        os.startfile(target)                      # type: ignore[attr-defined]
        return
    subprocess.Popen(
        ["xdg-open", target],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,                   # 别跟着我们一起被杀
    )


def find_cjk_font() -> str | None:
    """找一个能画中日韩的字体文件路径；找不到返回 None（由调用方回落）。

    配置里 `overlay.font` 默认不再写死 `C:/Windows/Fonts/msyh.ttc` ——
    那是 Windows 专有路径，在别的机器上会让渲染直接抛 `OSError: cannot open resource`
    （Linux 上 `test_wrap.py` 就是这么挂的）。
    """
    return backend().find_cjk_font()


def detect_ui_language() -> str:
    """探测系统界面语言，返回本项目支持的代码（zh/en/ja/ko/ru）。

    两端口径必须一致：**探测不到 / 不支持的语言一律回落 "en"** ——
    外国用户按英文接待远比按中文合理。

    `vlt/i18n.py: detect_system_language()` 就是转调这里（界面启动时用它定语言），
    所以这条口径只有一处实现：Windows 在 `win.py`（读 LANGID），Linux 在 `linux.py`
    （读 `LC_ALL`/`LC_MESSAGES`/`LANG`）。
    """
    return backend().detect_ui_language()
