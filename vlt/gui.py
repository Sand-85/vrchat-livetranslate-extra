"""Tkinter 图形界面：聊天气泡视图 + 开关 + 语言镜像 + 双向同时。
用法：
  python -m vlt.gui                    # 启动界面
  python -m vlt.gui --self-test        # 自动化验收（单方向，不起窗口）
  python -m vlt.gui --self-test-dual   # 双向同时验收（两个 PCM 驱动两个引擎）
"""
from __future__ import annotations

from .gui_voicelab import VoicelabMixin
from . import __version__ as __version__
from .config import Direction as Direction
from .config import _as_str_map as _as_str_map
from .config import load_api_key as load_api_key
from .config_io import _yaml_scalar as _yaml_scalar
from .config_io import _yaml_set_mapping as _yaml_set_mapping
from .devices import enumerate_audio_out_devices as enumerate_audio_out_devices
from .devices import enumerate_loopback_devices as enumerate_loopback_devices
from .devices import enumerate_mic_devices as enumerate_mic_devices
from .devices import format_device_display as format_device_display
from .engine import Engine as Engine
from .engine import EngineEvents as EngineEvents
from .engine import INPUT_GATE_MAX_DB as INPUT_GATE_MAX_DB
from .engine import INPUT_GATE_MIN_DB as INPUT_GATE_MIN_DB
from .engine import LEVEL_FLOOR_DB as LEVEL_FLOOR_DB
from .engine import input_gate_settings as input_gate_settings
from .glossary_text import _glossary_line_issues as _glossary_line_issues
from .glossary_text import _glossary_to_lines as _glossary_to_lines
from .glossary_text import _parse_glossary_lines as _parse_glossary_lines
from .level_probe import LevelProbe as LevelProbe
from .output.overlay import OverlayConfig as OverlayConfig
from .output.overlay import resolve_offset as resolve_offset
from .paths import BUNDLE_DIR as BUNDLE_DIR
from .platform import IS_WINDOWS as IS_WINDOWS
from .room.client import RoomClient as RoomClient
from .room.model import ConnectionState as ConnectionState
from .room.model import RoomConfig as RoomConfig
from .room.model import RoomMessage as RoomMessage
from .room.protocol import new_room_code as new_room_code
from .room.publisher import SourcePublisher as SourcePublisher
from .room.publisher import should_publish as should_publish
from .ui_state import current_key_slot as current_key_slot
from .ui_state import provider as provider
from .ui_state import refresh_api_key_in_cfg as refresh_api_key_in_cfg
from .ui_text import VOICE_PREVIEW_MODEL as VOICE_PREVIEW_MODEL
from .ui_text import VOICE_PREVIEW_TEXT as VOICE_PREVIEW_TEXT
from .ui_text import _Bubble as _Bubble
from .ui_text import _DownloadCancelled as _DownloadCancelled
from .ui_text import _dir_writable as _dir_writable
from .ui_text import _is_unsupported_voice_err as _is_unsupported_voice_err
from .ui_text import _persist_provider as _persist_provider
from .ui_text import _provider_choices as _provider_choices
from .ui_text import _sponsor_qr_specs as _sponsor_qr_specs
from .ui_text import updater_env as updater_env
from .ui_theme import ACCENT_HOVER as ACCENT_HOVER
from .ui_theme import BG as BG
from .ui_theme import CLOSE_WAIT_STOP_S as CLOSE_WAIT_STOP_S
from .ui_theme import COLOR_ERROR as COLOR_ERROR
from .ui_theme import COLOR_META as COLOR_META
from .ui_theme import COLOR_MINE as COLOR_MINE
from .ui_theme import COLOR_OK as COLOR_OK
from .ui_theme import COLOR_SRC_MINE as COLOR_SRC_MINE
from .ui_theme import COLOR_SRC_THEIRS as COLOR_SRC_THEIRS
from .ui_theme import COLOR_TEXT as COLOR_TEXT
from .ui_theme import COLOR_THEIRS as COLOR_THEIRS
from .ui_theme import COLOR_WARN as COLOR_WARN
from .ui_theme import FONT_MAX as FONT_MAX
from .ui_theme import FONT_MIN as FONT_MIN
from .ui_theme import MAX_BUBBLES as MAX_BUBBLES
from .ui_theme import PANEL as PANEL
from .ui_theme import PANEL_H_MAX as PANEL_H_MAX
from .ui_theme import PANEL_H_MIN as PANEL_H_MIN
from .ui_theme import PANEL_W_MAX as PANEL_W_MAX
from .ui_theme import PANEL_W_MIN as PANEL_W_MIN
from .ui_theme import SETTINGS_CHROME_H as SETTINGS_CHROME_H
from .ui_theme import SETTINGS_MAX_H as SETTINGS_MAX_H
from .ui_theme import SPONSORS as SPONSORS
from .ui_theme import SPONSOR_QR_SIZE as SPONSOR_QR_SIZE
from .ui_theme import SPONSOR_URL as SPONSOR_URL
from .ui_theme import SRC_FONT_MIN as SRC_FONT_MIN
from .ui_theme import STOP_WAIT_S as STOP_WAIT_S
from .ui_theme import SURFACE_HOVER as SURFACE_HOVER
from .ui_theme import TAB_INSET_X as TAB_INSET_X
from .ui_theme import TEXT_DIM as TEXT_DIM
from .ui_theme import TEXT_MUTED as TEXT_MUTED
from .ui_tk import _char_width_for as _char_width_for
from .ui_tk import _combo_width as _combo_width
from .ui_tk import _int_fmt as _int_fmt
from .ui_tk import combo_values as combo_values
from .ui_tk import round_rect as round_rect
from .voices import REALTIME_VOICES as REALTIME_VOICES
from datetime import datetime as datetime
from tkinter import font as font
import subprocess as subprocess
import time as time
import tkinter as tkinter
import webbrowser as webbrowser
import yaml as yaml

from .ui_text import _play_pcm_local
from tkinter import messagebox as messagebox   # 门面重导出（tests 打桩 vlt.gui.messagebox）
import argparse, os, queue, re, sys, threading, tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Any
from . import crashlog, i18n, update_check
from .config import DEFAULT_CONFIG, load_config
from .config_io import _fmt_scalar, _yaml_set_in_text, _yaml_set_or_create, _write_config_text
from .i18n import t
from .paths import APP_DIR
from . import platform
# ── 重导出（保持 from vlt.gui import X 向后兼容）──
from . import ui_tk
from .ui_text import (SOURCE_LANGS, TARGET_LANGS, _lang_key, _lang_label,
    _source_name, _target_name)
from .ui_theme import (QIANWEN_SIGNUP_URL as QIANWEN_SIGNUP_URL, SETTINGS_MIN_H, SETTINGS_WIDTH)
from .ui_tk import apply_theme
from .ui_state import room_status_text, save_room_cfg
from .gui_selftest import run_self_test
# 显式重导出（`X as X`）：这些名字 gui.py 自己不再用，但 `tests/` 与 `run_gui.py` 仍从
# `vlt.gui` 取。不写 `as` 的话 ruff 的 F401 会把它们当未用导入删掉（CI 门禁阻断）。
from . import gui_room, gui_voice, gui_audio, gui_update, gui_settings
from . import gui_desktop, gui_chat, gui_engine, gui_layout
from .gui_room import RoomCtx; from .gui_voice import VoiceCtx
from .gui_audio import AudioCtx; from .gui_desktop import DesktopCtx
from .gui_chat import ChatCtx; from .gui_engine import EngineCtx
ROOT = APP_DIR
FONT = ui_tk.FONT; FONT_SMALL = ui_tk.FONT_SMALL; FONT_META = ui_tk.FONT_META
def _yaml_write(p: Path, fn, *, err="保存"):
    if not p.exists(): return
    try: text = p.read_text(encoding="utf-8"); text = fn(text); _write_config_text(p, text)
    except Exception as exc: print(f"[gui] {err}失败：{exc}", flush=True)

