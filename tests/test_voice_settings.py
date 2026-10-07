"""「⚙ 设置 → 音色」的两个下拉：写盘路径、缺段补建、内存同步。

背景（本次改动）：项目里有**两条独立的出声音色** ——
- 说话译音：实时模型直出，音色写 `session.voice`；
- 打字译音：文本翻译后单独调 qwen3-tts-flash，音色写 `text_input.tts.voice`。
GUI 加了下拉之后，这两处必须能被**就地**写进 config.yaml（保住注释与键顺序），
而且老配置里整段没有 `text_input` 时也要能落盘（否则设置永远存不下去，界面还显示「已保存」）。

全程离线：不起引擎、不联网；GUI 用例用**临时 config.yaml**，绝不碰仓库里的真配置。

跑法：.venv/Scripts/python.exe tests/test_voice_settings.py
"""
from __future__ import annotations

import contextlib
import io
import os
import queue
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 与 test_config_save 同一招：CI/新克隆上没有 key，而 load_config 默认要 key。
# 这里只验配置读写，与 key 真假无关，给一个拼接出来的假 key。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-ws-" + "voicetestonly0123456789abcdef")

# 用例用不到界面语言（只按下拉的处理函数），但要**钉死**成中文：
# 设备/文案跟随系统语言，英文系统下控件文案不同不会影响本用例，但保持可复现总没错。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

from vlt.config_io import _yaml_set_or_create, _yaml_set_in_text  # noqa: E402
from vlt.voices import REALTIME_VOICES, TTS_VOICES, voice_choices  # noqa: E402

# 一份「老配置」的样子：从更早的模板生成，**根本没有 text_input 段**（用户实测就是这样）
OLD_STYLE = """\
# 主配置
session:
  model: qwen3.8-livetranslate-flash-realtime
  # ⚠️ 必须显式指定：不填会抛 Voice 'Chelsie' is not supported
  voice: Tina
  workspace_id: ''
directions:
  mine:
    source_lang: zh
    target_lang: ja
output:
  audio:
    enabled: true
ui:
  direction: mine
"""


def _tmp_config(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="vlt-voice-cfg-"))
    p = tmp / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------- ① 补建/就地改


def test_set_or_create_writes_existing_leaf() -> None:
    """路径本来就在（session.voice）→ 就地改值，注释与兄弟键一行不少。"""
    out = _yaml_set_or_create(OLD_STYLE, ["session", "voice"], "Ethan")
    data = yaml.safe_load(out)
    assert data["session"]["voice"] == "Ethan", f"没改成：{data['session']!r}"
    assert data["session"]["model"] == "qwen3.8-livetranslate-flash-realtime", "兄弟键被误删"
    assert data["session"]["workspace_id"] == "", "兄弟键被误删"
    assert "必须显式指定" in out, f"注释丢了：\n{out}"
    assert out.splitlines()[0] == "# 主配置", "首行注释丢了"
    assert data["ui"]["direction"] == "mine", "后面的段被破坏"
    print("  ✓ 已存在的叶子：就地改值，注释/兄弟键/后续段都还在")


def test_set_or_create_builds_missing_block() -> None:
    """★ 老配置没有 text_input 段 → 必须把整条链补出来（旧函数在这儿会静默 no-op）。"""
    # 先证明旧函数的行为确实是 no-op（这就是为什么要新增这个函数）
    assert _yaml_set_in_text(OLD_STYLE, ["text_input", "tts", "voice"], "Serena") == OLD_STYLE, \
        "前提：_yaml_set_in_text 遇到缺失父键应当原样返回"
    out = _yaml_set_or_create(OLD_STYLE, ["text_input", "tts", "voice"], "Serena")
    data = yaml.safe_load(out)
    assert data["text_input"]["tts"]["voice"] == "Serena", f"没补建出来：\n{out}"
    assert data["session"]["voice"] == "Tina", "补建时把别的段带坏了"
    assert data["output"]["audio"]["enabled"] is True, "补建时把别的段带坏了"
    print("  ✓ 缺失的段：整条链按缩进补到文件末尾，其它段原样不动")


