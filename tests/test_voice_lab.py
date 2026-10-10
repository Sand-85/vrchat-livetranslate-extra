#!/usr/bin/env python
"""声音设计客户端 `vlt/voice_lab.py` 的离线验收（假 HTTP，零花费、不碰网络）。

重点钉住三件事：
  ① **不重复花钱**：同名音色已存在时 `create_or_reuse()` 只查不建（用户反复点也不会反复计费）；
  ② **地址跟着线路走**：自定义音色接口从 `session.base_url` 的 host 派生（不写第二份域名常量）；
  ③ **描述与命名口径**：名字按官方规则规范化（字母数字下划线、≤16），描述只折叠空白不管内容，
     但会**提示**疑似「要求模仿特定人物」的写法（官方明确不支持）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vlt import voice_lab as vl                                    # noqa: E402


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """按脚本返回响应，并把每个请求的 (url, payload) 记下来供断言。"""

    def __init__(self, script: list[dict] | None = None) -> None:
        self.script = list(script or [])
        self.sent: list[tuple[str, dict]] = []

    def open(self, req, timeout=None):                             # noqa: ANN001
        payload = json.loads(req.data.decode("utf-8"))
        self.sent.append((req.full_url, payload))
        obj = self.script.pop(0) if self.script else {"output": {}}
        if isinstance(obj, Exception):
            raise obj
        return _Resp(obj)

    def actions(self) -> list[str]:
        return [p["input"]["action"] for _u, p in self.sent]


def _voice_payload(voice: str, preview: bytes = b"RIFFfake") -> dict:
    return {"output": {"voice": voice,
                       "preview_audio": {"data": base64.b64encode(preview).decode()}}}


# ---------------------------------------------------------------- 纯函数


def test_normalize_name() -> bool:
    ok = True
    cases = [
        ("clear_auto", "clear_auto", "合法名原样"),
        ("My Voice", "My_Voice", "空格 → 下划线"),
        ("声音一号", "my_voice", "纯中文 → 回落默认名"),
        ("a" * 30, "a" * 16, "超长截断到 16"),
        ("__x__", "x", "首尾下划线去掉"),
        ("", "my_voice", "空 → 默认名"),
        ("ok-name", "ok_name", "短横线 → 下划线"),
    ]
    for raw, want, why in cases:
        got = vl.normalize_name(raw)
        cond = got == want
        print(f"  normalize_name({raw[:14]!r}) = {got!r}（期望 {want!r}）{'OK' if cond else '✗'}  {why}")
        ok &= cond
    return ok


def test_normalize_prompt_and_hints() -> bool:
    ok = True
    got = vl.normalize_prompt("  年轻女性，\n\n 音色干净偏薄  ")
    cond = got == "年轻女性， 音色干净偏薄"
    print(f"  折叠空白与换行：{got!r}  {'OK' if cond else '✗'}")
    ok &= cond
    try:
        vl.normalize_prompt("   ")
        print("  空描述应当抛错 ✗")
        ok = False
    except vl.VoiceLabError:
        print("  空描述 → VoiceLabError  OK")
    for text, want in (("模仿某声优的声音", True), ("像新闻播报员一样平稳", False),
                       ("复刻某明星", True), ("年轻女性，语速偏慢", False)):
        got2 = vl.prompt_looks_like_imitation(text)
        cond = got2 is want
        print(f"  模仿提示({text[:12]}…) = {got2}（期望 {want}）{'OK' if cond else '✗'}")
        ok &= cond
    return ok


def test_name_from_id_and_find() -> bool:
    ok = True
    vid = "qwen-tts-vd-clear_auto-voice-20260926233229068-247d"
    cond = vl._name_of(vid) == "clear_auto"
    print(f"  从 id 反推名字：{vl._name_of(vid)!r}  {'OK' if cond else '✗'}")
    ok &= cond
    voices = [vl.VoiceInfo(voice="qwen-tts-vd-mix_a-voice-20260926234434731-e0fd", name="mix_a"),
              vl.VoiceInfo(voice="qwen-tts-vd-mix_a-voice-20261001010101-aaaa", name="mix_a"),
              vl.VoiceInfo(voice="qwen-tts-vd-other-voice-20260926234434731-bbbb", name="other")]
    hit = vl.find_by_name(voices, "mix_a")
    cond = hit is not None and hit.voice.endswith("-aaaa")
    print(f"  同名多条取最新：{None if hit is None else hit.voice[-5:]}  {'OK' if cond else '✗'}")
    ok &= cond
    cond = vl.find_by_name(voices, "nope") is None
    print(f"  找不到 → None  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_url_derivation() -> bool:
    ok = True
    q = vl.customization_url("wss://maas.qianwenaiapi.com/api-ws/v1/realtime")
    cond = q == "https://maas.qianwenaiapi.com/api/v1/services/audio/tts/customization"
    print(f"  千问云：{q}  {'OK' if cond else '✗'}")
    ok &= cond
    b = vl.customization_url("wss://maas.qwencloudapi.com/api-ws/v1/realtime")
    cond = b == "https://maas.qwencloudapi.com/api/v1/services/audio/tts/customization"
    print(f"  海外版（qwencloud）：{b}  {'OK' if cond else '✗'}")
    ok &= cond
    c = vl.customization_url("wss://llm-abc.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/realtime")
    cond = c == "https://llm-abc.ap-southeast-1.maas.aliyuncs.com/api/v1/services/audio/tts/customization"
    print(f"  旧国际版地址（上游已下线，仍能派生）：{c}  {'OK' if cond else '✗'}")
    ok &= cond
    bad = vl.customization_url("没有 scheme 的地址")
    cond = bad.startswith("https://maas.qianwenaiapi.com/")
    print(f"  解析不出 host → 回落千问云：{bad}  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


# ---------------------------------------------------------------- 请求形态与解析


def test_create_request_shape() -> bool:
    ok = True
    op = FakeOpener([_voice_payload("qwen-tts-vd-demo-voice-20261002120000-abcd", b"RIFFxy")])
    res = vl.create_voice("demo", "  年轻女性，语速偏慢  ", api_key="sk-test",
                          base_url="wss://maas.qianwenaiapi.com/x", opener=op)
    url, payload = op.sent[0]
    inp = payload["input"]
    cond = (payload["model"] == vl.DESIGN_MODEL and inp["action"] == "create"
            and inp["target_model"] == vl.DEFAULT_TARGET_MODEL
            and inp["preferred_name"] == "demo"
            and inp["voice_prompt"] == "年轻女性，语速偏慢"
            and inp["preview_text"] == vl.TEST_TEXT)
    print(f"  请求体（model={payload['model']} / action={inp['action']} / "
          f"名字={inp['preferred_name']}）  {'OK' if cond else '✗'}")
    ok &= cond
    cond = (res.voice.endswith("-abcd") and res.preview_wav == b"RIFFxy" and not res.reused)
    print(f"  解析返回值：voice 尾={res.voice[-5:]} 预览={len(res.preview_wav)}B  "
          f"reused={res.reused}  {'OK' if cond else '✗'}")
    ok &= cond
    cond = "customization" in url
    print(f"  URL 走派生：{url}  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_list_and_error() -> bool:
    ok = True
    design_rows = {"output": {"voice_list": [
        {"voice": "qwen-tts-vd-a-voice-20260901010101-1111", "preferred_name": "a",
         "voice_prompt": "描述A", "target_model": vl.DEFAULT_TARGET_MODEL,
         "gmt_create": "2026-09-01 01:01:01"},
        {"voice": ""},                       # 脏数据：跳过
        "不是字典",                           # 脏数据：跳过
    ]}}
    clone_rows = {"output": {"voice_list": [
        {"voice": "qwen-tts-vc-MetroPolice-voice-20261003201838-0a4b",
         "target_model": vl.CLONE_TARGET_MODEL, "gmt_create": "2026-10-03 20:18:40",
         "status": "OK"},
    ]}}
    op = FakeOpener([design_rows, clone_rows])
    rows = vl.list_voices(api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x", opener=op)
    cond = (len(rows) == 2 and rows[0].name == "a" and rows[0].prompt == "描述A"
            and rows[0].kind == "design"
            and rows[1].kind == "clone" and rows[1].status == "OK"
            and rows[1].target_model == vl.CLONE_TARGET_MODEL)
    print(f"  两族都列（脏数据跳过）：设计 {rows[0].kind} / 复刻 {rows[1].kind}"
          f"（{vl._name_of(rows[1].voice)}）  {'OK' if cond else '✗'}")
    ok &= cond
    cond = op.actions() == ["list", "list"]
    print(f"  两族各发一次 list：action={op.actions()}  {'OK' if cond else '✗'}")
    ok &= cond
    cond = "qwen-voice-enrollment" in json.dumps(op.sent[1][1]) and \
           "qwen-voice-design" in json.dumps(op.sent[0][1])
    print(f"  第二发的 model 是 enrollment 那族  {'OK' if cond else '✗'}")
    ok &= cond

    # 只查一族时不该多发请求（省钱：试听前只需确认复刻族里有没有同名的）
    op_f = FakeOpener([clone_rows])
    only = vl.list_voices(api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x",
                          family="clone", opener=op_f)
    cond = len(only) == 1 and op_f.actions() == ["list"]
    print(f"  family='clone' 只发一次且只回复刻族：{len(only)} 条  {'OK' if cond else '✗'}")
    ok &= cond

    # 2026-10-09 实测场景：某线路把「声音设计」模型下架 → design 族 404 Model not exist。
    # 单族不可用**不能**把整表搞空（原先会 —— 界面看起来像「音色全没了」，而克隆族其实好好的）。
    err404 = {"code": "InvalidParameter", "message": "Model not exist."}
    op_nf = FakeOpener([err404, clone_rows])
    warn: list[tuple[str, str]] = []
    rows_nf = vl.list_voices(api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x",
                             opener=op_nf, warn=warn)
    cond = (len(rows_nf) == 1 and rows_nf[0].kind == "clone" and len(warn) == 1
            and warn[0][0] == "design" and "Model not exist" in warn[0][1])
    print(f"  design 族 404 时仍列出 clone 族：{len(rows_nf)} 条 / warn {len(warn)} 条  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    # 两族都挂时必须抛错（别把「全挂了」伪装成「没有音色」）
    op_both = FakeOpener([err404, dict(err404)])
    try:
        vl.list_voices(api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x", opener=op_both)
        print("  两族都失败时应当抛错 ✗")
        ok = False
    except vl.VoiceLabError as exc:
        cond = "Model not exist" in str(exc)
        print(f"  两族都失败时抛错（不伪装成空表）  {'OK' if cond else '✗'}")
        ok &= cond

    op2 = FakeOpener([{"code": "InvalidApiKey", "message": "密钥无效 sk-abcdef"}])
    try:
        vl.list_voices(api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x", opener=op2)
        print("  服务端错误应当抛错 ✗")
        ok = False
    except vl.VoiceLabError as exc:
        msg = str(exc)
        cond = "密钥无效" in msg and "sk-test" not in msg
        print(f"  服务端错误 → VoiceLabError（且不回显我们的 key）：{msg[:40]}  "
              f"{'OK' if cond else '✗'}")
        ok &= cond
    return ok


def test_reuse_avoids_spending() -> bool:
    """核心：同名已存在 → **只 list、不 create**（用户反复点不重复计费）。"""
    ok = True
    same = "qwen-tts-vd-clear_auto-voice-20260926233229068-247d"
    op = FakeOpener([{"output": {"voice_list": [
        {"voice": same, "preferred_name": "clear_auto",
         "target_model": vl.DEFAULT_TARGET_MODEL}]}}])
    res = vl.create_or_reuse("clear_auto", "随便写点描述", api_key="sk-test",
                             base_url="wss://maas.qianwenaiapi.com/x", opener=op)
    cond = res.reused and res.voice == same and op.actions() == ["list"]
    print(f"  同名已存在：action 序列={op.actions()} reused={res.reused}  {'OK' if cond else '✗'}")
    ok &= cond

    op2 = FakeOpener([{"output": {"voice_list": []}},
                      _voice_payload("qwen-tts-vd-new-voice-20261002120000-ffff")])
    res2 = vl.create_or_reuse("new", "年轻女性，语速偏慢", api_key="sk-test",
                              base_url="wss://maas.qianwenaiapi.com/x", opener=op2)
    cond = (not res2.reused) and op2.actions() == ["list", "create"]
    print(f"  没有同名：action 序列={op2.actions()} reused={res2.reused}  {'OK' if cond else '✗'}")
    ok &= cond

    # 查询失败（网络）不阻断创建：宁可能建出来，也别因为列表接口抽风就干不了活
    class _Boom(FakeOpener):
        def open(self, req, timeout=None):                          # noqa: ANN001
            payload = json.loads(req.data.decode("utf-8"))
            self.sent.append((req.full_url, payload))
            if payload["input"]["action"] == "list":
                raise vl.VoiceLabError("网络不可达：模拟")
            return _Resp(_voice_payload("qwen-tts-vd-x-voice-20261002120000-1234"))

    op3 = _Boom()
    res3 = vl.create_or_reuse("x", "描述", api_key="sk-test",
                              base_url="wss://maas.qianwenaiapi.com/x", opener=op3)
    cond = (not res3.reused) and op3.actions() == ["list", "create"]
    print(f"  查询失败仍能建：action 序列={op3.actions()}  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_preview_cache() -> bool:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        voice = "qwen-tts-vd-cache-voice-20261002120000-abcd"
        cond = vl.load_preview(base, voice) is None
        print(f"  没缓存时读回 None  {'OK' if cond else '✗'}")
        ok &= cond
        p = vl.save_preview(base, voice, b"RIFFwav-data")
        cond = p is not None and p.exists() and vl.load_preview(base, voice) == b"RIFFwav-data"
        print(f"  落盘 → 读回一致：{None if p is None else p.name}  {'OK' if cond else '✗'}")
        ok &= cond
        cond = vl.save_preview(base, voice, b"") is None
        print(f"  空音频不落盘  {'OK' if cond else '✗'}")
        ok &= cond
        p2 = vl.save_preview(base, "../../坏/名字", b"RIFF")
        cond = p2 is not None and ".." not in p2.name and "/" not in p2.name
        print(f"  非法 id 的文件名被清洗：{None if p2 is None else p2.name}  {'OK' if cond else '✗'}")
        ok &= cond
    return ok


def test_preview_pcm() -> bool:
    """★ 试听音频统一解码：**裸 PCM 原样放行、WAV 容器解成 24k 单声道**。

    为什么单列：本地 `voices/*.wav` 缓存里其实混着两种格式 —— `create_or_reuse` 存的是
    服务端回的 **WAV 容器**，`sample_pcm` 存的是 `tts.synthesize` 的**裸 24k PCM**。
    以前两者都被塞进只认容器的解码器（`tts._decode_to_24k_mono`）→ 裸 PCM 必解码失败，
    真机表现就是「点试听一点声音都没有」。这个用例把两种格式都钉住。
    """
    import io
    import wave

    ok = True
    cond = vl.preview_pcm(b"") == b""
    print(f"  空输入 → 空输出  {'OK' if cond else '✗'}")
    ok &= cond

    raw = b"\x01\x00\x02\x00" * 480                    # 裸 24k 单声道 20ms
    cond = vl.preview_pcm(raw) == raw
    print(f"  裸 PCM → 原样放行（{len(raw)}B）  {'OK' if cond else '✗'}")
    ok &= cond

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\x00\x00" * 240)
    dec = vl.preview_pcm(buf.getvalue())
    cond = len(dec) == 240 * 2
    print(f"  WAV 容器（24k 单声道 10ms）→ 解出 {len(dec)}B / 期望 {240 * 2}B  {'OK' if cond else '✗'}")
    ok &= cond

    # 非 24k 的容器（44.1k 立体声 100ms）也要被**重采样**成 24k 单声道
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"\x00\x00\x00\x00" * 4410)
    dec = vl.preview_pcm(buf.getvalue())
    cond = abs(len(dec) - 2400 * 2) <= 8
    print(f"  WAV 容器（44.1k 立体声 100ms）→ 重采样后 {len(dec)}B / 期望约 {2400 * 2}B  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_recipes() -> bool:
    ok = True
    cond = len(vl.RECIPES) == 5
    print(f"  配方条数 = {len(vl.RECIPES)}（期望 5 = 用户 2026-10-03 试听后保留的 1·2·4·6·8）"
          f"  {'OK' if cond else '✗'}")
    ok &= cond
    labels = vl.recipe_labels()
    rt_ok = all(vl.recipe_from_label(lbl) is r for lbl, r in zip(labels, vl.RECIPES))
    print(f"  下拉文本 ↔ 配方往返一致（{labels[0]} …）  {'OK' if rt_ok else '✗'}")
    ok &= rt_ok
    cond = vl.recipe_by_key("nope") is None and vl.recipe_from_label("乱写") is None
    print(f"  未知 key/文本 → None  {'OK' if cond else '✗'}")
    ok &= cond
    bad = [r.key for r in vl.RECIPES
           if not r.prompt.strip() or len(r.prompt) > vl.PROMPT_MAX_LEN
           or not r.label.strip()]
    cond = not bad
    print(f"  每条配方都非空且不超长{'（问题项：' + str(bad) + '）' if bad else ''}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    cond = all(vl.normalize_name(r.key) == r.key for r in vl.RECIPES)
    print(f"  配方 key 都能当音色名（合法字符）  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_describe_rows() -> bool:
    rows = vl.describe([vl.VoiceInfo(voice="qwen-tts-vd-mix_a-voice-20260926234434731-e0fd",
                                     name="mix_a", created="2026-09-26 23:44:34")])
    cond = len(rows) == 1 and "mix_a" in rows[0] and "e0fd" in rows[0] and "2026-09-26" in rows[0]
    print(f"  展示行：{rows[0]}  {'OK' if cond else '✗'}")
    return cond


def test_sample_pcm() -> bool:
    """试听已有音色：测试文本固定 + 参数透传 + 失败给人话（不吞异常）。

    这条路的由来：官方只在**创建**时回 `preview_audio`，账号里原有的音色没有预览音频 →
    想试听只能自己合成一句。要钉住的是「合成谁、用哪句、哪个模型、失败了说什么」。
    """
    ok = True
    cond = vl.TEST_TEXT == "こんにちは、私はSANDです。今から声のテストです。"
    print(f"  测试文本 = {vl.TEST_TEXT!r}（{len(vl.TEST_TEXT)} 字）{'OK' if cond else '✗'}")
    ok &= cond

    calls: list[dict] = []
    orig = vl.tts.synthesize
    try:
        def fake_synth(text, *, voice="", model="", api_key="", endpoint=None, timeout=None, **kw):
            calls.append({"text": text, "voice": voice, "model": model, "endpoint": endpoint})
            return b"\x00\x01" * 64

        vl.tts.synthesize = fake_synth
        pcm = vl.sample_pcm("qwen-tts-vd-x-voice-1", "qwen3-tts-vd-2026-01-26",
                            api_key="sk-t", endpoint="https://ep.example/api/v1")
        c = calls[-1]
        cond = (pcm == b"\x00\x01" * 64 and c["text"] == vl.TEST_TEXT
                and c["voice"] == "qwen-tts-vd-x-voice-1"
                and c["model"] == "qwen3-tts-vd-2026-01-26"
                and c["endpoint"] == "https://ep.example/api/v1")
        print(f"  参数透传（句/音色/模型/端点）{'OK' if cond else '✗ ' + repr(c)}")
        ok &= cond

        # 没给模型 → 回落默认 TTS 模型（列表接口没带 target_model 时不至于直接崩）
        vl.sample_pcm("v1", "", api_key="sk-t")
        cond = calls[-1]["model"] == vl.tts.DEFAULT_MODEL
        print(f"  模型缺省回落 = {calls[-1]['model']}  {'OK' if cond else '✗'}")
        ok &= cond

        # 空音色 → 直接拒（别发一个必然失败的请求）
        try:
            vl.sample_pcm("", "m", api_key="sk-t")
            print("  空音色没被拒 ✗")
            ok = False
        except vl.VoiceLabError:
            print("  空音色被拒 OK")

        # 底层异常 → 包成 VoiceLabError（界面只显示人话，不吐栈）
        def boom(*a, **kw):
            raise RuntimeError("connection reset")

        vl.tts.synthesize = boom
        try:
            vl.sample_pcm("v1", "m", api_key="sk-t")
            print("  合成异常没被包装 ✗")
            ok = False
        except vl.VoiceLabError as exc:
            cond = "connection reset" in str(exc)
            print(f"  合成异常包装成人话 OK（{exc}）")
            ok &= cond
    finally:
        vl.tts.synthesize = orig
    return ok


def _write_wav(path, seconds: float, rate: int = 24000, channels: int = 1):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds) * channels)
    return path


def test_clone_payload_and_guard() -> bool:
    """复刻创建：字段名/形态与**真机实测**一致；裸 base64 本地就拦（省一次往返）。"""
    ok = True
    op = FakeOpener([{"output": {"voice": "qwen-tts-vc-my_clone-voice-20261004000000-abcd"}}])
    res = vl.enroll_voice("my copy!", "data:audio/wav;base64,QUJD",
                          api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x", opener=op)
    payload = op.sent[0][1]
    inp = payload["input"]
    cond = (payload["model"] == vl.CLONE_MODEL                      # qwen-voice-enrollment
            and inp["action"] == "create"
            and inp["audio"]["data"].startswith("data:audio/wav;base64,")   # 真机要 data URL
            and inp["preferred_name"] == "my_copy"                  # 名字被规范化
            and inp["target_model"] == vl.CLONE_TARGET_MODEL
            and res.voice.endswith("abcd") and res.reused is False)
    print(f"  请求体：model={payload['model']} audio.data 前缀={inp['audio']['data'][:22]}… "
          f"name={inp['preferred_name']}  {'OK' if cond else '✗'}")
    ok &= cond

    for bad in ("QUJD", "", "ftp://x/y.wav"):
        try:
            vl.enroll_voice("x", bad, api_key="sk-test", opener=FakeOpener([]))
            print(f"  非法素材形态没被拦下：{bad!r} ✗")
            ok = False
        except vl.VoiceLabError:
            pass
    print("  裸 base64 / 空 / ftp 一律本地拦下 OK")

    # 同族复用：账号里已有同名复刻音色 → 只 list、不 create（别为同一份素材反复付 0.01 元）
    existing = {"output": {"voice_list": [
        {"voice": "qwen-tts-vc-vc_dup-voice-20261004000000-1111", "target_model": vl.CLONE_TARGET_MODEL}]}}
    op2 = FakeOpener([existing])
    dup = vl.enroll_or_reuse("vc_dup", "data:audio/wav;base64,QUJD",
                             api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x", opener=op2)
    cond = dup.reused and op2.actions() == ["list"]
    print(f"  同名复用：请求 {op2.actions()}（只查不建）、reused={dup.reused}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_audio_probe_and_data_url() -> bool:
    """素材本地校验：时长/采样率/体积能拦的拦；data URL 必须带 `data:` 前缀。"""
    import tempfile
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="vlt-clone-"))
    cases = [
        ("短 5s", _write_wav(tmp / "s.wav", 5), True),
        ("合格 12s", _write_wav(tmp / "ok.wav", 12), False),
        ("过长 25s", _write_wav(tmp / "l.wav", 25), True),
        ("低采样率 8k", _write_wav(tmp / "r.wav", 12, rate=8000), True),
    ]
    for label, path, should_fail in cases:
        probe = vl.probe_audio(path)
        bad = vl.audio_problems(probe)
        cond = bool(bad) == should_fail
        print(f"  {label}：{probe.seconds:.1f}s / {probe.sample_rate}Hz → "
              f"{'拦下：' + '；'.join(bad) if bad else '放行'}  {'OK' if cond else '✗'}")
        ok &= cond

    stereo = vl.probe_audio(_write_wav(tmp / "st.wav", 12, channels=2))
    warn = vl.audio_warnings(stereo)
    cond = bool(warn) and not vl.audio_problems(stereo)      # 双声道只提示、不拦
    print(f"  双声道：提示 {warn}、不拦  {'OK' if cond else '✗'}")
    ok &= cond

    miss = vl.probe_audio(tmp / "nope.wav")
    cond = "不存在" in "；".join(vl.audio_problems(miss))
    print(f"  文件不存在：{miss.error}  {'OK' if cond else '✗'}")
    ok &= cond

    url = vl.audio_data_url(tmp / "ok.wav")
    import base64 as _b64
    payload = _b64.b64decode(url.split(",", 1)[1])
    cond = (url.startswith("data:audio/wav;base64,") and payload == (tmp / "ok.wav").read_bytes()
            and vl.mime_of("a.MP3") == "audio/mpeg" and vl.mime_of("a.xyz") == "audio/wav")
    print(f"  data URL：前缀正确、base64 往返逐字节一致（{len(payload)}B）；MIME 大写扩展名也认  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_labels_registry() -> bool:
    """克隆后自动记名字：本地登记表 + 按 `-voice-` 前缀认亲 + 坏文件不炸。"""
    import tempfile
    ok = True
    app = Path(tempfile.mkdtemp(prefix="vlt-labels-"))
    vid = "qwen-tts-vc-MetroPolice-voice-20261003201838644-0a4b"
    cond = vl.load_labels(app) == {} and vl.save_label(app, vid, "国民护卫队") is not None
    labels = vl.load_labels(app)
    cond = cond and labels.get(vid) == "国民护卫队"
    print(f"  存/读：{labels}  {'OK' if cond else '✗'}")
    ok &= cond

    lookup = vl.label_mapper(labels)
    cond = (lookup(vid, "fallback") == "国民护卫队"
            and lookup("qwen-tts-vc-MetroPolice-voice-20270101000000000-ffff", "fb") == "国民护卫队"
            and lookup("qwen-tts-vd-clear_auto-voice-x", "fb") == "fb"
            and lookup("", "fb") == "fb")
    print("  前缀认亲（时间戳/后缀变了仍认得）+ 未登记回落  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    (vl.labels_path(app)).write_text("{ 这不是 json", encoding="utf-8")
    cond = vl.load_labels(app) == {}
    print(f"  登记表损坏 → 当空表（不炸）  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_clone_presets() -> bool:
    """克隆预设（拼接范本）：内置那条在、本地能覆盖、样本能定位、导出成 txt。"""
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="vlt-presets-"))
    try:
        presets = vl.all_clone_presets(tmp)
        builtin = next((p for p in presets if p.key == "my_clip_4x"), None)
        cond = (builtin is not None and builtin.label == "MetroPolice"
                and "19.7" in builtin.spec and "standardloyaltycheck" in builtin.spec)
        print(f"  内置范本在（{builtin.label if builtin else '无'}）且含条名/合计  {'OK' if cond else '✗'}")
        ok &= cond

        # 本地覆盖：改显示名 + 指一份真样本
        sample = tmp / "my_sample.wav"
        _write_wav(sample, 12.0)
        vl.save_clone_preset(tmp, "my_clip_4x", label="我的范本（改名后）", sample_path=str(sample))
        again = next(p for p in vl.all_clone_presets(tmp) if p.key == "my_clip_4x")
        cond = again.label == "我的范本（改名后）" and "19.7" in again.spec
        print(f"  本地覆盖显示名生效、范本正文仍在  {'OK' if cond else '✗'}")
        ok &= cond
        got = vl.find_preset_sample(tmp, again)
        cond = got is not None and got.name == "my_sample.wav"
        print(f"  按范本定位样本 → {got}  {'OK' if cond else '✗'}")
        ok &= cond

        # 找不到样本（自定义预设、没名字也没显式路径）→ None，不抛
        bare = vl.ClonePreset(key="bare", label="光杆", spec="什么都没有")
        cond = vl.find_preset_sample(tmp, bare) is None
        print(f"  没样本时返回 None（不抛）  {'OK' if cond else '✗'}")
        ok &= cond

        # 本地试听：任意采样率都能解成 24k 单声道（用户样本就是 44.1kHz）
        pcm, seconds = vl.sample_pcm_from_file(sample)
        cond = len(pcm) > 0 and abs(seconds - 12.0) < 0.05
        print(f"  试听解码：12s 样本 → {len(pcm)} 字节 PCM / {seconds:.2f}s  {'OK' if cond else '✗'}")
        ok &= cond
        up = tmp / "up44.wav"
        _write_wav(up, 3.0, rate=44100)
        pcm44, sec44 = vl.sample_pcm_from_file(up)
        cond = len(pcm44) > 0 and abs(sec44 - 3.0) < 0.05
        print(f"  44.1kHz 也能解（重采样到 24k）→ {sec44:.2f}s  {'OK' if cond else '✗'}")
        ok &= cond
        broken = tmp / "broken.wav"
        broken.write_bytes(b"this is not audio")
        try:
            vl.sample_pcm_from_file(broken)
            cond = False
        except Exception as exc:                                     # noqa: BLE001
            cond = True
            print(f"  坏文件 → 抛出（界面捕获取状态）：{type(exc).__name__}  "
                  f"{'OK' if cond else '✗'}")
        ok &= cond
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_preset_fetch() -> bool:
    """范本音频按需拉取：地址顺序 / sha 校验 / 拉过就不再拉 / 全挂时报清楚。"""
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="vlt-fetch-"))
    try:
        raw = (Path(__file__).resolve().parents[1] / "vo_sample"
               / "source_sample_v2.wav").read_bytes()
        custom = vl.ClonePreset(key="t_fetch", label="测试范本", spec="",
                                sample_name="t_fetch_sample.wav",
                                sample_sha256=hashlib.sha256(raw).hexdigest())
        cond = vl.find_preset_sample(tmp, custom) is None
        print(f"  自定义样本名在本地不存在（触发下载）  {'OK' if cond else '✗'}")
        ok &= cond

        calls: list[str] = []

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self._d = data

            def read(self, n: int = -1) -> bytes:
                return self._d[:n] if (n and n > 0) else self._d

            def __enter__(self):
                return self

            def __exit__(self, *_a) -> bool:
                return False

        file_calls: list[str] = []

        def opener(req, timeout=None):                               # noqa: ANN001
            url = str(getattr(req, "full_url", req))
            calls.append(url)
            if url.endswith("manifest.json"):        # 取清单是「先看一眼有没有新版本」
                return _Resp(b'{"samples": {}}')
            file_calls.append(url)
            return _Resp(raw)

        got = vl.fetch_preset_sample(tmp, custom, opener=opener)
        cond = (got.is_file() and got.read_bytes() == raw and len(file_calls) == 1
                and file_calls[0].endswith("vo_sample/t_fetch_sample.wav")
                and "jsdelivr" in file_calls[0])       # 快的镜像排在最前（raw 兜底且国内必超时）
        print(f"  首次拉取 → {got.name}（{got.stat().st_size} 字节）、文件只请求首个（镜像）地址  "
              f"{'OK' if cond else '✗'}")
        ok &= cond

        calls.clear(); file_calls.clear()
        got2 = vl.fetch_preset_sample(tmp, custom, opener=opener)
        cond = got2 == got and calls == []
        print(f"  第二次 → 命中本地缓存、0 次请求  {'OK' if cond else '✗'}")
        ok &= cond

        got.unlink()
        calls.clear(); file_calls.clear()

        def tampered(req, timeout=None):                             # noqa: ANN001
            url = str(getattr(req, "full_url", req))
            if url.endswith("manifest.json"):
                return _Resp(b'{"samples": {}}')
            file_calls.append(url)
            return _Resp(b"tampered not audio")

        try:
            vl.fetch_preset_sample(tmp, custom, opener=tampered)
            cond = False
        except vl.VoiceLabError as exc:
            want_calls = len(vl.preset_sample_urls(custom)) * vl.PRESET_FETCH_ATTEMPTS
            cond = ("sha256" in str(exc) and len(file_calls) == want_calls
                    and not vl.preset_cache_path(tmp, custom).exists())
            print(f"  sha 不符 → 拒绝、每个地址都试过（含重试 {len(file_calls)} 次请求）、不落盘  "
                  f"{'OK' if cond else '✗'}")
        ok &= cond

        def dead(req, timeout=None):                                 # noqa: ANN001
            raise OSError("网络不通")

        try:
            vl.fetch_preset_sample(tmp, custom, opener=dead)
            cond = False
        except vl.VoiceLabError as exc:
            cond = (all(lbl in str(exc) or base in str(exc)
                        for lbl, base in vl.PRESET_MIRRORS)
                    and "vo_sample/t_fetch_sample.wav" in str(exc))
            print(f"  地址全挂 → 报错里每个镜像地址都点名  {'OK' if cond else '✗'}")
        ok &= cond

        empty = vl.ClonePreset(key="t_empty", label="空范本", spec="")
        try:
            vl.fetch_preset_sample(tmp, empty, opener=opener)
            cond = False
        except vl.VoiceLabError as exc:
            cond = "没写样本文件名" in str(exc)
            print(f"  没写样本文件名 → 明确报错  {'OK' if cond else '✗'}")
        ok &= cond
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


def test_sample_update_check() -> bool:
    """启动查更新（清单驱动）：本地已最新 → 不下载；sha 不一致 → 拉到缓存；
    清单取不到 → 抛（调用方记日志）；清单结构不对 → 抛。"""
    ok = True
    tmp = Path(tempfile.mkdtemp(prefix="vlt-sampleupd-"))
    try:
        raw = (Path(__file__).resolve().parents[1] / "vo_sample"
               / "source_sample_v2.wav").read_bytes()
        good = hashlib.sha256(raw).hexdigest()
        manifest = {"版本": 1, "samples": {"source_sample_v2.wav": {"sha256": good,
                                                                   "bytes": len(raw)}}}

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self._d = data

            def read(self, n: int = -1) -> bytes:
                return self._d[:n] if (n and n > 0) else self._d

            def __enter__(self):
                return self

            def __exit__(self, *_a) -> bool:
                return False

        hits: list[str] = []

        def opener(req, timeout=None):                               # noqa: ANN001
            url = str(getattr(req, "full_url", req))
            hits.append(url)
            if url.endswith("manifest.json"):
                return _Resp(json.dumps(manifest).encode("utf-8"))
            return _Resp(raw)

        # ① 跑源码：仓库里那份就是最新的 → 不下载
        hits.clear()
        changed = vl.check_sample_updates(tmp, opener=opener)
        cond = changed == [] and all(u.endswith("manifest.json") for u in hits)
        print(f"  本地已最新 → 不下载（只请求清单 {len(hits)} 次）  {'OK' if cond else '✗'}")
        ok &= cond

        # ② 清单里换了 sha（远端更新）→ 拉到本地缓存
        other = vl.ClonePreset(key="my_clip_4x", label="MetroPolice",
                               spec=vl.BUILTIN_CLONE_PRESETS[0].spec,
                               sample_name="source_sample_v2.wav", sample_sha256="deadbeef")
        # 用「缓存里放一份旧内容」模拟本地过期
        cache = vl.preset_cache_path(tmp, other)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(b"old content")
        hits.clear()
        changed = vl.check_sample_updates(tmp, [other], opener=opener)
        cond = (changed == ["source_sample_v2.wav"] and cache.read_bytes() == raw
                and any(u.endswith("source_sample_v2.wav") for u in hits))
        print(f"  本地过期 → 自动拉到缓存并覆盖（{changed}）  {'OK' if cond else '✗'}")
        ok &= cond

        # ③ 拉下来再查一次 → 已经最新，不再下载
        hits.clear()
        changed = vl.check_sample_updates(tmp, [other], opener=opener)
        cond = changed == [] and all(u.endswith("manifest.json") for u in hits)
        print(f"  再查一次 → 不再下载（请求 {len(hits)} 次）  {'OK' if cond else '✗'}")
        ok &= cond

        # ④ 清单取不到 → 抛（调用方记日志、不影响功能）
        def dead(req, timeout=None):                                 # noqa: ANN001
            raise OSError("网络不通")

        try:
            vl.check_sample_updates(tmp, opener=dead)
            cond = False
        except vl.VoiceLabError as exc:
            cond = "manifest.json" in str(exc)
            print(f"  清单取不到 → 抛（含地址）  {'OK' if cond else '✗'}")
        ok &= cond

        # ⑤ 清单结构不对 → 抛
        def bad(req, timeout=None):                                  # noqa: ANN001
            return _Resp(b'{"nope": 1}')

        try:
            vl.fetch_sample_manifest(opener=bad)
            cond = False
        except vl.VoiceLabError as exc:
            cond = "samples" in str(exc)
            print(f"  清单缺 samples 段 → 抛  {'OK' if cond else '✗'}")
        ok &= cond

        # ⑥ 仓库里的清单与样本 sha 对得上（避免改了样本忘了改清单）
        man = json.loads((Path(__file__).resolve().parents[1] / "vo_sample"
                          / "manifest.json").read_text(encoding="utf-8"))
        row = (man.get("samples") or {}).get("source_sample_v2.wav") or {}
        cond = str(row.get("sha256") or "").lower() == good and int(row.get("bytes") or 0) == len(raw)
        print(f"  仓库清单与样本一致（sha/字节数都对）  {'OK' if cond else '✗'}")
        ok &= cond
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return ok


if __name__ == "__main__":
    print("test_voice_lab:")
    print(" 1) 音色名规范化")
    ok = test_normalize_name()
    print(" 2) 描述规范化 + 模仿提示")
    ok &= test_normalize_prompt_and_hints()
    print(" 3) id 反推名字 + 同名查找")
    ok &= test_name_from_id_and_find()
    print(" 4) 地址按线路派生")
    ok &= test_url_derivation()
    print(" 5) 创建请求形态与返回值")
    ok &= test_create_request_shape()
    print(" 6) 列表解析与服务端错误")
    ok &= test_list_and_error()
    print(" 7) 幂等复用（不重复花钱）")
    ok &= test_reuse_avoids_spending()
    print(" 8) 试听缓存（本地存储）")
    ok &= test_preview_cache()
    print(" 8b) 试听音频统一解码（裸 PCM 原样 / WAV 容器解成 24k 单声道）")
    ok &= test_preview_pcm()
    print(" 9) 配方库")
    ok &= test_recipes()
    print(" 10) 展示行")
    ok &= test_describe_rows()
    print(" 11) 试听测试文本与合成入口")
    ok &= test_sample_pcm()
    print(" 12) 复刻请求体与本地拦截")
    ok &= test_clone_payload_and_guard()
    print(" 13) 复刻素材的本地校验与 data URL")
    ok &= test_audio_probe_and_data_url()
    print(" 14) 克隆音色的显示名登记表")
    ok &= test_labels_registry()
    print(" 15) 克隆预设（内置/本地覆盖/定位样本/本地试听解码）")
    ok &= test_clone_presets()
    print(" 16) 范本音频按需拉取（地址顺序/sha 校验/不重复拉/全挂报错）")
    ok &= test_preset_fetch()
    print(" 17) 启动查更新（清单驱动：最新不下载 / 过期自动拉 / 取不到就抛 / 结构校验）")
    ok &= test_sample_update_check()
    assert ok, "voice_lab 用例失败（见上）"
    print("ALL PASSED")
