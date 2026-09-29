"""专有词库（社团名 / 人名 / 术语）验收测试。

用户场景：在 VRChat 里念社团名（"VRChat"、"逆袭"），模型要么听错、
要么按字面意译（"VRChat" → "虚拟聊天"）。词库把这些词钉死，且**两条腿**都要吃：
  · 说话那条腿 → 实时会话的 `translation.corpus.phrases`
  · 打字那条腿 → qwen-mt 的 `translation_options.terms`（术语干预）

本文件守的就是「两条腿都没有漏、口径没有分裂」：
  ① 界面文本框的文本格式（一行一条 `原文=译名`）解析/回显；
  ② config.yaml 的 `glossary:` 段就地写入（保住段外注释、老配置补建、空词库保留键）；
  ③ 全局词库 + 方向级 hotwords 的**合并口径唯一**（config.merge_hotwords）；
  ④ 打字那条腿真的把 terms 传给了服务端请求体；
  ⑤ 实时那条腿真的把 phrases 下发进了 session.update；
  ⑥ 设置弹窗保存后：落盘 + 内存同步 + 通知正在跑的引擎重建会话。
  ⑦ 非法配置（写成列表/标量/空条目）必须**留痕后丢掉**，绝不原样下发把整条会话搞挂。

全程离线：不打桩就不发任何网络请求（translate_text 被替换、会话只构造不发包）；
GUI 用例用**临时 config.yaml**，绝不碰仓库里的真配置。

跑法：.venv/bin/python tests/test_glossary.py
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# CI / 新克隆上没有 key，而 load_config 默认要求 key。只验配置读写，与 key 真假无关。
os.environ.setdefault("DASHSCOPE_API_KEY", "sk" + "-glossary-testonly0123456789abcdef")

# 界面文案跟随系统语言，钉死成中文保证可复现（产品代码不依赖这个补丁）。
import vlt.i18n as _i18n  # noqa: E402
_i18n.detect_system_language = lambda: "zh"

import vlt.config as config_mod  # noqa: E402
import vlt.engine as engine_mod  # noqa: E402
from vlt.config import AppConfig, Direction, load_config, merge_hotwords  # noqa: E402
from vlt.config_io import _yaml_set_mapping  # noqa: E402
from vlt.engine import Engine, EngineEvents  # noqa: E402
from vlt.session.qwen38 import QwenLiveTranslateSession  # noqa: E402
from vlt.textin import terms_from_mapping  # noqa: E402


def _tmp_config(body: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="vlt-glossary-cfg-"))
    p = tmp / "config.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@contextlib.contextmanager
def _isolated_home():
    """临时 HOME：别让 load_config 去读用户真实的已保存 key / Bailian 配置。

    （不隔离也能过，但用例就依赖了跑测机器上有没有存过 key —— 那不是本用例要验的东西，
    而且读用户真实凭据文件这种事，测试里能免则免。）
    """
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    tmp = Path(tempfile.mkdtemp(prefix="vlt-glossary-home-"))
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    try:
        yield tmp
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


BASE_BODY = """\
# 主配置：注释必须活着
session:
  model: qwen3.8-livetranslate-flash-realtime
  base_url: wss://maas.qianwenaiapi.com/api-ws/v1/realtime
  voice: Tina
  api_key: sk-unused
directions:
  mine:
    source_lang: zh
    target_lang: en
    hotwords:
      "VRChat": "VRChat (方向级)"
  theirs:
    source_lang: null
    target_lang: zh
    output_audio: false
output:
  audio:
    enabled: false
ui:
  direction: mine