def test_set_or_create_fills_missing_middle() -> None:
    """text_input 在、tts 不在 → 只补 tts 这一层，而且要补在 text_input **块内**。"""
    text = ("text_input:\n"
            "  enabled: true      # 打字输入总开关\n"
            "session:\n"
            "  voice: Tina\n")
    out = _yaml_set_or_create(text, ["text_input", "tts", "voice"], "Moon")
    data = yaml.safe_load(out)
    assert data["text_input"]["tts"]["voice"] == "Moon", f"没补进 text_input 块内：\n{out}"
    assert data["text_input"]["enabled"] is True, "兄弟键被误删"
    assert data["session"]["voice"] == "Tina", "补错了位置（把 session 段吃进 text_input 了）"
    assert "打字输入总开关" in out, "注释丢了"
    print("  ✓ 缺中间层：补在所属父块末尾，没有跑出段外造成重复键")


def test_set_or_create_replaces_existing_leaf_value() -> None:
    """已存在的 tts.voice → 替换而不是追加第二个（重复键会让「改了没反应」）。"""
    text = ("text_input:\n"
            "  tts:\n"
            "    model: qwen3-tts-flash\n"
            "    voice: Cherry            # 多语言音色\n"
            "    timeout_s: 30\n")
    out = _yaml_set_or_create(text, ["text_input", "tts", "voice"], "Katerina")
    data = yaml.safe_load(out)
    assert data["text_input"]["tts"]["voice"] == "Katerina", f"没替换：{data!r}"
    assert out.count("voice:") == 1, f"多出一个 voice 键：\n{out}"
    assert data["text_input"]["tts"]["model"] == "qwen3-tts-flash", "兄弟键被误删"
    assert data["text_input"]["tts"]["timeout_s"] == 30, "兄弟键被误删"
    assert "多语言音色" in out, "行尾注释丢了"
    print("  ✓ 已存在的 tts.voice：替换值并保住行尾注释，不产生重复键")


def test_set_or_create_handles_voice_with_space() -> None:
    """带空格的音色（Theo Calm / Liora Mira）写进去必须还能被 YAML 读回同一个值。"""
    for v in ("Theo Calm", "Liora Mira"):
        out = _yaml_set_or_create(OLD_STYLE, ["session", "voice"], v)
        assert yaml.safe_load(out)["session"]["voice"] == v, f"带空格的值写坏了：{out!r}"
    print("  ✓ 带空格的音色 id：写盘/读回一致")


# ---------------------------------------------------------------- ② 音色表与下拉候选


def test_voice_catalogs_are_separate() -> None:
    """两张表**不能**混为一谈：打字侧的 Cherry 不在实时侧，反之 Tina 不在 TTS 侧。

    这是这次调查的核心结论 —— 跨模型混用音色会被服务端拒（InvalidParameter），
    所以界面必须给两个下拉、各配各的表。哪天有人「顺手合并成一张」，这条会红。
    """
    assert "Cherry" in TTS_VOICES and "Cherry" not in REALTIME_VOICES
    assert "Tina" in REALTIME_VOICES and "Tina" not in TTS_VOICES
    assert len(set(REALTIME_VOICES)) == len(REALTIME_VOICES), "实时表里有重复音色"
    assert len(set(TTS_VOICES)) == len(TTS_VOICES), "TTS 表里有重复音色"
    for v in REALTIME_VOICES + TTS_VOICES:
        assert v.strip() == v and v, f"音色 id 有前后空格/空值：{v!r}"
    print(f"  ✓ 两张音色表互相独立（实时 {len(REALTIME_VOICES)} 个 / 打字 {len(TTS_VOICES)} 个）")


def test_voice_choices_keeps_custom_value() -> None:
    """当前值不在表里（手填/复刻音色）→ 排到候选最前，重新打开设置才看得到真正生效的值。"""
    assert voice_choices("MyClonedVoice", TTS_VOICES)[0] == "MyClonedVoice"
    assert voice_choices("Cherry", TTS_VOICES) == list(TTS_VOICES), "表内的值不该被插到最前"
    assert voice_choices("", TTS_VOICES) == list(TTS_VOICES)
    assert voice_choices(None, TTS_VOICES) == list(TTS_VOICES)
    print("  ✓ voice_choices：表外的值排最前，表内的值不重复插")


# ---------------------------------------------------------------- ③ GUI 接线


