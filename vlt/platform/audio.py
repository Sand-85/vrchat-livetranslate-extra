"""音频采集的共享实现（两个平台都用得上，且**不 import 任何平台专有库**）。

## 为什么采集是「异步拉模型」

引擎侧原来是 `asyncio.wait_for(queue.get(), timeout=1.0)` 的循环，而底层
Windows 是 PortAudio 回调、Linux 是子进程 stdout —— 两种完全不同的推进方式。
把它们统一成「`await source.read()` 拿一块 PCM」，引擎就再也看不到平台差异。

## 不变式（历史教训，别破坏）

**关流之前必须让喂数据的那条线程先退出。** 原实现是 while True 死循环 +
阻塞 `stream.read()`，收尾时一个线程卡在读里、另一个线程把流销毁 →
Windows 上实测访问违规（`faulthandler` 抓到 `pyaudiowpatch` 的 read，
用户点「停止翻译」时闪退，退出码 139）。

所以 `close()` 的顺序是硬约束：
    ① 置停止位 → ② join 喂数据线程 → ③ 才真正关掉底层资源
`tests/test_loopback_teardown.py` 就钉这条。
"""
from __future__ import annotations

import array
import asyncio
import logging
import threading

log = logging.getLogger(__name__)


class QueueAudioSource:
    """采集源的共享底座：后台线程喂数据 → asyncio 队列 → `await read()` 取。

    子类只需实现 `_pump(stop)`（在后台线程里跑，负责把数据通过 `_emit()` 送进来）
    与 `_teardown()`（真正关闭底层资源，在喂数据线程**已经退出之后**才被调用）。
    """

    #: 出问题时给用户看的名字（日志前缀）
    label = "audio"

    def __init__(self, loop: asyncio.AbstractEventLoop, *, rate: int, channels: int,
                 max_queue: int = 64) -> None:
        self._loop = loop
        self.rate = rate
        self.channels = channels
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=max_queue)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------ 子类接口
    def _pump(self, stop: threading.Event) -> None:
        """在后台线程里跑：不断把 PCM 通过 `self._emit()` 送出来，直到 stop 置位。"""
        raise NotImplementedError

    def _teardown(self) -> None:
        """关闭底层资源。调用时喂数据线程**保证已退出**。"""

    # ------------------------------------------------------------ 对外
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"vlt-{self.label}-pump")
        self._thread.start()

    def _run(self) -> None:
        try:
            self._pump(self._stop)
        except Exception as exc:  # noqa: BLE001 — 采集线程绝不能把异常抛到主线程
            log.warning("[%s] 采集线程退出：%s: %s", self.label, type(exc).__name__, exc)

    def _emit(self, data: bytes) -> None:
        """线程安全地把一块 PCM 送进队列（队列满就丢最旧的，绝不阻塞采集）。"""
        if self._stop.is_set() or not data:
            return
        try:
            self._loop.call_soon_threadsafe(self._push, data)
        except RuntimeError:
            # 事件循环已关闭（收尾中）—— 正常退出路径，不是错误
            pass

    def _push(self, data: bytes) -> None:
        try:
            self._queue.put_nowait(data)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()      # 丢最旧的：宁可丢音频也不要卡住采集
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def read(self, timeout: float = 1.0) -> bytes | None:
        """取一块 PCM。超时返回 None（调用方据此判断「还活着但没数据」）。"""
        try:
            return await asyncio.wait_for(self._queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        """幂等。顺序不可改：停止位 → join 线程 → 关底层资源。"""
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        th = self._thread
        if th is not None and th.is_alive():
            # 卡在阻塞读里的实现（如 Windows loopback）由子类自己保证读有超时/轮询，
            # 这里给 2 秒是兜底；超了也继续关（并留痕），绝不永久挂住收尾。
            th.join(timeout=2.0)
            if th.is_alive():
                log.warning("[%s] 喂数据线程未在 2s 内退出，仍继续关闭底层资源（可能竞争）",
                            self.label)
        try:
            self._teardown()
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] 关闭底层资源时出错（忽略）：%s: %s", self.label,
                        type(exc).__name__, exc)


