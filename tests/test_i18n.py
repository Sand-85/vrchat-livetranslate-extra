"""i18n 验收：词表完整性机械守卫 + 格式化/回落/系统语言探测 + 英文界面无汉字守卫。

跑法：.venv/Scripts/python.exe tests/test_i18n.py

全程离线：不联网、不启动引擎。GUI 用例用**临时 config.yaml**（ui.lang: en）起真窗口，
绝不碰用户真实配置；「打开日志文件夹」用例把 os.startfile 打桩，绝不真开资源管理器。

已知边界（不是疏漏，是有意为之）：
- 机械守卫只认 `t("字面量")`；个别动态 key（`t(_zh)`，日志要留中文原文的场合）不在
  扫描面内 —— 这些词条在 en.py 里已就位，靠本文件的英文界面守卫兜底。
- 守卫扫的是控件的 text 属性；下拉框的**值**（源/目标语言名、界面语言母语名）
  不在 text 属性里，不在扫描面内。
"""
from __future__ import annotations

import ast
import collections
import contextlib
import io
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CJK_RE = re.compile(r"[一-鿿]")


# ---------------------------------------------------------------- 小工具


def _isolate_env(tmp: Path) -> dict:
    """把 HOME/USERPROFILE 指到临时目录并摘掉环境变量 key：隔绝本机可能存在的 key 来源。"""
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "DASHSCOPE_API_KEY")}
    os.environ["USERPROFILE"] = str(tmp)
    os.environ["HOME"] = str(tmp)
    os.environ.pop("DASHSCOPE_API_KEY", None)
    return saved


def _restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _temp_config(lang: str) -> Path:
    """写一份最小临时 config.yaml（只含 ui.lang），返回路径。绝不碰仓库里的真配置。"""
    tmp = Path(tempfile.mkdtemp(prefix="vlt-i18n-cfg-"))
    p = tmp / "config.yaml"
    p.write_text(f"ui:\n  lang: {lang}\n", encoding="utf-8")
    return p


def _make_gui(config_path: Path):
    """用临时配置起真窗口；取消启动 3 秒的自动更新检查调度（绝不真连 GitHub）。"""
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
    return gui


def _destroy(gui) -> None:
    if gui is None:
        return
    try:
        gui._root.destroy()
    except Exception:  # noqa: BLE001
        pass


def _walk_texts(win, out: list[str]) -> None:
    """递归收集一棵控件树里所有 text 属性（Label/Button/Radiobutton/Checkbutton 等）。"""
    for w in win.winfo_children():
        try:
            keys = w.keys()
        except Exception:  # noqa: BLE001 — 已销毁的控件直接跳过
            continue
        if "text" in keys:
            out.append(str(w.cget("text")))
        _walk_texts(w, out)


# ---------------------------------------------------------------- ① 词表完整性（机械守卫）