def _make_gui(config_path: Path):
    import vlt.config as cfg_mod
    import vlt.gui as gui_mod

    cfg_mod.DEFAULT_CONFIG = config_path
    gui_mod.DEFAULT_CONFIG = config_path
    from vlt.gui import TranslationGUI

    gui = TranslationGUI()
    if gui._update_check_job is not None:
        gui._root.after_cancel(gui._update_check_job)
        gui._update_check_job = None
    gui._root.update()
    return gui, gui_mod


def _destroy(gui) -> None:
    try:
        gui._root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _example_body() -> str:
    return (ROOT / "config.example.yaml").read_text(encoding="utf-8")


def _take_voice_preview(gui):
    """从界面队列里取**试听结果**（`("voice_preview", kind, voice, err)`），取不到返回 None。

    ⚠️ 不能假设「队首就是试听结果」：界面构造期就会往**同一个队列**塞状态消息
    （`("status", ...)` —— 例如麦克风代理启动/失败、引擎腿状态）。这里按标签取，
    其余消息原样放回队列，免得把别处的断言搞坏。
    """
    rest = []
    found = None
    while True:
        try:
            item = gui._q.get_nowait()
        except queue.Empty:
            break
        if found is None and item and item[0] == "voice_preview":
            found = item
        else:
            rest.append(item)
    for it in rest:
        gui._q.put(it)
    return found


@contextlib.contextmanager
def _gui_with_config(body: str):
    """临时 HOME + 临时 config.yaml 里起一个真窗口，用完拆掉（绝不碰仓库真配置）。"""
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    tmp = Path(tempfile.mkdtemp(prefix="vlt-voice-env-"))
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    cfg_path = _tmp_config(body)
    gui = None
    try:
        gui, gui_mod = _make_gui(cfg_path)
        yield gui, gui_mod, cfg_path
    finally:
        _destroy(gui)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _stubbed_tts(gui_mod, *, synth=None, raises=None, omni_raises=None):
    """把两条合成路（打字 qwen3-tts-flash / 说话 Qwen-Omni）与本地播放都打桩：
    离线不联网、不出声，并分开记下哪条路被调了。"""
    import vlt.tts as tts_mod

    calls: dict = {"syn": [], "omni": []}
    played: list = []
    orig_syn, orig_omni = tts_mod.synthesize, tts_mod.synthesize_omni
    orig_play = gui_mod._play_pcm_local

    def fake_syn(text, *, voice, model, api_key, **kw):
        calls["syn"].append({"text": text, "voice": voice, "model": model, "api_key": api_key})
        if raises is not None:
            raise raises
        return b"\x01\x00\x02\x00" if synth is None else synth

    def fake_omni(text, *, voice, api_key, model=None, **kw):
        calls["omni"].append({"text": text, "voice": voice, "model": model, "api_key": api_key})
        if omni_raises is not None:
            raise omni_raises
        return b"\x01\x00\x02\x00" if synth is None else synth

    tts_mod.synthesize = fake_syn
    tts_mod.synthesize_omni = fake_omni
    gui_mod._play_pcm_local = lambda pcm: played.append(pcm)
    try:
        yield calls, played
    finally:
        tts_mod.synthesize = orig_syn
        tts_mod.synthesize_omni = orig_omni
        gui_mod._play_pcm_local = orig_play