class SoundDeviceMicSource(QueueAudioSource):
    """麦克风采集（sounddevice / PortAudio）——**两个平台共用**。

    Windows 走 WASAPI、Linux 走 ALSA（pipewire-alsa），但 sounddevice 的接口一致，
    所以实现共用；差别只在**设备怎么指定**：

    * Linux 用**名字**（`sd.RawInputStream` 接受字符串）—— 我们的设备表来自
      pw-dump，索引与 PortAudio 根本不是一回事，不能混用；
    * Windows 用 **PortAudio 索引** —— 由 `vlt/platform/win.py: open_mic` 经
      `resolve_device_name(name, "input")` 解析得到，与 v0.3.x 的打开口径一致
      （同名端点并存时，选中的物理端点才不会漂）。

    所以这里两种都能收：`str | int | None`。
    """

    label = "mic"

    def __init__(self, loop, device: str | int | None, *, rate: int = 16000,
                 channels: int = 1, blocksize: int = 1600) -> None:
        super().__init__(loop, rate=rate, channels=channels)
        # ⚠️ 不能用 `device or None`：PortAudio 的索引 0 是合法设备，
        #    被 `or` 判成假值就悄悄回落默认设备了。
        self._device = None if device in (None, "") else device
        self._blocksize = blocksize

    def _pump(self, stop: threading.Event) -> None:
        import sounddevice as sd

        def callback(indata, frames, time_info, status):   # noqa: ANN001
            if status:
                log.debug("[mic] callback status: %s", status)
            self._emit(bytes(indata))

        # `with` 退出即关流；而 close() 是「先置 stop → 再 join」，
        # 所以关流时采集线程已经不再产生新数据 —— 顺序与 loopback 侧一致。
        with sd.RawInputStream(samplerate=self.rate, channels=self.channels,
                               dtype="int16", blocksize=self._blocksize,
                               device=self._device, callback=callback):
            stop.wait()


def _mix_pcm16(chunks: list[bytes]) -> bytes:
    """把多路 s16le PCM **逐样本相加并限幅**成一路（长度取最长的那路）。

    刻意不引入 numpy：本模块是「共享、无重依赖」的那一层，而每 100ms 只有千余个样本，
    纯 Python 足够快（实测一条 100ms 块 2 路混音远低于 1ms）。
    """
    n = max(len(c) for c in chunks) // 2
    if n <= 0:
        return b""
    acc = array.array("i", bytes(4 * n))
    for c in chunks:
        a = array.array("h")
        a.frombytes(bytes(c[: (len(c) // 2) * 2]))
        for i in range(len(a)):
            acc[i] += a[i]
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        v = acc[i]
        out[i] = -32768 if v < -32768 else (32767 if v > 32767 else v)
    return out.tobytes()


class MixedAudioSource:
    """把**多路**采集源混成一路（对外接口与 `AudioSource` 一致）。

    用途：VRChat 会开多个播放流（见 `vlt/platform/linux.py: find_vrchat_output_streams`），
    每一路各开一个 `pw-record`，在这里把 PCM 相加限幅后当**一路**喂给引擎。

    为什么是相加而不是取其一：多个流可能各自承载一部分声音（用户实测口径是「两个都抓」）。
    各路采样格式一致（16k 单声道 s16le），所以是逐样本相加后限幅。

    ⚠️ 若将来发现这些流其实是**同一份声音的重复**（相加会爆音），把 `read()` 里的
    `_mix_pcm16(...)` 换成取平均（逐样本除以路数）即可 —— 语义差异只在这一处。
    """

    def __init__(self, sources: list) -> None:
        if not sources:
            raise ValueError("MixedAudioSource 至少需要一路采集源")
        self._sources = list(sources)
        self.rate = self._sources[0].rate
        self.channels = self._sources[0].channels

    @property
    def count(self) -> int:
        return len(self._sources)

    async def read(self, timeout: float = 1.0) -> bytes | None:
        """并发读各路；全部都没数据时才返回 None（「还活着但暂时没声音」）。"""
        chunks = await asyncio.gather(*(s.read(timeout) for s in self._sources),
                                      return_exceptions=True)
        pcm = [bytes(c) for c in chunks if isinstance(c, (bytes, bytearray)) and c]
        if not pcm:
            return None
        if len(pcm) == 1:
            return pcm[0]              # 常见情形：只有一路在出声，不必混
        return _mix_pcm16(pcm)

    def close(self) -> None:
        """幂等收尾：一路关失败不拖垮其它路。"""
        for s in self._sources:
            try:
                s.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("[mix] 关闭一路采集源失败（忽略）：%s: %s",
                            type(exc).__name__, exc)
