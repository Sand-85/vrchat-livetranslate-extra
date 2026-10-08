"""引擎启动 / 停止 / 生命周期的纯逻辑。

从 ``vlt.gui.TranslationGUI`` 的 K 区（约 L1648-L2198）提取而来。
所有 Tk 控件与可变状态通过 :class:`EngineCtx` 传入，函数本身不持有 ``self`` 引用。

⚠️ 本模块 **不许** ``import vlt.gui``（防循环引用）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from .output.micproxy import MODE_TRANSLATED
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import config as _cfg_mod
from . import crashlog, gui_chat, gui_voice
from . import platform
from .config import Direction
from .config_io import (
    _fmt_scalar,
    _write_config_text,
    _yaml_set_or_create,
)
from .engine import Engine, EngineEvents
from .i18n import t
from .output.overlay import OverlayConfig
from .ui_theme import CLOSE_WAIT_STOP_S, STOP_WAIT_S


# ================================================================ 上下文

@dataclass
class EngineCtx:
    """引擎生命周期所需的全部 **控件引用**、**可变状态** 与 **回调**。

    可变状态由 gui.py 的薄壳方法管理，本模块的函数通过参数接收，不直接持有。
    """
    # ── 核心可变状态 ──
    engines: list = field(default_factory=list)
    engine_dirs: list = field(default_factory=list)
    specs: list = field(default_factory=list)
    sinks: set = field(default_factory=set)
    pending_starts: int = 0
    current: dict = field(default_factory=dict)
    auto_scroll: bool = True
    closing: bool = False

    # ── 覆盖层输出 ──
    overlay_out: Any = None          # WristOverlay 实例
    desktop_out: Any = None          # DesktopOverlay 实例
    desktop_dragging: bool = False

    # ── 控件引用 ──
    power_btn: Any = None            # ttk.Button（单按钮开关；headless 下为 None）
    power_state_fn: Optional[Callable] = None    # ("idle"|"running"|"stopping") -> None

    # ── Tk 变量 ──
    direction_var: Any = None        # tk.StringVar（翻译方向）
    chatbox_var: Any = None          # tk.BooleanVar
    overlay_var: Any = None          # tk.BooleanVar
    desktop_var: Any = None          # tk.BooleanVar
    vmic_var: Any = None             # tk.BooleanVar（译音输出）
    lang_pair: dict = field(default_factory=dict)

    # ── 桌面字幕控件变量 ──
    desktop_alpha_var: Any = None    # tk.DoubleVar
    desktop_alpha_lbl: Any = None    # ttk.Label
    desktop_font_var: Any = None     # tk.StringVar / tk.IntVar
    desktop_srcfont_var: Any = None  # tk.StringVar / tk.IntVar
    desktop_w_var: Any = None        # tk.DoubleVar
    desktop_h_var: Any = None        # tk.DoubleVar
    desktop_drag_btn: Any = None     # ttk.Button

    # ── 桌面字幕内部状态 ──
    desktop_save_job: Any = None     # root.after 的 job id
    desktop_alpha_touched: bool = False
    desktop_tuned: set = field(default_factory=set)

    # ── 气泡（用于推送给覆盖层）──
    bubbles: list = field(default_factory=list)

    # ── 基础设施 ──
    q: Any = None                    # queue.Queue
    root: Any = None                 # tk.Tk / tk.Misc
    cfg: Any = None                  # AppConfig
    start_job: Any = None            # root.after 的 job id（错开启动）
    stop_done_evt: Any = None        # threading.Event

    # ── 回调 ──
    set_status_fn: Optional[Callable] = None           # (level, msg) -> None
    on_engine_text_fn: Optional[Callable] = None       # (who, srcid, src, txt, final) -> None
    set_text_input_enabled_fn: Optional[Callable] = None  # (bool) -> None
    refresh_api_key_fn: Optional[Callable] = None      # () -> None
    start_room_fn: Optional[Callable] = None           # () -> None
    stop_room_fn: Optional[Callable] = None            # () -> None
    refresh_room_status_fn: Optional[Callable] = None  # () -> None
    sync_gate_probe_fn: Optional[Callable] = None      # () -> None
    stop_gate_probe_fn: Optional[Callable] = None      # () -> None
    maybe_replace_on_exit_fn: Optional[Callable] = None  # () -> None
    destroy_root_fn: Optional[Callable] = None         # () -> None
    save_ui_state_fn: Optional[Callable] = None        # () -> None
    cancel_poll_fn: Optional[Callable] = None          # () -> None（取消 poll 循环）
    proxy_fn: Optional[Callable] = None                # () -> MicProxy | None（每次现取：代理会被重开/关闭）


# ================================================================ API 密钥

def resolve_api_key_safe(ctx: EngineCtx) -> str:
    """安全获取 API 密钥（委托给 ``gui_voice``）。"""
    return gui_voice.resolve_api_key_safe(ctx.cfg)


# ================================================================ 语言推送

def push_lang_to_engines(ctx: EngineCtx) -> None:
    """把当前语言设置推送到所有正在运行的引擎。"""
    a = ctx.lang_pair.get("source", "")
    b = ctx.lang_pair.get("target", "") or "en"
    for name, (src, tgt) in (("mine", (a, b)), ("theirs", (b, a or "zh"))):
        d = ctx.cfg.directions.setdefault(name, Direction())
        d.source_lang = src
        d.target_lang = tgt
    for eng, direction in zip(ctx.engines, ctx.engine_dirs):
        if not eng.running:
            continue
        src, tgt = (a, b) if direction == "mine" else (b, a or "zh")
        eng.set_languages(src, tgt)


# ================================================================ 启动


def bind_gui_callbacks(ctx: EngineCtx, gui) -> None:
    """设置 EngineCtx 上的回调引用（从 gui 实例读取）。"""
    c = ctx
    c.set_status_fn = gui._set_status; c.on_engine_text_fn = gui._on_engine_text
    c.set_text_input_enabled_fn = gui._set_text_input_enabled; c.refresh_api_key_fn = gui._refresh_api_key_in_cfg
    c.start_room_fn = gui._start_room; c.stop_room_fn = gui._stop_room
    c.refresh_room_status_fn = gui._refresh_room_status_label
    c.sync_gate_probe_fn = gui._sync_gate_level_probe; c.stop_gate_probe_fn = gui._stop_gate_probe
    c.maybe_replace_on_exit_fn = gui._maybe_replace_on_exit
    c.destroy_root_fn = lambda: gui._root.destroy(); c.save_ui_state_fn = gui._save_ui_state
    c.power_state_fn = gui._set_power_state  # 开始/停止单按钮的唯一刷新入口
    c.cancel_poll_fn = lambda: gui_chat.cancel_poll(gui._chat_ctx, gui._root)
    c.proxy_fn = lambda: gui._proxy          # 现取：代理会被设置页重开/关闭，实例会变


def _proxy_of(ctx: EngineCtx):
    """取常驻麦克风代理；没有（Linux / 用户关掉 / 虚拟声卡没打开）则 None。"""
    if ctx.proxy_fn is None:
        return None
    try:
        return ctx.proxy_fn()
    except Exception as exc:              # noqa: BLE001
        print(f"[proxy] ⚠️ 读取麦克风代理失败（按「无代理」处理，译音输出回落到引擎自建）："
              f"{type(exc).__name__}: {exc}", flush=True)
        return None


def set_power_state(ctx: EngineCtx, state: str) -> None:
    """刷主界面那个「开始/停止」单按钮。回调缺失（headless / 老 ctx）时静默跳过。"""
    if ctx.power_state_fn is None:
        return
    try:
        ctx.power_state_fn(state)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] ⚠️ 刷新开始/停止按钮失败（忽略）：{type(exc).__name__}: {exc}",
              flush=True)


def notify_proxy_translation(ctx: EngineCtx, active: bool) -> None:
    """翻译启停 → 通知代理（译音档只在翻译运行时允许；停止即强制回落并锁定原声档）。"""
    p = _proxy_of(ctx)
    if p is None:
        return
    try:
        p.set_translation_active(bool(active))
        print(f"[proxy] 翻译{'开始' if active else '停止'} → "
              f"{'允许切到译音档' if active else '强制回落原声档'}"
              f"（当前档位 {p.mode}）", flush=True)
    except Exception as exc:              # noqa: BLE001
        print(f"[proxy] ⚠️ 同步翻译状态失败（忽略，代理仍可用）："
              f"{type(exc).__name__}: {exc}", flush=True)


def start(ctx: EngineCtx) -> bool:
    """启动翻译引擎（主入口，约 105 行）。

    检查 API key → 读方向 / 输出 → 建 specs → 配方向 → 起覆盖层 →
    起引擎 → 改按钮态。

    返回：因「没配 API key」而被第一段拦下时返回 ``True``（gui.py 薄壳据此
    自动弹设置窗引导填 key）；其余情况（已在跑 / 正常启动）返回 ``False``。
    """
    if any(e.running for e in ctx.engines):
        return

    # 防呆：key 可能在运行期被保存/清除/改环境，先重解再检查
    if ctx.refresh_api_key_fn:
        ctx.refresh_api_key_fn()
    if not (ctx.cfg.session_base.get("api_key") or "").strip():
        # 没 key 就别白连一次（会撞 401），直接把用户送到填 key 的地方
        if ctx.set_status_fn:
            ctx.set_status_fn("error", t("还没配置 API key —— 点右上角「API key ›」填一个再开始"))
        # 返回 True：光给状态栏红字不够，用户点了「开始」却什么都没发生会以为卡了。
        # gui.py 薄壳据此自动弹设置窗，把填 key 的入口送到眼前。
        return True

    d = ctx.direction_var.get()

    sinks: set[str] = set()
    if ctx.chatbox_var and ctx.chatbox_var.get():
        sinks.add("chatbox")
    if ctx.overlay_var and ctx.overlay_var.get():
        sinks.add("overlay")
    if ctx.desktop_var and ctx.desktop_var.get():
        # "desktop" 只是界面层的标记（字幕窗由界面持有，引擎不认这个 sink），
        # 放在这里是为了让「只勾桌面字幕」也能通过下面的"至少选一个输出"检查。
        sinks.add("desktop")
    if not sinks:
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", t("请至少选择一个输出"))
        return

    a = ctx.lang_pair.get("source", "")
    b = ctx.lang_pair.get("target", "") or "en"
    specs: list[tuple] = []
    if d in ("mine", "dual"):
        specs.append(("mine", "mine", "mic", a, b))
    if d in ("theirs", "dual"):
        specs.append(("theirs", "theirs", "loopback", b, a or "zh"))

    # chatbox 只承载「我说的话」的译文（气泡在别人眼里代表我发言）。
    # 若本次方向不含「我说」，用户勾了 chatbox 也一条都发不出去 —— 必须像
    # 译音输出那样明说，别让人对着"没反应的 chatbox"排查（禁静默降级）。
    chatbox_warn = ""
    if "chatbox" in sinks and not any(s[1] == "mine" for s in specs):
        _zh = ("chatbox 只发「我说的话」的译文（当前方向不含它）→ 本次 chatbox 不会输出；"
               "对方的译文看手腕屏／聊天区")
        chatbox_warn = t(_zh)
        print(f"[gui] ⚠️ {_zh}", flush=True)

    # 启动时把「方向 + 每条腿的来源/语言 + 输出面」写进日志。
    _label = {"mine": "我说的话", "theirs": "别人说", "dual": "双向同时"}.get(d, d)
    print(f"[gui] 启动：方向={_label}({d}) | 输出={','.join(sorted(sinks)) or '无'} | "
          f"语言对={a}→{b}", flush=True)
    for _w, _dir, _src, _sl, _tl in specs:
        print(f"[gui]   腿 {_dir}：来源={'麦克风' if _src == 'mic' else '游戏音频(loopback)'}"
              f" | {_sl}→{_tl}", flush=True)

    # 译音输出：勾选框（总开关）+ 方向级开关，两者是「与」关系，都开才出声。
    want_audio = bool(ctx.vmic_var.get()) if ctx.vmic_var else False
    audio_warn = ""
    if want_audio and not any(s[1] == "mine" for s in specs):
        _zh = "译音输出只对「我说的话」方向有效（当前方向不含它）→ 本次已忽略"
        audio_warn = t(_zh)
        print(f"[gui] ⚠️ {_zh}", flush=True)
        want_audio = False
    if isinstance(ctx.cfg.output, dict):
        ctx.cfg.output.setdefault("audio", {})["enabled"] = want_audio
        _dev = (ctx.cfg.output.get("audio") or {}).get("device_name") or "自动回退链"
    else:
        _dev = "自动回退链"
    # 代理在跑 → 译音不自己开流，而是灌进代理那条**常驻**虚拟声卡输出流。
    # 日志里必须区分两条路：出问题时「声音从哪来」是第一个要问的。
    _proxy = _proxy_of(ctx)
    _via = ""
    if _proxy is not None:
        _via = "，经麦克风代理"
        try:
            _dev = _proxy.translated_sink.device_name or _dev
        except Exception:                          # noqa: BLE001
            pass
    print(f"[gui]   译音输出={'开' if want_audio else '关'}（虚拟声卡：{_dev}{_via}）", flush=True)

    # 配置每条腿的方向
    for _who, direction, _src, src_lang, tgt_lang in specs:
        dd = ctx.cfg.directions.setdefault(direction, Direction())
        dd.source_lang = src_lang
        dd.target_lang = tgt_lang
        dd.output_audio = want_audio and direction == "mine"

    # 初始化运行态
    ctx.current = {}
    ctx.auto_scroll = True
    ctx.engines = []
    ctx.engine_dirs = []
    ctx.specs = specs
    ctx.sinks = sinks
    ctx.pending_starts = len(specs)

    # 手腕屏 / 桌面字幕由界面持有，内容镜像聊天区（两个方向都进同一块屏）
    start_overlay(ctx)
    start_desktop(ctx)
    # 连接意图开着就确保房间在跑（stop() 会连房间一起停；start_room 幂等，已在跑则无操作）
    if ctx.start_room_fn:
        ctx.start_room_fn()

    # 先告诉代理「翻译开始了」：译音档只在翻译运行时允许切，顺序反了会被拒
    notify_proxy_translation(ctx, True)
    # 开始翻译就默认走「译音」档（用户要求：勾了译音输出，对方就该听到译音）。
    # ⚠️ 只在**译音真的会有声音**时才切：勾选框没勾 / 方向不含「我说的话」时
    #    want_audio 是 False，切过去等于让对方听静音 —— 比原声更糟，所以不切。
    if want_audio:
        _p_mode = _proxy_of(ctx)
        if _p_mode is not None:
            if _p_mode.set_mode(MODE_TRANSLATED):
                print("[proxy] 开始翻译 → 默认档位切到「译音」"
                      "（可在主界面「原声/译音」按钮切回）", flush=True)
            else:
                print("[proxy] ⚠️ 开始翻译时默认切「译音」失败"
                      "（保持原档位；主界面按钮仍可手动切）", flush=True)
    # 启动第一个引擎（后续引擎由 start_engine 错开 300ms 调度）
    start_engine(ctx, 0)

    # 按钮态
    set_power_state(ctx, "running")
    if ctx.set_text_input_enabled_fn:
        ctx.set_text_input_enabled_fn(d in ("mine", "dual"))

    warns = [w for w in (audio_warn, chatbox_warn) if w]
    if warns:
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", "；".join(warns))
    elif d == "mine":
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("正在启动…（只翻译你说的话；要翻译对方/视频请选「双向同时」）"))
    else:
        if ctx.set_status_fn:
            ctx.set_status_fn("info",
                              t("正在启动（双向）…") if len(specs) == 2 else t("正在启动…"))


def start_engine(ctx: EngineCtx, index: int) -> None:
    """启动第 *index* 个引擎实例（递归错开 300ms 启动多条腿）。"""
    specs = ctx.specs
    if index >= len(specs):
        return
    who, direction, source, src_lang, tgt_lang = specs[index]
    # 引擎不碰手腕屏：它由界面持有（一块屏显示两个方向的对话）。
    # 若交给两个引擎各自创建，会撞 `OverlayError_KeyInUse`（用户实测）。
    # chatbox 只发「我说的话」的译文——theirs 腿不需要它（与 engine._chatbox_wanted 同义，双保险）。
    own_sinks = {s for s in ctx.sinks if s not in ("overlay", "desktop")}
    if direction == "theirs":
        own_sinks.discard("chatbox")
    events = EngineEvents(
        # 上行挂在界面这一层（不改 engine.py）：on_engine_text_fn 把文本塞进界面队列，
        # 再顺带把「我自己说的话」的源文发进房间。_srcid 绑定这条腿的真实来源
        # （mic/loopback）—— should_publish 靠它把 loopback 那条腿排除掉（防二次广播/回环）。
        on_text=(lambda src, txt, final, _who=who, _srcid=source:
                 ctx.on_engine_text_fn(_who, _srcid, src, txt, final))
                if ctx.on_engine_text_fn else lambda *a: None,
        on_status=lambda lvl, msg, _who=who: on_engine_status(ctx.q, lvl, msg, _who),
        on_stats=lambda s: ctx.q.put(("stats", s)),
    )
    # 麦克风代理在跑 → 译音灌进代理那条**常驻**输出流（引擎不自建、也不关它，
    # 见 engine._setup_virtualmic 的 _owns_virtualmic=False）；
    # 没代理 → None，引擎自建虚拟声卡输出（旧行为，随翻译启停）。
    proxy = _proxy_of(ctx)
    eng = Engine(
        cfg=ctx.cfg,
        direction=direction,
        source=source,
        sinks=own_sinks,
        events=events,
        config_path=_cfg_mod.DEFAULT_CONFIG,
        audio_sink=(proxy.translated_sink if proxy is not None else None),
    )
    ctx.engines.append(eng)
    ctx.engine_dirs.append(direction)
    ctx.pending_starts -= 1
    eng.start()
    # 引擎起来了 → 电平改由引擎那条腿提供，探针必须让位
    # （同一时刻只允许一路 loopback，否则两路抢同一个采集端点）
    if ctx.sync_gate_probe_fn:
        ctx.sync_gate_probe_fn()
    if ctx.pending_starts > 0:
        # 两个引擎错开 300ms 启动，避免同时抢占音频设备
        ctx.start_job = ctx.root.after(300, lambda: start_engine(ctx, index + 1))


def on_engine_status(q, lvl: str, msg: str, who: str) -> None:
    """引擎状态既要进状态栏，也要进日志（stdout）。

    状态栏文字不落盘 —— 少了这一行，「译音输出为什么没出声」「设备为什么没匹配上」
    这类提示在用户发来的日志里完全看不到，只能靠猜。
    """
    print(f"[{who}][{lvl}] {msg}", flush=True)
    q.put(("status", lvl, msg))


# ================================================================ 手腕屏（界面持有）

def start_overlay(ctx: EngineCtx, *, force: bool = False) -> bool:
    """按勾选状态接管 SteamVR 的手腕屏。失败只禁用这一项，绝不影响翻译。

    手腕屏由 **界面** 持有而不是某个引擎：手腕上只该有一块屏，内容是最近几句对话
    （镜像聊天区）。交给两个引擎各自创建会撞 ``OverlayError_KeyInUse``（用户实测）。

    *force* =True 用于「勾上就起」那条路（此时还没点开始翻译，``ctx.sinks`` 里没有 overlay）。
    返回是否 **真的起来了** —— 调用方要靠它决定失败时是否自动退回勾选。
    """
    if ctx.overlay_out is not None:
        return True                       # 已经起来了，别重复建（会撞 KeyInUse）
    if not force and "overlay" not in ctx.sinks:
        return False
    try:
        ctx.overlay_out = platform.create_wrist_overlay(
            OverlayConfig.from_dict(ctx.cfg.overlay), config_path=_cfg_mod.DEFAULT_CONFIG)
        if not ctx.overlay_out.start():
            ctx.overlay_out = None        # start() 内部已打印原因
            return False
        push_overlay(ctx, force=True)
        return True
    except Exception as exc:              # noqa: BLE001
        ctx.overlay_out = None
        print(f"[gui] ⚠️ 手腕屏初始化异常，已禁用（翻译不受影响）："
              f"{type(exc).__name__}: {exc}", flush=True)
        return False


def push_overlay(ctx: EngineCtx, force: bool = False) -> None:
    """把聊天区最近几条推给手腕屏（同一块屏显示两个方向的对话）。"""
    if ctx.overlay_out is None:
        return
    try:
        entries = [(b.who, b.source, b.text, b.label) for b in ctx.bubbles[-8:]]
        ctx.overlay_out.update_entries(entries, force=force)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 手腕屏刷新失败：{type(exc).__name__}: {exc}", flush=True)


# ================================================================ 桌面字幕（界面持有）

def desktop_cfg(cfg) -> dict:
    """安全取 ``cfg.desktop_overlay`` 段（可能是 dict 或 None）。"""
    d = cfg.desktop_overlay
    return d if isinstance(d, dict) else {}


def start_desktop(ctx: EngineCtx, *, force: bool = False) -> bool:
    """把桌面字幕窗拉起来（PC 桌面模式：贴在 VRChat 窗口上的叠加窗）。

    与手腕屏同一套约定：由 **界面** 持有（内容是聊天区的镜像），
    失败只禁用这一项，绝不影响翻译。
    """
    if ctx.desktop_out is not None:
        return True
    if not force and "desktop" not in ctx.sinks:
        return False
    try:
        from .output.desktop_overlay import DesktopOverlay, DesktopOverlayConfig
        cfg = DesktopOverlayConfig.from_dict(desktop_cfg(ctx.cfg))
        out = DesktopOverlay(cfg, config_path=_cfg_mod.DEFAULT_CONFIG, root=ctx.root)
        if not out.start():
            return False                  # start() 内部已打印原因
        ctx.desktop_out = out
        push_desktop(ctx, force=True)
        return True
    except Exception as exc:              # noqa: BLE001
        ctx.desktop_out = None
        print(f"[gui] ⚠️ 桌面字幕初始化异常，已禁用（翻译不受影响）："
              f"{type(exc).__name__}: {exc}", flush=True)
        return False


def stop_desktop(ctx: EngineCtx) -> None:
    """关闭桌面字幕窗并复位拖拽状态。"""
    if ctx.desktop_out is None:
        return
    try:
        ctx.desktop_out.close()
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 关闭桌面字幕时出错（忽略）：{exc}", flush=True)
    ctx.desktop_out = None
    ctx.desktop_dragging = False
    # 文案跟着复位：字幕窗都关掉了还写着「锁定位置」，与真实状态不符
    btn = ctx.desktop_drag_btn
    if btn is not None:
        try:
            btn.configure(text=t("解锁拖动"))
        except Exception:                 # noqa: BLE001
            pass


def push_desktop(ctx: EngineCtx, force: bool = False) -> None:
    """把聊天区最近几条推给桌面字幕（与手腕屏同一份内容）。

    ⚠️ 条目形状必须与 ``push_overlay`` 一致（4 元组，带说话人昵称）：桌面字幕与手腕屏
    共用 ``overlay.render_conversation``，房间里的成员靠这个 label 才显示得出昵称。
    """
    if ctx.desktop_out is None:
        return
    try:
        entries = [(b.who, b.source, b.text, b.label) for b in ctx.bubbles[-8:]]
        ctx.desktop_out.update_entries(entries, force=force)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 桌面字幕刷新失败：{type(exc).__name__}: {exc}", flush=True)


def toggle_desktop_drag(ctx: EngineCtx) -> None:
    """解锁 / 锁定桌面字幕拖动。

    字幕窗默认 **鼠标穿透**（不挡着点 VRChat），穿透开着时窗口收不到鼠标事件，
    所以要拖必须先解锁；锁定 = 把落点折算成锚点+偏移写回配置并恢复穿透。
    """
    if ctx.desktop_out is None:
        ctx.desktop_dragging = False
        if ctx.set_status_fn:
            ctx.set_status_fn("warn", t("桌面字幕还没开启，先勾上「桌面字幕」再解锁拖动"))
        return
    ctx.desktop_dragging = not ctx.desktop_dragging
    try:
        ctx.desktop_out.set_draggable(ctx.desktop_dragging)
    except Exception as exc:              # noqa: BLE001
        ctx.desktop_dragging = False
        print(f"[gui] 切换桌面字幕拖动失败：{type(exc).__name__}: {exc}", flush=True)
        return
    btn = ctx.desktop_drag_btn
    if btn is not None:
        try:
            btn.configure(text=t("锁定位置") if ctx.desktop_dragging else t("解锁拖动"))
        except Exception:                 # noqa: BLE001
            pass
    if ctx.desktop_dragging:
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("桌面字幕已解锁：拖动字幕窗到想要的位置，放好后点「锁定位置」"))
    else:
        save_desktop_cfg(ctx)
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("桌面字幕位置已记住"))


def schedule_desktop_save(ctx: EngineCtx) -> None:
    """防抖 300ms 后把桌面字幕参数写回 config.yaml。"""
    if ctx.desktop_save_job is not None:
        try:
            ctx.root.after_cancel(ctx.desktop_save_job)
        except Exception:
            pass
    ctx.desktop_save_job = ctx.root.after(300, lambda: save_desktop_cfg(ctx))


def save_desktop_cfg(ctx: EngineCtx, *, config_path: Path | None = None) -> None:
    """把桌面字幕的参数写回 config.yaml 的 ``desktop_overlay:`` 段。

    写什么：用户 **真动过** 的滑块（字号 / 尺寸 / 透明度）+ 拖动折算出的锚点/偏移。

    ⚠️ 滑块值只在 **用户真的动过** 时才写：无条件写的话，用户只是把字幕拖了个位置，
    滑块上那些（默认）值就被写进配置并热重载生效 —— 表现为「拖一下位置，字号/透明度突然变了」。
    """
    ctx.desktop_save_job = None
    p = config_path if config_path is not None else _cfg_mod.DEFAULT_CONFIG
    if not p.exists():
        return
    try:
        text = p.read_text(encoding="utf-8")
        updates: list[tuple[list[str], str]] = []
        mem: dict[str, Any] = {}
        tuned = ctx.desktop_tuned
        if ctx.desktop_alpha_touched and ctx.desktop_alpha_var is not None:
            a = float(ctx.desktop_alpha_var.get())
            updates.append((["desktop_overlay", "alpha"], _fmt_scalar(a)))
            mem["alpha"] = a
        if "font_size" in tuned and ctx.desktop_font_var is not None:
            v = int(float(ctx.desktop_font_var.get()))
            updates.append((["desktop_overlay", "font_size"], str(v)))
            mem["font_size"] = v
        if "source_font_size" in tuned and ctx.desktop_srcfont_var is not None:
            v = int(float(ctx.desktop_srcfont_var.get()))
            updates.append((["desktop_overlay", "source_font_size"], str(v)))
            mem["source_font_size"] = v
        if tuned & {"panel_width", "panel_height"} and ctx.desktop_w_var is not None and ctx.desktop_h_var is not None:
            w = int(float(ctx.desktop_w_var.get()))
            h = int(float(ctx.desktop_h_var.get()))
            updates.append((["desktop_overlay", "size_px"], f"[{w}, {h}]"))
            mem["size_px"] = [w, h]
        if ctx.desktop_out is not None:
            for key, val in (ctx.desktop_out.snap_to_config() or {}).items():
                if key in ("offset", "pos"):
                    updates.append((["desktop_overlay", key], f"[{val[0]}, {val[1]}]"))
                else:
                    updates.append((["desktop_overlay", key], str(val)))
        if not updates:
            return
        for key_path, value in updates:
            text = _yaml_set_or_create(text, key_path, value)
        _write_config_text(p, text)
        # 同步内存快照：start_desktop() 是按 ctx.cfg 建窗的，不同步的话
        # 「关掉桌面字幕再勾上」会用旧值重建窗口。
        if mem:
            if not isinstance(ctx.cfg.desktop_overlay, dict):
                ctx.cfg.desktop_overlay = {}
            ctx.cfg.desktop_overlay.update(mem)
        print("[gui] 桌面字幕参数已写入 config.yaml："
              + " ".join(f"{'/'.join(k)}={v}" for k, v in updates), flush=True)
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 保存桌面字幕参数失败：{exc}", flush=True)


# ================================================================ 停止

def stop(ctx: EngineCtx) -> None:
    """停止翻译：**绝不在界面线程等引擎收尾**（真机实测冻 20s = 窗口无响应）。

    先对 **所有** 引擎并发下发停止信号（采集立刻停），收尾交给后台线程，
    界面只留一行「正在停止…」，收尾完成由 ``_poll`` 从队列里收到通知再恢复。
    """
    ctx.pending_starts = 0
    ctx.specs = []
    if ctx.start_job is not None:
        try:
            ctx.root.after_cancel(ctx.start_job)
        except Exception:
            pass
        ctx.start_job = None

    engines = list(ctx.engines)
    for eng in engines:
        try:
            eng.request_stop()            # 只发信号：并发下发，谁都不等谁
        except Exception as exc:          # noqa: BLE001
            print(f"[gui] 下发停止信号失败（忽略）：{type(exc).__name__}: {exc}", flush=True)

    # 立刻让代理回落原声档（不等引擎收尾）：译音源没了，麦克风该马上重新直通
    notify_proxy_translation(ctx, False)

    # 关闭覆盖层
    _stop_overlay(ctx)
    stop_desktop(ctx)

    # 停房间
    if ctx.stop_room_fn:
        ctx.stop_room_fn()
    if ctx.refresh_room_status_fn:
        ctx.refresh_room_status_fn()

    ctx.engines = []                      # 立刻移走：收尾由后台线程负责
    ctx.engine_dirs = []

    if ctx.set_text_input_enabled_fn:
        ctx.set_text_input_enabled_fn(False)

    # 引擎没了 → 设置窗若还开着且勾了「启用」，电平交回独立探针
    if ctx.sync_gate_probe_fn:
        ctx.sync_gate_probe_fn()

    if not engines:
        # 没有引擎在手：但可能还有上一次的收尾在飞（只有 on_close 这条重复调用路径会走到）
        if ctx.stop_done_evt and ctx.stop_done_evt.is_set():
            set_power_state(ctx, "idle")
        if ctx.set_status_fn:
            ctx.set_status_fn("info", t("已停止"))
        return

    # 收尾期间禁掉「开始翻译」：旧引擎还在关麦克风/虚拟声卡，立刻重启会抢设备
    set_power_state(ctx, "stopping")
    if ctx.set_status_fn:
        ctx.set_status_fn("info", t("正在停止…"))

    if ctx.stop_done_evt:
        ctx.stop_done_evt.clear()
    threading.Thread(target=wait_stop_done, args=(ctx, engines), daemon=True,
                     name="vlt-stop-wait").start()


def _stop_overlay(ctx: EngineCtx) -> None:
    """关闭手腕屏。"""
    if ctx.overlay_out is None:
        return
    try:
        ctx.overlay_out.close()
    except Exception as exc:              # noqa: BLE001
        print(f"[gui] 关闭手腕屏时出错（忽略）：{exc}", flush=True)
    ctx.overlay_out = None


def wait_stop_done(ctx: EngineCtx, engines: list) -> None:
    """（**后台线程**）等引擎真正收尾完，再入队让 ``_poll`` 恢复界面。绝不碰 Tk。"""
    t0 = time.monotonic()
    stuck: list[int] = []
    for i, eng in enumerate(engines):
        try:
            if not eng.wait_stopped(STOP_WAIT_S):
                stuck.append(i)
        except Exception as exc:          # noqa: BLE001
            print(f"[gui] ⚠️ 等引擎收尾出错（忽略）：{type(exc).__name__}: {exc}", flush=True)
    elapsed = time.monotonic() - t0
    if stuck:
        print(f"[gui] ⚠️ 停止收尾超时（{STOP_WAIT_S:.0f}s）：第 {stuck} 个引擎还没退出"
              "（仍在关麦克风/虚拟声卡；界面照常恢复，状态栏会如实提示仍在收尾）",
              flush=True)
    ctx.q.put(("stop_done", elapsed, len(engines), bool(stuck)))
    if ctx.stop_done_evt:
        ctx.stop_done_evt.set()


# ================================================================ 窗口关闭

def on_close(ctx: EngineCtx) -> None:
    """关窗口：先停引擎（**有界** 等采集线程真正退出），再销毁窗口。

    顺序很重要——如果先销毁窗口再去等引擎，主线程会阻塞在一个已经失效的
    Tk 事件循环上，界面看起来就是"卡死后闪退"。
    """
    ctx.closing = True
    # 先取消 poll 循环，防止窗口销毁后 after 回调仍在事件队列里
    if ctx.cancel_poll_fn:
        try:
            ctx.cancel_poll_fn()
        except Exception:                       # noqa: BLE001
            pass
    # 先放掉电平探针占着的采集设备
    if ctx.stop_gate_probe_fn:
        ctx.stop_gate_probe_fn()
    try:
        stop(ctx)
    except Exception as exc:
        print(f"[gui] 停止引擎时出错（继续关闭）：{exc}", file=sys.stderr)
    try:
        if ctx.stop_done_evt and not ctx.stop_done_evt.wait(CLOSE_WAIT_STOP_S):
            print(f"[gui] ⚠️ 退出时等引擎收尾超过 {CLOSE_WAIT_STOP_S:.0f}s，"
                  f"直接关闭（进程退出会释放设备）", flush=True)
    except Exception:
        pass
    try:
        # 「稍后更新」的另一半：正常退出时替换（绝不自动拉起新版）。
        if ctx.maybe_replace_on_exit_fn:
            ctx.maybe_replace_on_exit_fn()
    except Exception as exc:
        print(f"[update] ⚠️ 退出时替换出现异常（继续关闭）：{exc}", file=sys.stderr)
    try:
        if ctx.destroy_root_fn:
            ctx.destroy_root_fn()
        else:
            ctx.root.destroy()
    except Exception:
        pass
    crashlog.close()