FONT_UI = ui_tk.FONT_UI; FONT_STATUS = ui_tk.FONT_STATUS
FONT_BOLD_SM = ui_tk.FONT_BOLD_SM; FONT_BOLD_MD = ui_tk.FONT_BOLD_MD; FONT_BOLD_LG = ui_tk.FONT_BOLD_LG
#: 一看就知道是测试入口的文件名（`python tests/test_x.py` 那条路走的是 `test_` 前缀）
_TEST_ENTRIES = ("run_tests.py", "pytest", "pytest.exe", "py.test")
def _is_test_process() -> bool:
    """当前进程是不是**测试/自检**进程 —— 这类进程绝不构造麦克风代理。

    代理会真开虚拟声卡输出流与麦克风（Windows 走 PortAudio；Linux 走 PipeWire 节点/子进程）。
    跑用例的纪律是「绝不碰真实音频设备」（见 tests/test_micproxy.py 顶部的打桩说明），
    所以在 GUI 构造阶段先问一句。
    判据只看**进程入口 / 已导入的测试框架 / 环境变量**，不靠「有没有显示器」这类
    间接信号 —— 间接信号在 CI 的 xvfb 下会误判成「真用户」。
    """
    if os.environ.get("PYTEST_CURRENT_TEST"): return True
    if "pytest" in sys.modules or "_pytest" in sys.modules: return True
    argv0 = (sys.argv[0] if sys.argv else "") or ""
    entry = Path(argv0).name
    return (entry.startswith("test_") or entry in _TEST_ENTRIES
            or "unittest" in argv0 or "pytest" in argv0)
# gui.py ↔ EngineCtx 双向同步的属性名
_GATE_HINT = "只有响度超过门限的声音才会被翻译；改完立刻生效（勾选「启用」后这里显示实时电平）"
_E2G = ["engines", "engine_dirs", "specs", "sinks", "pending_starts", "current",
    "auto_scroll", "closing", "overlay_out", "desktop_out", "desktop_dragging",
    "desktop_save_job", "desktop_alpha_touched", "start_job"]
_G2E = ["power_btn", "direction_var", "chatbox_var", "overlay_var",
    "desktop_var", "vmic_var", "desktop_alpha_var", "desktop_alpha_lbl",
    "desktop_font_var", "desktop_srcfont_var", "desktop_w_var", "desktop_h_var", "desktop_drag_btn"]
_DELEGATE_MAP = {
    "_build_settings_dialog": gui_settings.build_settings_dialog,
    "_sync_settings_pages": gui_settings.sync_settings_pages,
    "_on_settings_wheel": gui_settings.on_settings_wheel,
    "_size_settings_window": gui_settings.size_settings_window,
    "_build_settings_general": gui_settings.build_settings_general,
    "_on_save_osc_port": gui_settings.on_save_osc_port,
    "_build_provider_section": gui_settings.build_provider_section,
    "_on_provider_change": gui_settings.on_provider_change,
    "_on_save_provider": gui_settings.on_save_provider,
    "_build_settings_audio": gui_settings.build_settings_audio,
    "_build_settings_wrist": gui_settings.build_settings_wrist,
    "_build_settings_desktop": gui_settings.build_settings_desktop,
    "_build_settings_glossary": gui_settings.build_settings_glossary,
    "_build_settings_room": gui_settings.build_settings_room,
    "_build_settings_about": gui_settings.build_settings_about,
    "_on_export_logs": gui_settings.on_export_logs,
    "_on_open_log_folder": gui_settings.on_open_log_folder,
    "_select_settings_page": gui_settings.select_settings_page,
    "_open_settings": gui_settings.open_settings,
    "_close_settings": gui_settings.close_settings,
    "_on_ui_lang_change": gui_settings.on_ui_lang_change,
    "_save_ui_language": gui_settings.save_ui_language,
    "_refresh_log_info": gui_settings.refresh_log_info,
    "_open_kofi": gui_update.open_kofi,
    "_open_sponsor": gui_update.open_sponsor,
    "_build_sponsor_dialog": gui_update.build_sponsor_dialog,
    "_close_sponsor": gui_update.close_sponsor,
    "_schedule_update_check": gui_update.schedule_update_check,
    "_run_update_check": gui_update.run_update_check,
    "_on_update_check_result": gui_update.on_update_check_result,
    "_show_update_dialog": gui_update.show_update_dialog,
    "_close_update_dialog": gui_update.close_update_dialog,
    "_open_release_page": gui_update.open_release_page,
    "_on_update_ignore": gui_update.on_update_ignore,
    "_on_update_later": gui_update.on_update_later,
    "_on_update_now": gui_update.on_update_now,
    "_show_download_window": gui_update.show_download_window,
    "_close_download_window": gui_update.close_download_window,
    "_open_download_page": gui_update.open_download_page,
    "_on_download_window_close": gui_update.on_download_window_close,
    "_start_download": gui_update.start_download,
    "_on_download_progress": gui_update.on_download_progress,
    "_on_download_done": gui_update.on_download_done,
    "_enter_download_done_state": gui_update.enter_download_done_state,
    "_on_download_error": gui_update.on_download_error,
    "_on_reload_clicked": gui_update.on_reload_clicked,
    "_on_postpone_clicked": gui_update.on_postpone_clicked,
    "_mark_update_pending": gui_update.mark_update_pending,
    "_relaunch_appimage": gui_update.relaunch_appimage,
    "_maybe_replace_on_exit": gui_update.maybe_replace_on_exit,
    "_offer_manual_download": gui_update.offer_manual_download,
    "_check_pending_update_at_startup": gui_update.check_pending_update_at_startup,
    "_schedule_version_changed_hint": gui_update.schedule_version_changed_hint,
    "_show_version_changed_hint": gui_update.show_version_changed_hint,
    "_close_updated_hint": gui_update.close_updated_hint,
    "_refresh_key_status": gui_update.refresh_key_status,
    "_open_qianwen_signup": gui_update.open_qianwen_signup,
    "_refresh_api_key_in_cfg": gui_update.refresh_api_key_in_cfg,
    "_on_save_key": gui_update.on_save_key,
    "_on_clear_key": gui_update.on_clear_key,
    "_build_ui": gui_layout.build_ui,
    "_fit_window_width": gui_layout.fit_window_width,
    "_set_window_icon": gui_layout.set_window_icon,
    "_apply_dark_titlebar": gui_layout._apply_dark_titlebar,
    "_divider": gui_layout._divider, "_vsep": gui_layout._vsep,
    "_attach_edit_menu": gui_layout._attach_edit_menu,
    "_build_controls": gui_layout.build_controls,
    "_build_output_row": gui_layout.build_output_row,
    "_indicator_kw": gui_layout._indicator_kw,
}


