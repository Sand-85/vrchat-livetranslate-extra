#!/usr/bin/env python
"""「⚙ 设置 → 音色」页的离线接线验收（无头：不开窗口、不联网、不花一分钱）。

验的是这条链路能不能端到端走通：
    填描述 → 生成（镜像/复用的判定）→ 列表里出现 → 选中 → 试听（读本地缓存）
    → 保存为当前音色（**模型 + 音色一起写**）→ 下拉里跟上 → 删除

四个必须钉住的点（都是真会踩的坑）：
1. **保存必须同时写 `text_input.tts.model`** —— 自定义音色是「声音设计」模型的产物，
   只改 voice 不改 model，合成会直接 `InvalidParameter`（用户看到的是「音色不支持」）；
2. **同名音色走复用、不重复计费** —— 反复点「生成」不能反复花钱（文案也要说清「没有再花钱」）；
3. **没选中时按钮要拦下**，不能点了没反应；
4. **花钱前必须确认一次** —— 钱记在用户自己账号上，取消就一个请求都不发。

跑法：.venv/Scripts/python.exe tests/test_voice_lab_gui.py
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "voicelabtest0123456789abcdef")

import vlt.i18n as _i18n                                            # noqa: E402
_i18n.detect_system_language = lambda: "zh"

import vlt.config as cfg_mod                                        # noqa: E402
import vlt.gui as gui_mod                                           # noqa: E402
from vlt import voice_lab as vl                                     # noqa: E402
from vlt.gui import TranslationGUI                                  # noqa: E402


class _Root:
    """占位 Tk root（无头模式下 `_poll` 会用到 after/after_cancel）。"""

    def after(self, *_a, **_k) -> str:
        return "job"

    def after_cancel(self, *_a, **_k) -> None:
        return None

    def update(self) -> None:
        return None


class FakeVoiceLab:
    """替身：把 voice_lab 的网络调用换掉，并记下调用参数。"""

    def __init__(self, *, voices: list | None = None, reused: bool = False) -> None:
        self.created: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.listed = 0
        self._voices = voices if voices is not None else [
            vl.VoiceInfo(voice="qwen-tts-vd-clear_auto-voice-20260926233229068-247d",
                         name="clear_auto", created="2026-09-26 23:32:29")]
        self._reused = reused
        self._real = {n: getattr(vl, n) for n in
                      ("create_or_reuse", "list_voices", "delete_voice")}

    def install(self) -> None:
        def create_or_reuse(name, prompt, **kw):                    # noqa: ANN001
            self.created.append((name, prompt))
            return vl.VoiceCreation(voice="qwen-tts-vd-%s-voice-20261002120000-abcd" % name,
                                    name=name, preview_wav=_tiny_wav(),
                                    reused=self._reused)

        def list_voices(**kw):                                      # noqa: ANN001
            self.listed += 1
            return list(self._voices)

        def delete_voice(voice, **kw):                              # noqa: ANN001
            self.deleted.append(voice)
            self._voices = [v for v in self._voices if v.voice != voice]

        vl.create_or_reuse = create_or_reuse                        # type: ignore[assignment]
        vl.list_voices = list_voices                                # type: ignore[assignment]
        vl.delete_voice = delete_voice                              # type: ignore[assignment]

    def restore(self) -> None:
        for n, f in self._real.items():
            setattr(vl, n, f)


PLAYED: list[bytes] = []


def _tiny_wav() -> bytes:
    """一段合法的最简 WAV（24k 单声道 10ms 静音）——用来验「能解码并播放」这条路。"""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 240)
    return buf.getvalue()


# ---- 假控件（headless 下 `_build_settings_voice` 不会跑，所以这里注入替身）----
# 为什么要这样：`TranslationGUI(headless=True)` 不建 UI（`_build_ui` 被跳过），
# 而用例要验的是**逻辑与接线**（生成/复用/保存/试听/删除的花钱与落盘行为）。
# 页面本身能不能建出来、装不装得下，由 `test_i18n` 的真窗口几何检查覆盖
# （它每次都打印「设置弹窗 760x…（最高一页需 …px）」）。


class _Var:
    def __init__(self, v: str = "") -> None:
        self._v = v

    def get(self) -> str:
        return self._v

    def set(self, v: str) -> None:
        self._v = v


class _Text:
    def __init__(self) -> None:
        self._s = ""

    def get(self, *_a) -> str:
        return self._s

    def delete(self, *_a) -> None:
        self._s = ""

    def insert(self, _index, text: str) -> None:
        self._s += text

    def winfo_exists(self) -> bool:
        return True


class _Combo(_Var):
    def __init__(self, items: list[str]) -> None:
        super().__init__("")
        self.items = items

    def current(self, i: int) -> None:
        self.set(self.items[i] if 0 <= i < len(self.items) else "")


class _List:
    def __init__(self) -> None:
        self.items: list[str] = []
        self.sel: list[int] = []

    def delete(self, *_a) -> None:
        self.items = []
        self.sel = []

    def insert(self, _index, text: str) -> None:
        self.items.append(text)

    def size(self) -> int:
        return len(self.items)

    def curselection(self) -> tuple[int, ...]:
        return tuple(self.sel)

    def selection_clear(self, *_a) -> None:
        self.sel = []

    def selection_set(self, i: int) -> None:
        self.sel = [int(i)]


class _Status:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, **kw) -> None:                              # noqa: ANN003
        if "text" in kw:
            self.text = str(kw["text"])

    def cget(self, key: str) -> str:
        return self.text if key == "text" else ""

    def winfo_exists(self) -> bool:
        return True


class _Btn:
    def __init__(self) -> None:
        self.state = "normal"
        self.values: list[str] | None = None

    def configure(self, **kw) -> None:                              # noqa: ANN003
        if "state" in kw:
            self.state = str(kw["state"])
        if "values" in kw:
            self.values = list(kw["values"])

    def winfo_exists(self) -> bool:
        return True


def _install_widgets(gui) -> None:
    """把音色页的控件替换成替身（一行窗口都不开）。"""
    gui._lab_name_var = _Var("my_voice")                            # type: ignore[assignment]
    gui._lab_name_entry = _Btn()                                    # type: ignore[assignment]
    gui._lab_prompt_text = _Text()                                  # type: ignore[assignment]
    gui._lab_recipe_combo = _Combo(vl.recipe_labels())               # type: ignore[assignment]
    gui._lab_list = _List()                                         # type: ignore[assignment]
    gui._lab_status = _Status()                                     # type: ignore[assignment]
    # 音频页那两个下拉（保存后要把新音色排进「音色选择」里）
    gui._tts_voice_var = _Var("")                                   # type: ignore[assignment]
    gui._tts_voice_combo = _Btn()                                   # type: ignore[assignment]
    for name in ("_lab_gen_btn", "_lab_save_btn", "_lab_del_btn", "_lab_recipe_btn",
                 "_lab_preview_btn", "_lab_refresh_btn"):
        setattr(gui, name, _Btn())
    gui._lab_voices = []
    gui._lab_cost_ok = True
    gui._lab_busy = False
    gui._lab_last = {}
    gui._lab_banner = ""


@contextlib.contextmanager
def _gui():
    """临时 HOME + 临时 config.yaml + 无头 GUI + 假控件；用完拆掉（绝不碰仓库真配置）。"""
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    tmp = Path(tempfile.mkdtemp(prefix="vlt-lab-"))
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                        encoding="utf-8")
    old = (cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod.APP_DIR,
           gui_mod._play_pcm_local)
    cfg_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.APP_DIR = tmp / "app"
    PLAYED.clear()
    gui_mod._play_pcm_local = lambda pcm: PLAYED.append(pcm)         # type: ignore[assignment]
    gui = None
    try:
        gui = TranslationGUI(headless=True)
        gui._root = _Root()                                          # type: ignore[assignment]
        gui._resolve_api_key_safe = lambda: "sk-test"                 # type: ignore[assignment]
        _install_widgets(gui)
        yield gui, cfg_path, tmp
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:                                        # noqa: BLE001
                pass
        cfg_mod.DEFAULT_CONFIG, gui_mod.DEFAULT_CONFIG, gui_mod.APP_DIR, \
            gui_mod._play_pcm_local = old
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _drain(gui, timeout: float = 5.0) -> list[tuple]:
    """等守护线程把结果塞进队列（无头下不跑 Tk 循环，所以自己收）。"""
    out: list[tuple] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out.append(gui._q.get(timeout=0.1))
        except Exception:                                            # noqa: BLE001
            if out:
                break
    return out


def _status(gui) -> str:
    try:
        return gui._lab_status.cget("text")
    except Exception:                                                # noqa: BLE001
        return ""


def _config(cfg_path: Path) -> dict:
    return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------- 用例


def test_generate_then_save() -> bool:
    """生成 → 列表 → 保存：**model 与 voice 必须一起写**（只写 voice 会 InvalidParameter）。"""
    ok = True
    fake = FakeVoiceLab()
    with _gui() as (gui, cfg_path, _tmp):
        fake.install()
        try:
            gui._lab_name_var.set("My Voice")            # 故意带空格 → 应被规范化成 My_Voice
            gui._lab_prompt_text.insert("1.0", "年轻女性，音色干净偏薄，语速平稳略慢")
            gui._on_lab_generate()
            msgs = _drain(gui)
            kinds = [m for m in msgs if m[0] == "voice_lab"]
            created_ok = fake.created and fake.created[0][0] == "My_Voice"
            print(f"  生成：worker 入队 {len(kinds)} 条，规范化后的名字={fake.created[0][0] if fake.created else '无'}  "
                  f"{'OK' if created_ok else '✗'}")
            ok &= bool(created_ok)
            for m in kinds:
                gui._on_lab_done(m[1], m[2], m[3], m[4])
            # 生成后自动刷新列表 → 列表里应有那条
            msgs2 = _drain(gui)
            for m in msgs2:
                if m[0] == "voice_lab":
                    gui._on_lab_done(m[1], m[2], m[3], m[4])
            listed = gui._lab_list.size()
            print(f"  列表条数={listed}（应有 1 条）  {'OK' if listed == 1 else '✗'}")
            ok &= listed == 1
            gui._lab_list.selection_clear(0, "end")
            gui._lab_list.selection_set(0)
            gui._on_lab_select()
            gui._on_lab_save()
            cfg = _config(cfg_path).get("text_input", {}).get("tts", {})
            cond = (cfg.get("model") == vl.DEFAULT_TARGET_MODEL
                    and cfg.get("voice", "").startswith("qwen-tts-vd-clear_auto-voice-"))
            print(f"  保存后 config：model={cfg.get('model')} voice 尾={str(cfg.get('voice'))[-5:]}  "
                  f"{'OK' if cond else '✗'}")
            ok &= cond
            cond = (gui._tts_voice_var.get().startswith("qwen-tts-vd-clear_auto")
                    and bool(gui._tts_voice_combo.values)
                    and gui._tts_voice_combo.values[0].startswith("qwen-tts-vd-clear_auto"))
            print(f"  打字译音下拉已跟上：{gui._tts_voice_var.get()[-12:]}"
                  f"（候选首位={None if not gui._tts_voice_combo.values else gui._tts_voice_combo.values[0][-12:]}）"
                  f"  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            fake.restore()
    return ok


def test_reuse_message_and_no_extra_cost() -> bool:
    """同名复用：文案要明说「没有再花钱」（用户最怕的是反复计费）。"""
    ok = True
    fake = FakeVoiceLab(reused=True)
    with _gui() as (gui, _cfg_path, _tmp):
        fake.install()
        try:
            gui._lab_name_var.set("clear_auto")
            gui._lab_prompt_text.insert("1.0", "随便写点描述")
            gui._on_lab_generate()
            for m in _drain(gui):
                if m[0] == "voice_lab" and m[1] == "create":
                    gui._on_lab_done(m[1], m[2], m[3], m[4])
            text = _status(gui)
            cond = "没有再花钱" in text
            print(f"  复用文案：{text[:34]}…  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            fake.restore()
    return ok


def test_cost_confirm_blocks_request() -> bool:
    """没确认花钱 → 一个请求都不发（取消不等于「静默继续」）。"""
    ok = True
    fake = FakeVoiceLab()
    orig = gui_mod.messagebox.askokcancel
    with _gui() as (gui, _cfg_path, _tmp):
        fake.install()
        try:
            gui._lab_cost_ok = False
            gui_mod.messagebox.askokcancel = lambda *a, **k: False    # 用户点了取消
            gui._lab_name_var.set("demo")
            gui._lab_prompt_text.insert("1.0", "年轻女性，语速偏慢")
            gui._on_lab_generate()
            time.sleep(0.3)
            cond = not fake.created
            print(f"  取消确认 → 创建请求数={len(fake.created)}（期望 0）  {'OK' if cond else '✗'}")
            ok &= cond
            # 确认后应当放行
            gui_mod.messagebox.askokcancel = lambda *a, **k: True
            gui._lab_cost_ok = False
            gui._on_lab_generate()
            _drain(gui)
            cond = len(fake.created) == 1
            print(f"  确认后 → 创建请求数={len(fake.created)}（期望 1）  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            gui_mod.messagebox.askokcancel = orig
            fake.restore()
    return ok


def test_preview_uses_local_cache() -> bool:
    """试听读本地缓存（回放不花钱）；没缓存时给明确提示而不是静默失败。"""
    ok = True
    fake = FakeVoiceLab()
    with _gui() as (gui, _cfg_path, tmp):
        fake.install()
        try:
            gui._on_lab_refresh()
            for m in _drain(gui):
                if m[0] == "voice_lab":
                    gui._on_lab_done(m[1], m[2], m[3], m[4])
            gui._lab_list.selection_clear(0, "end")
            gui._lab_list.selection_set(0)
            gui._on_lab_select()
            gui._on_lab_preview()
            cond = "没有本地试听缓存" in _status(gui)
            print(f"  无缓存 → 提示：{_status(gui)[:26]}…  {'OK' if cond else '✗'}")
            ok &= cond
            voice = fake._voices[0].voice
            vl.save_preview(gui_mod.APP_DIR, voice, _tiny_wav())
            gui._on_lab_preview()
            cond = bool(PLAYED)
            print(f"  有缓存 → 播放调用 {len(PLAYED)} 次（且没再请求网络）  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            fake.restore()
    return ok


def test_recipe_fills_and_generates() -> bool:
    """配方一键生成：把配方写进控件再走同一条路（名字用配方 key）。"""
    ok = True
    fake = FakeVoiceLab()
    with _gui() as (gui, _cfg_path, _tmp):
        fake.install()
        try:
            gui._lab_recipe_combo.current(0)
            gui._on_lab_recipe_generate()
            _drain(gui)
            name, prompt = fake.created[0] if fake.created else ("", "")
            cond = (name == vl.RECIPES[0].key and prompt == vl.RECIPES[0].prompt
                    and gui._lab_prompt_text.get("1.0", "end").strip() == vl.RECIPES[0].prompt)
            print(f"  配方 → 名字={name!r} 描述与配方一致={prompt == vl.RECIPES[0].prompt}  "
                  f"{'OK' if cond else '✗'}")
            ok &= cond
        finally:
            fake.restore()
    return ok


def test_delete_and_guards() -> bool:
    """删除要确认；没选中时保存/试听只给提示、不崩。"""
    ok = True
    fake = FakeVoiceLab()
    orig = gui_mod.messagebox.askokcancel
    with _gui() as (gui, _cfg_path, _tmp):
        fake.install()
        try:
            gui._on_lab_save()
            cond = "还没选音色" in _status(gui)
            print(f"  未选中就保存 → 提示：{_status(gui)}  {'OK' if cond else '✗'}")
            ok &= cond
            gui._on_lab_refresh()
            for m in _drain(gui):
                if m[0] == "voice_lab":
                    gui._on_lab_done(m[1], m[2], m[3], m[4])
            gui._lab_list.selection_clear(0, "end")
            gui._lab_list.selection_set(0)
            gui._on_lab_select()
            gui_mod.messagebox.askokcancel = lambda *a, **k: False
            gui._on_lab_delete()
            cond = not fake.deleted
            print(f"  删除取消 → 删除请求数={len(fake.deleted)}（期望 0）  {'OK' if cond else '✗'}")
            ok &= cond
            gui_mod.messagebox.askokcancel = lambda *a, **k: True
            gui._on_lab_delete()
            for m in _drain(gui):
                if m[0] == "voice_lab" and m[1] == "delete":
                    gui._on_lab_done(m[1], m[2], m[3], m[4])
            cond = bool(fake.deleted)
            print(f"  确认删除 → 删除请求数={len(fake.deleted)}  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            gui_mod.messagebox.askokcancel = orig
            fake.restore()
    return ok


def test_empty_prompt_and_no_key() -> bool:
    """空描述 / 没 key：都要给明确提示，且不发请求。"""
    ok = True
    fake = FakeVoiceLab()
    with _gui() as (gui, _cfg_path, _tmp):
        fake.install()
        try:
            gui._lab_prompt_text.delete("1.0", "end")
            gui._on_lab_generate()
            cond = "生成失败" in _status(gui) and not fake.created
            print(f"  空描述 → 提示：{_status(gui)[:30]}…  {'OK' if cond else '✗'}")
            ok &= cond
            gui._resolve_api_key_safe = lambda: ""                    # type: ignore[assignment]
            gui._lab_prompt_text.insert("1.0", "年轻女性，语速偏慢")
            gui._on_lab_generate()
            cond = "API key" in _status(gui) and not fake.created
            print(f"  没 key → 提示：{_status(gui)[:30]}…  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            fake.restore()
    return ok


def test_poll_dispatch_wired() -> bool:
    """接线：`_poll()` 必须认识 ("voice_lab", …) 这条消息（否则结果永远回不到界面）。"""
    ok = True
    fake = FakeVoiceLab()
    with _gui() as (gui, _cfg_path, _tmp):
        fake.install()
        try:
            gui._lab_set_status("")
            gui._q.put(("voice_lab", "list", True, "",
                        [vl.VoiceInfo(voice="qwen-tts-vd-x-voice-20261002120000-9999", name="x")]))
            gui._poll()
            cond = gui._lab_list.size() == 1
            print(f"  _poll 派发后列表条数={gui._lab_list.size()}（期望 1）  {'OK' if cond else '✗'}")
            ok &= cond
            gui._q.put(("voice_lab", "create", False, "网络不可达：模拟", None))
            gui._poll()
            cond = "生成失败" in _status(gui)
            print(f"  失败消息经 _poll 上屏：{_status(gui)[:26]}…  {'OK' if cond else '✗'}")
            ok &= cond
        finally:
            fake.restore()
    return ok


if __name__ == "__main__":
    print("test_voice_lab_gui:")
    print(" 1) 生成 → 列表 → 保存（model+voice 一起写）")
    ok = test_generate_then_save()
    print(" 2) 同名复用（不重复花钱）")
    ok &= test_reuse_message_and_no_extra_cost()
    print(" 3) 花钱确认")
    ok &= test_cost_confirm_blocks_request()
    print(" 4) 试听读本地缓存")
    ok &= test_preview_uses_local_cache()
    print(" 5) 配方一键生成")
    ok &= test_recipe_fills_and_generates()
    print(" 6) 删除与未选中的兜底")
    ok &= test_delete_and_guards()
    print(" 7) 空描述 / 没 key")
    ok &= test_empty_prompt_and_no_key()
    print(" 8) _poll 接线")
    ok &= test_poll_dispatch_wired()
    assert ok, "音色页接线用例失败（见上）"
    print("ALL PASSED")
