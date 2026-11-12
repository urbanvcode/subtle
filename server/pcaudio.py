"""
Subtitles for the PC's own sound ("any app"): Discord, Zoom, games, VLC, a local film...

What the speakers play is recorded (WASAPI loopback on Windows, the "monitor" source on Linux) at
16 kHz mono and fed into the same pipeline a browser tab uses (supervisor.Tab), through an in-memory
socket. The subtitles come back to `on_message`, which draws them in a window on top of the desktop.
Audio is never written anywhere; it only travels to the speech engine on this PC.
"""

import asyncio
import json
import threading
import types
import warnings

import numpy as np

SR = 16000
BLOCK = 512                                # 32 ms, the same chunks the browser extension sends


def _soundcard():
    """The soundcard module, with its Windows backend adapted to numpy 2 (it still calls the removed
    numpy.fromstring). The stand-in only lives inside that backend."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        import soundcard as sc
    try:
        import soundcard.mediafoundation as mf

        class _Numpy(types.ModuleType):
            def __getattr__(self, name):
                return getattr(np, name)

        shim = _Numpy("numpy")
        shim.fromstring = lambda data, dtype=float: np.frombuffer(data, dtype=dtype).copy()
        mf.numpy = shim
    except ImportError:
        pass                               # not Windows
    return sc


def programs() -> list[dict]:
    """Programs that can be listened to on their own (Windows 10 2004+): [{"id": "app:discord.exe", "name"}]."""
    try:
        import procloop
        if not procloop.supported():
            return []
        import psutil
        out = []
        for d in procloop.audio_programs():
            try:
                exe = psutil.Process(d["pid"]).name()
            except Exception:
                continue
            out.append({"id": "app:" + exe.lower(), "name": d["name"]})
        return out
    except Exception:
        return []


def _program_pid(exe: str) -> int | None:
    """The top process of a running program with that file name (its children are recorded too)."""
    import procloop
    for d in procloop.audio_programs():
        try:
            import psutil
            if psutil.Process(d["pid"]).name().lower() == exe:
                return d["pid"]
        except Exception:
            continue
    try:
        import psutil
        for proc in psutil.process_iter(["name", "pid"]):
            if (proc.info["name"] or "").lower() == exe:
                return proc.info["pid"]
    except Exception:
        pass
    return None


def output_devices() -> list[dict]:
    """Speakers / headphones that can be listened to, default first."""
    try:
        sc = _soundcard()
        default = sc.default_speaker()
        out = [{"id": str(default.name), "name": default.name, "default": True}]
        for spk in sc.all_speakers():
            if spk.name != default.name:
                out.append({"id": str(spk.name), "name": spk.name, "default": False})
        return out
    except Exception:
        return []


class QueueSocket:
    """Looks like a websocket to supervisor.Tab: audio and commands go in, subtitles come out."""

    def __init__(self, on_message):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=200)   # ~6 s of audio
        self.on_message = on_message
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    def put(self, item):
        if self.closed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:          # the engine stalled: drop the oldest audio, keep it live
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def send(self, data):
        if isinstance(data, (bytes, bytearray)):
            return
        try:
            msg = json.loads(data) if isinstance(data, str) else data
        except ValueError:
            return
        try:
            self.on_message(msg)
        except Exception:
            pass

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.queue.put_nowait(None)
            except asyncio.QueueFull:
                self.queue.get_nowait()
                self.queue.put_nowait(None)


class PcAudio:
    def __init__(self, sup, on_message, log=print, tab_cls=None):
        self.sup = sup
        self.tab_cls = tab_cls       # supervisor.Tab (passed in: supervisor usually runs as __main__)
        self.on_message = on_message   # every message for the subtitle window (sub, clear, status...)
        self.log = log
        self.sock: QueueSocket | None = None
        self.tab = None
        self.task: asyncio.Task | None = None
        self.thread: threading.Thread | None = None
        self.running = False
        self.config: dict = {}
        self.error = ""
        self.device = ""

    def snapshot(self) -> dict:
        return {"running": self.running, "error": self.error, "device": self.device,
                "language": self.config.get("language"), "target": self.config.get("target"),
                "mode": self.config.get("mode")}

    async def start(self, config: dict, device: str = "") -> str:
        await self.stop()
        try:
            if device.startswith("app:"):                    # one program only
                import procloop
                exe = device[4:]
                pid = _program_pid(exe)
                if pid is None:
                    self.error = f"{exe} isn't running. Start it (and let it play something), then press Start."
                    return self.error
                open_rec = lambda: procloop.ProcessRecorder(pid)       # noqa: E731
                label = f"{exe.rsplit('.', 1)[0]} only"
            else:
                sc = _soundcard()
                spk = sc.default_speaker() if not device else next(
                    (s for s in sc.all_speakers() if str(s.name) == device), sc.default_speaker())
                mic = sc.get_microphone(id=str(spk.name), include_loopback=True)
                open_rec = lambda: mic.recorder(samplerate=SR, channels=1, blocksize=BLOCK)   # noqa: E731
                label = spk.name
        except Exception as e:
            self.error = f"Can't listen to the PC's sound: {type(e).__name__}: {e}"
            return self.error
        self.error, self.device = "", label
        self.config = {"type": "config", **config}
        loop = asyncio.get_running_loop()
        self.sock = QueueSocket(self.on_message)
        self.tab = self.tab_cls(self.sup, self.sock)
        self.tab.pc = True
        self.sup.tabs.add(self.tab)
        self.running = True
        self.sock.put(json.dumps(self.config))
        self.task = asyncio.create_task(self._run_tab())
        self.thread = threading.Thread(target=self._record, args=(open_rec, loop, self.sock), daemon=True)
        self.thread.start()
        self.log(f"[pc] listening to {label}: {config.get('language')} -> {config.get('target')} ({config.get('mode')})")
        await self.sup.push_state()
        return ""

    async def _run_tab(self):
        try:
            await self.tab.run()
        except Exception as e:
            self.log(f"[pc] {type(e).__name__}: {e}")
        finally:
            self.sup.tabs.discard(self.tab)

    def _record(self, open_rec, loop, sock: QueueSocket):
        try:
            with open_rec() as rec:
                while self.running and sock is self.sock:
                    data = rec.record(numframes=BLOCK)
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    chunk = np.ascontiguousarray(data, dtype=np.float32).tobytes()
                    loop.call_soon_threadsafe(sock.put, chunk)
        except Exception as e:
            if self.running and sock is self.sock:
                self.error = f"Listening stopped: {type(e).__name__}: {e}"
                self.log(f"[pc] {self.error}")
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.stop()))

    async def update(self, config: dict):
        """New languages or engine while running."""
        if self.running and self.sock is not None:
            self.config = {"type": "config", **config}
            self.sock.put(json.dumps(self.config))
            await self.sup.push_state()

    async def stop(self):
        was = self.running
        self.running = False
        sock, self.sock = self.sock, None
        if sock is not None:
            sock.close()
        if self.task is not None:
            try:
                await asyncio.wait_for(self.task, 5)
            except (asyncio.TimeoutError, Exception):
                self.task.cancel()
            self.task = None
        self.tab = None
        if was:
            self.log("[pc] stopped listening")
            try:
                self.on_message({"type": "clear"})
            except Exception:
                pass
            await self.sup.push_state()