class TranslationGUI(VoicelabMixin):
    """主界面。headless=True 时不创建 Tk 窗口。"""
    def _save_audio_flag(self) -> None:
        want = bool(self._vmic_var.get())
        self._refresh_voice_mode_hint()          # 我们的：开关变了刷新音源提示
        _yaml_write(DEFAULT_CONFIG, lambda t: _yaml_set_in_text(t, ["output", "audio", "enabled"], _fmt_scalar(want)), err="保存译音开关")

    def _save_audio_mode(self, mode: str) -> None:
        """把译音音源写回 config.yaml（output.audio.mode），下次启动沿用。"""
        p = DEFAULT_CONFIG
        if not p.exists():
            return
        try:
            text = p.read_text(encoding="utf-8")
            text = _yaml_set_in_text(text, ["output", "audio", "mode"], _fmt_scalar(mode))
            _write_config_text(p, text)
            print(f"[gui] 译音音源 → {mode}（已写入 config.yaml）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 保存译音音源失败：{exc}", flush=True)
    def __init__(self, headless: bool = False) -> None:
        self._headless = headless; self._q: queue.Queue = queue.Queue()
        self._engines: list = []; self._engine_dirs: list = []; self._specs: list = []; self._sinks: set = set(); self._pending_starts = 0
        self._overlay_out: Any = None; self._desktop_out: Any = None; self._desktop_dragging = False
        self._desktop_save_job: str | None = None; self._desktop_alpha_touched = False; self._start_job: str | None = None
        self._current: dict = {}; self._auto_scroll = True; self._stop_done_evt = threading.Event(); self._stop_done_evt.set()
        self._voice_ctx = VoiceCtx(); self._bubbles: list = []; self._stats: dict = {}; self._canvas_w = 600
        self._relayout_job: str | None = None; self._last_status_level = ""
        self._sponsor_win: tk.Toplevel | None = None; self._sponsor_imgs: list = []; self._sponsor_qr_labels: list = []
        self._update_check_done = False; self._update_check_running = False; self._update_snoozed = False
        self._update_win: tk.Toplevel | None = None; self._update_check_job: str | None = None
        self._update_checker = update_check.check_for_updates; self._update_downloader = update_check.download_and_verify
        self._dl_win: tk.Toplevel | None = None; self._dl_bar: ttk.Progressbar | None = None
        self._dl_text: ttk.Label | None = None; self._dl_note: ttk.Label | None = None; self._dl_btn_frame: ttk.Frame | None = None
        self._dl_info: update_check.ReleaseInfo | None = None; self._dl_new_exe: Path | None = None
        self._dl_cancel: threading.Event | None = None; self._dl_downloading = False
        self._dl_throttle_s = 0.1; self._dl_last_push = 0.0; self._dl_reload_btn: ttk.Button | None = None
        self._dl_postpone_btn: ttk.Button | None = None; self._dl_link: tk.Label | None = None
        self._update_pending_exit = False; self._update_pending_info: update_check.ReleaseInfo | None = None
        self._reload_started = False; self._closing = False
        self._updated_hint_win: tk.Toplevel | None = None; self._updated_hint_job: str | None = None
        self._settings_win: tk.Toplevel | None = None; self._settings_nb: ttk.Notebook | None = None
        self._settings_pages: list = []; self._settings_size = (SETTINGS_WIDTH, SETTINGS_MIN_H); self._settings_ctx = None
        self._device_scan_pending = False
        from .engine import LEVEL_FLOOR_DB
        self._gate_level_canvas: tk.Canvas | None = None; self._gate_level_lbl: ttk.Label | None = None
        self._gate_level_hold = LEVEL_FLOOR_DB; self._gate_level_tick = 0; self._gate_probe: Any = None; self._gate_save_job: str | None = None
        self._gate_hold_ms = 500.0; self._gate_preroll_ms = 250; self._audio_ctx = AudioCtx(); self._proxy = None
        # 设备名唯一真源：只由设备扫描（gui_audio.on_device_scan_result）写入。
        # ⚠️ 曾经这里另存一份 `_mic_names/_loopback_names/_audio_out_names` 镜像、并在
        #    `gui_audio.sync_from_gui` 里回写本字典 —— 而启动扫描走的是同步路径、
        #    不经过 `_on_device_scan_result`，镜像永远是空的，于是每次同步都把刚扫到的
        #    设备名清空，`on_device_change` 按下标取名字失败 → `mic_device` 存成空串
        #    （表现：改麦克风下拉无效、永远用系统默认）。别再引入第二份名字表。
        self._names_holder: dict[str, list[str]] = {"mic": [], "loop": [], "out": []}
        self._scan_holder = {"pending": False, "names": self._names_holder}
        self._gate_holder = {"probe": self._gate_probe, "save_job": self._gate_save_job,
            "level_hold": self._gate_level_hold, "hold_ms": self._gate_hold_ms,
            "preroll_ms": self._gate_preroll_ms, "engines": []}
        self._desktop_ctx = DesktopCtx(); self._chat_ctx = ChatCtx(); self._engine_ctx = EngineCtx()
        self._power_state = "idle"      # 开始/停止单按钮态："idle" | "running" | "stopping"
        self._wire_desktop_ctx()
        # 本账号的自定义音色（设计族 + 复刻族）：后台拉一次，用于**打字译音下拉**与显示名
        self._tts_custom: list = []
        self._cfg = load_config(require_key=False)
        _sl = (self._cfg.ui or {}).get("lang"); i18n.set_language(_sl if _sl else i18n.detect_system_language())
        mine = self._cfg.directions.get("mine")
        self._lang_pair = {"source": mine.source_lang if mine else "zh",
                           "target": (mine.target_lang if mine else "en") or "en"}
        from .room.model import RoomConfig; from .room.publisher import SourcePublisher
        self._room: RoomClient | None = None
        self._room_cfg = RoomConfig.from_dict(self._cfg.room); self._publisher = SourcePublisher()
        self._room_status_next = 0.0; self._room_ctx = RoomCtx()
        gui_engine.bind_gui_callbacks(self._engine_ctx, self)
        if not headless:
            self._build_ui()
            # 代理与翻译解耦：程序一起来就工作（见 micproxy 模块头「生命周期」）
            self._start_proxy()
            # 按钮默认态是 NORMAL，不刷一次的话「无代理」时它看起来还能点
            self._refresh_voice_mode_btn()
    def __getattr__(self, name):
        """薄壳委托：把 ~60 个简单方法代理到子模块。"""
        fn = _DELEGATE_MAP.get(name)
        if fn is not None:
            def _d(*a, _fn=fn, _s=self, **kw): return _fn(_s, *a, **kw)
            return _d
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")
    # ── gui_voice 显式委托（函数签名需要 cfg/ctx 而非 gui）──
    def _effective_speech_voice(self) -> str: return gui_voice.effective_speech_voice(self._cfg)
    def _on_speech_voice_change(self) -> None: gui_voice.on_speech_voice_change(self._voice_ctx, self._cfg, self._engines, set_status=self._set_status)
    def _on_tts_voice_change(self) -> None: gui_voice.on_tts_voice_change(self._voice_ctx, self._cfg, set_status=self._set_status, gui=self)
    def _set_voice_config(self, voice: str) -> None: gui_voice.set_voice_config(self._cfg, voice)
    def _set_tts_voice_config(self, voice: str) -> None: gui_voice.set_tts_voice_config(self._cfg, voice)
    def _glossary_scope(self) -> str: return gui_voice.glossary_scope(self._voice_ctx)
    def _glossary_scope_label(self, scope: str) -> str: return gui_voice.glossary_scope_label(self._voice_ctx, scope)
    def _read_glossary_from_disk(self, scope: str): return gui_voice.read_glossary_from_disk(self._cfg, scope)
    def _read_glossary_from_memory(self, scope: str): return gui_voice.read_glossary_from_memory(self._cfg, scope)
    def _glossary_hint_text(self, scope: str) -> str: return gui_voice.glossary_hint_text(self._voice_ctx, scope)
    def _on_glossary_scope_change(self) -> None: gui_voice.on_glossary_scope_change(self._voice_ctx, self._cfg)
    def _refresh_glossary_box(self) -> None: gui_voice.refresh_glossary_box(self._voice_ctx, self._cfg)
    def _resolve_api_key_safe(self) -> str: return gui_voice.resolve_api_key_safe(self._cfg)
    def _on_voice_preview_done(self, kind, voice, err) -> None: gui_voice.on_voice_preview_done(self._voice_ctx, kind, voice, err, set_status=self._set_status)
    def _refresh_voice_mode_btn(self) -> None:
        self._voice_ctx.proxy = self._proxy; gui_voice.refresh_voice_mode_btn(self._voice_ctx, self._engines)
    # ── 气泡显示原文 / 译文（只影响 chatbox）──
    def _chatbox_text_mode(self) -> str:
        from .engine import chatbox_text_mode
        return chatbox_text_mode(self._cfg)
    def _on_chatbox_toggle(self) -> None:
        """勾/取消 chatbox：落盘 + 刷新切换按钮的可用态。"""
        self._save_ui_state(); self._refresh_chatbox_text_btn()
    def _refresh_chatbox_text_btn(self) -> None:
        """刷新按钮文案（显示**当前**模式）与可用态（未勾 chatbox → 置灰）。"""
        btn = getattr(self, "_chatbox_text_btn", None)
        if btn is None: return
        from .engine import CHATBOX_TEXT_SOURCE
        src = self._chatbox_text_mode() == CHATBOX_TEXT_SOURCE
        btn.configure(text=(t("📝 气泡: 原文") if src else t("🌐 气泡: 译文")))
        btn.configure(state=(tk.NORMAL if self._chatbox_var.get() else tk.DISABLED))
    def _on_chatbox_text_toggle(self) -> None:
        """点一下切到另一种。**改内存 cfg**（引擎每条现读 → 同一次会话内立即生效），
        再落盘（下次启动仍是它）—— 与切界面语言的即时生效口径一致。"""
        from .engine import CHATBOX_TEXT_SOURCE, CHATBOX_TEXT_TRANSLATED
        to_source = self._chatbox_text_mode() != CHATBOX_TEXT_SOURCE
        self._cfg.ui = self._cfg.ui or {}
        self._cfg.ui["chatbox_text"] = CHATBOX_TEXT_SOURCE if to_source else CHATBOX_TEXT_TRANSLATED
        self._save_ui_state(); self._refresh_chatbox_text_btn()
        self._set_status("info", t("气泡改为显示原文") if to_source else t("气泡改为显示译文"))
    # ── 需要特殊处理的设置/更新/音色方法 ──
    def _settings_page(self, nb, title): return gui_settings.settings_page(self._settings_ctx, self, title)
    def _sync_page_scrollbar(self, canvas, inner, sb) -> None: gui_settings.sync_page_scrollbar(canvas, inner, sb)
    def _chatbox_port(self) -> int: return gui_settings.chatbox_port(self)
    def _selected_provider(self) -> str: return gui_settings.selected_provider(self)
    def _provider(self) -> str: return gui_settings.get_provider(self)
    def _provider_label(self) -> str: return gui_settings.provider_label(self)
    def _current_key_slot(self) -> str: return gui_settings.get_current_key_slot(self)
    def _signup_url(self) -> str: return gui_settings.signup_url(self)
    def _log_dir(self) -> Path: return gui_settings.log_dir(self)
    _SETTINGS_PAGE_TAB = gui_settings._SETTINGS_PAGE_TAB
    def _build_sponsor_list(self, flow) -> None: gui_settings.build_sponsor_list(flow, self)
    def _load_qr(self, parent, path): return gui_update._load_qr(self, parent, path)
    def _launch_updater_bat(self, bat_text) -> Path: return gui_update.launch_updater_bat(self, bat_text)
    def _on_save_glossary(self) -> None:
        gui_voice.on_save_glossary(self._voice_ctx, self._cfg, set_glossary_status=self._set_glossary_status, push_to_engines=self._push_glossary_to_engines, config_path=DEFAULT_CONFIG)
    def _save_glossary_config(self, path, mapping) -> bool: return gui_voice.save_glossary_config(path, mapping, config_path=DEFAULT_CONFIG)
    def _push_glossary_to_engines(self, scope, mapping) -> None: gui_voice.push_glossary_to_engines(self._engines, self._engine_dirs, self._voice_ctx, scope, mapping)
    def _set_glossary_status(self, text, *, warn=False) -> None: gui_voice.set_glossary_status(self._voice_ctx, text, warn=warn)
    @staticmethod
    def _glossary_scope_path(scope) -> list: return gui_voice.glossary_scope_path(scope)
    def _write_leaf(self, path, value, err_label, *, create=False) -> None:
        _yaml_write(DEFAULT_CONFIG, lambda t, _s=(_yaml_set_or_create if create else _yaml_set_in_text): _s(t, path, value), err=err_label)
    @property
    def _preview_busy(self) -> bool: return self._voice_ctx.preview_busy
    @_preview_busy.setter
    def _preview_busy(self, value) -> None: self._voice_ctx.preview_busy = value
    def _preview_voice(self, kind) -> None:
        gui_voice.preview_voice(self._voice_ctx, self._cfg, kind, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local,
            gui=self)
    def _preview_worker(self, kind, voice, api_key, **kw) -> None: gui_voice.preview_worker(self._q, kind, voice, api_key, play_fn=_play_pcm_local, **kw)
    def _on_preview_speech_voice(self) -> None:
        gui_voice.on_preview_speech_voice(self._voice_ctx, self._cfg, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local,
            gui=self)
    def _on_preview_tts_voice(self) -> None:
        gui_voice.on_preview_tts_voice(self._voice_ctx, self._cfg, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local,
            gui=self)
    def _apply_theme(self) -> None: apply_theme(self._root)
    # ── 房间 ──
    def _build_room_row(self) -> None:
        row = gui_room.build_room_row(self._root, self._room_ctx, self._room_cfg)
        self._room_row = row; self._room_var = self._room_ctx.room_var; self._room_btn = self._room_ctx.room_btn
        self._room_status = self._room_ctx.room_status; self._room_btn.configure(command=self._on_room_button); self._refresh_room_btn()
    def _room_status_text(self) -> str: return room_status_text(self._room)
    def _refresh_room_status_label(self) -> None: gui_room.refresh_status_label(self._room_ctx, self._room)
    def _refresh_room_btn(self) -> None: gui_room.refresh_btn(self._room_ctx, self._room)
    def _on_room_button(self) -> None:
        if self._room is not None: self._room_var.set(False); self._on_room_toggle(); return
        self._sync_room_cfg_from_fields()
        if not self._room_cfg.room_code: self._set_status("warn", t("先在「设置 → 房间」里填房间码")); self._open_settings(page="room"); return
        self._room_var.set(True); self._on_room_toggle()
    def _on_room_toggle(self) -> None:
        self._sync_room_cfg_from_fields(); self._save_room_cfg()
        if self._room_var.get(): self._start_room()
        else: self._stop_room()
        self._refresh_room_status_label()
    def _on_room_field_change(self) -> None:
        was_on = self._room is not None; self._sync_room_cfg_from_fields(); self._save_room_cfg()
        if was_on and self._room_var.get(): self._stop_room(); self._start_room()
        self._refresh_room_status_label()
    def _on_room_generate(self) -> None: gui_room.on_room_generate(self, field_change_fn=self._on_room_field_change)
    def _sync_room_cfg_from_fields(self) -> None: self._room_cfg = gui_room.sync_room_cfg_from_fields(self, self._room_cfg)
    def _save_room_cfg(self) -> None: self._room_cfg = save_room_cfg(DEFAULT_CONFIG, self._room_cfg)
    def _start_room(self) -> None:
        if self._room is not None: return
        self._sync_room_cfg_from_fields()
        self._room = gui_room.start_room(self._room_cfg, self._publisher, on_message_fn=self._on_room_message, on_status_fn=self._on_room_status, set_status=self._set_status)
    def _stop_room(self) -> None: room = self._room; self._room = None; gui_room.stop_room(room, self._publisher)
    def _on_room_message(self, msg) -> None: gui_room.on_room_message(msg, self._room_cfg, self._q)
    def _on_room_status(self, text: str) -> None: gui_room.on_room_status(text, self._q)
    def _on_engine_text(self, who, source_id, src_text, tgt_text, is_final) -> None:
        self._q.put(("text", who, src_text, tgt_text, is_final)); self._publish_to_room(source_id, src_text, is_final)
    def _publish_to_room(self, source_id, src_text, is_final) -> None:
        gui_room.publish_to_room(self._room, self._room_cfg, self._publisher, source_id, src_text, is_final)
    # ── 手腕屏微调 ──
    def _ov_fn(self): return self._cfg.overlay if isinstance(self._cfg.overlay, dict) else {}
    def _build_tune_page(self, body) -> None:
        self._wire_desktop_ctx()
        self._wire_desktop_ctx()
        # 内容区可用宽按**有效**设置窗宽算（HiDPI 下窗口更宽，滑块能多留一列）——
        # 有效值在 build_settings_dialog 开头由 apply_settings_metrics 挂上。
        _m = getattr(self, "_settings_metrics", None)
        settings_width = _m.width if _m is not None else SETTINGS_WIDTH
        gui_desktop.build_tune_page(body, self._desktop_ctx, self._ov_fn(), overlay_fn=self._ov_fn, settings_width=settings_width)
        for a in ("anchor_combo", "tracker_var", "tune_panel_w", "anchor_label_to_key", "tune_values",
                  "tune_vars", "tune_lbls", "tune_units", "tune_grid", "ov_save_job"):
            setattr(self, f"_{a}", getattr(self._desktop_ctx, a))
    def _build_tune_grid(self, grid, specs, label_w, cols):
        gui_desktop.build_tune_grid(grid, specs, label_w, cols, self._desktop_ctx, overlay_fn=self._ov_fn)
    def _build_desktop_tune_page(self, body) -> None:
        gui_desktop.build_desktop_tune_page(body, self._desktop_ctx, self._desktop_cfg())
        for a in ("desktop_font_var", "desktop_font_lbl", "desktop_srcfont_var", "desktop_srcfont_lbl",
                  "desktop_w_var", "desktop_w_lbl", "desktop_h_var", "desktop_h_lbl",
                  "desktop_alpha_var", "desktop_alpha_lbl", "desktop_drag_btn", "desktop_tuned"):
            setattr(self, f"_{a}", getattr(self._desktop_ctx, a))
    @staticmethod
    def _apply_desktop_slider(key, var, lbl, fmt, gui): gui_desktop.apply_desktop_slider(key, var, lbl, fmt, gui._desktop_ctx)
    def _on_desktop_font(self, _v=""): gui_desktop.on_desktop_font(self._desktop_ctx)
    def _on_desktop_srcfont(self, _v=""): gui_desktop.on_desktop_srcfont(self._desktop_ctx)
    def _on_desktop_width(self, _v=""): gui_desktop.on_desktop_width(self._desktop_ctx)
    def _on_desktop_height(self, _v=""): gui_desktop.on_desktop_height(self._desktop_ctx)
    def _current_anchor(self): return gui_desktop.current_anchor(self._desktop_ctx)
    def _load_anchor_offset(self, anchor): gui_desktop.load_anchor_offset(self._desktop_ctx, anchor, self._ov_fn())
    def _make_tune_handler(self, key, var, lbl, unit):
        return gui_desktop.make_tune_handler(key, var, lbl, unit, self._desktop_ctx, overlay_fn=self._ov_fn)
    def _on_anchor_change(self): gui_desktop.on_anchor_change(self._desktop_ctx, overlay_fn=self._ov_fn)
    def _schedule_overlay_save(self): gui_desktop.schedule_overlay_save(self._desktop_ctx, root=self._root, overlay_fn=self._ov_fn)
    def _save_overlay_cfg(self): gui_desktop.save_overlay_cfg(self._desktop_ctx, cfg=self._cfg, overlay_fn=self._ov_fn)
    # ── 聊天 / 输入 / 状态 ──
    def _build_chat(self):
        ctx = self._chat_ctx; gui_chat.build_chat(self._root, ctx, self._on_mousewheel)
        self._canvas = ctx.canvas; self._vsb = ctx.vsb
    def _build_input_row(self):
        ctx = self._chat_ctx; gui_chat.build_input_row(
            self._root, ctx, self._cfg, self._attach_edit_menu,
            # ⚠️ 必须传我们自己的包装：默认兜底调用 `send_typed(ctx)` / `on_text_enter(ctx)`，
            #    缺 engines / engine_dirs / set_status → 点「发送」或回车必 TypeError。
            send_fn=self._send_typed, enter_fn=self._on_text_enter)
        self._text_var = ctx.text_var; self._text_entry = ctx.text_entry; self._send_btn = ctx.send_btn
    def _set_text_input_enabled(self, on): gui_chat.set_text_input_enabled(self._chat_ctx, on)
    def _on_text_enter(self, _event=None): return gui_chat.on_text_enter(self._chat_ctx, self._engines, self._engine_dirs, self._set_status)
    def _send_typed(self): gui_chat.send_typed(self._chat_ctx, self._engines, self._engine_dirs, self._set_status)
    def _build_status(self):
        gui_chat.build_status(self._root, self._chat_ctx)
        self._status_dot = self._chat_ctx.status_dot; self._status_label = self._chat_ctx.status_label; self._stats_label = self._chat_ctx.stats_label
    def _check_api_key(self) -> None:
        self._refresh_key_status()
        try:
            from .config import load_api_key; load_api_key(slot=self._current_key_slot())
        except SystemExit as e: self._set_status("error", str(e))
    def _on_canvas_scroll(self, first, last): gui_chat.on_canvas_scroll(self._chat_ctx, first, last); self._auto_scroll = self._chat_ctx.auto_scroll
    def _on_mousewheel(self, event): gui_chat.on_mousewheel(self._chat_ctx, event)
    def _on_canvas_configure(self, event=None): gui_chat.on_canvas_configure(self._chat_ctx, self._root, event); self._canvas_w = self._chat_ctx.canvas_w
    # ── 事件处理 ──
    def _on_direction_change(self) -> None: self._update_direction_langs(); self._save_ui_state()
    def _update_direction_langs(self) -> None:
        d = self._direction_var.get(); a, b = self._lang_pair["source"], self._lang_pair["target"]
        if d == "theirs": src_code, tgt_code = b, a or "zh"; src_langs = tgt_langs = TARGET_LANGS
        else: src_code, tgt_code = a, b; src_langs = SOURCE_LANGS; tgt_langs = TARGET_LANGS
        self._source_combo.configure(values=[_lang_label(k) for k in src_langs])
        self._target_combo.configure(values=[_lang_label(k) for k in tgt_langs])
        self._source_combo.set(_lang_label(_source_name(src_code))); self._target_combo.set(_lang_label(_target_name(tgt_code)))
    def _on_lang_change(self, _event=None) -> None:
        d = self._direction_var.get()
        src_shown = _lang_key(self._source_combo.get(), SOURCE_LANGS if d != "theirs" else TARGET_LANGS)
        tgt_shown = _lang_key(self._target_combo.get(), TARGET_LANGS)
        if d == "theirs": self._lang_pair["target"] = TARGET_LANGS.get(src_shown, self._lang_pair["target"]); self._lang_pair["source"] = TARGET_LANGS.get(tgt_shown)
        else: self._lang_pair["source"] = SOURCE_LANGS.get(src_shown); self._lang_pair["target"] = TARGET_LANGS.get(tgt_shown, self._lang_pair["target"])
        if self._lang_pair["source"] is None:
            self._set_status("info", t("已切换为{target} → 中文", target=_lang_label(_target_name(self._lang_pair["target"]))))
        self._save_lang_config(); self._push_lang_to_engines(); self._publisher.reset(); self._update_direction_langs()
    def _save_lang_config(self) -> None:
        a, b = self._lang_pair["source"], self._lang_pair["target"] or "en"
        pairs = {"mine": {"source_lang": a, "target_lang": b}, "theirs": {"source_lang": b, "target_lang": a or "zh"}}
        def _fn(text):
            for name, langs in pairs.items():
                for key, val in langs.items(): text = _yaml_set_in_text(text, ["directions", name, key], _fmt_scalar(val))
            return text
        _yaml_write(DEFAULT_CONFIG, _fn, err="保存配置")
    def _save_ui_state(self) -> None:
        def _fn(text):
            if not re.search(r"^ui:", text, re.M): text = text.rstrip("\n") + "\n\n# 界面上次的选择\nui:\n"
            for kp, val in [("direction", self._direction_var.get()), ("chatbox", _fmt_scalar(bool(self._chatbox_var.get()))),
                            ("overlay", _fmt_scalar(bool(self._overlay_var.get()))), ("desktop_overlay", _fmt_scalar(bool(self._desktop_var.get()))),
                            ("chatbox_text", _fmt_scalar(self._chatbox_text_mode()))]:
                text = _yaml_set_in_text(text, ["ui", kp], val)
            return text
        _yaml_write(DEFAULT_CONFIG, _fn, err="保存界面选择")
    def _push_lang_to_engines(self):
        ctx = self._engine_ctx; ctx.cfg = self._cfg; ctx.lang_pair = self._lang_pair; ctx.engines = self._engines; ctx.engine_dirs = self._engine_dirs
        gui_engine.push_lang_to_engines(ctx)
    # ── 引擎控制 ──
    def _start(self):
        self._sync_engine_ctx()
        need_key = gui_engine.start(self._engine_ctx)
        self._unsync_engine_ctx()
        self._refresh_voice_mode_btn()     # 解禁条件 = 有代理 且 翻译在跑
        if need_key:                       # 无 key 被拦下 → 自动弹设置窗引导填 key
            self._open_settings()
    def _start_engine(self, index): self._sync_engine_ctx(); gui_engine.start_engine(self._engine_ctx, index); self._unsync_engine_ctx()
    def _on_engine_status(self, lvl, msg, who): gui_engine.on_engine_status(self._q, lvl, msg, who)
    def _start_overlay(self, *, force=False):
        self._sync_engine_ctx(); r = gui_engine.start_overlay(self._engine_ctx, force=force); self._overlay_out = self._engine_ctx.overlay_out; self._unsync_engine_ctx(); return r
    def _on_overlay_toggle(self):
        if self._overlay_var.get():
            if self._start_overlay(force=True): self._set_status("info", t("手腕屏已开启（位置 / 字号等见「设置 → 手腕屏」）"))
            else: self._overlay_var.set(False); self._set_status("error", t("手腕屏没启动起来，已自动取消勾选（先把 SteamVR 打开，再勾一次即可）"))
        else: self._stop_overlay(); self._sinks.discard("overlay")
        self._save_ui_state()
    def _stop_overlay(self): self._engine_ctx.overlay_out = self._overlay_out; gui_engine._stop_overlay(self._engine_ctx); self._overlay_out = self._engine_ctx.overlay_out
    def _push_overlay(self, force=False): self._engine_ctx.overlay_out = self._overlay_out; self._engine_ctx.bubbles = self._bubbles; gui_engine.push_overlay(self._engine_ctx, force=force)
    def _desktop_cfg(self): return gui_engine.desktop_cfg(self._cfg)
    def _start_desktop(self, *, force=False):
        self._sync_engine_ctx(); r = gui_engine.start_desktop(self._engine_ctx, force=force); self._desktop_out = self._engine_ctx.desktop_out; self._unsync_engine_ctx(); return r
    def _on_desktop_toggle(self):
        if self._desktop_var.get():
            if self._start_desktop(force=True): self._set_status("info", t("桌面字幕已开启（拖到想要的位置；尺寸/字号/透明度见「设置 → 桌面字幕」）"))
            else: self._desktop_var.set(False); self._set_status("error", t("桌面字幕没启动起来，已自动取消勾选"))
        else: self._stop_desktop(); self._sinks.discard("desktop")
        self._save_ui_state()
    def _stop_desktop(self):
        self._sync_engine_ctx(); gui_engine.stop_desktop(self._engine_ctx); self._desktop_out = self._engine_ctx.desktop_out; self._desktop_dragging = False; self._unsync_engine_ctx()
    def _push_desktop(self, force=False): self._engine_ctx.desktop_out = self._desktop_out; self._engine_ctx.bubbles = self._bubbles; gui_engine.push_desktop(self._engine_ctx, force=force)
    def _on_desktop_alpha(self, _v=""):
        # 透明度：立刻贴到活窗口 + 防抖落盘。实现只有 gui_desktop 一份（它读 DesktopCtx），
        # 落盘再回调到 _schedule_desktop_save；改完后把「动过」标志镜像回本类（供同步/测试读）。
        gui_desktop.on_desktop_alpha(self._desktop_ctx)
        self._desktop_alpha_touched = self._desktop_ctx.desktop_alpha_touched
    def _on_power(self) -> None:
        """主控制行那个单按钮的分发：翻译中就停，否则就开。"""
        if self._power_state == "running":
            self._stop()
        elif self._power_state != "stopping":   # stopping：按钮已置灰，理论上点不到
            self._start()

    def _set_power_state(self, state: str) -> None:
        """刷「开始/停止」单按钮（唯一入口；文案/颜色/可用态都由它一处决定）。"""
        self._power_state = state
        gui_layout.apply_power_state(getattr(self, "_power_btn", None), state)

    def _wire_desktop_ctx(self) -> None:
        """把设置页的「落盘 / 拖动」回调接到本类的薄壳上。

        ⚠️ 桌面字幕的**唯一**保存在 gui_engine（root / 活实例 / 引擎 ctx 同步都在那边）。
        设置页的滑块只认自己那份 DesktopCtx，持久化必须回调进来 —— 早先 gui_desktop 里
        另留了一整套同名实现且 `root` 没接上，于是滑块改了既不落盘也不热重载（回归）。
        """
        c = self._desktop_ctx
        c.schedule_overlay_save_fn = self._schedule_overlay_save
        c.schedule_desktop_save_fn = self._schedule_desktop_save
        c.toggle_desktop_drag_fn = self._toggle_desktop_drag
        c.desktop_out_fn = lambda: self._desktop_out

    def _schedule_desktop_save(self):
        # 设置页的滑块状态记在 DesktopCtx 上，这里先汇入本类属性再同步给引擎 ctx
        # （gui_engine.save_desktop_cfg 读的是引擎 ctx，二者必须同源）。
        self._desktop_tuned = self._desktop_ctx.desktop_tuned
        self._desktop_alpha_touched = self._desktop_ctx.desktop_alpha_touched
        self._sync_engine_ctx()
        self._engine_ctx.desktop_out = self._desktop_out
        gui_engine.schedule_desktop_save(self._engine_ctx); self._desktop_save_job = self._engine_ctx.desktop_save_job
    def _save_desktop_cfg(self):
        self._sync_engine_ctx(); gui_engine.save_desktop_cfg(self._engine_ctx, config_path=DEFAULT_CONFIG); self._desktop_save_job = self._engine_ctx.desktop_save_job
    def _toggle_desktop_drag(self):
        self._sync_engine_ctx(); gui_engine.toggle_desktop_drag(self._engine_ctx); self._desktop_out = self._engine_ctx.desktop_out; self._desktop_dragging = self._engine_ctx.desktop_dragging
    def _stop(self):
        self._sync_engine_ctx(); gui_engine.stop(self._engine_ctx); self._unsync_engine_ctx()
        self._refresh_voice_mode_btn()     # 必须在 unsync 之后：stop() 已把 engines 清空
    def _wait_stop_done(self, engines):
        self._engine_ctx.q = self._q; self._engine_ctx.stop_done_evt = self._stop_done_evt; self._engine_ctx.root = self._root; gui_engine.wait_stop_done(self._engine_ctx, engines)
    def _on_close(self):
        # 先关代理再走引擎收尾：on_close 末尾会 destroy root + crashlog.close()，
        # 放在后面的话「代理已关闭」这行留痕就落不进日志了。
        self._close_proxy()
        self._sync_engine_ctx(); gui_engine.on_close(self._engine_ctx)
    def _sync_engine_ctx(self):
        c = self._engine_ctx
        for n in _E2G: setattr(c, n, getattr(self, f"_{n}"))
        for n in _G2E: setattr(c, n, getattr(self, f"_{n}", None))
        c.lang_pair = self._lang_pair; c.bubbles = self._bubbles; c.q = self._q; c.root = self._root; c.cfg = self._cfg
        c.desktop_tuned = getattr(self, '_desktop_tuned', set())
    def _unsync_engine_ctx(self):
        c = self._engine_ctx
        for n in _E2G: setattr(self, f"_{n}", getattr(c, n))
    # ── 设备选择（→ gui_audio）──
    def _sync_audio_ctx(self) -> None: gui_audio.sync_from_gui(self._audio_ctx, self)
    def _start_device_scan(self) -> None:
        self._sync_audio_ctx(); gui_audio.start_device_scan(self._audio_ctx, self._root, self._headless, self._scan_holder, cfg=self._cfg); self._device_scan_pending = self._scan_holder["pending"]
    def _on_refresh_devices(self) -> None:
        self._sync_audio_ctx(); gui_audio.on_refresh_devices(self._audio_ctx, self._root, self._engines, self._headless, self._scan_holder, cfg=self._cfg)
    # ── 麦克风代理（MicProxy）：常驻虚拟声卡路由，与翻译解耦 ──
    def _proxy_audio_cfg(self) -> dict:
        """取 ``output.audio`` 这一段（含 proxy 子段）的**副本**；配置畸形时回落空 dict。"""
        out = self._cfg.output if isinstance(self._cfg.output, dict) else {}
        a = out.get("audio")
        return dict(a) if isinstance(a, dict) else {}

    def _proxy_wanted(self) -> bool:
        """配置里是不是想要代理（缺省即启用，见 config.py 的 proxy 归一化）。"""
        p = self._proxy_audio_cfg().get("proxy") or {}
        return bool(p.get("enabled", True)) if isinstance(p, dict) else True

    def _sync_proxy_cfg_mem(self, *, audio: dict | None = None,
                            proxy: dict | None = None) -> None:
        """落盘之后同步**内存快照**：代理随时可能重开，它读的是这份而不是磁盘。"""
        if not isinstance(self._cfg.output, dict):
            return
        a = self._cfg.output.get("audio")
        if not isinstance(a, dict):
            a = {}; self._cfg.output["audio"] = a
        if audio:
            a.update(audio)
        if proxy:
            p = a.get("proxy")
            if not isinstance(p, dict):
                p = {}; a["proxy"] = p
            p.update(proxy)

    def _on_proxy_status(self, level: str, msg: str, **params) -> None:
        """代理的 on_status：**日志打中文原文、状态栏走 i18n**（与 ``gui_engine.on_engine_status`` 同口径）。

        `msg` 是**中文模板**（同时就是 i18n 词条 key），带值的部分走 `**params`：

          · 日志：`msg.format(**params)` —— 全仓日志统一中文，中英混排只会更难读；
          · 状态栏：`t(msg, **params)` —— 用户看的地方必须能翻译（英文界面下不许闪中文）。

        micproxy 内部每条消息自带 ``"[proxy] "`` 前缀，这里剥掉再补一个带级别的，
        否则日志里会出现 ``[proxy][warn] [proxy] …`` 这种双重前缀。
        """
        body = msg[len("[proxy] "):] if msg.startswith("[proxy] ") else msg
        print(f"[proxy][{level}] {body.format(**params) if params else body}", flush=True)
        self._q.put(("status", level, t(body, **params) if params else t(body)))

    def _start_proxy(self) -> bool:
        """构造并启动麦克风代理。返回是否**真的起来了**（False = 走旧行为）。

        ⚠️ 绝不抛异常、绝不阻塞启动：任何一步失败都只降级 + 留痕（禁静默降级）。
        """
        if self._proxy is not None:
            return True
        if not self._proxy_wanted():
            print("[proxy] 配置 output.audio.proxy.enabled=false：不启动代理"
                  "（译音输出回到旧行为：随翻译启停、由引擎自建）", flush=True)
            return False
        if _is_test_process():
            # 测试纪律：绝不碰真实音频设备（虚拟声卡输出流与麦克风：Windows 是 PortAudio，
            # Linux 是 PipeWire 节点/子进程 —— 都会真的开流/起节点）
            print("[proxy] 测试/自检进程：不构造麦克风代理（避免打开真实音频设备）", flush=True)
            return False
        a = self._proxy_audio_cfg()
        out = self._cfg.output if isinstance(self._cfg.output, dict) else {}
        mic_name = (out.get("capture") or {}).get("mic_device") or None
        proxy = None
        try:
            # ★ 平台差异**只在门面里**：Windows → `vlt/output/micproxy.py`（PortAudio 输出流，
            #   虚拟声卡是用户自备的 VoiceMeeter/VB-Cable）；Linux → `vlt/output/micproxy_linux.py`
            #   （`pw-cat` 管道 + 运行时声明的 `VLT Mic` 节点）。档位语义两端一致。
            proxy = platform.create_mic_proxy(a, mic_name, self._on_proxy_status)
            ok = bool(proxy.start())
        except Exception as exc:                       # noqa: BLE001
            print(f"[proxy] 启动异常：{type(exc).__name__}: {exc}（其余功能不受影响）", flush=True)
            ok = False
        if not ok:
            if proxy is not None:
                try:
                    proxy.close()                      # 半开的流/线程必须放掉
                except Exception:                      # noqa: BLE001
                    pass
            self._proxy_degraded(
                t("麦克风代理不可用（虚拟声卡没打开？）；原声/译音切换已禁用"),
                "[proxy] 代理不可用 → 回到旧行为：译音输出由引擎自建（随翻译启停），"
                "主界面「原声/译音」切换已禁用")
            return False
        self._proxy = proxy
        self._refresh_voice_mode_btn()
        pt = (a.get("proxy") or {}).get("passthrough_buffer_ms")
        print(f"[proxy] 麦克风代理已启动（VRChat 的麦克风请**永久**指向虚拟声卡）："
              f"直通缓冲 {pt}ms / 译音缓冲 {a.get('buffer_ms')}ms / 当前档位 {proxy.mode}",
              flush=True)
        return True

    def _proxy_degraded(self, status_text: str, log_line: str) -> None:
        """代理不可用 → 回到旧行为，并**各留一行痕**（状态栏一行 + stdout 一行）。"""
        self._proxy = None
        self._refresh_voice_mode_btn()                 # 无代理 → 按钮置灰
        print(log_line, flush=True)
        self._set_status("warn", status_text)

    def _close_proxy(self) -> None:
        """幂等关闭代理：先摘引用再 close，绝不让异常打断关窗流程。"""
        p = self._proxy
        if p is None:
            return
        self._proxy = None
        try:
            p.close()                                  # 幂等：停麦克风线程 + 关输出流
            print("[proxy] 麦克风代理已关闭（虚拟声卡输出流与麦克风直通线程已释放）", flush=True)
        except Exception as exc:                       # noqa: BLE001
            print(f"[proxy] ⚠️ 关闭代理时出错（忽略）：{type(exc).__name__}: {exc}", flush=True)

    def _restart_proxy(self) -> None:
        """设置页勾选框的落地点：想开就开、想关就关，按钮态与提示一起刷新。"""
        if self._proxy_wanted():
            if self._start_proxy():
                self._set_status("info", t("麦克风代理已启用"))
            return
        running = any(getattr(e, "running", False) for e in self._engines)
        self._close_proxy()
        self._refresh_voice_mode_btn()
        if running:
            # 引擎手里那条 audio_sink 指向刚关掉的代理缓冲：本轮译音**没人接管**了。
            # 必须说清楚，别让人以为还在出声（禁静默降级）。
            print("[proxy] ⚠️ 翻译进行中关掉了代理：本轮译音输出已失效，"
                  "重新点「开始翻译」后由引擎自建虚拟声卡输出（旧行为）", flush=True)
        self._set_status("info", t("麦克风代理已关闭（回到旧行为）"))

    def _apply_proxy_buffers(self, passthrough_ms: int, translated_ms: int) -> None:
        """缓冲改动落地：代理在跑就即时生效，不在跑就只落盘 + 如实说明下次何时生效。"""
        p = self._proxy
        if p is not None:
            try:
                p.reopen_with(passthrough_ms=passthrough_ms,
                              translated_buffer_ms=translated_ms)
                print(f"[proxy] 缓冲已更新：直通 {passthrough_ms}ms / 译音 {translated_ms}ms"
                      "（软件侧参数，输出流不重开、没有静音间隙）", flush=True)
                self._set_status("info", t("缓冲已更新（即时生效）"))
            except Exception as exc:                   # noqa: BLE001
                print(f"[proxy] ⚠️ 应用缓冲失败：{type(exc).__name__}: {exc}"
                      "（已落盘，下次启动生效）", flush=True)
                self._set_status("warn", t("缓冲已保存（译音缓冲下次开始翻译生效）"))
            return
        if self._proxy_wanted():
            print(f"[proxy] 代理没在跑（虚拟声卡没打开？）：缓冲已落盘 —— "
                  f"直通 {passthrough_ms}ms / 译音 {translated_ms}ms，下次启动代理时生效",
                  flush=True)
            self._set_status("warn", t("虚拟声卡未打开——检查「译音输出」设备；缓冲改动已存，下次生效"))
        else:
            print(f"[proxy] 代理已关闭：缓冲已落盘（直通 {passthrough_ms}ms / "
                  f"译音 {translated_ms}ms），译音缓冲下次开始翻译生效", flush=True)
            self._set_status("info", t("缓冲已保存（译音缓冲下次开始翻译生效）"))

    def _on_proxy_toggle(self) -> None:
        self._sync_audio_ctx(); self._audio_ctx.proxy_enabled = self._proxy_enabled_var.get()
        _yaml_write(DEFAULT_CONFIG, lambda t: _yaml_set_in_text(t, ["output", "audio", "proxy", "enabled"], _fmt_scalar(self._audio_ctx.proxy_enabled)), err="保存代理开关")
        self._sync_proxy_cfg_mem(proxy={"enabled": bool(self._audio_ctx.proxy_enabled)})
        self._restart_proxy()
        self._sync_proxy_controls_state()
    def _on_proxy_buffer_change(self, _event=None) -> None:
        if self._passthrough_spin is None: return
        try:
            pt = int(self._passthrough_var.get()); tr = int(self._translated_buf_var.get())
            pt_clamped = max(60, min(500, pt)); tr_clamped = max(50, min(2000, tr))
            if pt != pt_clamped: self._passthrough_var.set(pt_clamped)
            if tr != tr_clamped: self._translated_buf_var.set(tr_clamped)
            pt, tr = pt_clamped, tr_clamped
        except (ValueError, TypeError): return
        self._sync_audio_ctx()
        def _fn(t):
            t = _yaml_set_in_text(t, ["output", "audio", "proxy", "passthrough_buffer_ms"], _fmt_scalar(pt))
            return _yaml_set_in_text(t, ["output", "audio", "buffer_ms"], _fmt_scalar(tr))
        _yaml_write(DEFAULT_CONFIG, _fn, err="保存代理缓冲")
        self._sync_proxy_cfg_mem(audio={"buffer_ms": tr}, proxy={"passthrough_buffer_ms": pt})
        self._apply_proxy_buffers(pt, tr)
    def _sync_proxy_controls_state(self) -> None:
        if self._proxy_check is None: return
        on = bool(self._proxy_enabled_var.get())
        if self._passthrough_spin is not None:
            try: self._passthrough_spin.configure(state=tk.NORMAL if on else tk.DISABLED)
            except Exception: pass
        if self._translated_spin is not None:
            try: self._translated_spin.configure(state=tk.NORMAL)
            except Exception: pass
        if hasattr(self, '_proxy_hint') and self._proxy_hint is not None:
            self._proxy_hint.configure(text="" if on else t("已关闭：回到旧行为（译音输出随翻译启停，主界面切换开关置灰）"))
    def _on_device_scan_result(self, mics, loops, outs) -> None:
        self._sync_audio_ctx(); gui_audio.on_device_scan_result(self._audio_ctx, self._cfg, mics, loops, outs, self._names_holder, self._scan_holder)
        self._device_scan_pending = self._scan_holder.get("pending", False)
    def _on_device_change(self, _event=None) -> None:
        self._sync_audio_ctx()
        old_mic = self._current_mic_device()
        gui_audio.on_device_change(self._audio_ctx, self._cfg, self._names_holder)
        new_mic = self._current_mic_device()
        if new_mic != old_mic:
            self._apply_mic_change(new_mic)
    def _current_mic_device(self) -> str:
        out = self._cfg.output if isinstance(self._cfg.output, dict) else {}
        return str((out.get("capture") or {}).get("mic_device") or "")
    def _apply_mic_change(self, device_name: str) -> None:
        """麦克风变更后让**直通腿**即时生效：只重启代理的采集线程，不动虚拟声卡输出流
        （引擎手里的 `translated_sink` 不受影响，翻译不断）。代理没在跑就只落盘，下次
        `start()` 生效。翻译输入那条腿在引擎启动时读定设备，运行中切换不影响本轮 ——
        状态栏如实说明，别让人以为翻译输入也换了（禁静默降级）。"""
        p = self._proxy
        if p is None:
            return
        try:
            p.reopen_mic(device_name or None)
        except Exception as exc:                        # noqa: BLE001 — 绝不因切麦打断界面
            print(f"[proxy] ⚠️ 切换麦克风失败：{type(exc).__name__}: {exc}"
                  "（已落盘，下次启动生效）", flush=True)
            self._set_status("warn", t("麦克风已保存（切换未即时生效，下次启动生效）"))
            return
        print(f"[proxy] 直通麦克风已切换：{device_name or '自动检测'}"
              "（仅重启采集线程；翻译输入下轮生效）", flush=True)
        self._set_status("info", t("麦克风已切换（直通即时生效；翻译输入下轮生效）"))
    def _save_device_config(self, mic_name, loop_name, out_name) -> None: self._sync_audio_ctx(); gui_audio.save_device_config(self._audio_ctx, self._cfg, mic_name, loop_name, out_name)
    def _on_gate_change(self, _v=None) -> None:
        self._sync_audio_ctx(); gui_audio.on_gate_change(self._audio_ctx, self._cfg, self._root, self._engines, self._gate_holder); self._gate_probe = self._gate_holder["probe"]; self._gate_save_job = self._gate_holder["save_job"]
    def _apply_gate_live(self) -> None: self._sync_audio_ctx(); gui_audio.apply_gate_live(self._audio_ctx, self._engines)
    def _save_gate_cfg(self) -> None: self._sync_audio_ctx(); gui_audio.save_gate_cfg(self._audio_ctx, self._cfg, self._gate_holder); self._gate_save_job = self._gate_holder["save_job"]
    def _gate_probe_wanted(self) -> bool: self._sync_audio_ctx(); return gui_audio.gate_probe_wanted(self._audio_ctx, self._engines)
    def _sync_gate_level_probe(self) -> None: self._sync_audio_ctx(); gui_audio.sync_gate_level_probe(self._audio_ctx, self._cfg, self._gate_holder); self._gate_probe = self._gate_holder["probe"]
    def _stop_gate_probe(self) -> None: self._sync_audio_ctx(); gui_audio.stop_gate_probe(self._audio_ctx, self._gate_holder); self._gate_probe = self._gate_holder["probe"]
    def _gate_level_db(self): self._sync_audio_ctx(); return gui_audio.gate_level_db(self._audio_ctx, self._engines, self._gate_holder)
    def _refresh_gate_level(self) -> None:
        self._sync_audio_ctx(); gui_audio.refresh_gate_level(self._audio_ctx, self._engines, self._gate_holder); self._gate_level_hold = self._gate_holder.get("level_hold", 0)
    # ── 轮询 / 气泡 / 状态 ──
    def _poll(self):
        gui_chat.setup_poll_ctx(self._chat_ctx, self)
        # ⚠️ 手腕屏/桌面字幕实例**不在这里传**：它们在「开始翻译」后才建，而 poll 在构建期
        #    就起跑了。早先传单元素 list 容器且从不写回 → 两个 tick() 永不执行（6083052 回归，
        #    桌面字幕收不到内容 / 拖不动 / 配置热重载失效）。现在由 setup_poll_ctx 绑活引用。
        gui_chat.poll(self._chat_ctx, self._root, self._set_status, self._add_text, self._refresh_status, self._stats, on_voice_lab=lambda _it: self._on_lab_done(_it[1], _it[2], _it[3], _it[4]))
        c = self._chat_ctx
        self._room_status_next = c.room_status_next; self._gate_level_tick = c.gate_level_tick
        if c.engines_ref is not self._engines: self._engines = c.engines_ref; self._engine_dirs = c.engine_dirs_ref
    def _add_text(self, source, text, is_final, who="mine", label=""):
        gui_chat.add_text(self._chat_ctx, source, text, is_final, who, label)
        self._bubbles = self._chat_ctx.bubbles; self._current = self._chat_ctx.current; self._auto_scroll = self._chat_ctx.auto_scroll
    def _draw_bubble(self, b): return gui_chat.draw_bubble(self._chat_ctx, b)
    def _redraw_current(self, b): gui_chat.redraw_current(self._chat_ctx, b)
    def _trim(self): gui_chat.trim(self._chat_ctx)
    def _update_scrollregion(self): gui_chat.update_scrollregion(self._chat_ctx)
    def _redraw_all(self): gui_chat.redraw_all(self._chat_ctx)
    def _set_status(self, level, msg): gui_chat.set_status(self._chat_ctx, level, msg); self._last_status_level = self._chat_ctx.last_status_level
    def _refresh_status(self): gui_chat.refresh_status(self._chat_ctx, self._engines, getattr(self, '_direction_var', None), self._stats)
    def run_self_test(self) -> int: return run_self_test()
    def run_self_test_dual(self) -> int:
        from .gui_selftest import run_self_test_dual as _rst2; return _rst2(self)















































































def main() -> int:
    ap = argparse.ArgumentParser(description="VRChat 实时同传 - 图形界面")
    ap.add_argument("--self-test", action="store_true", help="自动化验收（单方向）")
    ap.add_argument("--self-test-dual", action="store_true", help="双向同时验收")
    args = ap.parse_args()
    from .paths import ensure_app_dir, migrate_legacy_files
    migrate_legacy_files(); ensure_app_dir()
    crashlog.install(ROOT / "logs", "gui")
    crashlog.log_startup_info(f"gui {'--self-test-dual' if args.self_test_dual else args.self_test and '--self-test' or ''}")
    if args.self_test_dual: gui = TranslationGUI(headless=True); return gui.run_self_test_dual()
    if args.self_test: gui = TranslationGUI(headless=True); return gui.run_self_test()
    gui = TranslationGUI()
    crashlog.install_tk(gui._root)
    try: gui._root.mainloop()
    finally: crashlog.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
