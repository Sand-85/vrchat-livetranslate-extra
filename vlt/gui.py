"""Tkinter 图形界面：聊天气泡视图 + 开关 + 语言镜像 + 双向同时。
用法：
  python -m vlt.gui                    # 启动界面
  python -m vlt.gui --self-test        # 自动化验收（单方向，不起窗口）
  python -m vlt.gui --self-test-dual   # 双向同时验收（两个 PCM 驱动两个引擎）
"""
from __future__ import annotations

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

from . import endpoints
from . import tts
from . import voice_lab
from .ui_text import _play_pcm_local
from .ui_theme import ACCENT
from .ui_theme import BORDER
from .ui_theme import SETTINGS_WRAP
from .ui_theme import SURFACE
from .ui_theme import TEXT
from .voices import TTS_VOICES
from .voices import display_name
from .voices import real_id
from .voices import voice_choices
from tkinter import messagebox
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
FONT_UI = ui_tk.FONT_UI; FONT_STATUS = ui_tk.FONT_STATUS
FONT_BOLD_SM = ui_tk.FONT_BOLD_SM; FONT_BOLD_MD = ui_tk.FONT_BOLD_MD; FONT_BOLD_LG = ui_tk.FONT_BOLD_LG
def _yaml_write(p: Path, fn, *, err="保存"):
    if not p.exists(): return
    try: text = p.read_text(encoding="utf-8"); text = fn(text); _write_config_text(p, text)
    except Exception as exc: print(f"[gui] {err}失败：{exc}", flush=True)
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
_G2E = ["start_btn", "stop_btn", "direction_var", "chatbox_var", "overlay_var",
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
def _lab_model_of(info) -> str:                                      # noqa: ANN001
    """这条音色该配哪个合成模型：**它自己的** target_model 优先，缺失时才按族回落。

    设计(vd)/复刻(vc)两族的模型不通用 —— 这是本仓库实测过的硬约束（写错必 InvalidParameter）。
    """
    got = str(getattr(info, "target_model", "") or "").strip()
    if got:
        return got
    kind = str(getattr(info, "kind", "design") or "design")
    return voice_lab.CLONE_TARGET_MODEL if kind == "clone" else voice_lab.DEFAULT_TARGET_MODEL


class TranslationGUI:
    """主界面。headless=True 时不创建 Tk 窗口。"""
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
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local)
    def _preview_worker(self, kind, voice, api_key, **kw) -> None: gui_voice.preview_worker(self._q, kind, voice, api_key, play_fn=_play_pcm_local, **kw)
    def _on_preview_speech_voice(self) -> None:
        gui_voice.on_preview_speech_voice(self._voice_ctx, self._cfg, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local)
    def _on_preview_tts_voice(self) -> None:
        gui_voice.on_preview_tts_voice(self._voice_ctx, self._cfg, q=self._q, set_status=self._set_status,
            provider_fn=self._provider, resolve_api_key=self._resolve_api_key_safe, play_fn=_play_pcm_local)
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
        gui_desktop.build_tune_page(body, self._desktop_ctx, self._ov_fn(), overlay_fn=self._ov_fn, settings_width=SETTINGS_WIDTH)
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
        ctx = self._chat_ctx; gui_chat.build_input_row(self._root, ctx, self._cfg, self._attach_edit_menu)
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
    def _save_audio_flag(self) -> None:
        want = bool(self._vmic_var.get())
        self._refresh_voice_mode_hint()          # 我们的：开关变了刷新音源提示
        _yaml_write(DEFAULT_CONFIG, lambda t: _yaml_set_in_text(t, ["output", "audio", "enabled"], _fmt_scalar(want)), err="保存译音开关")
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
        self._desktop_alpha_touched = True; a = float(self._desktop_alpha_var.get())
        lbl = getattr(self, "_desktop_alpha_lbl", None)
        if lbl is not None: lbl.configure(text=f"{a:.2f}")
        if self._desktop_out is not None: self._desktop_out.set_alpha(a); self._schedule_desktop_save()
    def _schedule_desktop_save(self):
        self._engine_ctx.desktop_out = self._desktop_out; self._engine_ctx.desktop_save_job = self._desktop_save_job; self._engine_ctx.root = self._root
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
        self._sync_audio_ctx(); gui_audio.start_device_scan(self._audio_ctx, self._root, self._headless, self._scan_holder); self._device_scan_pending = self._scan_holder["pending"]
    def _on_refresh_devices(self) -> None:
        self._sync_audio_ctx(); gui_audio.on_refresh_devices(self._audio_ctx, self._root, self._engines, self._headless, self._scan_holder)
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
        _ov = [self._overlay_out]; _dov = [self._desktop_out]
        gui_chat.poll(self._chat_ctx, self._root, self._set_status, self._add_text, self._refresh_status, self._stats, [self._pending_starts], _ov, _dov, on_voice_lab=lambda _it: self._on_lab_done(_it[1], _it[2], _it[3], _it[4]))
        self._pending_starts = [self._pending_starts][0]; self._overlay_out = _ov[0]; self._desktop_out = _dov[0]
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

    def _build_settings_voice(self, body: ttk.Frame) -> None:
        """「音色」页：用一句话炼一个自己的音色 + 配方一键生成 + 过往生成（可回放）。

        为什么值得有这一页：官方「声音设计」只要一句**描述**就能出一个新音色，但入口在
        控制台里 —— 用户得去网页炼、再把 40 多位的一串 id 抄进配置（抄错就是
        `InvalidParameter`，界面只显示「暂不支持试听」，排查方向全错）。这一页把
        「描述 → 生成 → 试听 → 保存为当前音色」收成四步，并把**花钱**这件事摆在明面上。

        三条硬约束（都能在新手第一次点击时把人劝退，所以写在控件附近）：
        ① 费用记在**用户自己**的账号上（音色绑定创建它的账号，平台没有共享/转移）；
        ② 描述里写「像某声优/某角色」服务端不支持，也可能涉版权 → 只写声学特征；
        ③ 同一句描述每次生成**不保证同一条**音色（官方 FAQ），所以这里按名字幂等复用：
           同名音色已存在就直接拿来用，不会因为反复点击而反复计费。
        """
        ttk.Label(body, text=t("音色自定义"), style="Section.TLabel").pack(anchor=tk.W)
        ttk.Label(body, text=t("用一句话描述你想要的声音：生成后可直接试听，满意就保存为当前音色。"),
                  style="Muted.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(4, 0))

        form = ttk.Frame(body)
        form.pack(fill=tk.X, pady=(8, 0))
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text=t("名称:"), style="Dim.TLabel").grid(row=0, column=0, sticky="w")
        self._lab_name_var = tk.StringVar(value="my_voice")
        self._lab_name_entry = ttk.Entry(form, textvariable=self._lab_name_var, width=16,
                                         style="Key.TEntry", font=FONT_UI)
        self._lab_name_entry.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self._attach_edit_menu(self._lab_name_entry)
        ttk.Label(form, text=t("（字母/数字/下划线，≤16）"), style="Dim.TLabel").grid(
            row=0, column=2, sticky="w", padx=(8, 0))

        ttk.Label(form, text=t("描述:"), style="Dim.TLabel").grid(row=1, column=0,
                                                                  sticky="nw", pady=(6, 0))
        # 描述通常一二十字到几十字，单行 Entry 会让人看不到全貌 → 与词库页同一套 tk.Text 样式
        self._lab_prompt_text = tk.Text(
            form, height=3, width=44, wrap=tk.WORD, undo=True,
            bg=SURFACE, fg=TEXT, insertbackground=TEXT, selectbackground=ACCENT,
            selectforeground="#ffffff", relief=tk.FLAT, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, font=FONT_UI)
        self._lab_prompt_text.grid(row=1, column=1, columnspan=2, sticky="ew",
                                   padx=(8, 0), pady=(6, 0))
        self._attach_edit_menu(self._lab_prompt_text)
        ttk.Label(body, text=t("提示：只描述声学特征（年龄感、音高、语速、情绪），"
                               "不要写「像某声优/某角色」——服务端不支持模仿，也可能涉及版权。"),
                  style="Muted.TLabel", justify=tk.LEFT, wraplength=SETTINGS_WRAP).pack(
            anchor=tk.W, pady=(4, 0))

        btns = ttk.Frame(body)
        btns.pack(fill=tk.X, pady=(8, 0))
        self._lab_gen_btn = ttk.Button(btns, text=t("生成并试听"),
                                       command=self._on_lab_generate)
        self._lab_gen_btn.pack(side=tk.LEFT)
        self._lab_save_btn = ttk.Button(btns, text=t("保存为当前音色"),
                                        command=self._on_lab_save, state=tk.DISABLED)
        self._lab_save_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._lab_del_btn = ttk.Button(btns, text=t("删除所选"),
                                       command=self._on_lab_delete, state=tk.DISABLED)
        self._lab_del_btn.pack(side=tk.LEFT, padx=(6, 0))
        # 价格直接印在按钮旁：点下去花的是**用户自己的钱**，别藏在文档里
        ttk.Label(body, text=t("0.2 元/个（北京地域开通后 90 天内前 10 次免费，创建失败不计费）"),
                  style="Muted.TLabel", justify=tk.LEFT, wraplength=SETTINGS_WRAP).pack(
            anchor=tk.W, pady=(4, 0))

        ttk.Label(body, text=t("配方（预先做好的描述，一键生成）:"),
                  style="Dim.TLabel").pack(anchor=tk.W, pady=(10, 0))
        rrow = ttk.Frame(body)
        rrow.pack(fill=tk.X, pady=(4, 0))
        self._lab_recipe_combo = ttk.Combobox(rrow, values=voice_lab.recipe_labels(),
                                              state="readonly", width=26)
        self._lab_recipe_combo.pack(side=tk.LEFT)
        if voice_lab.RECIPES:
            self._lab_recipe_combo.current(0)
        self._lab_recipe_btn = ttk.Button(rrow, text=t("用配方一键生成"),
                                          command=self._on_lab_recipe_generate)
        self._lab_recipe_btn.pack(side=tk.LEFT, padx=(6, 0))
        self._lab_recipe_preview_btn = ttk.Button(rrow, text=t("试听配方"),
                                                  command=self._on_lab_recipe_preview)
        self._lab_recipe_preview_btn.pack(side=tk.LEFT, padx=(6, 0))

        # ---- 音色克隆（上传音频 → 复刻 → 试听）---------------------------------
        ttk.Label(body, text=t("音色克隆"), style="Section.TLabel").pack(anchor=tk.W,
                                                                       pady=(12, 0))
        ttk.Label(body, text=t("上传一段 10~20 秒的单人朗读（≥24kHz、无背景音、无音乐）。"
                               "只克隆你有权利的声音：自己的录音，或已获授权的素材。"),
                  style="Dim.TLabel", justify=tk.LEFT,
                  wraplength=SETTINGS_WRAP).pack(anchor=tk.W, pady=(4, 0))
        crow = ttk.Frame(body)
        crow.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(crow, text=t("名称:"), style="Dim.TLabel").pack(side=tk.LEFT)
        self._lab_clone_name_var = tk.StringVar(value="my_clone")
        clone_name = ttk.Entry(crow, textvariable=self._lab_clone_name_var, width=14,
                               style="Key.TEntry", font=FONT_UI)
        clone_name.pack(side=tk.LEFT, padx=(6, 0))
        self._attach_edit_menu(clone_name)
        self._lab_pick_btn = ttk.Button(crow, text=t("选择音频文件…"),
                                        command=self._on_lab_pick_audio)
        self._lab_pick_btn.pack(side=tk.LEFT, padx=(8, 0))
        self._lab_clone_btn = ttk.Button(crow, text=t("克隆并试听"), command=self._on_lab_clone)
        self._lab_clone_btn.pack(side=tk.LEFT, padx=(6, 0))
        # 素材要求 + 花费都要在点之前看得见（与上面「生成」那条同一样式）
        self._lab_clone_hint = ttk.Label(
            body, text=t("复刻素材：10~20 秒、单声道朗读、≥24kHz、无背景音／音乐；"
                         "克隆 0.01 元/次，素材需服务端审核。"),
            style="Dim.TLabel", justify=tk.LEFT, wraplength=SETTINGS_WRAP)
        self._lab_clone_hint.pack(anchor=tk.W, pady=(4, 0))

        # ---- 克隆预设（拼接范本：一键试听 / 一键克隆）----------------------------
        ttk.Label(body, text=t("克隆预设:"),
                  style="Dim.TLabel").pack(anchor=tk.W, pady=(8, 0))
        self._lab_presets = voice_lab.all_clone_presets(APP_DIR)
        self._lab_preset_combo = ttk.Combobox(
            body, state="readonly", width=30, font=FONT_UI,
            values=[voice_lab.preset_label(p, i18n.current_language())
                    for p in self._lab_presets])
        self._lab_preset_combo.pack(anchor=tk.W, pady=(3, 0))
        if self._lab_presets:
            self._lab_preset_combo.current(0)
        prow = ttk.Frame(body)
        prow.pack(anchor=tk.W, pady=(4, 0))
        self._lab_preset_preview_btn = ttk.Button(prow, text=t("试听范本"),
                                                  command=self._on_lab_preset_preview)
        self._lab_preset_preview_btn.pack(side=tk.LEFT)
        self._lab_preset_clone_btn = ttk.Button(prow, text=t("一键克隆"),
                                                command=self._on_lab_preset_clone)
        self._lab_preset_clone_btn.pack(side=tk.LEFT, padx=(6, 0))

        ttk.Label(body, text=t("过往生成（本账号的自定义音色）:"),
                  style="Dim.TLabel").pack(anchor=tk.W, pady=(10, 0))
        lrow = ttk.Frame(body)
        lrow.pack(fill=tk.X, pady=(4, 0))
        self._lab_list = tk.Listbox(
            lrow, height=5, bg=SURFACE, fg=TEXT, selectbackground=ACCENT,
            selectforeground="#ffffff", relief=tk.FLAT, highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT, font=FONT_UI,
            activestyle="none", exportselection=False)
        self._lab_list.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._lab_list.bind("<<ListboxSelect>>", self._on_lab_select)
        lcol = ttk.Frame(lrow)
        lcol.pack(side=tk.LEFT, padx=(6, 0))
        self._lab_preview_btn = ttk.Button(lcol, text=t("试听所选"),
                                           command=self._on_lab_preview)
        self._lab_preview_btn.pack(fill=tk.X)
        self._lab_refresh_btn = ttk.Button(lcol, text=t("刷新列表"),
                                           command=self._on_lab_refresh)
        self._lab_refresh_btn.pack(fill=tk.X, pady=(4, 0))

        # 试听的花费提示：**常显**，不靠点击后的状态行 —— 用户点之前就该知道要不要花钱。
        # 只写「试听免费」是不诚实的（首次试听一条已有音色确实要合成一句）。
        self._lab_preview_hint = ttk.Label(
            body, text=t("试听：命中本地缓存不花钱；没有缓存时用测试文本合成一句（约 0.003 元）"),
            style="Dim.TLabel", justify=tk.LEFT, wraplength=SETTINGS_WRAP)
        self._lab_preview_hint.pack(anchor=tk.W, pady=(4, 0))

        self._lab_status = ttk.Label(body, text="", style="Muted.TLabel", justify=tk.LEFT,
                                     wraplength=SETTINGS_WRAP)
        self._lab_status.pack(anchor=tk.W, pady=(8, 0))

        # 状态：列表行 → 数据；本次会话是否已确认过花费；正在跑的活儿（防重入）
        self._lab_voices: list[voice_lab.VoiceInfo] = []
        self._lab_cost_ok = False
        self._lab_clone_ok = False                    # 复刻的费用确认（与设计分开）
        self._lab_audio = None                        # 已选素材的本地探测结果
        self._lab_autoplay_voice = ""
        self._lab_playing_preset = False
        self._lab_preset_then = ""       # 拉取完成后接着做哪件事（preview / clone）                 # 克隆完自动试听哪条（列表刷新后触发）
        self._lab_audition_suffix = ""                # 异步试听完成后要拼在状态后面的话
        self._lab_busy = False
        self._lab_last: dict[str, str] = {}          # voice → 生成时的描述（保存时一并写日志）
        # 生成/删除的结论（尤其「复用：没有再花钱」）要活过紧随其后的那次自动刷新 —— 否则
        # 用户只看到「正在读取…/已刷新」，最关键的花钱信息一闪就没了。
        self._lab_banner = ""


    def _lab_set_status(self, msg: str) -> None:
        try:
            if self._lab_status.winfo_exists():
                self._lab_status.configure(text=msg)
        except Exception:  # noqa: BLE001
            pass


    def _lab_running(self, job: str, on: bool) -> None:
        """置忙/闲：按钮禁用 + 状态行提示（网络活儿全在守护线程里，界面绝不卡）。"""
        self._lab_busy = on
        for btn in (self._lab_gen_btn, self._lab_recipe_btn, self._lab_recipe_preview_btn,
                    self._lab_pick_btn, self._lab_clone_btn,
                    self._lab_preset_preview_btn, self._lab_preset_clone_btn,
                    self._lab_refresh_btn, self._lab_del_btn):
            try:
                if btn is not None and btn.winfo_exists():
                    btn.configure(state=tk.DISABLED if on else tk.NORMAL)
            except Exception:  # noqa: BLE001
                pass
        if not on:
            self._on_lab_select()          # 恢复「保存/删除」按选中状态决定可用性


    def _lab_ctx(self) -> tuple[str, str, str]:
        """(api_key, base_url, workspace_id)：与试听同口径，缺失时由调用方报错。"""
        return (self._resolve_api_key_safe(),
                str(self._cfg.session_base.get("base_url") or ""),
                str(self._cfg.session_base.get("workspace_id") or ""))


    def _lab_confirm_cost(self) -> bool:
        """首次生成前确认一次（钱记在用户自己账号上）；本次运行内不再重复问。"""
        if self._lab_cost_ok:
            return True
        ok = messagebox.askokcancel(
            t("生成音色会调用「声音设计」接口，费用记在你自己账号上："
              "0.2 元/个（北京地域开通后 90 天内前 10 次免费，创建失败不计费）。确定继续吗？"))
        if ok:
            self._lab_cost_ok = True
        return bool(ok)


    def _on_lab_generate(self) -> None:
        """按当前「名称 + 描述」生成（同名已存在则复用，不重复花钱）。"""
        if self._lab_busy:
            return
        name = self._lab_name_var.get().strip()
        prompt = self._lab_prompt_text.get("1.0", tk.END).strip()
        try:
            prompt = voice_lab.normalize_prompt(prompt)
        except voice_lab.VoiceLabError as exc:
            self._lab_set_status(t("生成失败：{msg}", msg=exc))
            return
        if voice_lab.prompt_looks_like_imitation(prompt):
            # 不拦，只提醒（「像播报员」这类职业比喻是允许的，硬拦会误伤）
            print(f"[gui] ⚠️ 音色描述疑似要求模仿特定人物：{prompt[:40]}…", flush=True)
        if not self._lab_confirm_cost():
            return
        api_key, base_url, ws_id = self._lab_ctx()
        if not api_key:
            self._lab_set_status(t("还没配置 API key，无法试听（见右上角「设置」）"))
            return
        nm = voice_lab.normalize_name(name)
        self._lab_name_var.set(nm)         # 回显规范后的名字，避免用户以为「我写的中文去哪了」
        self._lab_running("create", True)
        self._lab_set_status(t("正在生成音色「{v}」…", v=nm))
        print(f"[gui] 声音设计：创建/复用音色 name={nm!r} 描述={prompt[:40]!r}", flush=True)
        threading.Thread(target=self._lab_worker, args=("create",),
                         kwargs={"name": nm, "prompt": prompt, "api_key": api_key,
                                 "base_url": base_url, "workspace_id": ws_id},
                         daemon=True, name="vlt-voice-lab").start()


    def _on_lab_recipe_generate(self) -> None:
        """配方一键生成：把配方的名字与描述填进控件，再走同一条生成路径。"""
        recipe = voice_lab.recipe_from_label(self._lab_recipe_combo.get())
        if recipe is None:
            self._lab_set_status(t("请先选一个配方"))
            return
        self._lab_name_var.set(recipe.key)
        self._lab_prompt_text.delete("1.0", tk.END)
        self._lab_prompt_text.insert("1.0", recipe.prompt)
        self._on_lab_generate()


    def _on_lab_refresh(self) -> None:
        """拉本账号的自定义音色列表（列表接口不花钱）。"""
        if self._lab_busy:
            return
        api_key, base_url, ws_id = self._lab_ctx()
        if not api_key:
            self._lab_set_status(t("还没配置 API key，无法试听（见右上角「设置」）"))
            return
        self._lab_running("list", True)
        if not self._lab_banner:
            self._lab_set_status(t("正在读取本账号的音色…"))
        threading.Thread(target=self._lab_worker, args=("list",),
                         kwargs={"api_key": api_key, "base_url": base_url,
                                 "workspace_id": ws_id},
                         daemon=True, name="vlt-voice-lab").start()


    def _lab_selected(self) -> voice_lab.VoiceInfo | None:
        sel = list(self._lab_list.curselection()) if self._lab_list is not None else []
        if not sel:
            return None
        idx = int(sel[0])
        return self._lab_voices[idx] if 0 <= idx < len(self._lab_voices) else None


    def _on_lab_select(self, _event=None) -> None:
        """选中变化 → 决定「保存/删除」能不能点（没选中就是灰的，别让用户点了没反应）。"""
        has = self._lab_selected() is not None
        for btn in (self._lab_save_btn, self._lab_del_btn):
            try:
                if btn is not None and btn.winfo_exists():
                    btn.configure(state=tk.NORMAL if (has and not self._lab_busy)
                                   else tk.DISABLED)
            except Exception:  # noqa: BLE001
                pass


    def _on_lab_save(self) -> None:
        """把选中的音色保存为**当前打字译音音色**（模型必须一起换，否则合成 InvalidParameter）。"""
        info = self._lab_selected()
        if info is None:
            self._lab_set_status(t("还没选音色"))
            return
        # ⚠️ 自定义音色是**声音设计模型**出来的：合成必须用同一个 model，只改 voice 会失败
        # ⚠️ 必须写**这条音色自己的** target_model：设计族(vd)与复刻族(vc)不通用，
        #    写错就是真机上必现的 InvalidParameter（本仓库踩过：保存复刻音色却写了 vd 的模型）
        self._write_leaf(["text_input", "tts", "model"], _lab_model_of(info),
                         "保存音色模型", create=True)
        self._write_leaf(["text_input", "tts", "voice"], info.voice, "保存音色", create=True)
        if isinstance(self._cfg.text_input, dict):
            tts_cfg = self._cfg.text_input.setdefault("tts", {})
            tts_cfg["model"] = _lab_model_of(info)
            tts_cfg["voice"] = info.voice
        # 下拉里跟上（`voice_choices` 会把不在表里的当前值排到最前）
        try:
            self._tts_voice_var.set(display_name(info.voice, t))
            self._tts_voice_combo.configure(values=self._tts_voice_choices())
        except Exception:  # noqa: BLE001
            pass
        self._lab_set_status(t("音色已保存：{v}（打字译音与语音腿 B 模式都生效）",
                               v=info.name or info.voice[-12:]))
        print(f"[gui] 音色已保存 → text_input.tts.model={voice_lab.DEFAULT_TARGET_MODEL} "
              f"voice={info.voice!r}", flush=True)


    def _lab_apply_audio(self, path) -> bool:
        """把一份素材挂上界面（探测 + 报结论），合格返回 True。

        选文件与「用此范本」共用这一处 —— 免得两条路的校验/提示各走一套。
        """
        probe = voice_lab.probe_audio(path)
        self._lab_audio = probe
        name = Path(str(path)).name
        bad = voice_lab.audio_problems(probe)
        if bad:
            self._lab_set_status(t("素材不合格：{msg}", msg="；".join(bad)))
            print(f"[gui] 复刻素材不合格：{name} → {bad}", flush=True)
            return False
        summary = f"{name} · {probe.seconds:.1f}s · {probe.sample_rate}Hz · " + (
            "单声道" if probe.channels <= 1 else f"{probe.channels} 声道")
        warn = voice_lab.audio_warnings(probe)
        self._lab_set_status(t("素材合格：{msg}", msg=summary)
                             + (t("（提示：{msg}）", msg="；".join(warn)) if warn else ""))
        print(f"[gui] 复刻素材已选：{summary}（提示 {warn}）", flush=True)
        return True


    def _on_lab_pick_audio(self) -> None:
        """选一段复刻素材：本地先探时长/采样率/声道，能把不合格的当场拦下。"""
        from tkinter import filedialog          # 仅此处用到，按仓库习惯局部导入

        path = filedialog.askopenfilename(title=t("选择音频文件…"),
                                          filetypes=voice_lab.CLONE_FILETYPES)
        if not path:
            return
        self._lab_apply_audio(path)


    def _lab_preset(self):
        """当前选中的预设（下拉没建 / 越界时退回第一条）。"""
        presets = getattr(self, "_lab_presets", None) or []
        if not presets:
            return None
        idx = 0
        try:
            cur = int(self._lab_preset_combo.current())
            if 0 <= cur < len(presets):
                idx = cur
        except Exception:  # noqa: BLE001
            pass
        return presets[idx]


    def _lab_preset_begin(self, action: str) -> None:
        """范本的两件事（试听 / 一键克隆）**统一入口**：本地有就直接做，没有就先从仓库拉。

        用户点名的做法：范本音频只托管在仓库里，**需要的时候才拉**；拉回来落到本地缓存，
        之后试听/克隆都走本地文件，不再联网。
        """
        p = self._lab_preset()
        if p is None:
            return
        sample = voice_lab.find_preset_sample(APP_DIR, p)
        if sample is not None:
            self._lab_preset_run(action, sample)
            return
        if self._lab_busy:
            return
        if not voice_lab.preset_sample_urls(p):
            self._lab_set_status(t("范本的样本音频没找到：把 {n} 放到 {d}",
                                   n=p.sample_name or t("范本样本"),
                                   d=str(voice_lab.preview_dir(APP_DIR))))
            print(f"[gui] 范本没写样本文件名，无法拉取：{p.key}", flush=True)
            return
        self._lab_preset_then = action
        self._lab_running("preset_fetch", True)
        self._lab_set_status(t("正在从仓库拉取范本音频（{n}）…", n=p.sample_name))
        print(f"[gui] 拉取范本音频：{p.sample_name}（接下来用于 {action}）", flush=True)
        threading.Thread(target=self._lab_worker, args=("preset_fetch",),
                         kwargs={"app_dir": APP_DIR, "preset": p}, daemon=True).start()


    def _lab_preset_run(self, action: str, sample) -> None:
        """本地音频到手后才真正干活：`preview` = 本地试听，`clone` = 挂素材 + 走克隆。"""
        if action == "clone":
            p = self._lab_preset()
            if p is None:
                return
            self._lab_clone_name_var.set(voice_lab.normalize_name(p.key))
            if not self._lab_apply_audio(sample):
                return
            print(f"[gui] 一键克隆：按范本 {p.key} ← {sample}", flush=True)
            self._on_lab_clone()
            return
        if self._lab_playing_preset:
            self._lab_set_status(t("还在试听上一段范本…"))
            return
        try:
            pcm, seconds = voice_lab.sample_pcm_from_file(sample)
        except Exception as exc:  # noqa: BLE001
            self._lab_set_status(t("试听失败：{msg}", msg=f"{type(exc).__name__}: {exc}"))
            print(f"[gui] 范本试听解码失败：{sample} → {type(exc).__name__}: {exc}", flush=True)
            return
        self._lab_playing_preset = True
        self._lab_set_status(t("正在试听范本（{s:.1f} 秒，本地播放不花钱）…", s=seconds))
        print(f"[gui] 试听范本：{Path(str(sample)).name}（{seconds:.1f}s，本地）", flush=True)
        threading.Thread(target=self._lab_play_preset_worker, args=(pcm, seconds),
                         daemon=True).start()


    def _on_lab_preset_preview(self) -> None:
        """试听范本：本地播这份样本音频（不联网、不花钱、绝不进虚拟声卡）。"""
        self._lab_preset_begin("preview")


    def _on_lab_preset_clone(self) -> None:
        """一键克隆：范本素材挂上 + 名字预填 → 直接走克隆（费用确认、自动试听都在那条路上）。"""
        self._lab_preset_begin("clone")


    def _lab_play_preset_worker(self, pcm: bytes, seconds: float) -> None:
        """播完/播挂了都经队列回主线程改状态（播放是阻塞的，绝不能占界面线程）。"""
        try:
            _play_pcm_local(pcm)
            self._q.put(("voice_lab", "preset_play", True, f"{seconds:.1f}", None))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("voice_lab", "preset_play", False, f"{type(exc).__name__}: {exc}", None))


    def _on_lab_clone(self) -> None:
        """克隆并试听：本地校验 → 费用确认 → 后台复刻 → 刷新列表 → 自动试听。"""
        if self._lab_busy:
            return
        if self._lab_audio is None:
            self._lab_set_status(t("还没选素材音频"))
            return
        bad = voice_lab.audio_problems(self._lab_audio)
        if bad:
            self._lab_set_status(t("素材不合格：{msg}", msg="；".join(bad)))
            return
        name = voice_lab.normalize_name(self._lab_clone_name_var.get())
        if not self._lab_clone_ok:
            if not messagebox.askokcancel(
                    t("克隆会调用「声音复刻」接口：0.01 元/次，素材需服务端审核。"
                      "费用记在你自己账号上。确定继续吗？")):
                return
            self._lab_clone_ok = True
        api_key, base_url, ws_id = self._lab_ctx()
        if not api_key:
            self._lab_set_status(t("还没配置 API key，无法试听（见右上角「设置」）"))
            return
        self._lab_running("clone", True)
        self._lab_set_status(t("正在克隆音色「{v}」（上传素材并送审）…", v=name))
        threading.Thread(target=self._lab_worker, args=("clone",), kwargs={
            "name": name, "audio_path": self._lab_audio.path,
            "api_key": api_key, "base_url": base_url, "ws_id": ws_id,
        }, daemon=True).start()


    def _on_lab_preview(self) -> None:
        """试听所选音色：**有本地缓存就直接放**（不花钱）；没有就现场合成一句测试文本。

        为什么要有「现场合成」这条路：官方只在**创建**时回一份 `preview_audio` ——
        账号里原有的音色（用户在控制台/旧脚本建的，正是「我预设的那几条」）根本没有缓存，
        只按「没缓存就拒绝」的话，那些音色永远听不了，列表也就成了摆设。
        合成 26 字 ≈ 0.003 元，比「听不到没法比」便宜得多。
        """
        info = self._lab_selected()
        if info is None:
            self._lab_set_status(t("还没选音色"))
            return
        self._lab_audition(info)


    def _on_lab_recipe_preview(self) -> None:
        """试听所选配方对应的音色（账号里已有同名音色时）—— **不创建、不花 0.2 元**。"""
        if self._lab_busy:
            return
        recipe = voice_lab.recipe_from_label(self._lab_recipe_combo.get())
        if recipe is None:
            self._lab_set_status(t("请先选一个配方"))
            return
        info = voice_lab.find_by_name(self._lab_voices, recipe.key)
        if info is None:
            self._lab_set_status(t("账号里还没有「{v}」，先点「用配方一键生成」", v=recipe.label))
            return
        self._lab_audition(info)


    def _lab_audition(self, info: voice_lab.VoiceInfo, *, suffix: str = "") -> None:
        """试听的唯一实现：缓存命中 → 直接放；未命中 → 后台合成一句并落盘。

        `suffix` 用来把别的结论（例如「音色已克隆（0.01 元，审核中）」）拼在试听结果后面 ——
        否则那句会被试听状态覆盖掉，用户就看不到「刚花了钱、还在审核」这个关键信息。
        """
        label = info.name or info.voice[-12:]
        wav = voice_lab.load_preview(APP_DIR, info.voice)
        if wav:
            try:
                _play_pcm_local(tts._decode_to_24k_mono(wav))
                self._lab_set_status(t("试听完成：{v}", v=label) + suffix)
            except Exception as exc:  # noqa: BLE001
                self._lab_set_status(t("试听失败：{msg}", msg=f"{type(exc).__name__}: {exc}"))
            return
        if self._lab_busy:
            return
        api_key, base_url, ws_id = self._lab_ctx()
        if not api_key:
            self._lab_set_status(t("还没配置 API key，无法试听（见右上角「设置」）"))
            return
        self._lab_audition_suffix = suffix
        self._lab_running("audition", True)
        self._lab_set_status(t("正在合成试听「{v}」（{n} 字，约 0.003 元）…",
                               v=label, n=len(voice_lab.TEST_TEXT)))
        threading.Thread(target=self._lab_worker, args=("audition",), kwargs={
            "voice": info.voice, "model": info.target_model, "name": label,
            "api_key": api_key, "base_url": base_url, "ws_id": ws_id,
        }, daemon=True).start()


    def _on_lab_delete(self) -> None:
        """删除所选音色（不可恢复，先确认）。"""
        if self._lab_busy:
            return
        info = self._lab_selected()
        if info is None:
            self._lab_set_status(t("还没选音色"))
            return
        label = info.name or info.voice[-12:]
        if not messagebox.askokcancel(t("确定删除音色「{v}」？删除后无法恢复。", v=label)):
            return
        api_key, base_url, ws_id = self._lab_ctx()
        if not api_key:
            self._lab_set_status(t("还没配置 API key，无法试听（见右上角「设置」）"))
            return
        self._lab_running("delete", True)
        self._lab_set_status(t("正在删除音色「{v}」…", v=label))
        threading.Thread(target=self._lab_worker, args=("delete",),
                         kwargs={"voice": info.voice, "family": info.kind, "api_key": api_key,
                                 "base_url": base_url, "workspace_id": ws_id},
                         daemon=True, name="vlt-voice-lab").start()


    def _lab_worker(self, job: str, **kw) -> None:
        """守护线程体：全部网络调用在这里；结果（含失败原因）经 `_q` 回主线程。

        与「试听」同一纪律：任何失败都要把**原因**带回界面并留痕，绝不静默。
        """
        try:
            if job == "create":
                res = voice_lab.create_or_reuse(
                    kw["name"], kw["prompt"], api_key=kw["api_key"],
                    base_url=kw["base_url"], workspace_id=kw["workspace_id"])
                if res.preview_wav:
                    voice_lab.save_preview(APP_DIR, res.voice, res.preview_wav)
                self._q.put(("voice_lab", "create", True, "", res))
            elif job == "list":
                rows = voice_lab.list_voices(api_key=kw["api_key"], base_url=kw["base_url"],
                                             workspace_id=kw["workspace_id"])
                self._q.put(("voice_lab", "list", True, "", rows))
            elif job == "audition":
                # 试听已有音色：用测试文本现场合成一句（按字符计费，26 字 ≈ 0.003 元）。
                # 端点按当前线路派生 —— 与「试听」同一条纪律，别回落到模块常量。
                endpoint = endpoints.multimodal_url(kw["base_url"])
                pcm = voice_lab.sample_pcm(kw["voice"], kw["model"], api_key=kw["api_key"],
                                          endpoint=endpoint)
                if pcm:
                    voice_lab.save_preview(APP_DIR, kw["voice"], pcm)
                self._q.put(("voice_lab", "audition", True, "", {
                    "voice": kw["voice"], "name": kw["name"], "pcm": pcm}))
            elif job == "sample_check":
                # 自带兜底：查更新失败不影响任何功能，只留痕（不占状态栏）
                try:
                    changed = voice_lab.check_sample_updates(kw["app_dir"])
                except Exception as exc:                             # noqa: BLE001
                    print(f"[gui] 范本样本更新检查失败（不影响功能）："
                          f"{type(exc).__name__}: {exc}", flush=True)
                    changed = []
                if changed:                      # 没更新就**一条消息都不发**（后台动作不该搅队列）
                    self._q.put(("voice_lab", "sample_check", True, "", changed))
                return
            elif job == "preset_fetch":
                got = voice_lab.fetch_preset_sample(kw["app_dir"], kw["preset"])
                self._q.put(("voice_lab", "preset_fetch", True, "", got))
                return
            elif job == "tts_list":
                # 与 sample_check 同一纪律：**后台维护动作失败只记日志、不进队列** ——
                # 否则会塞一条永远没人关心的错误消息，搅乱别处对 _q 的断言（CI 上真红过）。
                try:
                    res = voice_lab.list_voices(api_key=kw["api_key"], base_url=kw["base_url"],
                                                workspace_id=kw["ws_id"], family="all")
                except Exception as exc:                             # noqa: BLE001
                    print(f"[gui] 拉自定义音色失败（下拉退化成只有内置，不影响其它功能）："
                          f"{type(exc).__name__}: {exc}", flush=True)
                    return
                if res:                          # 空结果也不发消息
                    self._q.put(("voice_lab", "tts_list", True, "", res))
                return
            elif job == "clone":
                # 素材可能十几兆：读盘 + base64 一律放在守护线程里（别卡界面）
                data_url = voice_lab.audio_data_url(kw["audio_path"])
                res = voice_lab.enroll_or_reuse(
                    kw["name"], data_url, api_key=kw["api_key"],
                    base_url=kw["base_url"], workspace_id=kw["ws_id"])
                self._q.put(("voice_lab", "clone", True, "", res))
            else:
                voice_lab.delete_voice(kw["voice"], api_key=kw["api_key"],
                                       base_url=kw["base_url"],
                                       workspace_id=kw["workspace_id"],
                                       family=kw.get("family", "design"))
                self._q.put(("voice_lab", "delete", True, "", kw["voice"]))
        except voice_lab.VoiceLabError as exc:
            self._q.put(("voice_lab", job, False, str(exc), None))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("voice_lab", job, False, f"{type(exc).__name__}: {exc}", None))


    def _on_lab_done(self, job: str, ok: bool, msg: str, payload) -> None:  # noqa: ANN001
        """主线程收尾：报结果、更新列表、回放新生成的试听音频。"""
        self._lab_running(job, False)
        if not ok:
            verb = {"create": t("生成失败：{msg}", msg=msg),
                    "list": t("读取失败：{msg}", msg=msg),
                    "delete": t("删除失败：{msg}", msg=msg),
                    "preset_fetch": t("拉取范本音频失败：{msg}", msg=msg)}.get(job, msg)
            self._lab_set_status(verb)
            print(f"[gui] 音色页 {job} 失败：{msg}", flush=True)
            return
        if job == "create":
            res = payload
            self._lab_last[res.voice] = ""
            if res.preview_wav:
                try:
                    _play_pcm_local(tts._decode_to_24k_mono(res.preview_wav))
                except Exception as exc:  # noqa: BLE001
                    print(f"[gui] ⚠️ 预览音频回放失败（已存盘，可点「试听所选」重放）：{exc}",
                          flush=True)
            self._lab_banner = (t("账号里已有同名音色，直接复用：{v}（没有再花钱）", v=res.name)
                                if res.reused else t("音色已生成：{v}", v=res.name))
            self._lab_set_status(self._lab_banner)
            print(f"[gui] 声音设计{'复用' if res.reused else '创建'}成功 → {res.voice!r}"
                  f"（预览 {len(res.preview_wav)}B）", flush=True)
            self._on_lab_refresh()          # 立刻刷新列表，新音色就在里面（选中它即可保存）
            return
        if job == "sample_check":
            changed = list(payload or [])
            if changed:
                self._lab_set_status(t("范本样本已更新：{n}", n="、".join(changed)))
            return

        if job == "preset_fetch":
            # 失败已在上面那张表里统一报过（「拉取范本音频失败：…」）；这里只管成功：接着把
            # 用户点的那件事做完（试听 / 一键克隆），他不用再点第二下。
            then = self._lab_preset_then
            self._lab_preset_then = ""
            if payload is not None and then:
                self._lab_preset_run(then, payload)
            return

        if job == "tts_list":
            self._tts_custom = list(payload or [])
            print(f"[gui] 本账号自定义音色 {len(self._tts_custom)} 条 → 并入「打字译音」下拉",
                  flush=True)
            self._refresh_tts_voice_combo()
            return

        if job == "preset_play":
            self._lab_playing_preset = False
            if ok:
                self._lab_set_status(t("范本试听完成（{s} 秒，本地播放）", s=str(payload or "")))
            else:
                self._lab_set_status(t("试听失败：{msg}", msg=str(msg)))
            return

        if job == "list":
            self._lab_voices = list(payload or [])
            if self._lab_voices:
                self._tts_custom = list(self._lab_voices)      # 与「打字译音」下拉共用同一批
                self._refresh_tts_voice_combo()
            local = voice_lab.label_mapper(voice_lab.load_labels(APP_DIR))
            _label = lambda vid, fallback: self._voice_label(vid, fallback, local)

            if self._lab_list is not None:
                self._lab_list.delete(0, tk.END)
                for row in voice_lab.describe(
                        self._lab_voices, label_of=_label,
                        tag_of=lambda kind: t("[复刻] ") if kind == "clone" else ""):
                    self._lab_list.insert(tk.END, row)
                if self._lab_voices:
                    self._lab_list.selection_set(0)
            self._on_lab_select()
            # 刚克隆完 → 选中它并**直接试听**（用户要的「克隆并试听」是一条动作）
            want = self._lab_autoplay_voice
            self._lab_autoplay_voice = ""
            played = False
            if want:
                for i, v in enumerate(self._lab_voices):
                    if v.voice == want:
                        self._lab_list.selection_clear(0, tk.END)
                        self._lab_list.selection_set(i)
                        self._on_lab_select()
                        self._lab_audition(v, suffix="　｜" + (self._lab_banner or ""))
                        self._lab_banner = ""      # 已经拼进试听状态里了，别再覆盖它
                        played = True
                        break
                else:
                    print(f"[gui] 克隆结果没出现在刷新后的列表里：{want!r}", flush=True)
            if not played:
                text = self._lab_banner or t("已刷新：{n} 条自定义音色", n=len(self._lab_voices))
                self._lab_banner = ""
                self._lab_set_status(text)
            return
        if job == "clone":
            res = payload
            # 把名字记到本地登记表 → 列表里显示人话而不是一长串 id（下次刷新也认）
            voice_lab.save_label(APP_DIR, res.voice, res.name)
            self._lab_last[res.voice] = ""
            self._lab_banner = (t("账号里已有同名音色，直接复用：{v}（没有再花钱）", v=res.name)
                                if res.reused else
                                t("音色已克隆：{v}（0.01 元，服务端审核中）", v=res.name))
            self._lab_set_status(self._lab_banner)
            print(f"[gui] 声音复刻{'复用' if res.reused else '创建'}成功 → {res.voice!r}"
                  f"（名字 {res.name!r}）", flush=True)
            self._lab_autoplay_voice = res.voice           # 列表刷新后自动试听
            self._on_lab_refresh()
            return
        if job == "delete":
            self._lab_banner = t("已删除音色：{v}", v=str(payload)[-12:])
            self._lab_set_status(self._lab_banner)
            self._on_lab_refresh()
            return
        if job == "audition":
            label = str((payload or {}).get("name") or "")
            pcm = (payload or {}).get("pcm") or b""
            if not pcm:
                self._lab_set_status(t("试听失败：{msg}", msg=t("服务端没回音频")))
                return
            suffix = self._lab_audition_suffix
            self._lab_audition_suffix = ""
            try:
                _play_pcm_local(tts._decode_to_24k_mono(pcm))
                self._lab_set_status(t("试听完成：{v}（已存本地，下次直接放）", v=label) + suffix)
            except Exception as exc:  # noqa: BLE001
                self._lab_set_status(t("试听失败：{msg}", msg=f"{type(exc).__name__}: {exc}"))
            print(f"[gui] 试听合成完成 → {label!r}（{len(pcm)}B）", flush=True)


    def _on_voice_mode_change(self) -> None:
        """A/B 热切换：改内存配置 → 写回 config.yaml → 通知在跑的引擎重建会话。

        「热」在哪：两条腿都**不用重启程序**。引擎侧重建会话是按需的（受连接预算保护，
        RPM 10 下每次切换算一次连接），切换期间的文字输出不受影响。
        """
        mode = "tts" if self._voice_mode_var.get() == "tts" else "realtime"
        audio_cfg = self._cfg.output.setdefault("audio", {})
        audio_cfg["mode"] = mode
        self._save_audio_mode(mode)
        for eng in list(self._engines):
            try:
                eng.set_voice_output(mode)
            except Exception as exc:  # noqa: BLE001
                print(f"[gui] 切换译音音源失败：{type(exc).__name__}: {exc}", flush=True)
        label = t("B 打字腿同款音色（TTS）") if mode == "tts" else t("A 实时模型音色")
        self._set_status("info", t("译音音源已切到 {label}", label=label))
        self._refresh_voice_mode_hint()


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


    def _refresh_voice_mode_hint(self) -> None:
        """给 A/B 选项配一句「现在会怎样」的说明（含没开总开关时的提醒）。"""
        if not hasattr(self, "_voice_mode_hint"):
            return
        on = bool(((self._cfg.output or {}).get("audio") or {}).get("enabled", False))
        if not on:
            txt = t("⚠️ 还没勾选「译音输出」：本项暂不生效（没有虚拟声卡，VRChat 里听不到）。")
        elif self._voice_mode_var.get() == "tts":
            txt = t("语音腿的译音由本地流式 TTS 合成，音色与打字腿一致（每句约 +0.5s）；音色用下方「打字译音」。")
        else:
            txt = t("语音腿的译音来自实时模型本身，延迟最低；音色用下方「说话译音」。")
        self._voice_mode_hint.configure(text=txt)


    def _voice_label(self, vid: str, fallback: str, local=None) -> str:
        """一个音色的**人话名字**：源码登记表（display_name）→ 本地登记表 → id 反推的短名。

        ⚠️ `display_name` 对**未登记**的音色会原样回一长串 id（它是给下拉框用的），直接拿它会把
        列表行/下拉挤成一串 id、名字全看不见（用户报「我之前做的音色怎么不见了」就是这个）。
        所以只在它**确实给出人话**（≠ 原 id）时才用。
        """
        human = display_name(vid, t)
        if human and human != vid:
            return human
        if local is None:
            local = voice_lab.label_mapper(voice_lab.load_labels(APP_DIR))
        return local(vid, fallback)


    def _tts_voice_choices(self) -> list[str]:
        """**打字译音**下拉候选：内置目录 + 本账号的自定义音色 + 当前值（都显示成人话）。

        为什么要把自定义音色排进来：用户自己炼/复刻的音色（`clear_auto`、`MetroPolice`…）不在
        内置表里，老实现只有它**正好是当前值**时才显示 —— 一旦切去别的音色就再也选不回来
        （他报过「clear_auto 不见了」，就是这个）。
        """
        cur = str((self._cfg.text_input.get("tts") or {}).get("voice") or "")
        ids = list(voice_choices(cur, TTS_VOICES))
        for info in list(getattr(self, "_tts_custom", None) or []):
            vid = str(getattr(info, "voice", "") or "")
            if vid and vid not in ids:
                ids.append(vid)
        local = voice_lab.label_mapper(voice_lab.load_labels(APP_DIR))
        out: list[str] = []
        for vid in ids:
            short = voice_lab._name_of(vid) or vid          # 未登记时用 id 反推的短名，别甩一串 id
            label = vid if vid in TTS_VOICES else self._voice_label(vid, short, local)
            if label not in out:                       # 同名去重（两个 id 反推出同一个短名时别重复）
                out.append(label)
        return out


    def _refresh_tts_voice_combo(self) -> None:
        """把候选重新铺进下拉（保留当前选中项，别让用户的选择被刷掉）。"""
        combo = getattr(self, "_tts_voice_combo", None)
        if combo is None:
            return
        try:
            combo.configure(values=self._tts_voice_choices())
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 打字译音下拉刷新失败（不影响其它功能）：{type(exc).__name__}: {exc}",
                  flush=True)
        else:
            print(f"[gui] 打字译音下拉候选 {len(self._tts_voice_choices())} 条（含自定义音色 "
                  f"{len(getattr(self, '_tts_custom', None) or [])} 条）", flush=True)


    def _kick_sample_check(self) -> None:
        """每次启动查一次范本样本有没有更新（清单 + sha 比对，几 KB 的请求）。

        与账号音色那次不同：**失败只记日志、不上状态栏**（后台维护动作，离线启动不该打扰用户），
        只有真更新到了才提示一句。
        """
        print("[gui] 启动检查：范本样本更新", flush=True)
        threading.Thread(target=self._lab_worker, args=("sample_check",),
                         kwargs={"app_dir": APP_DIR}, daemon=True).start()


    def _kick_tts_voice_list(self) -> None:
        """启动后台拉一次本账号的自定义音色，好把它们排进「打字译音」下拉。

        只读、免费；没配 key / 网络不通就静默跳过（下拉退化成只有内置音色，不影响其它功能）。
        """
        try:
            api_key, base_url, ws_id = self._lab_ctx()
        except Exception as exc:  # noqa: BLE001
            print(f"[gui] 拉自定义音色前取配置失败（跳过）：{type(exc).__name__}: {exc}",
                  flush=True)
            return
        if not api_key:
            return
        threading.Thread(target=self._lab_worker, args=("tts_list",), kwargs={
            "api_key": api_key, "base_url": base_url, "ws_id": ws_id},
            daemon=True).start()


    def _tts_voice_id_from_input(self, text: str) -> str:
        """把下拉里显示的**名字**（或用户手打的 id）还原成**真 id**。

        下拉里显示的是人话（内置名 / 登记表名 / id 反推的短名），写进配置和发给 API 的必须是真 id。
        顺序：内置原样 → 源码登记表 `real_id()` → 本账号自定义音色的三种写法（真 id / 短名 / 显示名）。
        """
        raw = (text or "").strip()
        if not raw:
            return ""
        if raw in TTS_VOICES:
            return raw
        got = real_id(raw, t)
        if got and got != raw:                       # 源码登记表认得这个显示名
            return got
        local = voice_lab.label_mapper(voice_lab.load_labels(APP_DIR))
        for info in list(getattr(self, "_tts_custom", None) or []):
            vid = str(getattr(info, "voice", "") or "")
            if not vid:
                continue
            short = voice_lab._name_of(vid) or vid
            if raw in (vid, short, self._voice_label(vid, short, local)):
                return vid
        return got or raw


    def _tts_model_for_voice(self, voice: str) -> str:
        """这条音色该配哪个合成模型：自定义音色用**它自己的** target_model，内置回内置默认。

        ⚠️ 设计族(vd) / 复刻族(vc) / 内置三者模型互不通用，写错在真机上必 `InvalidParameter`
        （本项目已经踩过两次：保存复刻音色写了设计族模型；这里以前只写 voice 不写 model）。
        """
        for info in list(getattr(self, "_tts_custom", None) or []):
            if str(getattr(info, "voice", "")) == voice:
                got = str(getattr(info, "target_model", "") or "").strip()
                if got:
                    return got
        # 缓存里没有（例如刚手打的 id）→ 按 id 形态兜底：`-vc-` 是复刻族、`-vd-` 是设计族。
        # 宁可这样猜，也别回落到内置模型（那在真机上必 InvalidParameter）。
        low = (voice or "").lower()
        if "-vc-" in low:
            return voice_lab.CLONE_TARGET_MODEL
        if "-vd-" in low:
            return voice_lab.DEFAULT_TARGET_MODEL
        return tts.DEFAULT_MODEL


    def _set_tts_model_config(self, model: str) -> None:
        """把合成模型写回 `text_input.tts.model`（与 voice 同段，老配置可能整段没有 → create）。"""
        self._write_leaf(["text_input", "tts", "model"], model, "保存打字译音模型", create=True)

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
