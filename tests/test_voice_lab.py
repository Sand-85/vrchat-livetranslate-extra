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
import json
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
    op = FakeOpener([{"output": {"voice_list": [
        {"voice": "qwen-tts-vd-a-voice-20260901010101-1111", "preferred_name": "a",
         "voice_prompt": "描述A", "target_model": vl.DEFAULT_TARGET_MODEL,
         "gmt_create": "2026-09-01 01:01:01"},
        {"voice": ""},                       # 脏数据：跳过
        "不是字典",                           # 脏数据：跳过
    ]}}])
    rows = vl.list_voices(api_key="sk-test", base_url="wss://maas.qianwenaiapi.com/x", opener=op)
    cond = len(rows) == 1 and rows[0].name == "a" and rows[0].prompt == "描述A"
    print(f"  列表解析（脏数据跳过）：{len(rows)} 条  {'OK' if cond else '✗'}")
    ok &= cond
    cond = op.actions() == ["list"]
    print(f"  请求 action={op.actions()}  {'OK' if cond else '✗'}")
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
    print(" 9) 配方库")
    ok &= test_recipes()
    print(" 10) 展示行")
    ok &= test_describe_rows()
    print(" 11) 试听测试文本与合成入口")
    ok &= test_sample_pcm()
    assert ok, "voice_lab 用例失败（见上）"
    print("ALL PASSED")
