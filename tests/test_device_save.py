"""设备选择保存：改设备下拉**不能**毁掉 config.yaml 的注释与键顺序（回归）。

## 治什么

`gui._save_device_config` 原来用 `yaml.safe_load` + `yaml.dump` **整文件重写**：
用户每改一次设备下拉，配置里的注释、空行、键顺序全部消失。实测一份 154 行 /
53 行注释的 `config.yaml` 被拍成 116 行、键按字母重排的转储（注释是配置里唯一的
说明书，丢了只能回去看模板）。

其它保存路径（语言 / 译音开关 / 手腕屏微调 / 音色 / 输入门限）早就改成了就地写
（`_yaml_set_or_create`），只有这条漏了 —— 而设备下拉恰恰是最常动的控件之一。
现在它也走就地写，本文件守着它别退回去。

顺带守住两个就地写特有的坑（整份 dump 时代是自动的，手拼就得自己保证）：

- **值必须安全渲染**：设备名形如 `Speaker #2 (USB: Audio)`，含 `#` 时手拼会把
  后半行截成注释 → 用 `_yaml_scalar`（交给 PyYAML 决定要不要加引号）后读出仍一致；
- **老配置可能整段没有 `capture` / `output.audio`** → 用 `_yaml_set_or_create`
  补建，别静默 no-op（否则这个设置永远存不下去）。

## 离线可跑

不连网络、不枚举真实设备（下拉的 `values` / 内部名字表直接注入，只走
「取值 → 保存」这条真实路径）。个人 `config.yaml` 先备份、测完还原。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 界面语言钉死 zh：CI 与外国机器是英文系统，`自动检测` 这类文案会跟着变
# （产品代码不依赖这个补丁，只有本文件的下拉文案依赖）。
import vlt.i18n as _i18n  # noqa: E402

_i18n.detect_system_language = lambda: "zh"

CONFIG = ROOT / "config.yaml"          # 源码运行的配置路径（与 gui.DEFAULT_CONFIG 同一个）

MIC = "麦克风 (USB Audio Device)"
LOOP = "扬声器 (CABLE In 16 Ch) [Loopback]"
OUT = "CABLE Input (VB-Audio Virtual Cable)"
WEIRD = "Speaker #2 (USB: Audio) [Loopback]"     # 含 `#` 与 `: ` —— 手拼会写出坏 YAML


def _n_comments(t: str) -> int:
    return sum(1 for ln in t.splitlines() if ln.strip().startswith("#"))


def _top_keys(t: str) -> list[str]:
    return [ln.split(":")[0] for ln in t.splitlines()
            if ln and not ln[0].isspace() and ln.rstrip().endswith(":")]


def _prepare() -> str:
    """确保有 config.yaml（CI / 新克隆上没有 → 照程序规矩从模板生成），返回原文。"""
    if not CONFIG.exists():
        CONFIG.write_text((ROOT / "config.example.yaml").read_text(encoding="utf-8"),
                          encoding="utf-8")
    return CONFIG.read_text(encoding="utf-8")


def _make_gui():  # noqa: ANN202
    from vlt.gui import TranslationGUI

    return TranslationGUI()


def _set_device_combos(gui, mic: str = MIC, loop: str = LOOP, out: str = OUT) -> None:  # noqa: ANN001
    """把设备下拉的候选与内部名字表注入好（不碰真实设备枚举）。

    下拉第 0 项是「自动检测」，真实设备从下标 1 开始 —— `_on_device_change` 就是
    按这个偏移取回原始设备名的。

    ⚠️ **Linux 上界面根本不建 loopback / 译音输出这两个下拉**（`platform.IS_LINUX` →
    `_linux_fixed_audio`，那两个属性是 `None`）。CI 的 `linux-tests` 腿就是这么挂的：
    对着 `None` 调 `.configure()` → `AttributeError`。所以这里必须容忍它们不存在。
    """
    from vlt.i18n import t

    auto = t("自动检测")
    gui._names_holder["mic"] = [mic]                                    # noqa: SLF001 — 设备名唯一真源
    gui._mic_combo.configure(values=[auto, mic])                        # noqa: SLF001
    gui._mic_combo.set(mic)                                             # noqa: SLF001
    loop_combo = getattr(gui, "_loopback_combo", None)
    out_combo = getattr(gui, "_audio_out_combo", None)
    if loop_combo is None or out_combo is None:
        return                              # Linux：只有麦克风下拉，其余交给 _save_device_config 跳过
    gui._names_holder["loop"] = [loop]                                  # noqa: SLF001
    loop_combo.configure(values=[auto, loop])
    loop_combo.set(loop)
    gui._names_holder["out"] = [out]                                    # noqa: SLF001
    out_combo.configure(values=[auto, out])
    out_combo.set(out)


def _assert_devices(data: dict, base: dict, gui, mic: str, loop: str, out: str) -> None:  # noqa: ANN001
    """断言三个设备键的落点：Windows 三个都写；Linux 只写麦克风、另两个**原样保留**。"""
    cap = data.get("capture") or {}
    base_cap = base.get("capture") or {}
    audio = (data.get("output") or {}).get("audio") or {}
    base_audio = (base.get("output") or {}).get("audio") or {}
    assert cap.get("mic_device") == mic, f"麦克风没写进去：{cap}"
    if gui._linux_fixed_audio:                                             # noqa: SLF001
        assert cap.get("loopback_device") == base_cap.get("loopback_device", ""), \
            "Linux 上 loopback_device 应原样保留（界面不给选，别清成空串）"
        assert audio.get("device_name") == base_audio.get("device_name", ""), \
            "Linux 上译音输出设备应原样保留"
    else:
        assert cap.get("loopback_device") == loop, f"loopback 没写进去：{cap}"
        assert audio.get("device_name") == out, f"译音输出没写进去：{audio}"


def _destroy(gui) -> None:  # noqa: ANN001
    try:
        gui._root.destroy()                                             # noqa: SLF001
    except Exception:  # noqa: BLE001
        pass


def test_device_change_keeps_comments_and_key_order() -> None:
    """★ 核心回归：改设备后注释行数、顶层键顺序、其它段的值全都不许变。"""
    before = _prepare()
    n_before, keys_before = _n_comments(before), _top_keys(before)
    base = yaml.safe_load(before)
    gui = None
    try:
        gui = _make_gui()
        _set_device_combos(gui)
        gui._on_device_change()              # 真实路径：按下拉取值 → 保存
        after = CONFIG.read_text(encoding="utf-8")
        data = yaml.safe_load(after)         # ← 必须仍是合法 YAML
        _assert_devices(data, base, gui, MIC, LOOP, OUT)
        assert _n_comments(after) == n_before, \
            f"注释被破坏：{n_before} → {_n_comments(after)} 行"
        assert _top_keys(after) == keys_before, \
            f"顶层键顺序被改变：{keys_before} → {_top_keys(after)}"
        # 非设备键的值一个都不能动
        for seg in ("session", "directions", "chatbox", "merger", "overlay",
                    "text_input", "ui"):
            assert data.get(seg) == base.get(seg), f"{seg} 段的值被改动了"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print(f"  改设备后注释 {n_before} 行、键顺序、其它段值全保持 OK")


def test_unsafe_device_name_is_quoted_not_broken() -> None:
    """设备名含 `#` / `: ` 时必须安全渲染 —— 写出合法 YAML 且读回原名。"""
    before = _prepare()
    base = yaml.safe_load(before)
    n_before = _n_comments(before)
    gui = None
    try:
        gui = _make_gui()
        _set_device_combos(gui, mic=WEIRD, loop=WEIRD, out=WEIRD)
        gui._on_device_change()
        after = CONFIG.read_text(encoding="utf-8")
        data = yaml.safe_load(after)          # 旧实现靠 yaml.dump 自动加引号，就地写要自己保证
        _assert_devices(data, base, gui, WEIRD, WEIRD, WEIRD)
        assert _n_comments(after) == n_before, "注释被破坏"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print(f"  含 `#`/`: ` 的设备名（{WEIRD!r}）写入后仍能读回原名 OK")


def test_linux_branch_writes_mic_only() -> None:
    """Linux 分支：只写麦克风，`loopback_device` / 译音输出设备**原样保留**。"""
    before = _prepare()
    base = yaml.safe_load(before)
    old_loop = (base.get("capture") or {}).get("loopback_device", "")
    old_out = ((base.get("output") or {}).get("audio") or {}).get("device_name", "")
    gui = None
    try:
        gui = _make_gui()
        gui._linux_fixed_audio = True         # noqa: SLF001
        gui._save_device_config("麦克风 A", "不该写入", "不该写入")   # noqa: SLF001
        data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        cap = data.get("capture") or {}
        assert cap.get("mic_device") == "麦克风 A"
        assert cap.get("loopback_device", "") == old_loop, "Linux 上 loopback 应原样保留"
        assert ((data.get("output") or {}).get("audio") or {}).get("device_name", "") == old_out, \
            "Linux 上译音输出设备应原样保留"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print("  Linux 分支只写麦克风、其余键原样保留 OK")


def test_missing_sections_are_created() -> None:
    """老配置整段没有 `capture` / `output.audio` 时也要存得下去（补建，不静默 no-op）。

    Windows / Linux 两种语义各跑一遍：Linux 只写麦克风，所以它**只**补建
    `capture.mic_device`，不会顺手造出 `loopback_device` / `output.audio`。
    """
    before = _prepare()
    try:
        for linux in (False, True):
            CONFIG.write_text("session:\n  model: x\n  # 只留 session 的老配置\n",
                              encoding="utf-8")
            gui = _make_gui()
            try:
                gui._linux_fixed_audio = linux                 # noqa: SLF001
                gui._save_device_config(MIC, LOOP, OUT)        # noqa: SLF001
                data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
                assert data["session"]["model"] == "x", "原有段被破坏"
                cap = data.get("capture") or {}
                assert cap.get("mic_device") == MIC, f"capture 段没被补建：{cap}"
                audio = (data.get("output") or {}).get("audio") or {}
                if linux:
                    assert "loopback_device" not in cap, "Linux 不该补建 loopback_device"
                    assert not audio, "Linux 不该补建 output.audio"
                else:
                    assert cap.get("loopback_device") == LOOP, f"loopback 没补建：{cap}"
                    assert audio.get("device_name") == OUT, f"output.audio 没补建：{data.get('output')}"
            finally:
                _destroy(gui)
    finally:
        CONFIG.write_text(before, encoding="utf-8")
    print("  缺 capture / output.audio 的老配置：两种平台语义下都能补建存档 OK")


def test_yaml_scalar_quotes_when_needed() -> None:
    """`_yaml_scalar`：该加引号时加、不该加时保持原样（中文不转义）。"""
    from vlt.config_io import _yaml_scalar

    assert _yaml_scalar("") == "''", "空串要写成 ''（与模板里的 mic_device: '' 一致）"
    assert _yaml_scalar("CABLE Input") == "CABLE Input"
    assert _yaml_scalar("扬声器 (VB-Audio Voicemeeter VAIO)") == "扬声器 (VB-Audio Voicemeeter VAIO)", \
        "中文设备名不该被转义成 \\uXXXX"
    for weird in ("a#b", "a: b", "[x]", "*star", "&anchor", "%pct", "- dash", "中文 # 注释"):
        rendered = _yaml_scalar(weird)
        assert yaml.safe_load("k: " + rendered)["k"] == weird, \
            f"{weird!r} → {rendered!r} 读回不一致（会写出坏 YAML）"
    # ★ 长值不许被 PyYAML 折行截断（默认 width=80）：截断后仍是**合法 YAML**，
    #   于是静默写进一个错的设备名；需要加引号的那种会因首行引号未闭合让整份拒写。
    long_cases = (
        "Speakers (Realtek(R) Audio) 2- USB Audio Device with an extremely long descriptor name",
        "VoiceMeeter Input (VB-Audio VoiceMeeter VAIO) #2 [Loopback] 24bit 48000Hz Stereo Mix",
        "x" * 120,
        "长设备名" * 30,
    )
    for name in long_cases:
        rendered = _yaml_scalar(name)
        assert "\n" not in rendered, f"长值（{len(name)} 字符）被折行了：{rendered!r}"
        assert yaml.safe_load("k: " + rendered)["k"] == name, \
            f"长值（{len(name)} 字符）读回不一致：{rendered!r}"
    print("  _yaml_scalar：特殊字符加引号、中文不转义、长值不折行 OK")


def test_long_device_name_round_trips() -> None:
    """★ 长设备名（>80 字符）走完整保存路径后必须**一字不少**。

    PyYAML 默认 `width=80` 会给超宽标量折行，而 `_yaml_scalar` 只取第一行 ——
    截断结果是合法 YAML，配置照写、无任何报错，只有下次启动时设备选择悄悄回落
    「自动检测」（用户上次的选择凭空消失）。这条用例就是钉住它。
    """
    long_mic = ("Speakers (Realtek(R) Audio) 2- USB Audio Device with an extremely "
                "long descriptor name")
    long_loop = ("VoiceMeeter Input (VB-Audio VoiceMeeter VAIO) #2 [Loopback] "
                 "24bit 48000Hz Stereo Mix")
    assert len(long_mic) > 80 and len(long_loop) > 80, "样本本身要超过 80 字符才有意义"
    before = _prepare()
    base = yaml.safe_load(before)
    n_before = _n_comments(before)
    gui = None
    try:
        gui = _make_gui()
        _set_device_combos(gui, mic=long_mic, loop=long_loop, out=long_loop)
        gui._on_device_change()
        after = CONFIG.read_text(encoding="utf-8")
        data = yaml.safe_load(after)
        _assert_devices(data, base, gui, long_mic, long_loop, long_loop)
        assert _n_comments(after) == n_before, "注释被破坏"
        # 再把文件读回内存一次，模拟「重启程序后还记得上次选的设备」
        assert (yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
                .get("capture", {}).get("mic_device")) == long_mic, \
            "重启读回时设备名被截断了"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print(f"  {len(long_mic)} 字符设备名走完整保存路径后一字不少 OK")


def test_simulated_linux_env_writes_mic_only() -> None:
    """★ 本机（Windows）也能验证 Linux 语义：那两个下拉为 `None` 时**不许崩**、只写麦克风。

    这条是 CI 教出来的：`tests/test_device_save.py` 第一次进 `linux-tests` 腿时，
    因为对着 `None` 调 `.configure()` 直接把整个 job 打挂了（`AttributeError:
    'NoneType' object has no attribute 'configure'`）—— 本机是 Windows，那两个下拉
    一直存在，看不见这个分支。现在把"Linux 状态"在本机复现出来，Windows 上就能守住它。
    """
    before = _prepare()
    base = yaml.safe_load(before)
    gui = None
    try:
        gui = _make_gui()
        gui._linux_fixed_audio = True          # noqa: SLF001
        gui._loopback_combo = None             # noqa: SLF001
        gui._audio_out_combo = None            # noqa: SLF001
        _set_device_combos(gui)                # 不许崩（Linux 上这两个下拉根本不建）
        gui._on_device_change()                # noqa: SLF001
        after = CONFIG.read_text(encoding="utf-8")
        _assert_devices(yaml.safe_load(after), base, gui, MIC, LOOP, OUT)
        assert _n_comments(after) == _n_comments(before), "注释被破坏"
    finally:
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print("  模拟 Linux（两个下拉为 None）：不崩、只写麦克风、其余键原样 OK")


def test_device_save_after_real_scan() -> None:
    """★ 核心回归（2026-10）：**不预先注入名字表**，走真实设备扫描 → 选设备 → 必须存进去。

    曾经的形态：`gui` 另存一份 `_mic_names` 镜像，只有队列化的 `_on_device_scan_result`
    会刷新它；而启动扫描是同步路径、不经过那里，镜像永远是空的 —— `sync_from_gui` 每次
    同步都用空镜像把刚扫到的名字清掉 → 改麦克风下拉静默存成 `mic_device: ''` → 永远用
    系统默认（表现：AppImage 与源码都一样；10-06 起的 gui 拆分引入）。

    这条用例桩掉 `platform.device_backend()` 给出假设备，让 **GUI 构造期的真扫描** 填表，
    再按下拉选一项保存；并断言一次 `_sync_audio_ctx()` 不会清空名字表。
    """
    before = _prepare()
    gui = None
    import vlt.platform as _plat

    class _FakeBackend:
        def query_devices(self):                             # noqa: ANN201
            return [
                {"name": "Fake Mic A", "max_input_channels": 2, "max_output_channels": 0,
                 "default_samplerate": 48000.0},
                {"name": "Fake Speaker", "max_input_channels": 0, "max_output_channels": 2,
                 "default_samplerate": 48000.0},
            ]

        def query_loopback_devices(self):                    # noqa: ANN201
            return [{"index": 1, "name": "Fake Speaker [Loopback]",
                     "defaultSampleRate": 48000, "maxInputChannels": 2}]

    orig_backend = _plat.device_backend
    _plat.device_backend = lambda: _FakeBackend()            # type: ignore[assignment]
    try:
        gui = _make_gui()                                   # 构造期真扫描 → 填 _names_holder
        assert gui._names_holder["mic"] == ["Fake Mic A"], \
            f"启动扫描没填进名字表：{gui._names_holder['mic']}"
        gui._sync_audio_ctx()                               # 曾经这一下就把名字表清空了
        assert gui._names_holder["mic"] == ["Fake Mic A"], \
            "同步后名字表被清空 —— `_names_holder` 又被别的名字表覆盖了"

        from vlt.ui_tk import combo_values
        vals = combo_values(gui._mic_combo)
        assert any("Fake Mic A" in v for v in vals), f"下拉里没有假麦克风：{vals}"
        gui._mic_combo.set(next(v for v in vals if "Fake Mic A" in v))
        gui._on_device_change()
        cap = (yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}).get("capture") or {}
        assert cap.get("mic_device") == "Fake Mic A", f"选了假麦克风却没存进去（原 bug）：{cap}"
    finally:
        _plat.device_backend = orig_backend                 # type: ignore[assignment]
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print("  真扫描 → 选设备 → 落盘（名字表不被同步清空）OK")


def test_startup_scan_restores_saved_mic() -> None:
    """★ 回归（2026-10）：启动后麦克风下拉必须显示**上次保存的设备**，不能总是「自动检测」。

    根因：`gui_audio.start_device_scan` 调 `on_device_scan_result` 时硬写 `cfg=None`，
    而「恢复上次选择」完全依赖 `cfg`（读 `cfg.output.capture.mic_device`）→ 下拉每次都被
    设成自动。实际采集不受影响（代理/引擎直接读配置），所以只是 UI 显示 bug。本用例走
    **构造期真扫描**（`_make_gui` → `_start_device_scan`）。
    """
    before = _prepare()
    gui = None
    import vlt.platform as _plat

    class _FakeBackend:
        def query_devices(self):                             # noqa: ANN201
            return [
                {"name": "Fake Mic A", "max_input_channels": 2, "max_output_channels": 0,
                 "default_samplerate": 48000.0},
                {"name": "Fake Speaker", "max_input_channels": 0, "max_output_channels": 2,
                 "default_samplerate": 48000.0},
            ]

        def query_loopback_devices(self):                    # noqa: ANN201
            return [{"index": 1, "name": "Fake Speaker [Loopback]",
                     "defaultSampleRate": 48000, "maxInputChannels": 2}]

    orig_backend = _plat.device_backend
    _plat.device_backend = lambda: _FakeBackend()            # type: ignore[assignment]
    try:
        data = yaml.safe_load(before) or {}
        data.setdefault("capture", {})["mic_device"] = "Fake Mic A"
        CONFIG.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")

        gui = _make_gui()                                   # 构造期真扫描
        from vlt.i18n import t
        got = gui._mic_combo.get()                          # noqa: SLF001
        auto = t("自动检测")
        assert got != auto, f"启动后麦克风下拉竟然是「{auto}」：{got!r}"
        assert "Fake Mic A" in got, f"没恢复上次保存的麦克风：{got!r}"
    finally:
        _plat.device_backend = orig_backend                 # type: ignore[assignment]
        CONFIG.write_text(before, encoding="utf-8")
        if gui is not None:
            _destroy(gui)
    print("  启动扫描后下拉显示上次保存的麦克风（不是自动检测）OK")


if __name__ == "__main__":
    print("test_device_save:")
    test_yaml_scalar_quotes_when_needed()
    test_device_change_keeps_comments_and_key_order()
    test_unsafe_device_name_is_quoted_not_broken()
    test_long_device_name_round_trips()
    test_linux_branch_writes_mic_only()
    test_simulated_linux_env_writes_mic_only()
    test_missing_sections_are_created()
    test_device_save_after_real_scan()
    test_startup_scan_restores_saved_mic()
    print("ALL PASSED")