def test_gui_voice_dropdowns_save() -> None:
    """真窗口点两个下拉：一个写 session.voice，一个写 text_input.tts.voice；
    内存里的 cfg 也要同步（引擎下次建会话/下次打字就读这个对象）。"""
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    tmp = Path(tempfile.mkdtemp(prefix="vlt-voice-env-"))
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    cfg_path = _tmp_config((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    gui = None
    try:
        gui, gui_mod = _make_gui(cfg_path)
        # 回显：模板里是 Tina / Cherry
        assert gui._speech_voice_var.get() == "Tina", gui._speech_voice_var.get()
        assert gui._tts_voice_var.get() == "Cherry", gui._tts_voice_var.get()
        assert "Tina" in gui._speech_voice_combo.cget("values")
        assert "Cherry" in gui._tts_voice_combo.cget("values")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._speech_voice_var.set("Ryan")
            gui._on_speech_voice_change()
            gui._tts_voice_var.set("Ethan")
            gui._on_tts_voice_change()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["session"]["voice"] == "Ryan", f"session.voice 没写进去：{data['session']!r}"
        assert data["text_input"]["tts"]["voice"] == "Ethan", \
            f"text_input.tts.voice 没写进去：{data.get('text_input')!r}"
        assert gui._cfg.session_base["voice"] == "Ryan", "内存 cfg 没同步（引擎读的是它）"
        assert gui._cfg.text_input["tts"]["voice"] == "Ethan", "内存 cfg 没同步"
        assert "音色表" not in buf.getvalue()
        assert "说话译音音色" in buf.getvalue() and "打字译音音色" in buf.getvalue(), \
            "改音色必须留痕（不许静默）"
        print("  ✓ 两个下拉：各写各的路径 + 同步内存 + 日志留痕")
        _destroy(gui)

        # 老配置（没有 text_input 段）也得能存：这才是新增补建的意义
        gui = None
        cfg_path = _tmp_config(OLD_STYLE)
        gui, _ = _make_gui(cfg_path)
        assert gui._tts_voice_var.get() == "Cherry", \
            f"配置里没有 tts.voice 时应回显真正在生效的默认值：{gui._tts_voice_var.get()!r}"
        gui._tts_voice_var.set("Bella")
        gui._on_tts_voice_change()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["text_input"]["tts"]["voice"] == "Bella", \
            f"老配置补建失败：\n{cfg_path.read_text(encoding='utf-8')}"
        assert data["session"]["voice"] == "Tina", "补建把别的段带坏了"
        print("  ✓ 老配置（无 text_input 段）：下拉仍可用，保存后整段补出来")
    finally:
        _destroy(gui)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_gui_speech_voice_respects_direction_override() -> None:
    """方向级 `directions.<X>.voice` 优先于 session.voice（config.to_session_config 的口径）。

    两头都得管：
    1. **回显**：下拉不能只读 session.voice —— 有方向覆盖时显的是没在生效的值；
    2. **保存**：用户改的是「我要听哪个声」，所以把赢的那个键也一并改掉，
       绝不能做一个「改了但被覆盖挡掉、于是没反应」的下拉（项目口径：禁静默降级）。
    """
    body = ("session:\n"
            "  model: qwen3.8-livetranslate-flash-realtime\n"
            "  voice: Tina\n"
            "directions:\n"
            "  mine:\n"
            "    source_lang: zh\n"
            "    target_lang: en\n"
            "    voice: Cindy\n"
            "ui:\n"
            "  direction: mine\n")
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    tmp = Path(tempfile.mkdtemp(prefix="vlt-voice-env-"))
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    cfg_path = _tmp_config(body)
    gui = None
    try:
        gui, _ = _make_gui(cfg_path)
        # ① 回显：方向级 Cindy 赢，下拉就该显 Cindy（而不是被挡住的 Tina）
        assert gui._speech_voice_var.get() == "Cindy", \
            f"有方向级覆盖时应当回显 Cindy，实际 {gui._speech_voice_var.get()!r}"
        assert "Cindy" in gui._speech_voice_combo.cget("values")

        # ② 保存：两个键一起改，内存里的 Direction.voice 也要跟上
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._speech_voice_var.set("Maia")
            gui._on_speech_voice_change()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["session"]["voice"] == "Maia", f"session.voice 没改：{data['session']!r}"
        assert data["directions"]["mine"]["voice"] == "Maia", \
            f"方向级覆盖没同步（会变成「改了没反应」）：{data['directions']!r}"
        assert gui._cfg.directions["mine"].voice == "Maia", "内存里的 Direction.voice 没同步"
        assert "directions.mine.voice" in buf.getvalue(), "额外改了一个键必须留痕"
        print("  ✓ 方向级覆盖：回显赢的那个键，保存时两个键一起改（绝不做无反应的下拉）")
    finally:
        _destroy(gui)
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- ④ 音色试听


def test_is_unsupported_voice_err() -> None:
    """服务端拒收音色的几种报文都要认得，但网络错误不能误伤。"""
    import vlt.gui as gui_mod

    f = gui_mod._is_unsupported_voice_err
    assert f('HTTP 400：{"code":"InvalidParameter"}')
    assert f("Voice 'Tina' is not supported")
    assert f("[cosyvoice:]Engine error [411]: TTS speak operation failed")
    assert not f("网络不可达：timed out"), "网络错误不该被当成「音色不支持」"
    assert not f("")
    print("  ✓ _is_unsupported_voice_err：识别拒收音色，不误伤网络错误")


def test_preview_buttons_exist() -> None:
    """两个音色下拉各带一个「试听」按钮。"""
    with _gui_with_config(_example_body()) as (gui, _mod, _cfg):
        assert gui._speech_preview_btn is not None, "说话侧缺试听按钮"
        assert gui._tts_preview_btn is not None, "打字侧缺试听按钮"
        assert str(gui._speech_preview_btn.cget("text")) == "试听"
        assert str(gui._tts_preview_btn.cget("text")) == "试听"
    print("  ✓ 两个下拉各带一个「试听」按钮")


def test_preview_worker_success_plays_and_reports() -> None:
    """合成成功→本地播放→状态栏报「试听完成」，忙标记复位。"""
    with _gui_with_config(_example_body()) as (gui, mod, _cfg):
        with _stubbed_tts(mod) as (calls, played):
            gui._preview_worker("tts", "Cherry", "sk-fake")
            _pv = _take_voice_preview(gui)
            assert _pv is not None, "没收到试听结果（队列里只有状态消息？）"
            kind, voice, err = _pv[1:]
            assert (kind, voice, err) == ("tts", "Cherry", ""), f"回主线程的消息不对：{(kind, voice, err)!r}"
            assert played == [b"\x01\x00\x02\x00"], "没把合成音频交给本地播放"
            assert calls["syn"][0]["voice"] == "Cherry"
            assert calls["syn"][0]["model"] == "qwen3-tts-flash", "打字侧试听必须走 qwen3-tts-flash"
            assert calls["syn"][0]["api_key"] == "sk-fake"
            assert calls["omni"] == [], "打字侧不该碰 Omni 那条路"
            gui._on_voice_preview_done(kind, voice, err)
            assert "试听完成" in gui._status_label.cget("text")
            assert gui._preview_busy is False, "完成后必须解除忙标记"
    print("  ✓ 打字侧试听成功：走 qwen3-tts-flash→本地播放→报「试听完成」，不碰 Omni")


def test_preview_speech_uses_omni_model() -> None:
    """说话侧（Tina 等实时音色）必须走非实时 Qwen-Omni 合成 —— qwen3-tts-flash 不认这些 id。"""
    with _gui_with_config(_example_body()) as (gui, mod, _cfg):
        with _stubbed_tts(mod) as (calls, played):
            gui._preview_worker("speech", "Tina", "sk-fake")
            _pv = _take_voice_preview(gui)
            assert _pv is not None, "没收到试听结果（队列里只有状态消息？）"
            kind, voice, err = _pv[1:]
            assert (kind, voice, err) == ("speech", "Tina", ""), f"{(kind, voice, err)!r}"
            assert calls["syn"] == [], "说话侧不该走 qwen3-tts-flash（会 InvalidParameter）"
            assert len(calls["omni"]) == 1 and calls["omni"][0]["voice"] == "Tina", \
                f"说话侧应走 Omni 并带上正确音色：{calls['omni']!r}"
            assert played == [b"\x01\x00\x02\x00"], "合成音频未交给本地播放"
            gui._on_voice_preview_done(kind, voice, err)
            assert "试听完成" in gui._status_label.cget("text")
    print("  ✓ 说话侧试听：走 Qwen-Omni（不碰 qwen3-tts-flash），Tina 可正常试听")


def test_preview_speech_unsupported_voice_reports_friendly() -> None:
    """服务端拒收某个音色 id（如自定义/复刻 id 对不上模型）：
    说成人话「暂不支持试听」，不甩服务端原始报文，也不播放。"""
    import vlt.tts as tts_mod

    boom = tts_mod.TtsError(
        'HTTP 400：{"code":"InvalidParameter","message":"Voice \'X\' is not supported"}')
    with _gui_with_config(_example_body()) as (gui, mod, _cfg):
        with _stubbed_tts(mod, omni_raises=boom) as (_calls, played):
            gui._preview_worker("speech", "SomeClonedId", "sk-fake")
            _pv = _take_voice_preview(gui)
            assert _pv is not None, "没收到试听结果（队列里只有状态消息？）"
            kind, voice, err = _pv[1:]
            assert err and "InvalidParameter" in err
            assert not played, "合成失败就不该播放"
            gui._on_voice_preview_done(kind, voice, err)
            label = gui._status_label.cget("text")
            assert "暂不支持试听" in label, label
            assert "InvalidParameter" not in label, "服务端原始报文不该甩给用户"
            assert gui._preview_busy is False
    print("  ✓ 音色被服务端拒收：→人话「暂不支持试听」，不甩原始报文")


def test_preview_generic_failure_reports_reason() -> None:
    """一般失败（网络）：状态栏报「试听失败」并带上原因。"""
    with _gui_with_config(_example_body()) as (gui, _mod, _cfg):
        gui._on_voice_preview_done("tts", "Cherry", "网络不可达：timed out")
        label = gui._status_label.cget("text")
        assert "试听失败" in label and "timed out" in label, label
    print("  ✓ 一般失败：报「试听失败」并带上原因")


def test_preview_voice_guards() -> None:
    """前置校验：空音色 / 无 key 都只提示不合成；忙时忽略重复点击（不叠音）。"""
    with _gui_with_config(_example_body()) as (gui, _mod, _cfg):
        gui._tts_voice_var.set("   ")
        gui._preview_voice("tts")
        assert "请先选择或填写音色" in gui._status_label.cget("text")
        assert gui._preview_busy is False and _take_voice_preview(gui) is None, "空音色不应起合成"

        gui._tts_voice_var.set("Cherry")
        orig = gui._resolve_api_key_safe
        gui._resolve_api_key_safe = lambda: ""
        try:
            gui._preview_voice("tts")
        finally:
            gui._resolve_api_key_safe = orig
        assert "还没配置 API key" in gui._status_label.cget("text")
        assert gui._preview_busy is False and _take_voice_preview(gui) is None, "无 key 不应起合成"

        gui._preview_busy = True
        gui._preview_voice("tts")
        assert _take_voice_preview(gui) is None, "忙时应直接忽略，不再排一条"
        gui._preview_busy = False
    print("  ✓ 前置校验：空音色/无 key 只提示不合成；忙时忽略重复点击")


def test_preview_full_thread_roundtrip() -> None:
    """端到端：点按钮→线程合成播放→_poll 回主线程恢复按钮并报「试听完成」。"""
    with _gui_with_config(_example_body()) as (gui, mod, _cfg):
        with _stubbed_tts(mod, synth=b"\x09\x00") as (_calls, played):
            gui._tts_voice_var.set("Ethan")
            gui._on_preview_tts_voice()               # 起线程
            assert gui._preview_busy is True
            assert str(gui._tts_preview_btn.cget("text")) == "试听中…", "进行中按钮应变文案并禁用"
            assert str(gui._tts_preview_btn.cget("state")) == "disabled"
            end = time.monotonic() + 5.0
            while time.monotonic() < end and any(
                    th.name == "vlt-voice-preview" for th in threading.enumerate()):
                time.sleep(0.02)
            gui._poll()                                # 主线程消费队列
            assert played == [b"\x09\x00"], "线程里应已播放合成音频"
            assert gui._preview_busy is False
            assert str(gui._tts_preview_btn.cget("text")) == "试听", "完成后按钮应恢复"
            assert str(gui._tts_preview_btn.cget("state")) == "normal"
            assert "试听完成" in gui._status_label.cget("text")
    print("  ✓ 端到端：线程合成播放 + _poll 回主线程恢复按钮并报完成")


def test_preview_tts_custom_voice_uses_own_model() -> None:
    """打字侧试听**自定义音色**：先还原真 id，再用**它自己的** target_model。

    真机证据：下拉里显示的是短名，而账号里的设计族(vd)/复刻族(vc)音色**不认内置模型** ——
    以前写死 `qwen3-tts-flash` 去合成 → 400 `InvalidParameter: Invalid voice specified`，
    用户看到的就是「点试听没声音」。这条用例把「真 id + 自己的模型」钉死。
    """
    from vlt import voice_lab as vl

    clone_id = "qwen-tts-vc-my_clip_4x-voice-20261007121947169-ee85"
    with _gui_with_config(_example_body()) as (gui, mod, _cfg):
        gui._tts_custom = [vl.VoiceInfo(voice=clone_id, name="my_clip_4x",
                                        target_model=vl.CLONE_TARGET_MODEL, kind="clone")]
        gui._refresh_tts_voice_combo()
        assert "my_clip_4x" in gui._tts_voice_combo.cget("values"), \
            f"自定义音色没进下拉：{gui._tts_voice_combo.cget('values')!r}"
        gui._tts_voice_var.set("my_clip_4x")          # 用户选的是下拉里那个短名
        with _stubbed_tts(mod) as (calls, played):
            gui._on_preview_tts_voice()
            end = time.monotonic() + 5.0
            while time.monotonic() < end and any(
                    th.name == "vlt-voice-preview" for th in threading.enumerate()):
                time.sleep(0.02)
            _pv = _take_voice_preview(gui)
            assert _pv is not None, "没收到试听结果（队列里只有状态消息？）"
            kind, voice, err = _pv[1:]
            assert (kind, voice, err) == ("tts", clone_id, ""), f"{(kind, voice, err)!r}"
            assert calls["syn"][0]["voice"] == clone_id, \
                f"必须还原成真 id（短名发给 API 服务端不认）：{calls['syn']!r}"
            assert calls["syn"][0]["model"] == vl.CLONE_TARGET_MODEL, \
                f"自定义音色必须用它自己的模型（写死内置模型 → 400）：{calls['syn']!r}"
            assert played == [b"\x01\x00\x02\x00"], "合成音频未交给本地播放"
    print("  ✓ 打字侧试听自定义音色：还原真 id + 用它自己的 vc 模型（不再写死内置模型）")


class _FakeSseResp:
    """假 SSE 响应：可迭代逐行、可当上下文管理器（和 urllib 的返回对象同形）。"""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._lines)


class _FakeOpener:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines
        self.req = None

    def open(self, req, timeout=None):   # noqa: ARG002 — 与真 opener 同签名
        self.req = req
        return _FakeSseResp(self._lines)


def test_synthesize_omni_parses_sse_and_payload() -> None:
    """直接验 synthesize_omni 的请求体与 SSE 拼接（打桩 opener，全程离线）。

    这是说话侧试听的核心：stream=True + 分片 base64 拼接后一次解码。
    这里喂两块 base64（AQID / BAUG），应拼成 01..06；miniaudio 解不动裸字节，
    应走兜底分支原样返回（本就是目标格式）。
    """
    import json as _json

    import vlt.tts as tts_mod

    lines = [
        b'data: {"choices":[{"delta":{"audio":{"data":"AQID"}}}]}',
        b'\n',
        b'data: {"choices":[{"delta":{"audio":{"data":"BAUG"}}}]}',
        b'data: {"choices":[{"finish_reason":"stop","delta":{}}]}',
        b'data: [DONE]',
    ]
    fake = _FakeOpener(lines)
    orig = tts_mod._get_opener
    tts_mod._get_opener = lambda: fake
    try:
        pcm = tts_mod.synthesize_omni("你好", voice="Tina", api_key="sk-fake")
    finally:
        tts_mod._get_opener = orig
    assert pcm == b"\x01\x02\x03\x04\x05\x06", f"SSE 分片拼接/解码不对：{pcm!r}"
    # 请求体：端点 / 流式 / 音色 / 朗读指令都在
    assert fake.req.full_url == tts_mod.OMNI_ENDPOINT
    payload = _json.loads(fake.req.data.decode("utf-8"))
    assert payload["stream"] is True, "Omni 音频输出必须 stream=True"
    assert payload["modalities"] == ["text", "audio"]
    assert payload["audio"]["voice"] == "Tina" and payload["audio"]["format"] == "wav"
    assert "你好" in payload["messages"][0]["content"], "样例句应进了朗读指令"
    assert fake.req.get_header("Authorization") == "Bearer sk-fake"
    print("  ✓ synthesize_omni：请求体对（流式/音色/指令）+ SSE 分片 base64 拼接解码正确")


def test_synthesize_omni_surfaces_server_error() -> None:
    """SSE 里回的错误帧 / 没音频都要抛 TtsError（绝不静默返回空）。"""
    import vlt.tts as tts_mod

    orig = tts_mod._get_opener
    try:
        # ① 错误帧
        tts_mod._get_opener = lambda: _FakeOpener(
            [b'data: {"error":{"message":"Voice \'X\' is not supported"}}'])
        try:
            tts_mod.synthesize_omni("你好", voice="X", api_key="sk-fake")
            raise AssertionError("应抛 TtsError")
        except tts_mod.TtsError as exc:
            assert "not supported" in str(exc)
        # ② 没有任何音频帧
        tts_mod._get_opener = lambda: _FakeOpener([b'data: [DONE]'])
        try:
            tts_mod.synthesize_omni("你好", voice="Tina", api_key="sk-fake")
            raise AssertionError("应抛 TtsError")
        except tts_mod.TtsError as exc:
            assert "没返回音频" in str(exc)
        # ③ 缺 key
        try:
            tts_mod.synthesize_omni("你好", voice="Tina", api_key="")
            raise AssertionError("应抛 TtsError")
        except tts_mod.TtsError as exc:
            assert "API key" in str(exc)
    finally:
        tts_mod._get_opener = orig
    print("  ✓ synthesize_omni：错误帧/空音频/缺 key 都抛 TtsError（不静默）")


def test_cloned_voice_display_name() -> None:
    """复刻音色的**显示名**：中文「国民护卫队」/ 其它语言「MetroPolice」；写配置必须还原成真 id。

    这条防的是"界面好看但配置被写坏"：下拉里显示的是本地化名字，一旦原样写进
    `text_input.tts.voice`，合成就会拿着「国民护卫队」去调 API → 必然失败。
    """
    import vlt.voices as voices
    from vlt.locales import en as en_loc
    from vlt.locales import ja as ja_loc

    vid, zh = next(iter(voices.CLONED_VOICES.items()))

    def humanize_en(s: str) -> str:
        return en_loc.STRINGS.get(s, s)

    def humanize_ja(s: str) -> str:
        return ja_loc.STRINGS.get(s, s)

    # ① 中文 = 中文名；英文/日文 = 英文名
    assert voices.display_name(vid) == zh, voices.display_name(vid)
    assert voices.display_name(vid, humanize_en) == "MetroPolice"
    assert voices.display_name(vid, humanize_ja) == "MetroPolice"

    # ② 写配置用的真 id：三种输入（真 id / 中文名 / 英文名）都要还原
    for typed in (vid, zh, "MetroPolice"):
        assert voices.real_id(typed, humanize_en) == vid, typed
    assert voices.real_id(zh, humanize_ja) == vid
    assert voices.real_id("  " + zh + " ", humanize_en) == vid

    # ③ 音色被重建（只有时间戳/后缀不同）也认亲；未登记的（内置音色）原样通过
    rebuilt = "qwen-tts-vc-MetroPolice-voice-20990101000000000-zzzz"
    assert voices.display_name(rebuilt, humanize_en) == "MetroPolice"
    assert voices.real_id(rebuilt, humanize_en) == vid
    assert voices.display_name("Cherry", humanize_en) == "Cherry"
    assert voices.real_id("Cherry", humanize_en) == "Cherry"
    print("  ✓ 复刻音色显示名：中文「国民护卫队」/ 英文「MetroPolice」，写配置还原成真 id")


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_set_or_create_writes_existing_leaf,
        test_set_or_create_builds_missing_block,
        test_set_or_create_fills_missing_middle,
        test_set_or_create_replaces_existing_leaf_value,
        test_set_or_create_handles_voice_with_space,
        test_voice_catalogs_are_separate,
        test_voice_choices_keeps_custom_value,
        test_cloned_voice_display_name,
        test_gui_voice_dropdowns_save,
        test_gui_speech_voice_respects_direction_override,
        test_is_unsupported_voice_err,
        test_preview_buttons_exist,
        test_preview_worker_success_plays_and_reports,
        test_preview_speech_uses_omni_model,
        test_preview_speech_unsupported_voice_reports_friendly,
        test_preview_generic_failure_reports_reason,
        test_preview_voice_guards,
        test_preview_full_thread_roundtrip,
        test_preview_tts_custom_voice_uses_own_model,
        test_synthesize_omni_parses_sse_and_payload,
        test_synthesize_omni_surfaces_server_error,
    ]
    print("test_voice_settings:")
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