"""


def _mk_engine(direction: str = "mine", glossary: dict | None = None,
               hotwords: dict | None = None) -> Engine:
    cfg = AppConfig(
        session_base={
            "api_key": "sk-test",
            "model": "qwen3.8-livetranslate-flash-realtime",
            "base_url": "wss://example.invalid/api-ws/v1/realtime",
            "glossary": glossary if glossary is not None else {"VRChat": "VRChat"},
        },
        directions={"mine": Direction(source_lang="zh", target_lang="en",
                                      hotwords=hotwords or {}),
                    "theirs": Direction(source_lang=None, target_lang="zh")},
        chatbox={"max_chars": 144},
        merger={},
        text_input={"model": "qwen-mt-flash", "timeout_s": 5, "tts": {}},
    )
    return Engine(cfg=cfg, direction=direction, source="mic", sinks={"chatbox"},
                  events=EngineEvents(), dry_run=True)


# ---------------------------------------------------------------- ① 文本格式


def test_parse_and_render_lines() -> None:
    import vlt.gui as gui_mod

    text = (
        "# 这是注释，忽略\n"
        "\n"
        "VRChat=VRChat\n"
        "  逆袭  =  Nixi  \n"
        "没有等号的行应被忽略\n"
        "全角＝equals\n"
        "a=b=c\n"          # 只按第一个等号切 → 原文 a、译名 b=c
        "空译名=\n"        # 译名空 → 丢弃
        "=空原文\n"        # 原文空 → 丢弃
        "VRChat=后写的赢\n"   # 重复原文以最后一条为准
    )
    got = gui_mod._parse_glossary_lines(text)
    want = {
        "VRChat": "后写的赢",
        "逆袭": "Nixi",
        "全角": "equals",
        "a": "b=c",
    }
    assert got == want, f"解析结果不对：{got!r}"
    assert list(got) == ["VRChat", "逆袭", "全角", "a"], \
        f"必须保持出现顺序（用户按重要性排的）：{list(got)!r}"

    assert gui_mod._parse_glossary_lines("") == {}
    assert gui_mod._parse_glossary_lines("   \n#注释\n") == {}

    lines = gui_mod._glossary_to_lines({"VRChat": "VRChat", "逆袭": "Nixi"})
    assert lines == ["VRChat=VRChat", "逆袭=Nixi"], lines
    assert gui_mod._parse_glossary_lines("\n".join(lines)) == {
        "VRChat": "VRChat", "逆袭": "Nixi"}
    print("  ✓ 一行一条 `原文=译名`：注释/空行/全角等号/重复覆盖/顺序保持")


def test_terms_from_mapping() -> None:
    got = terms_from_mapping({"VRChat": "VRChat", "逆袭": "Nixi"})
    assert got == [{"source": "VRChat", "target": "VRChat"},
                   {"source": "逆袭", "target": "Nixi"}], got
    # 空 key / 空 value 丢掉；None / {} → 空数组（调用方据此不传该字段）
    assert terms_from_mapping({"": "x", "y": "", "  ": " ", "a": "b"}) == [{"source": "a", "target": "b"}]
    assert terms_from_mapping(None) == [] and terms_from_mapping({}) == []
    print("  ✓ terms 数组转换：跳过空条目、保持顺序")


# ---------------------------------------------------------------- ② 写盘


def test_yaml_mapping_write() -> None:
    # 没有 glossary 段 → 补建到文件末尾
    out = _yaml_set_mapping("# 顶部\nsession:\n  voice: Tina   # 别动我\n",
                            ["glossary"], {"VRChat": "VRChat"})
    data = yaml.safe_load(out)
    assert data["glossary"] == {"VRChat": "VRChat"}, data
    assert "# 顶部" in out and "# 别动我" in out, out

    # 已有 glossary（旧条目 + 段外注释）→ 整段替换，段外注释与后续段一行不少
    old = ('# 顶部\n'
           'glossary:\n'
           '  "旧词": "旧译"\n'
           '\n'
           '# ★ 段外说明注释（必须保住）\n'
           'ui:\n'
           '  direction: mine  # 行内注释\n')
    out2 = _yaml_set_mapping(old, ["glossary"], {"逆袭": "Nixi"})
    data2 = yaml.safe_load(out2)
    assert data2["glossary"] == {"逆袭": "Nixi"}, data2
    assert "旧词" not in out2, f"旧条目没被清掉（会「复活」）：\n{out2}"
    assert "# ★ 段外说明注释（必须保住）" in out2 and "# 行内注释" in out2, out2
    assert data2["ui"]["direction"] == "mine", "后续段被破坏"

    # 空词库 → 保留 `glossary: {}`（键在，用户才知道有这个功能）
    out3 = _yaml_set_mapping("glossary:\n  a: b\n", ["glossary"], {})
    assert yaml.safe_load(out3) == {"glossary": {}}, out3

    # 特殊字符必须加引号，否则 YAML 会变味（"Rob: a club" / "# 不是注释"）
    out4 = _yaml_set_mapping("", ["glossary"], {"Rob: a club": "x # not a comment"})
    assert yaml.safe_load(out4) == {"glossary": {"Rob: a club": "x # not a comment"}}, out4

    # 中英混排的社团名与中文译名
    out5 = _yaml_set_mapping("", ["glossary"], {"VRChat": "虚拟聊天", "社团": "Nixi"})
    assert yaml.safe_load(out5)["glossary"] == {"VRChat": "虚拟聊天", "社团": "Nixi"}
    print("  ✓ glossary 段：补建 / 整段替换 / 段外注释保住 / 空值保留键 / 特殊字符加引号")


# ---------------------------------------------------------------- ③ 配置读取与合并


def test_yaml_mapping_write_nested_siblings() -> None:
    """⚠️ 回归：同级另有一张同名的表时，必须改**目标那张**。

    `directions.mine.hotwords` 与 `directions.theirs.hotwords` 缩进相同（都是 4 空格）。
    早先的实现按缩进在全文件里找**第一个**匹配 → 存「别人说」的词条会写进「我说」那张，
    目标那张留成 `{}`（GUI 用例先抓到的，这里给个不依赖窗口的独立守卫）。
    """
    body = ('directions:\n'
            '  mine:\n'
            '    source_lang: zh\n'
            '    hotwords:\n'
            '      "A": "a"\n'
            '  theirs:\n'
            '    source_lang: null\n'
            '    hotwords: {}\n'
            'ui:\n'
            '  direction: mine\n')
    out = _yaml_set_mapping(body, ["directions", "theirs", "hotwords"], {"VRChat": "猫屋"})
    data = yaml.safe_load(out)
    assert data["directions"]["theirs"]["hotwords"] == {"VRChat": "猫屋"}, data["directions"]
    assert data["directions"]["mine"]["hotwords"] == {"A": "a"}, "改错表了（写进 mine 去了）"

    out2 = _yaml_set_mapping(out, ["directions", "mine", "hotwords"], {"B": "b"})
    d2 = yaml.safe_load(out2)
    assert d2["directions"]["mine"]["hotwords"] == {"B": "b"}, d2["directions"]
    assert d2["directions"]["theirs"]["hotwords"] == {"VRChat": "猫屋"}, "反向也改错表了"
    assert d2["ui"]["direction"] == "mine", "后面的段被破坏"
    print("  ✓ 同级同名表（mine / theirs 的 hotwords）互不串写")


def test_load_and_merge() -> None:
    p = _tmp_config(BASE_BODY.replace(
        "directions:", 'glossary:\n  "VRChat": "VRChat"\n  "逆袭": "Nixi"\n\ndirections:'))
    with _isolated_home():
        cfg = load_config(p)
    assert cfg.session_base["glossary"] == {"VRChat": "VRChat", "逆袭": "Nixi"}, \
        cfg.session_base["glossary"]
    # 方向级 hotwords 优先于全局
    assert cfg.merged_hotwords("mine") == {"VRChat": "VRChat (方向级)",
                                           "逆袭": "Nixi"}, cfg.merged_hotwords("mine")
    # 方向级没有的键 → 全局照旧生效
    assert cfg.merged_hotwords("theirs") == {"VRChat": "VRChat",
                                             "逆袭": "Nixi"}, cfg.merged_hotwords("theirs")

    # 合并口径只有一处：Direction.to_session_config 与 merged_hotwords 必须一致
    scfg = cfg.directions["mine"].to_session_config(cfg.session_base)
    assert scfg.hotwords == cfg.merged_hotwords("mine"), \
        f"两条腿口径分裂了：{scfg.hotwords!r} vs {cfg.merged_hotwords('mine')!r}"

    assert merge_hotwords(None, None) == {}
    assert merge_hotwords({"a": "1"}, {"a": "2", "b": "3"}) == {"a": "2", "b": "3"}
    print("  ✓ load_config 读词库 + 方向级覆盖全局 + 两条腿口径一致")


def test_bad_glossary_is_dropped_with_trace() -> None:
    """写成列表 / 标量 / 空条目 → 留痕后丢掉，绝不能原样下发给服务端。"""
    body = ("glossary:\n"
            "  - 这不是映射表\n"
            "  - 真的不是\n")
    buf = io.StringIO()
    with _isolated_home(), contextlib.redirect_stdout(buf):
        cfg = load_config(_tmp_config(body))
    assert cfg.session_base["glossary"] == {}, cfg.session_base["glossary"]
    assert "glossary" in buf.getvalue() and "不是映射表" in buf.getvalue(), \
        f"非法配置必须留痕（否则用户只会看到「翻译不工作」）：{buf.getvalue()!r}"

    body2 = ("glossary:\n"
             '  "好词": "好译"\n'
             '  "空值词": ""\n'
             '  "": "空原文"\n')
    buf2 = io.StringIO()
    with _isolated_home(), contextlib.redirect_stdout(buf2):
        cfg2 = load_config(_tmp_config(body2))
    assert cfg2.session_base["glossary"] == {"好词": "好译"}, cfg2.session_base["glossary"]
    assert "空条目" in buf2.getvalue() or "跳过" in buf2.getvalue(), buf2.getvalue()
    print("  ✓ 非法/空词条：留痕 + 丢弃（不把整条会话搞挂）")


# ---------------------------------------------------------------- ④ 打字那条腿


def test_typing_leg_sends_terms() -> None:
    seen: list[dict] = []
    real = engine_mod.translate_text

    def fake(text, **kw):
        seen.append(kw)
        return "VRChat"

    engine_mod.translate_text = fake
    try:
        eng = _mk_engine(glossary={"VRChat": "VRChat", "逆袭": "Nixi"})
        asyncio.run(eng._async_send_text("我加入了 VRChat 这个社团"))
    finally:
        engine_mod.translate_text = real

    assert len(seen) == 1, seen
    assert seen[0]["terms"] == [{"source": "VRChat", "target": "VRChat"},
                                {"source": "逆袭", "target": "Nixi"}], seen[0]
    print("  ✓ 打字 → translation_options.terms（术语干预）真的带上了")


def test_typing_leg_prefers_direction_override() -> None:
    seen: list[dict] = []
    real = engine_mod.translate_text
    engine_mod.translate_text = lambda text, **kw: (seen.append(kw), "x")[1]
    try:
        eng = _mk_engine(glossary={"VRChat": "VRChat"},
                         hotwords={"VRChat": "VRChat 方向级"})
        asyncio.run(eng._async_send_text("念社团名"))
    finally:
        engine_mod.translate_text = real
    assert seen[0]["terms"] == [{"source": "VRChat", "target": "VRChat 方向级"}], seen[0]
    print("  ✓ 打字：方向级热词覆盖全局同名词条")


# ---------------------------------------------------------------- ⑤ 说话那条腿


def test_realtime_payload_carries_phrases() -> None:
    cfg = AppConfig(
        session_base={
            "api_key": "sk-test", "model": "qwen3.8-livetranslate-flash-realtime",
            "base_url": "wss://example.invalid/api-ws/v1/realtime", "voice": "Tina",
            "glossary": {"VRChat": "VRChat", "逆袭": "Nixi"},
        },
        directions={"mine": Direction(source_lang="zh", target_lang="en")},
        chatbox={}, merger={},
    )
    scfg = cfg.directions["mine"].to_session_config(cfg.session_base)
    payload = QwenLiveTranslateSession(scfg)._session_payload()
    assert payload["translation"]["corpus"]["phrases"] == {
        "VRChat": "VRChat", "逆袭": "Nixi"}, payload
    assert payload["translation"]["language"] == "en", payload

    # 词库为空 → 不下发 corpus（别给服务端塞空表）
    empty = _mk_engine(glossary={})
    scfg2 = empty._cfg.directions["mine"].to_session_config(empty._cfg.session_base)
    payload2 = QwenLiveTranslateSession(scfg2)._session_payload()
    assert "corpus" not in payload2["translation"], payload2
    print("  ✓ 说话 → session.translation.corpus.phrases（空词库则不下发）")


# ---------------------------------------------------------------- ⑥ set_glossary


def test_set_glossary_engine() -> None:
    import time as _time

    class _RunningLoop:
        """够用的假 loop：只回答 is_running（set_glossary 的预算分支只看这一句）。"""

        def is_running(self) -> bool:
            return True

    st: list[tuple] = []
    eng = _mk_engine(glossary={"old": "旧"})
    eng._events = EngineEvents(on_status=lambda l, m: st.append((l, m)))

    # ① 还没开会话：只改内存，返回 True（下次建会话自然带上）
    assert eng.set_glossary({"VRChat": "VRChat"}) is True
    assert eng._cfg.session_base["glossary"] == {"VRChat": "VRChat"}
    assert st == [], st

    # ② 会话在跑但预算已满 → 返回 False 且**必须出声**（不许静默不一致）
    eng._session = object()
    eng._loop = _RunningLoop()
    eng._connect_ts = [_time.monotonic()] * 60
    eng._cfg.session_base["max_new_sessions_per_minute"] = 4
    assert eng.set_glossary({"a": "b"}) is False, "预算不足必须返回 False"
    assert any(l == "warn" and "专有词库" in m for l, m in st), st
    # 内存仍要更新（用户已经保存了；下次重建会话就会用上）
    assert eng._cfg.session_base["glossary"] == {"a": "b"}

    # ③ 预算够 → 必须真的去重建会话（否则「已保存」是假的）
    st.clear()
    eng._connect_ts = [_time.monotonic()]
    asked: list = []
    real_rct = asyncio.run_coroutine_threadsafe

    def fake_rct(coro, loop):  # noqa: ANN001
        asked.append(coro)
        coro.close()               # 没人 await 它，显式关掉免掉 RuntimeWarning
        return None

    engine_mod.asyncio.run_coroutine_threadsafe = fake_rct
    try:
        assert eng.set_glossary({"c": "d"}) is True
    finally:
        engine_mod.asyncio.run_coroutine_threadsafe = real_rct
    assert len(asked) == 1, "预算够时必须请求重建会话"
    assert st == [], f"预算够不该报 warn：{st}"
    print("  ✓ set_glossary：改内存 / 预算不足留痕返回 False / 预算够则重建会话")


# ---------------------------------------------------------------- ⑦ GUI 接线


@contextlib.contextmanager
def _gui_with_config(body: str):
    """临时 HOME + 临时 config.yaml 里起一个真窗口（绝不碰仓库真配置）。"""
    import vlt.gui as gui_mod

    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME")}
    tmp = Path(tempfile.mkdtemp(prefix="vlt-glossary-env-"))
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    cfg_path = _tmp_config(body)
    config_mod.DEFAULT_CONFIG = cfg_path
    gui_mod.DEFAULT_CONFIG = cfg_path
    gui = None
    try:
        gui = gui_mod.TranslationGUI()
        if gui._update_check_job is not None:
            gui._root.after_cancel(gui._update_check_job)
            gui._update_check_job = None
        gui._root.update()
        yield gui, cfg_path
    finally:
        if gui is not None:
            try:
                gui._root.destroy()
            except Exception:  # noqa: BLE001
                pass
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class FakeEngine:
    """只记录「界面通知了哪个引擎、词库是什么」，不真起会话。"""

    def __init__(self, running: bool = True) -> None:
        self.running = running
        self.glossaries: list[dict] = []
        self.direction_hotwords: list[tuple[str, dict]] = []

    def set_glossary(self, mapping: dict) -> bool:
        self.glossaries.append(dict(mapping))
        return True

    def set_direction_hotwords(self, name: str, mapping: dict, label: str = "") -> bool:
        self.direction_hotwords.append((name, dict(mapping)))
        return True


def test_engine_set_direction_hotwords() -> None:
    """方向级热词：写 `directions.<名>.hotwords`、覆盖全局、只影响那一条腿。"""
    import time as _time

    class _RunningLoop:
        def is_running(self) -> bool:
            return True

    st: list[tuple] = []
    eng = _mk_engine(glossary={"VRChat": "VRChat"})
    eng._events = EngineEvents(on_status=lambda l, m: st.append((l, m)))

    # ① 还没开会话：只改内存，返回 True；**全局那份不能被顺手带改**
    assert eng.set_direction_hotwords("theirs", {"VRChat": "猫屋"}, label="别人说") is True
    assert eng._cfg.directions["theirs"].hotwords == {"VRChat": "猫屋"}
    assert eng._cfg.session_base["glossary"] == {"VRChat": "VRChat"}, "全局那张表被动了"
    assert st == [], st

    # ② 生效口径：方向级覆盖全局，且**两条腿互不影响**（这正是加方向级的理由）
    assert eng._cfg.merged_hotwords("theirs") == {"VRChat": "猫屋"}
    assert eng._cfg.merged_hotwords("mine") == {"VRChat": "VRChat"}
    theirs_cfg = eng._cfg.directions["theirs"].to_session_config(eng._cfg.session_base)
    assert theirs_cfg.hotwords == {"VRChat": "猫屋"}, theirs_cfg.hotwords

    # ③ 未知方向 → False（不写坏配置、也不静默成功）
    assert eng.set_direction_hotwords("nope", {"a": "b"}) is False

    # ④ 预算不足 → False 且提示里用**人类可读的方向名**（不是内部 key mine/theirs）
    eng._session = object()
    eng._loop = _RunningLoop()
    eng._connect_ts = [_time.monotonic()] * 60
    eng._cfg.session_base["max_new_sessions_per_minute"] = 4
    assert eng.set_direction_hotwords("theirs", {"a": "b"}, label="别人说") is False
    assert any(l == "warn" and "别人说" in m and "预算不足" in m for l, m in st), st

    # ⑤ 预算够 → 真的请求重建会话（否则「已保存」是假的）
    st.clear()
    eng._connect_ts = [_time.monotonic()]
    asked: list = []
    real_rct = asyncio.run_coroutine_threadsafe

    def fake_rct(coro, loop):  # noqa: ANN001
        asked.append(coro)
        coro.close()
        return None

    engine_mod.asyncio.run_coroutine_threadsafe = fake_rct
    try:
        assert eng.set_direction_hotwords("theirs", {"c": "d"}, label="别人说") is True
    finally:
        engine_mod.asyncio.run_coroutine_threadsafe = real_rct
    assert len(asked) == 1, "预算够时必须请求重建会话"
    assert st == [], f"预算够不该报 warn：{st}"
    print("  ✓ set_direction_hotwords：写方向级 / 覆盖全局 / 未知方向 False / 预算分支 + 人类可读提示")


def test_gui_glossary_scope_roundtrip() -> None:
    """「作用方向」下拉：切表编辑、写对应键、**两张表互不覆盖**、只通知对应引擎。"""
    body = BASE_BODY.replace(
        "directions:", 'glossary:\n  "VRChat": "VRChat"\n\ndirections:')
    with _gui_with_config(body) as (gui, cfg_path):
        # 默认落在「全局」，显示全局那份
        assert gui._glossary_scope() == "global"
        assert gui._glossary_text.get("1.0", "end").strip() == "VRChat=VRChat"

        # 切到「别人说」→ 显示 directions.theirs.hotwords（模板里没有 → 空），提示语跟着换
        gui._glossary_scope_var.set(gui._glossary_scope_names["theirs"])
        gui._on_glossary_scope_change()
        assert gui._glossary_scope() == "theirs"
        assert gui._glossary_text.get("1.0", "end").strip() == "", "切表后应显示该表内容（空）"
        assert "别人说" in gui._glossary_hint.cget("text"), gui._glossary_hint.cget("text")

        # 编辑 + 保存 → 只写 directions.theirs.hotwords，只通知 they 那条腿
        fake_mine, fake_theirs = FakeEngine(), FakeEngine()
        gui._engines, gui._engine_dirs = [fake_mine, fake_theirs], ["mine", "theirs"]
        gui._glossary_text.insert("1.0", "VRChat=猫屋\n")
        with contextlib.redirect_stdout(io.StringIO()):
            gui._on_save_glossary()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["directions"]["theirs"]["hotwords"] == {"VRChat": "猫屋"}, data["directions"]
        assert data["glossary"] == {"VRChat": "VRChat"}, "保存方向级时把全局那张表动了！"
        assert gui._cfg.directions["theirs"].hotwords == {"VRChat": "猫屋"}, "内存没同步"
        assert gui._cfg.session_base["glossary"] == {"VRChat": "VRChat"}, "内存里的全局被动了"
        assert fake_theirs.direction_hotwords == [("theirs", {"VRChat": "猫屋"})], \
            fake_theirs.direction_hotwords
        assert fake_mine.direction_hotwords == [] and fake_mine.glossaries == [], \
            "方向级改动只该通知那一条腿（否则平白撞一次 RPM 预算）"
        assert "别人说" in gui._glossary_status.cget("text"), gui._glossary_status.cget("text")

        # 切回「全局」→ 内容仍是全局那份（没被方向级的内容顶掉）
        gui._glossary_scope_var.set(gui._glossary_scope_names["global"])
        gui._on_glossary_scope_change()
        assert gui._glossary_text.get("1.0", "end").strip() == "VRChat=VRChat"

        # 保存全局 → 不能吃掉方向级那张表
        gui._glossary_text.delete("1.0", "end")
        gui._glossary_text.insert("1.0", "VRChat=VRChat\n逆袭=Nixi\n")
        with contextlib.redirect_stdout(io.StringIO()):
            gui._on_save_glossary()
        data2 = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data2["glossary"] == {"VRChat": "VRChat", "逆袭": "Nixi"}, data2["glossary"]
        assert data2["directions"]["theirs"]["hotwords"] == {"VRChat": "猫屋"}, \
            "保存全局时把方向级那张表吃掉了！"
        assert fake_mine.glossaries == [{"VRChat": "VRChat", "逆袭": "Nixi"}], fake_mine.glossaries
    print("  ✓ 作用方向下拉：切表 / 写对应键 / 两表互不覆盖 / 只通知对应引擎")


def test_gui_glossary_roundtrip() -> None:
    body = BASE_BODY.replace(
        "directions:", 'glossary:\n  "VRChat": "VRChat"\n\ndirections:')
    with _gui_with_config(body) as (gui, cfg_path):
        # 回显：文本框里是配置里的现有词条（不是空的）
        shown = gui._glossary_text.get("1.0", "end").strip()
        assert shown == "VRChat=VRChat", repr(shown)

        # 保存：界面上改内容 → 落盘 + 内存同步 + 通知在跑的引擎
        fake = FakeEngine(running=True)
        gui._engines, gui._engine_dirs = [fake], ["mine"]
        gui._glossary_text.delete("1.0", "end")
        gui._glossary_text.insert("1.0", "VRChat=VRChat\n逆袭=Nixi\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._on_save_glossary()

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert data["glossary"] == {"VRChat": "VRChat", "逆袭": "Nixi"}, data
        assert "# 主配置：注释必须活着" in cfg_path.read_text(encoding="utf-8"), "注释被吃掉了"
        assert gui._cfg.session_base["glossary"] == data["glossary"], "内存 cfg 没同步"
        assert fake.glossaries == [data["glossary"]], \
            f"没通知正在跑的引擎（词库不会生效）：{fake.glossaries!r}"
        assert "专有词库已保存" in buf.getvalue(), "保存必须留痕"
        assert "已保存 2 条" in gui._glossary_status.cget("text"), \
            gui._glossary_status.cget("text")

        # 清空词库也要能存：保留 glossary: {}，并在界面上如实显示 0 条
        gui._glossary_text.delete("1.0", "end")
        with contextlib.redirect_stdout(io.StringIO()):
            gui._on_save_glossary()
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["glossary"] == {}
        assert gui._cfg.session_base["glossary"] == {}
    print("  ✓ 设置弹窗：回显 / 落盘 / 内存同步 / 通知引擎 / 清空")


def test_gui_settings_open_rereads_disk() -> None:
    """手改 config.yaml 后打开设置：文本框必须显示**磁盘上**的词库。

    否则用户手工加的条目会在下一次点「保存词库」时被界面上的旧内容覆盖掉 ——
    属于「把我配置搞丢了」的 bug，必须有用例守着。
    """
    with _gui_with_config(BASE_BODY) as (gui, cfg_path):
        assert gui._glossary_text.get("1.0", "end").strip() == "", "模板里没有 glossary 段"

        # 用户手工往 config.yaml 里加词条（程序还在跑，内存快照里没有）
        text = cfg_path.read_text(encoding="utf-8")
        cfg_path.write_text(
            _yaml_set_mapping(text, ["glossary"], {"手工加的": "Manual Entry"}),
            encoding="utf-8")
        assert gui._cfg.session_base.get("glossary") in ({}, None), "前提：内存快照里没有它"

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._open_settings()          # 打开设置 → 必须重新读盘
        shown = gui._glossary_text.get("1.0", "end").strip()
        assert shown == "手工加的=Manual Entry", f"没重读磁盘，界面显示的是：{shown!r}"

        # 再保存一次：手工加的条目必须还在（没被空内容覆盖）
        gui._on_save_glossary()
        assert yaml.safe_load(cfg_path.read_text(encoding="utf-8"))["glossary"] == {
            "手工加的": "Manual Entry"}
    print("  ✓ 打开设置时重读磁盘：手工改的 config.yaml 不会被界面覆盖")


def test_gui_save_failure_is_visible() -> None:
    """写盘失败必须**如实报错**，不能回显「已保存」——项目口径：禁静默降级。"""
    with _gui_with_config(BASE_BODY) as (gui, cfg_path):
        real = config_mod.DEFAULT_CONFIG

        class Boom(type(cfg_path)):
            def read_text(self, *a, **kw):  # noqa: ANN002, ANN003
                raise OSError("模拟磁盘只读")

        import vlt.gui as gui_mod
        gui_mod.DEFAULT_CONFIG = Boom(cfg_path)
        try:
            gui._glossary_text.delete("1.0", "end")
            gui._glossary_text.insert("1.0", "VRChat=VRChat\n")
            with contextlib.redirect_stdout(io.StringIO()):
                gui._on_save_glossary()
        finally:
            gui_mod.DEFAULT_CONFIG = real
        status = gui._glossary_status.cget("text")
        assert "保存失败" in status, f"写盘失败却说保存成功：{status!r}"
    print("  ✓ 写盘失败 → 界面如实报错（不回显「已保存」）")


def test_gui_unreadable_lines_are_visible() -> None:
    """格式看不懂的行：解析照旧忽略，但**必须报出来**——以前静默丢掉，用户以为「保存没反应」。"""
    import vlt.gui as gui_mod

    text = ("VRChat=VRChat\n"
            "逆袭：Nixi\n"          # 用了全角冒号：最容易踩的那种
            "# 注释行\n"
            "\n"
            "只有左边=\n"
            "=只有右边\n"
            "坏行\n")
    mapping = gui_mod._parse_glossary_lines(text)
    issues = gui_mod._glossary_line_issues(text)
    assert mapping == {"VRChat": "VRChat"}, mapping
    assert [n for n, _ in issues] == [2, 5, 6, 7], issues
    print(f"  ✓ 解析照旧忽略格式错误行，但行号报得出来：{[n for n, _ in issues]}")

    body = BASE_BODY.replace("directions:", "glossary:\n  \"旧词\": \"Old\"\n\ndirections:")
    with _gui_with_config(body) as (gui, cfg_path):
        gui._glossary_text.delete("1.0", "end")
        gui._glossary_text.insert("1.0", text)
        with contextlib.redirect_stdout(io.StringIO()):
            gui._on_save_glossary()
        status = gui._glossary_status.cget("text")
        style = gui._glossary_status.cget("style")
        assert "忽略" in status and "1 条" in status and style == "Warn.TLabel", (status, style)
        saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8")).get("glossary")
        assert saved == {"VRChat": "VRChat"}, saved
    print(f"  ✓ 界面用警告样式提示「{status}」，配置里只落有效词条")


def main() -> int:
    tests = [
        test_parse_and_render_lines,
        test_terms_from_mapping,
        test_yaml_mapping_write,
        test_yaml_mapping_write_nested_siblings,
        test_load_and_merge,
        test_bad_glossary_is_dropped_with_trace,
        test_typing_leg_sends_terms,
        test_typing_leg_prefers_direction_override,
        test_realtime_payload_carries_phrases,
        test_set_glossary_engine,
        test_engine_set_direction_hotwords,
        test_gui_glossary_scope_roundtrip,
        test_gui_glossary_roundtrip,
        test_gui_settings_open_rereads_disk,
        test_gui_save_failure_is_visible,
        test_gui_unreadable_lines_are_visible,
    ]
    print("test_glossary:")
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
