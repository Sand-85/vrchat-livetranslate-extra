"""VRChat 实时同传 · 本仓库增强（音色页 / 译音音色 / 音源选择）。

这些是增强线独有的 GUI 功能，集中在本 mixin 里，与上游把纯逻辑拆到 `gui_*.py` 同构；
挂在 `TranslationGUI` 上，所以内部照旧用 `self.xxx`。
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
import threading, tkinter as tk
from pathlib import Path
from tkinter import ttk
from . import i18n
from .i18n import t
from .paths import APP_DIR
from . import ui_tk
from .ui_theme import (QIANWEN_SIGNUP_URL as QIANWEN_SIGNUP_URL)
 


def _lab_model_of(info) -> str:                                      # noqa: ANN001
    """这条音色该配哪个合成模型：**它自己的** target_model 优先，缺失时才按族回落。

    设计(vd)/复刻(vc)两族的模型不通用 —— 这是本仓库实测过的硬约束（写错必 InvalidParameter）。
    """
    got = str(getattr(info, "target_model", "") or "").strip()
    if got:
        return got
    kind = str(getattr(info, "kind", "design") or "design")
    return voice_lab.CLONE_TARGET_MODEL if kind == "clone" else voice_lab.DEFAULT_TARGET_MODEL



class VoicelabMixin:

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
                                         style="Key.TEntry", font=ui_tk.FONT_UI)
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
            highlightbackground=BORDER, highlightcolor=ACCENT, font=ui_tk.FONT_UI)
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
                               style="Key.TEntry", font=ui_tk.FONT_UI)
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
            body, state="readonly", width=30, font=ui_tk.FONT_UI,
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
            highlightbackground=BORDER, highlightcolor=ACCENT, font=ui_tk.FONT_UI,
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
        # 下拉里跟上（`voice_choices` 会把不在表里的当前值排到最前）；回显**下拉里那一项**的人话名字
        try:
            self._tts_voice_var.set(self._tts_voice_display(info.voice))
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

    def _lab_play_async(self, pcm: bytes, *, voice: str = "",
                        done: str = "", suffix: str = "") -> None:
        """把试听播放挪到守护线程（`_play_pcm_local` 会**阻塞整段音频**，最长 60s）。

        为什么必须挪：以前这三次试听都在**主线程**里等着播完 —— 用户点一下「试听」，音色页
        整个界面就卡住不动（与「卡死」是同一个体感）。播完/播挂都走队列回主线程改状态。

        `done` 为空 = **不动状态栏**（例如「生成」那条路，横幅另有结论）；失败一律只留日志。
        """
        threading.Thread(target=self._lab_play_worker,
                         args=(pcm, voice, suffix, done),
                         daemon=True, name="vlt-voice-lab-play").start()

    def _lab_play_worker(self, pcm, voice, suffix, done) -> None:
        info = {"voice": voice, "suffix": suffix, "done": done}
        try:
            _play_pcm_local(pcm)
            self._q.put(("voice_lab", "preview_play", True, "", info))
        except Exception as exc:  # noqa: BLE001
            self._q.put(("voice_lab", "preview_play", False,
                         f"{type(exc).__name__}: {exc}", info))

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
            # 缓存里**混着两种格式**（create 的 WAV 容器 / sample_pcm 的裸 PCM）→ 按头分派，
            # 统一成 24k 单声道再播；播放是阻塞的（最长 60s），必须挪出主线程，否则界面卡住。
            try:
                pcm = voice_lab.preview_pcm(wav)
            except Exception as exc:  # noqa: BLE001
                self._lab_set_status(t("试听失败：{msg}", msg=f"{type(exc).__name__}: {exc}"))
                return
            self._lab_set_status(t("正在试听「{v}」…", v=label))
            self._lab_play_async(pcm, voice=label,
                                 done=t("试听完成：{v}", v=label), suffix=suffix)
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
                    pcm = voice_lab.preview_pcm(res.preview_wav)
                except Exception as exc:  # noqa: BLE001
                    print(f"[gui] ⚠️ 预览音频解码失败（已存盘，可点「试听所选」重放）：{exc}",
                          flush=True)
                    pcm = b""
                if pcm:
                    # 播放挪出主线程；`done=""` = 不动状态栏（下面的横幅才是这条路的结论）
                    self._lab_play_async(pcm, voice=res.name or "")
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

        if job == "preview_play":
            info = payload or {}
            done = str(info.get("done") or "")
            if ok:
                if done:
                    self._lab_set_status(done + str(info.get("suffix") or ""))
                return
            print(f"[gui] 试听播放失败：{msg}", flush=True)
            if done:
                self._lab_set_status(t("试听失败：{msg}", msg=msg))
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
                pcm = voice_lab.preview_pcm(pcm)
            except Exception as exc:  # noqa: BLE001
                self._lab_set_status(t("试听失败：{msg}", msg=f"{type(exc).__name__}: {exc}"))
                print(f"[gui] 试听合成完成 → {label!r}（{len(payload.get('pcm') or b'')}B，解码失败）",
                      flush=True)
                return
            self._lab_play_async(
                pcm, voice=label, suffix=suffix,
                done=t("试听完成：{v}（已存本地，下次直接放）", v=label))
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
            label = self._tts_voice_display(vid, local)
            if label not in out:                       # 同名去重（两个 id 反推出同一个短名时别重复）
                out.append(label)
        return out

    def _tts_voice_display(self, voice: str, local=None) -> str:
        """「打字译音」下拉/输入框里该显示的**人话名字**（与 `_tts_voice_choices` 同一口径）。

        为什么要单独一处：保存/切换音色后要把下拉回显成**候选里那一项**，否则用户看到的是
        一长串 id（与下拉里显示的名字对不上，看起来像"没选上"）。配置里存的仍是真 id。
        """
        if voice in TTS_VOICES:
            return voice
        short = voice_lab._name_of(voice) or voice     # 未登记时用 id 反推的短名，别甩一串 id
        if local is None:
            local = voice_lab.label_mapper(voice_lab.load_labels(APP_DIR))
        return self._voice_label(voice, short, local)

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
