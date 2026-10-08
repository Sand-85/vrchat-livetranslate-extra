"""「开始翻译就默认走译音档」的验收（改动 2）。

跑法：.venv/Scripts/python.exe tests/test_start_defaults_to_translated.py

## 钉住的事

`gui_engine.start()` 在 `notify_proxy_translation(ctx, True)` 之后、`start_engine(ctx, 0)`
之前，**只在译音真的会有声音时**（`want_audio` 为真）把代理默认切到「译音」档：

1. 代理可用 + `vmic_var` 勾着 + 方向 `mine` → `start()` 后 `proxy.mode == MODE_TRANSLATED`，
   且 stdout 里有那行切档留痕；
2. 同上但 `vmic_var` 关着 → 档位**仍是** `MODE_PASSTHROUGH`，且**没有**切档留痕
   （不许静默切过去让对方听静音）；
3. 代理为 `None` → 不抛异常，按钮状态照常 `"running"`（旧行为：引擎自建虚拟声卡）；
4. 方向 `theirs`（不含「我说的话」）→ 同样不切档、不抛
   （`want_audio` 被 `start()` 里既有的逻辑降级为 False）。

## 打桩纪律（与 test_proxy_wiring.py 同一条）

代理用**假对象**（记录 `set_translation_active` / `set_mode` 调用、带 `mode` 属性），
**绝不构造真 `MicProxy`**（会真开虚拟声卡流）；引擎一律换成 `_FakeEngine`；
GUI 用 headless（不建 Tk 窗口），控件用 `_Widget` 占位。

## 故障注入自检（开发期手动跑过，非本文件用例）

把 `start()` 里新增的「默认切译音」那段注释掉 → 用例 1 必须变红
（`proxy.mode` 停在 `passthrough`、stdout 没有切档留痕）→ 再还原。详见交付报告。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 构造 TranslationGUI 之前钉死界面语言：CI 是英文系统，不钉就会按 en 取词条。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

import vlt.gui_engine as gui_engine  # noqa: E402
from vlt.output.micproxy import MODE_PASSTHROUGH, MODE_TRANSLATED  # noqa: E402

_SWITCH_TRACE = "默认档位切到「译音」"      # start() 里切档成功那行留痕的关键片段


# ---------------------------------------------------------------- 占位替身（复制自 test_proxy_wiring，不 import 别的测试文件）


class _Widget:
    """占位控件：只记 configure 的关键字（headless 下没有真 Tk 控件）。"""

    def __init__(self) -> None:
        self.kw: dict = {}

    def configure(self, **kw) -> None:  # noqa: ANN003
        self.kw.update(kw)

    def cget(self, key: str):
        return self.kw.get(key)


class _Var:
    """占位 Tk 变量（BooleanVar / StringVar 都只需要 get/set）。"""

    def __init__(self, value) -> None:               # noqa: ANN001
        self._v = value

    def get(self):
        return self._v

    def set(self, value) -> None:                    # noqa: ANN001
        self._v = value


class _Root:
    """占位 Tk root：只提供 after/after_cancel。"""

    def after(self, *_a, **_k) -> str:
        return "job"

    def after_cancel(self, *_a, **_k) -> None:
        return None


class _FakeSink:
    """`TranslatedSink` 替身：引擎只认这几个成员。"""

    device_name = "FakeCard Output"
    opened = True

    def __init__(self) -> None:
        self.pushed: list[bytes] = []
        self.sentences = 0
        self.close_calls = 0

    def push(self, pcm) -> None:                     # noqa: ANN001
        self.pushed.append(pcm)

    def end_sentence(self) -> None:
        self.sentences += 1

    def close(self) -> None:
        self.close_calls += 1


class _FakeProxy:
    """`MicProxy` 替身：只记录接线相关的调用，绝不开任何音频流。

    `set_mode` 复刻真代理语义：翻译未运行时拒绝切到译音档（返回 False）。
    """

    def __init__(self, mode: str = MODE_PASSTHROUGH) -> None:
        self.mode = mode
        self.opened = True
        self.active_calls: list[bool] = []
        self.mode_calls: list[str] = []
        self.close_calls = 0
        self.translated_sink = _FakeSink()

    def set_translation_active(self, active: bool) -> None:
        self.active_calls.append(bool(active))
        if not active and self.mode == MODE_TRANSLATED:
            self.mode = MODE_PASSTHROUGH             # 与真代理同语义：停翻译即回落

    def set_mode(self, mode: str) -> bool:
        self.mode_calls.append(mode)
        if mode == MODE_TRANSLATED and True not in self.active_calls:
            return False
        self.mode = mode
        return True

    def close(self) -> None:
        self.close_calls += 1
        self.opened = False


class _FakeEngine:
    """引擎替身：只记录构造参数与启停调用，绝不真连。"""

    instances: list = []

    def __init__(self, **kw) -> None:
        self.kw = kw
        self.running = False
        self.start_calls = 0
        self.stop_requests = 0
        _FakeEngine.instances.append(self)

    def start(self) -> None:
        self.start_calls += 1
        self.running = True

    def request_stop(self) -> None:
        self.stop_requests += 1
        self.running = False

    def wait_stopped(self, timeout: float = 5.0) -> bool:   # noqa: ARG002
        return True


@contextlib.contextmanager
def _fake_engine_class():
    """把 `gui_engine.Engine` 换成替身（`EngineEvents` 保持真的：它只是个数据类）。"""
    saved = gui_engine.Engine
    _FakeEngine.instances.clear()
    gui_engine.Engine = _FakeEngine                  # type: ignore[assignment]
    try:
        yield _FakeEngine.instances
    finally:
        gui_engine.Engine = saved                    # type: ignore[assignment]


def _make_gui():                                     # noqa: ANN202
    """起一个 headless GUI，钉上占位 root 与一个假 API key（start() 要检查它）。"""
    from vlt.gui import TranslationGUI

    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        gui = TranslationGUI(headless=True)
    gui._root = _Root()                              # type: ignore[assignment]
    gui._power_btn = _Widget()                       # type: ignore[assignment]
    gui._cfg.session_base["api_key"] = "test-key-not-real"
    _i18n.set_language("zh")
    return gui


def _arm_engine_ctx(gui, *, vmic: bool, direction: str):   # noqa: ANN001, ANN202
    """把 headless GUI 的 EngineCtx 武装到「能跑 start()」的最小可用状态。"""
    gui._sync_engine_ctx()
    ctx = gui._engine_ctx
    ctx.direction_var = _Var(direction)
    ctx.chatbox_var = _Var(True)
    ctx.overlay_var = _Var(False)                    # 手腕屏/桌面字幕都不勾 → 直接短路
    ctx.desktop_var = _Var(False)
    ctx.vmic_var = _Var(vmic)                        # 「译音输出」勾选框：本文件的核心变量
    ctx.lang_pair = {"source": "zh", "target": "en"}
    ctx.refresh_api_key_fn = None                    # 别让用例去碰真实 key 解析
    ctx.start_room_fn = None
    ctx.refresh_room_status_fn = None
    ctx.sync_gate_probe_fn = None
    ctx.set_text_input_enabled_fn = None
    return ctx


def _run_start(gui, ctx) -> str:                     # noqa: ANN001, ANN202
    """在假引擎类 + 吞日志下跑一次 start()，返回它打出的 stdout。"""
    with _fake_engine_class():
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            gui_engine.start(ctx)
    return buf.getvalue()


# ---------------------------------------------------------------- 1) 勾了译音 + mine → 默认切译音


def test_defaults_to_translated_when_vmic_on() -> None:
    """代理可用 + vmic 勾着 + 方向 mine → 默认切到译音档，且留痕。"""
    gui = _make_gui()
    fake = _FakeProxy()
    gui._proxy = fake
    ctx = _arm_engine_ctx(gui, vmic=True, direction="mine")

    out = _run_start(gui, ctx)

    assert fake.mode == MODE_TRANSLATED, f"开始翻译应默认切到译音档：{fake.mode!r}"
    assert MODE_TRANSLATED in fake.mode_calls, f"应调用过 set_mode(translated)：{fake.mode_calls!r}"
    # 顺序硬约束：切档发生在「翻译已激活」之后，否则真代理会拒掉
    assert fake.active_calls and fake.active_calls[0] is True, \
        f"切档前必须先 set_translation_active(True)：{fake.active_calls!r}"
    assert _SWITCH_TRACE in out, f"切档成功必须留痕：{out!r}"
    print("  ✓ (1) vmic 开 + mine → proxy.mode=translated，且 stdout 有切档留痕")


# ---------------------------------------------------------------- 2) 没勾译音 → 不切（别让对方听静音）


def test_no_switch_when_vmic_off() -> None:
    """vmic 关着 → 档位仍是 passthrough，且没有切档留痕。"""
    gui = _make_gui()
    fake = _FakeProxy()
    gui._proxy = fake
    ctx = _arm_engine_ctx(gui, vmic=False, direction="mine")

    out = _run_start(gui, ctx)

    assert fake.mode == MODE_PASSTHROUGH, f"vmic 关着不该切档（否则对方听静音）：{fake.mode!r}"
    assert MODE_TRANSLATED not in fake.mode_calls, f"不该调用 set_mode(translated)：{fake.mode_calls!r}"
    assert _SWITCH_TRACE not in out, f"不该有切档留痕：{out!r}"
    print("  ✓ (2) vmic 关 → 档位仍 passthrough、无切档留痕（不静默切过去）")


# ---------------------------------------------------------------- 3) 无代理 → 不抛、按钮照常 running


def test_proxy_none_does_not_raise_and_button_runs() -> None:
    """代理为 None → 不抛异常，按钮状态照常 running（旧行为：引擎自建虚拟声卡）。"""
    gui = _make_gui()
    gui._proxy = None
    recorded: list[str] = []
    ctx = _arm_engine_ctx(gui, vmic=True, direction="mine")
    ctx.power_state_fn = recorded.append             # 记录按钮态

    out = _run_start(gui, ctx)                        # 不抛即通过

    assert "running" in recorded, f"无代理时按钮态仍应是 running：{recorded!r}"
    assert _SWITCH_TRACE not in out, f"无代理不该有切档留痕：{out!r}"
    print("  ✓ (3) 代理 None → 不抛、按钮态 running、无切档留痕")


# ---------------------------------------------------------------- 4) 方向 theirs → 不切、不抛


def test_no_switch_when_direction_theirs() -> None:
    """方向 theirs（不含「我说的话」）→ want_audio 被降级为 False，同样不切档、不抛。"""
    gui = _make_gui()
    fake = _FakeProxy()
    gui._proxy = fake
    ctx = _arm_engine_ctx(gui, vmic=True, direction="theirs")

    out = _run_start(gui, ctx)                        # 不抛即通过

    assert fake.mode == MODE_PASSTHROUGH, \
        f"theirs 方向 want_audio 应降级为 False，不该切档：{fake.mode!r}"
    assert MODE_TRANSLATED not in fake.mode_calls, f"不该调用 set_mode(translated)：{fake.mode_calls!r}"
    assert _SWITCH_TRACE not in out, f"不该有切档留痕：{out!r}"
    print("  ✓ (4) 方向 theirs → 不切档、不抛（want_audio 已降级）")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_defaults_to_translated_when_vmic_on,
        test_no_switch_when_vmic_off,
        test_proxy_none_does_not_raise_and_button_runs,
        test_no_switch_when_direction_theirs,
    ]
    print("test_start_defaults_to_translated:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