def test_catalog_completeness() -> None:
    """★ 机械守卫：AST 扫 vlt/**/*.py 里所有 `t("字面量")`，每个 key 都必须能在
    **每一套**词表（en / ja / ko / ru）的 STRINGS 里找到 —— 任一语言漏翻直接红。

    用 AST 而不是正则：隐式拼接的多行字面量在 AST 里已经合并成完整字符串，
    正则处理多行拼接既脆弱又容易漏。
    """
    import importlib

    catalogs = {code: importlib.import_module(f"vlt.locales.{code}").STRINGS
                for code in ("en", "ja", "ko", "ru")}

    used = 0
    missing: dict[str, list[str]] = {code: [] for code in catalogs}
    for path in sorted((ROOT / "vlt").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            f = node.func
            name = (f.id if isinstance(f, ast.Name)
                    else f.attr if isinstance(f, ast.Attribute) else "")
            if name != "t":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                used += 1
                for code, cat in catalogs.items():
                    if first.value not in cat:
                        missing[code].append(f"  {path.relative_to(ROOT)}:{first.lineno} "
                                             f"{first.value[:70]!r}")
    assert used > 100, f"扫描面太小了（{used} 处），守卫形同虚设"
    for code, miss in missing.items():
        assert not miss, (f"以下 {len(miss)} 处 t() 的 key 在 {code}.py 里缺词条（漏翻）：\n"
                          + "\n".join(miss))
    # 各词表条数一致（多的那些说明词表里留了没人用的 key，也是漂移）
    sizes = {code: len(cat) for code, cat in catalogs.items()}
    assert len(set(sizes.values())) == 1, f"各语言词表条数不一致：{sizes}"
    # ★ 重复 key 守卫：同一个 key 写两遍会**静默覆盖**（本次踩过：「自动检测」本来就在
    #   词表里，我新加的一行把老翻译挡掉了 —— Python 不报错、条数还对得上，肉眼也难看出）
    for code in catalogs:
        src = (ROOT / "vlt" / "locales" / f"{code}.py").read_text(encoding="utf-8")
        keys: list[str] = []
        for node in ast.parse(src).body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            if not any(getattr(x, "id", "") == "STRINGS" for x in targets):
                continue
            keys += [k.value for k in node.value.keys]
        dup = [k for k, c in collections.Counter(keys).items() if c > 1]
        assert not dup, f"{code}.py 词表里有重复 key（后者会静默覆盖前者）：{dup}"
    print(f"  ✓ 词表完整性：{used} 处 t() 字面量在 {len(catalogs)} 套词表里全有词条"
          f"（各 {sizes['en']} 条）")


# ---------------------------------------------------------------- ② 格式化与回落


def test_format_and_fallback() -> None:
    """带占位符的词条在 en 下正确格式化；占位符对不上时返回未格式化文本、绝不抛异常；
    未知 key 回落中文原文。"""
    from vlt import i18n
    from vlt.i18n import t

    buf = io.StringIO()
    try:
        i18n.set_language("en")
        assert t("已下载 {n} MB", n=3) == "Downloaded 3 MB", \
            f"en 格式化不对：{t('已下载 {n} MB', n=3)!r}"
        # 占位符对不上：返回未格式化文本（不抛异常），并留一行告警
        with contextlib.redirect_stdout(buf):
            got = t("已下载 {n} MB", wrong=3)
        assert got == "Downloaded {n} MB", f"占位符对不上时应返回未格式化文本：{got!r}"
        assert "格式化失败" in buf.getvalue(), "格式化失败必须留痕"
        # 未知 key：回落中文原文
        assert t("这是一条不存在的词条") == "这是一条不存在的词条"
        # zh 下原样返回
        i18n.set_language("zh")
        assert t("已下载 {n} MB", n=3) == "已下载 3 MB"
    finally:
        i18n.set_language("zh")
    print("  ✓ 格式化 / 占位符兜底 / 未知 key 回落 全对")


def test_normalize_language() -> None:
    """normalize_language 容错：en-US/ZH_cn/未知/空 都不许抛、不许产出未登记代码。"""
    from vlt.i18n import normalize_language

    cases = {"en-US": "en", "EN": "en", "en_US": "en", "ZH_cn": "zh", "zh": "zh",
             "fr": "zh", "日语": "zh", "": "zh", None: "zh", "  en  ": "en"}
    for raw, want in cases.items():
        got = normalize_language(raw)
        assert got == want, f"normalize_language({raw!r}) = {got!r}，期望 {want!r}"
    print(f"  ✓ normalize_language 容错：{len(cases)} 个用例全对")


def test_detect_system_language_stubbed() -> None:
    """detect_system_language 打桩：支持的五种语言各归各的；
    **其它一律 en**（德语/法语等已知但未支持的语言，以及检测失败/异常）——
    用户口径「不是支持的语言就显示英文」。"""
    from vlt import i18n

    class _K:
        langid = 0x0409
        boom = None

        @staticmethod
        def GetUserDefaultUILanguage():
            if _K.boom is not None:
                raise _K.boom
            return _K.langid

    class _W:
        kernel32 = _K

    orig = getattr(i18n.ctypes, "windll", None)
    i18n.ctypes.windll = _W()
    try:
        for langid, want in ((0x0804, "zh"),   # zh-CN
                             (0x0404, "zh"),   # zh-TW：主语言也是 0x04
                             (0x0409, "en"),   # en-US
                             (0x0411, "ja"),   # ja-JP
                             (0x0412, "ko"),   # ko-KR
                             (0x0419, "ru"),   # ru-RU
                             (0x0407, "en"),   # de-DE：已知但未支持 → 按英文接待
                             (0x040c, "en")):  # fr-FR：同上
            _K.langid = langid
            got = i18n.detect_system_language()
            assert got == want, f"langid={langid:#06x} → {got!r}，期望 {want!r}"
        _K.boom = OSError("no such api")
        assert i18n.detect_system_language() == "en", "异常时必须回落 en（不认识 → 英文）"
        _K.boom = None
    finally:
        if orig is not None:
            i18n.ctypes.windll = orig
    print("  ✓ detect_system_language：zh/en/ja/ko/ru 各归各的；"
          "de/fr 与异常一律 → en（已打桩）")


def test_available_languages_order() -> None:
    """语言列表：五种、顺序固定、名字用各自母语写法（不随界面语言变）。"""
    from vlt.i18n import available_languages

    want = [("zh", "简体中文"), ("en", "English"), ("ja", "日本語"),
            ("ko", "한국어"), ("ru", "Русский")]
    got = available_languages()
    assert got == want, f"语言列表不对：{got!r}"
    print(f"  ✓ 语言列表：{[c for c, _ in got]}（顺序与母语写法都对）")


# ---------------------------------------------------------------- ③ 英文界面无汉字守卫（真 Tk）


def test_english_ui_has_no_cjk() -> int:
    """★ 界面守卫：ui.lang: en 的临时配置起真窗口，递归扫**主窗口 + 设置弹窗**的
    所有控件 text 与窗口标题 —— 一个汉字都不许有（漏翻的直接现形）。返回扫描条数。"""
    saved_env = _isolate_env(Path(tempfile.mkdtemp(prefix="vlt-i18n-env-")))
    gui = None
    try:
        gui = _make_gui(_temp_config("en"))
        from vlt import i18n

        assert i18n.current_language() == "en", \
            f"前提：界面语言应是 en，实际 {i18n.current_language()!r}"

        texts = [gui._root.title(), gui._settings_win.title()]
        _walk_texts(gui._root, texts)
        _walk_texts(gui._settings_win, texts)
        assert len(texts) > 50, f"扫到的文案太少（{len(texts)} 条），守卫形同虚设"
        bad = [x for x in texts if CJK_RE.search(x)]
        assert not bad, (f"英文界面下仍有 {len(bad)} 条文案含汉字（漏翻）：\n"
                         + "\n".join(f"  {x[:80]!r}" for x in bad[:20]))
        print(f"  ✓ 英文界面无汉字：主窗口+设置弹窗共扫 {len(texts)} 条 text，0 条含汉字")
        return len(texts)
    finally:
        _destroy(gui)
        _restore_env(saved_env)
        from vlt import i18n
        i18n.set_language("zh")


# ---------------------------------------------------------------- ③b 各语言界面守卫（真 Tk）

HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
KANA_RE = re.compile(r"[\u3040-\u30ff]")


def test_all_ui_languages_window_guard() -> None:
    """★ 分语言界面守卫：en / ja / ko / ru 各起一次真窗口（主窗口 + 设置弹窗），
    按各语言的书写系统断言：

    - `en` / `ru` / `ko`：不得出现中日韩汉字（`[一-鿿]`）；
    - `en` / `ru` / `ja`：不得出现谚文（`[가-힣]`）；
    - `en` / `ru` / `ko`：不得出现假名（`[぀-ヿ]`）—— 出现说明串了日文词表；
    - `ja`：不做汉字断言（日语本来就用汉字），但**假名比例必须 ≥ 50%** ——
      整片中文没翻译时这个比例会掉到 0，一样能抓住。

    下拉框的「值」单独扫一遍：方向下拉的语言名以前整片是中文（机器守卫只看 text 属性，
    扫不到），现在会翻译，所以必须一起守。界面语言下拉的母语名（简体中文/한국어…）按设计
    永远用母语写法，不参与判断。

    另加一条**固定字符宽**规则：Label/Button 写了 `width=N` 时文案超过 N 字就会被裁 ——
    微调面板曾固定 width=6，英文 "Curvature"→"Curvatu"、俄语 "Позиция X/Y/Z" 全变 "Позиц."。
    """
    from vlt import i18n

    reports: list[str] = []
    for lang in ("en", "ja", "ko", "ru"):
        saved_env = _isolate_env(Path(tempfile.mkdtemp(prefix="vlt-i18n-env-")))
        gui = None
        try:
            gui = _make_gui(_temp_config(lang))
            assert i18n.current_language() == lang, \
                f"前提：界面语言应是 {lang}，实际 {i18n.current_language()!r}"
            texts = [gui._root.title(), gui._settings_win.title()]
            _walk_texts(gui._root, texts)
            _walk_texts(gui._settings_win, texts)
            assert len(texts) > 50, f"{lang}：扫到的文案太少（{len(texts)} 条），守卫形同虚设"

            bad: list[str] = []
            if lang in ("en", "ko", "ru"):
                bad += [f"[汉字] {x!r}" for x in texts if CJK_RE.search(x)]
            if lang in ("en", "ja", "ru"):
                bad += [f"[谚文] {x!r}" for x in texts if HANGUL_RE.search(x)]
            if lang in ("en", "ko", "ru"):
                bad += [f"[假名] {x!r}" for x in texts if KANA_RE.search(x)]
            assert not bad, (f"{lang} 界面出现不该有的书写系统（{len(bad)} 条）：\n"
                             + "\n".join(f"  {b}" for b in bad[:15]))

            if lang == "ja":
                ratio = len([x for x in texts if KANA_RE.search(x)]) / len(texts)
                assert ratio >= 0.5, \
                    f"日文界面含假名的文案只有 {ratio:.0%}（<50%）—— 像是整片没翻译"
                reports.append(f"{lang}:{len(texts)}条(假名{ratio:.0%})")
            else:
                reports.append(f"{lang}:{len(texts)}条")

            # ② 方向下拉的候选项 + 当前选中项（不在 text 属性里，单独扫）
            combo_vals: list[str] = []
            for combo in (gui._source_combo, gui._target_combo, gui._anchor_combo):
                combo_vals += [str(v) for v in combo.cget("values")] + [str(combo.get())]
            bad_vals: list[str] = []
            for x in combo_vals:
                if x not in texts:      # 界面语言下拉的母语名不在其中，这里的都是待翻译项
                    if lang in ("en", "ko", "ru") and CJK_RE.search(x):
                        bad_vals.append(f"[汉字] {x!r}")
                    if lang in ("en", "ja", "ru") and HANGUL_RE.search(x):
                        bad_vals.append(f"[谚文] {x!r}")
                    if lang in ("en", "ko", "ru") and KANA_RE.search(x):
                        bad_vals.append(f"[假名] {x!r}")
            assert not bad_vals, (f"{lang} 下拉选项出现不该有的书写系统（{len(bad_vals)} 条）：\n"
                                  + "\n".join(f"  {b}" for b in bad_vals[:15]))

            # ③ 固定字符宽的控件：文案要的宽度 > 给的宽度就会被裁
            #    ⚠️ 不能只数字符数：Tk 的 `width=N` 单位是**字体平均字符宽**（本机 ≈7px），
            #    而一个汉字/假名 ≈2 倍宽 ⇒ 字符数相等照样裁字。实测「訳文の文字サイズ」
            #    自然宽 100px，按 8 字符只申请到 67px，屏幕上只剩「訳文の文字」。
            #    所以用 `vlt.gui._char_width_for`（字体真实 measure 换算）来判。
            from vlt.gui import FONT_UI, _char_width_for
            narrow: list[str] = []

            def _font_spec_of(w):
                try:
                    return w.cget("font") or FONT_UI
                except Exception:      # ttk 控件的字体在 style 里，取不到就用应用字体
                    return FONT_UI

            def _walk_fixed(win) -> None:
                for w in win.winfo_children():
                    try:
                        keys = w.keys()
                    except Exception:
                        continue
                    if ("text" in keys and "width" in keys
                            and w.winfo_class() in ("TLabel", "TButton", "Label", "Button")):
                        txt = str(w.cget("text"))
                        try:
                            cw = int(w.cget("width") or 0)
                        except Exception:  # noqa: BLE001
                            cw = 0
                        if txt.strip() and cw:
                            need_chars = _char_width_for(max(txt.split("\n"), key=len),
                                                         _font_spec_of(w))
                            if need_chars > cw:
                                narrow.append(f"{txt!r} 需 {need_chars} 字符宽，只给了 {cw}")
                    _walk_fixed(w)

            _walk_fixed(gui._root)
            _walk_fixed(gui._settings_win)
            assert not narrow, (f"{lang} 有 {len(narrow)} 个固定宽度控件会裁字：\n"
                                + "\n".join(f"  {n}" for n in narrow[:15]))

            # ④ 窗口得装得下内容：Tk 在容器不够宽时**从最后打包的控件开始裁**，
            #    俄语第一行要 1251px，写死 940 会把右边两个按钮直接裁掉（实测）。
            #    例外：屏幕本身比内容还窄（CI 的 1024px 屏）—— 那时窗口已顶到屏宽，
            #    装不下是物理限制，不是我们的 bug。
            gui._root.update_idletasks()
            need_w, have_w = gui._root.winfo_reqwidth(), gui._root.winfo_width()
            screen_w = int(gui._root.winfo_screenwidth() or 0)
            fits = need_w <= have_w + 1 or (screen_w and have_w >= screen_w - 32)
            assert fits, (f"{lang}：内容需要 {need_w}px，窗口只有 {have_w}px"
                          f"（屏宽 {screen_w}）—— 右侧会被裁掉")
        finally:
            _destroy(gui)
            _restore_env(saved_env)
            i18n.set_language("zh")
    print("  ✓ 分语言界面守卫：" + "，".join(reports) + " —— 书写系统均正确")


# ---------------------------------------------------------------- ④ 语言下拉写 ui.lang


def test_direction_language_names_translated() -> None:
    """★ 方向下拉（源/目标语言）的语言名必须随界面语言翻译，且选中后能正确还原成语言码。

    历史坑：`SOURCE_LANGS`/`TARGET_LANGS` 是「中文名 → 语言码」的表，下拉直接拿中文 key
    当候选项 —— 英文/日文/俄文界面里方向下拉仍是「中文 → 英语」，外国用户看不懂自己选的是啥。
    而机器守卫原来只看控件的 text 属性，扫不到下拉的「值」，两边都得补上。现在：
    显示走 `_lang_label()`（中文名 → 译名），选中值走 `_lang_key()`（译名 → 中文 key）。
    """
    from vlt import i18n

    # lang: (源下拉当前值=中文, 目标下拉当前值=英语, 源候选里的自动检测, 目标候选里的日语)
    cases = {
        "en": ("Chinese", "English", "Auto-Detect", "Japanese"),
        "ja": ("中国語", "英語", "自動検出", "日本語"),
        "ko": ("중국어", "영어", "자동 감지", "일본어"),
        "ru": ("Китайский", "Английский", "Автоопределение", "Японский"),
    }
    reports: list[str] = []
    for lang, (want_src, want_tgt, want_auto, want_ja) in cases.items():
        saved_env = _isolate_env(Path(tempfile.mkdtemp(prefix="vlt-i18n-env-")))
        gui = None
        try:
            gui = _make_gui(_temp_config(lang))
            assert i18n.current_language() == lang
            got_src, got_tgt = gui._source_combo.get(), gui._target_combo.get()
            assert got_src == want_src, f"{lang}：源语言下拉显示 {got_src!r}，期望 {want_src!r}"
            assert got_tgt == want_tgt, f"{lang}：目标语言下拉显示 {got_tgt!r}，期望 {want_tgt!r}"
            vals = [str(v) for v in gui._source_combo.cget("values")]
            assert want_auto in vals and want_ja in vals, \
                f"{lang}：源语言候选项不对（应含自动检测与日语译名）：{vals!r}"
            assert want_ja in [str(v) for v in gui._target_combo.cget("values")], \
                f"{lang}：目标语言候选项里没有 {want_ja!r}"

            # 反查：选「日语」（译名）→ 语言码必须落回 ja
            gui._target_combo.set(want_ja)
            gui._on_lang_change()
            assert gui._lang_pair["target"] == "ja", \
                f"{lang}：选了 {want_ja!r} 之后语言码应为 ja，实际 {gui._lang_pair['target']!r}"
            # 再选回英语（译名）→ 落回 en，且下拉回填的仍是译名
            gui._target_combo.set(cases[lang][1])
            gui._on_lang_change()
            assert gui._lang_pair["target"] == "en", \
                f"{lang}：选回 {cases[lang][1]!r} 后语言码应为 en，实际 {gui._lang_pair['target']!r}"
            assert gui._target_combo.get() == cases[lang][1], "选完下拉回填的不是译名"
            reports.append(f"{lang}:{got_src}→{got_tgt}")
        finally:
            _destroy(gui)
            _restore_env(saved_env)
            i18n.set_language("zh")
    print("  ✓ 方向下拉语言名已翻译且可反查：" + "，".join(reports))


def test_language_combo_writes_config() -> None:
    """设置弹窗的语言下拉：选中立即写进**临时** config.yaml 的 ui.lang（就地改文本）。"""
    import yaml

    saved_env = _isolate_env(Path(tempfile.mkdtemp(prefix="vlt-i18n-env-")))
    cfg_path = _temp_config("zh")
    gui = None
    try:
        gui = _make_gui(cfg_path)
        # zh → English
        gui._ui_lang_var.set("English")
        gui._on_ui_lang_change()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert (data.get("ui") or {}).get("lang") == "en", \
            f"ui.lang 没写进去：{cfg_path.read_text(encoding='utf-8')!r}"
        # English → 简体中文
        gui._ui_lang_var.set("简体中文")
        gui._on_ui_lang_change()
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        assert (data.get("ui") or {}).get("lang") == "zh", \
            f"ui.lang 没改回来：{data!r}"
        assert gui._cfg.ui.get("lang") == "zh", "内存里的 cfg.ui 也要同步"
        print("  ✓ 语言下拉：English↔简体中文 都正确写进临时 config.yaml 的 ui.lang")
    finally:
        _destroy(gui)
        _restore_env(saved_env)
        from vlt import i18n
        i18n.set_language("zh")


# ---------------------------------------------------------------- ⑤ 打开日志文件夹


def test_open_log_folder_button() -> None:
    """★ 设置弹窗「日志」区有「打开日志文件夹」按钮；处理函数用 os.startfile 打开
    **与导出压缩包同一个目录**（_log_dir）；打不开时不许静默（状态栏 + 日志）。
    os.startfile 全程打桩，绝不真开资源管理器。"""
    import vlt.gui as gui_mod

    saved_env = _isolate_env(Path(tempfile.mkdtemp(prefix="vlt-i18n-env-")))
    gui = None
    had_startfile = hasattr(gui_mod.os, "startfile")
    orig_startfile = getattr(gui_mod.os, "startfile", None)
    try:
        gui = _make_gui(_temp_config("zh"))
        # 按钮存在且在导出按钮旁边（同一个容器）
        assert gui._log_open_btn.cget("text") == "打开日志文件夹", \
            f"按钮文案不对：{gui._log_open_btn.cget('text')!r}"
        assert gui._log_open_btn.master is gui._log_export_btn.master, \
            "「打开日志文件夹」不在日志区"

        opened: list[str] = []
        gui_mod.os.startfile = lambda p: opened.append(p)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._on_open_log_folder()
        assert opened == [str(gui._log_dir())], \
            f"打开的目录不对：{opened!r}（期望 {[str(gui._log_dir())]!r}）"
        assert "已打开日志文件夹" in buf.getvalue(), "成功路径必须留痕"

        # 失败路径：不抛异常、状态栏可见、日志留痕（禁静默）
        def _boom(_p: str) -> None:
            raise OSError("denied-for-test")

        gui_mod.os.startfile = _boom
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gui._on_open_log_folder()              # 绝不许抛
        assert "打不开日志文件夹" in str(gui._status_label.cget("text")), \
            f"失败时状态栏没提示：{gui._status_label.cget('text')!r}"
        assert "打不开日志文件夹" in buf.getvalue(), "失败路径必须留痕"
        print(f"  ✓ 打开日志文件夹：startfile 收到正确目录（{opened[0]}）；"
              f"失败路径状态栏+日志双留痕（已打桩，未真开资源管理器）")
    finally:
        if had_startfile:
            gui_mod.os.startfile = orig_startfile
        elif hasattr(gui_mod.os, "startfile"):
            delattr(gui_mod.os, "startfile")
        _destroy(gui)
        _restore_env(saved_env)


# ---------------------------------------------------------------- 入口


def main() -> int:
    tests = [
        test_catalog_completeness,
        test_format_and_fallback,
        test_normalize_language,
        test_detect_system_language_stubbed,
        test_available_languages_order,
        test_english_ui_has_no_cjk,
        test_all_ui_languages_window_guard,
        test_direction_language_names_translated,
        test_language_combo_writes_config,
        test_open_log_folder_button,
    ]
    print("test_i18n:")
    failed = 0
    for fn in tests:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ❌ {fn.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failed:
        print(f"❌ {failed} 个用例失败")
        return 1
    print("ALL PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
