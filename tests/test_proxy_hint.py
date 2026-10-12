"""首次启用麦克风代理的说明弹窗：**只弹一次** + headless 绝不污染用户配置。

跑法：.venv/Scripts/python.exe tests/test_proxy_hint.py

为什么单独一份：这条链路横跨「配置标记 + GUI 接线 + 弹窗」三处，任何一处断掉的表现都很安静 ——
  · 标记没落盘 → 每次启动都弹（用户被烦到）；
  · headless 也落盘 → `--self-test` / 打包自检把用户的标记改成「已提示」，用户永远看不到说明（**踩过的形态**）；
  · 早退判断丢掉 → 代理每次启动都弹。
所以三条各自一条用例，另加「关掉代理时不该弹」与「关窗幂等」。

打桩纪律（与 test_proxy_wiring 同一条）：不碰任何真实音频设备 ——
`platform.create_mic_proxy` 换成假代理，GUI 用 headless，弹窗用 opener 替身（CI 上不开 Tk 窗）。
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

CONFIG_TEXT = (
    "ui:\n"
    "  lang: zh\n"
    "session:\n"
    "  api_key: test-key-not-real\n"
    "output:\n"
    "  capture:\n"
    "    mic_device: ''\n"
    "  audio:\n"
    "    enabled: true\n"
    "    device: ['FakeCard Output']\n"
    "    sample_rate: 48000\n"
    "    buffer_ms: 300\n"
    "    max_buffer_ms: 2000\n"
    "    proxy:\n"
    "      enabled: {enabled}\n"
    "      passthrough_buffer_ms: 150\n"
)


class _Widget:
    def __init__(self) -> None:
        self.kw: dict = {}

    def configure(self, **kw) -> None:
        self.kw.update(kw)

    def cget(self, k):
        return self.kw.get(k)


class _FakeProxy:
    """假代理：只满足 `_start_proxy` / `_refresh_voice_mode_btn` 用到的那几个面。"""

    def __init__(self) -> None:
        self.mode = "passthrough"
        self.closed = False
        self.started = False

    def start(self) -> bool:
        self.started = True
        return True

    def close(self) -> None:
        self.closed = True


def _read_proxy_cfg(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return ((doc.get("output") or {}).get("audio") or {}).get("proxy") or {}


@contextlib.contextmanager
def _harness(*, enabled: bool = True, headless: bool = True, hint_shown: bool | None = None):
    """沙箱配置 + headless GUI + 假代理 + 假 opener。yield (gui, cfg_path, opened, buf)。"""
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod
    import vlt.gui_proxy as gui_proxy_mod      # 代理那一节现在真住在 vlt/gui_proxy.py（E4 拆分）
    from vlt import gui_proxy_hint

    tmp = Path(tempfile.mkdtemp(prefix="vlt-proxy-hint-"))
    cfg_path = tmp / "config.yaml"
    text = CONFIG_TEXT.format(enabled="true" if enabled else "false")
    if hint_shown is not None:
        text = text.replace("      passthrough_buffer_ms: 150\n",
                            f"      passthrough_buffer_ms: 150\n      hint_shown: {str(hint_shown).lower()}\n")
    cfg_path.write_text(text, encoding="utf-8", newline="\n")

    saved_env = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    os.environ.pop("DASHSCOPE_API_KEY", None)

    saved = (cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod._is_test_process,
             gui_proxy_mod.platform.create_mic_proxy, gui_proxy_hint._open_window)
    cfg_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui_mod._is_test_process = lambda: False                 # 只在本上下文里放开（假代理，不开流）
    # ⚠️ 代理实现住在 `vlt.gui_proxy`（E4 的 GUI 拆分）：打桩点要落在**真正用到**它的模块上。
    #    `_yaml_write` / `DEFAULT_CONFIG` / `_is_test_process` 仍是 `vlt.gui` 的属性
    #    （gui_proxy 经 `_m(self)` 现取），所以那几样照旧打在 `gui_mod` 上。
    gui_proxy_mod.platform.create_mic_proxy = lambda *a, **k: _FakeProxy()

    opened: list = []
    gui_proxy_hint._open_window = lambda g: opened.append(g)

    from vlt.gui import TranslationGUI

    gui = None
    try:
        # ⚠️ 构造一律用 headless=True（CI 上不开真 Tk 窗、也不让构造函数自己去起代理），
        #    然后**只翻 `_headless` 标志**来模拟「正式运行时」这条路 —— 那条路的判据就在标志上。
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            gui = TranslationGUI(headless=True)
        gui._headless = headless
        gui._voice_ctx.voice_mode_btn = _Widget()
        gui._proxy_check = _Widget()
        gui._passthrough_spin = _Widget()
        gui._translated_spin = _Widget()
        gui._proxy_hint = _Widget()
        gui._cfg.session_base["api_key"] = "test-key-not-real"
        yield gui, cfg_path, opened
    finally:
        cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod._is_test_process, \
            gui_proxy_mod.platform.create_mic_proxy, gui_proxy_hint._open_window = saved
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from vlt import i18n
        i18n.set_language("zh")


def _start(gui) -> tuple[bool, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = gui._start_proxy()
    return ok, buf.getvalue()


def test_headless_never_shows_and_never_writes_flag() -> None:
    """① headless（跑用例 / --self-test / 打包自检）：不弹、**也不落盘标记**。"""
    with _harness(headless=True) as (gui, cfg_path, opened):
        ok, out = _start(gui)
        assert ok is True, f"假代理应能起来：{out!r}"
        assert opened == [], "headless 下不许弹窗"
        cfg = _read_proxy_cfg(cfg_path)
        assert "hint_shown" not in cfg, \
            f"headless 不许写「已提示」标记（会把用户配置写坏、用户再也看不到说明）：{cfg!r}"
    print("  ✓ ① headless：不弹窗、不写标记")


def test_first_start_shows_once_then_persists() -> None:
    """② 首次真正启用 → 弹一次 + 落盘；再次启动代理不再弹。"""
    with _harness(headless=False) as (gui, cfg_path, opened):
        ok, out = _start(gui)
        assert ok is True, out
        assert len(opened) == 1, f"首次启用应弹一次，实际 {len(opened)} 次：{out!r}"
        assert "[proxy] 首次启用说明已弹出" in out, f"必须留痕（禁静默）：{out!r}"
        cfg = _read_proxy_cfg(cfg_path)
        assert cfg.get("hint_shown") is True, f"标记必须落盘：{cfg!r}"

        # 关掉代理再开一次（同一进程里模拟「下次再启用」）
        with contextlib.redirect_stdout(io.StringIO()):
            gui._close_proxy()
        ok2, out2 = _start(gui)
        assert ok2 is True, out2
        assert len(opened) == 1, f"已提示过就不该再弹：{len(opened)} 次"
        assert "[proxy] 首次启用说明已弹出" not in out2, "已提示过不该再留这行痕"
    print("  ✓ ② 首次启用弹一次 + 落盘；之后不再弹")


def test_second_process_reads_flag_and_stays_quiet() -> None:
    """③ 配置里已有标记（= 上次弹过）→ 新进程启动代理时不弹。"""
    with _harness(headless=False, hint_shown=True) as (gui, _cfg_path, opened):
        ok, out = _start(gui)
        assert ok is True, out
        assert opened == [], f"配置里已标记提示过，不许再弹：{out!r}"
    print("  ✓ ③ 配置已有标记：不弹")


def test_disabled_proxy_does_not_hint() -> None:
    """④ 代理在配置里关着 → 既不弹也不写标记（提示只属于「真的启用了」这件事）。"""
    with _harness(headless=False, enabled=False) as (gui, cfg_path, opened):
        ok, _out = _start(gui)
        assert ok is False, "关掉时代理不该起来"
        assert opened == [], "没启用就不该弹说明"
        assert "hint_shown" not in _read_proxy_cfg(cfg_path), "没启用就不该写标记"
    print("  ✓ ④ 代理关着：不弹、不写标记")


def test_close_is_idempotent_and_wired_into_exit() -> None:
    """⑤ 弹窗关闭幂等，且 `_on_close` 真的会把它收掉。"""
    from vlt import gui_proxy_hint

    with _harness(headless=False) as (gui, _cfg_path, _opened):
        class _Win:
            def __init__(self) -> None:
                self.destroyed = 0

            def destroy(self) -> None:
                self.destroyed += 1

        win = _Win()
        gui._proxy_hint_win = win
        gui._close_proxy_hint()                      # 走 delegate → gui_proxy_hint.close
        assert win.destroyed == 1 and gui._proxy_hint_win is None, "关窗应销毁并摘引用"
        gui_proxy_hint.close(gui)                    # 再关一次：幂等，不许抛
        assert win.destroyed == 1, "第二次 close 应是无操作"
    print("  ✓ ⑤ 关窗幂等 + 退出时收窗")


if __name__ == "__main__":
    test_headless_never_shows_and_never_writes_flag()
    test_first_start_shows_once_then_persists()
    test_second_process_reads_flag_and_stays_quiet()
    test_disabled_proxy_does_not_hint()
    test_close_is_idempotent_and_wired_into_exit()
    print("\nALL PASSED")
