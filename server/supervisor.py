"""
Subtle supervisor: the always-on part of Subtle (tray icon + the Subtle window, ui.py), with no
torch and no models loaded, so it uses ~40 MB of RAM and no video memory.

  ws://127.0.0.1:8765/            the browser extension connects here, as before
  ws://127.0.0.1:8765/panel       the same commands the window uses, for local tools and tests
  http://127.0.0.1:8765/api/ping  "is Subtle running?" (a second start then asks /api/show)

When a tab starts subtitles, the supervisor starts the engine (server.py: Whisper, voice
detection) and, for the Best engine, the translator (llama-server), and relays the tab's
connection to the engine. When nothing has been subtitled for a while (Settings, default
5 minutes) both are stopped and all their VRAM and RAM is given back.
"""

import argparse
import asyncio
import collections
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

import downloads
import edition
import gpuinfo
import llm_client
import llm_runner
import modelstore
import pcaudio
import updater as updater_mod
import settings as settings_mod
import winutil
from version import VERSION

SERVER_DIR = Path(__file__).resolve().parent
ROOT = SERVER_DIR.parent
ASSETS = SERVER_DIR / "assets"
TRANSCRIPT_DIR = ROOT / "transcripts"
LOG_DIR = ROOT / "logs"
EXTENSION_DIR = ROOT / "extension"

LOG = collections.deque(maxlen=600)   # what the console used to show; memory only, shown in the panel
ASR_DEFAULT_LANGS = ("zh", "ja", "ko", "other")      # Settings: speech model per spoken language


def log(msg: str):
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    LOG.append(line)
    try:
        print(line, flush=True)
    except Exception:
        pass


def uses_llm(cfg: dict | None) -> bool:
    cfg = cfg or {}
    return cfg.get("mode") == "llm" or str(cfg.get("target") or "en").lower() != "en"


def _version_tuple(v: str) -> tuple:
    nums = []
    for part in str(v).lstrip("vV").split("-")[0].split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    return tuple(nums + [0] * (3 - len(nums)))


def check_update() -> dict | None:
    """{"version", "url"} if GitHub has a newer release, {} if this is the newest, None if unknown."""
    try:
        req = urllib.request.Request(Supervisor.RELEASES_URL, headers={
            "User-Agent": f"Subtle/{VERSION}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        tag = str(data.get("tag_name") or "")
        if tag and _version_tuple(tag) > _version_tuple(VERSION):
            return {"version": tag.lstrip("vV"), "url": str(data.get("html_url") or "https://github.com/ddsaamir/subtle/releases/latest")}
        return {}
    except Exception:
        return None


# ---------------------------------------------------------------------------------------------
# Engine process (server.py)
# ---------------------------------------------------------------------------------------------
class Engine:
    def __init__(self, job: winutil.ChildJob):
        self.job = job
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.token = ""
        self.state = "stopped"        # stopped | starting | ready | error
        self.error = ""
        self.loaded: list[str] = []   # Whisper models on the GPU (from the engine's log)
        self.started_at = 0.0
        self.lock = asyncio.Lock()
        self._ready: asyncio.Future | None = None

    def pid(self):
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else None

    def running(self) -> bool:
        return self.state == "ready" and self.pid() is not None

    def snapshot(self) -> dict:
        return {"state": self.state if self.pid() or self.state == "error" else "stopped",
                "error": self.error, "loaded": self.loaded, "pid": self.pid()}

    async def ensure(self) -> str:
        async with self.lock:
            if self.running():
                return ""
            await self._stop()
            self.port = llm_runner.free_port(8767)
            self.token = secrets.token_urlsafe(24)
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", SUBTLE_ENGINE_TOKEN=self.token)
            cmd = [winutil.console_python(), "-u", str(SERVER_DIR / "server.py"),
                   "--host", "127.0.0.1", "--port", str(self.port)]
            self.state, self.error, self.loaded = "starting", "", []
            self.started_at = time.monotonic()
            log("[engine] starting")
            loop = asyncio.get_running_loop()
            self._ready = loop.create_future()
            try:
                self.proc = subprocess.Popen(cmd, cwd=str(SERVER_DIR), env=env, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                             **winutil.popen_kwargs())
            except OSError as e:
                self.state, self.error = "error", f"could not start the engine: {e}"
                return self.error
            self.job.add(self.proc)
            threading.Thread(target=self._pump, args=(self.proc, loop, self._ready), daemon=True).start()
            try:
                await asyncio.wait_for(asyncio.shield(self._ready), 180)
            except asyncio.TimeoutError:
                pass
            if self.proc is not None and self.proc.poll() is None and self._ready.done():
                self.state = "ready"
                log(f"[engine] ready in {time.monotonic() - self.started_at:.1f}s")
                return ""
            tail = " | ".join(line for line in list(LOG)[-4:] if "[engine]" not in line)
            await self._stop()
            self.state, self.error = "error", f"the engine did not start. {tail}"[:500]
            return self.error

    def _pump(self, proc, loop, ready):
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            log(line)
            if line.startswith("[ready]"):
                loop.call_soon_threadsafe(lambda: ready.done() or ready.set_result(True))
            elif line.startswith("[model] ") and " ready on " in line:
                mid = line[8:].split(" ready on ")[0]
                self.loaded = [mid]
            elif line.startswith("[model] unloaded"):
                self.loaded = []
        loop.call_soon_threadsafe(lambda: ready.done() or ready.set_result(False))

    async def stop(self):
        async with self.lock:
            await self._stop()

    async def _stop(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            await asyncio.to_thread(winutil.kill_tree, proc)
            log("[engine] stopped - its memory is free again")
        self.loaded = []
        if self.state != "error":
            self.state = "stopped"

    def kill_now(self):
        if self.proc is not None and self.proc.poll() is None:
            winutil.kill_tree(self.proc, 2)


# ---------------------------------------------------------------------------------------------
# One browser tab, relayed to the engine
# ---------------------------------------------------------------------------------------------
class Tab:
    def __init__(self, sup: "Supervisor", ws):
        self.sup = sup
        self.ws = ws
        self.config: dict | None = None
        self.link = None
        self.linking: asyncio.Task | None = None
        self.send_lock = asyncio.Lock()
        self.cfg_lock = asyncio.Lock()
        self.audio_at = 0.0
        self.sent_samples = 0                  # audio passed to the engine on the current link (its clock)
        self.shown: dict = {}                  # (line id, text) -> when that text first reached the tab
        # While the engine starts, the newest few seconds of audio are kept (the line being spoken
        # right now still gets its subtitle); anything older would only show up late, so it's dropped.
        self.pending: collections.deque[bytes] = collections.deque()
        self.pending_bytes = 0
        # Finished lines of the last minutes, for "Catch me up" (memory only, gone with the tab).
        self.history: collections.deque[tuple[float, str]] = collections.deque(maxlen=400)
        self.summarizing = False

    def remember_line(self, raw: str):
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("final") and msg.get("text"):
            self.history.append((time.monotonic(), str(msg["text"])))

    # Prompts stay inside the translator's small context (2048 tokens): about 1200 tokens of lines.
    SUMMARY_CHARS = {"ja": 1100, "zh": 1100, "ko": 1300}

    async def summarize(self, minutes: float):
        if self.summarizing:
            return
        self.summarizing = True
        try:
            cutoff = time.monotonic() - max(1.0, min(30.0, minutes)) * 60
            lines = [t for at, t in self.history if at >= cutoff]
            target = str((self.config or {}).get("target") or "en").lower()
            budget = self.SUMMARY_CHARS.get(target, 4000)
            picked, used = [], 0
            for line in reversed(lines):                  # the newest lines matter most
                if used + len(line) > budget:
                    break
                picked.append(line)
                used += len(line) + 1
            picked.reverse()
            if len(picked) < 3:
                await self.send({"type": "summary", "error": "Not much has been said yet - try again in a minute."})
                return
            url, model_id, err = await self.sup.ensure_llm(str((self.config or {}).get("llm_model") or ""))
            if err or not url:
                await self.send({"type": "summary", "error": err or "No translator model - download one on the Models page."})
                return
            self.sup.last_llm_use = time.monotonic()
            t0 = time.perf_counter()
            text = await asyncio.to_thread(llm_client.llm_summary, url, model_id, picked, target)
            self.sup.last_llm_use = time.monotonic()
            log(f"[summary] {len(picked)} lines summarised in {time.perf_counter() - t0:.1f}s")
            await self.send({"type": "summary", "text": text or "(nothing to summarise)", "lines": len(picked)})
        except Exception as e:
            log(f"[summary] {type(e).__name__}: {e}")
            await self.send({"type": "summary", "error": f"The translator could not make a summary ({type(e).__name__})."})
        finally:
            self.summarizing = False

    async def define(self, word: str, context: str, surface: str = "", reading: str = ""):
        if not word.strip():
            return
        cfg = self.config or {}
        try:
            url, model_id, err = await self.sup.ensure_llm(str(cfg.get("llm_model") or ""))
            if err or not url:
                await self.send({"type": "define", "word": word, "error": err or "No translator model."})
                return
            self.sup.last_llm_use = time.monotonic()
            lang = str(cfg.get("language") or "")
            text = await asyncio.to_thread(llm_client.llm_define, url, model_id, word, context,
                                           None if lang in ("", "auto") else lang, str(cfg.get("target") or "en"),
                                           surface, reading)
            await self.send({"type": "define", "word": word, "text": text})
        except Exception as e:
            log(f"[define] {type(e).__name__}: {e}")
            await self.send({"type": "define", "word": word, "error": f"Could not look it up ({type(e).__name__})."})

    async def send(self, obj):
        async with self.send_lock:
            try:
                await self.ws.send(obj if isinstance(obj, (str, bytes)) else json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

    async def status(self, message: str, level: str = "busy"):
        await self.send({"type": "status", "message": message, "level": level})

    async def run(self):
        await self.send({"type": "ready", "version": VERSION})
        try:
            async for message in self.ws:
                if isinstance(message, bytes):
                    self.audio_at = time.monotonic()
                    self.sup.touch(self)
                    link = self.link
                    if link is not None:
                        try:
                            await link.send(message)
                            self.sent_samples += len(message) // 4
                        except ConnectionClosed:
                            self.link = None
                    else:
                        self.keep_pending(message)
                        self.start_link()
                    continue
                try:
                    msg = json.loads(message)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "show_panel":        # the extension popup's "Open Subtle" link
                    self.sup.show_window()
                elif kind == "info":
                    self.sup.prewarm()          # the popup is open: Start is probably next
                    await self.send(await self.sup.info())
                elif kind == "define":          # learning mode: a word was clicked
                    self.define_task = asyncio.create_task(self.define(
                        str(msg.get("word") or "")[:60], str(msg.get("context") or "")[:400],
                        str(msg.get("surface") or "")[:60], str(msg.get("reading") or "")[:80]))
                elif kind == "check_update":    # the popup's "Check for updates"
                    res = await self.sup.check_update_now()
                    await self.send({"type": "update_check", **res})
                elif kind == "update":          # the popup's "Update now"
                    await self.sup.update_now()
                    await self.send({"type": "update_started"})
                elif kind == "summary":         # "Catch me up"
                    self.summary_task = asyncio.create_task(self.summarize(float(msg.get("minutes") or 5)))
                elif kind == "config":
                    self.config = msg
                    self.sup.touch(self)
                    if self.link is not None:
                        await self.forward_config(self.link)
                    else:
                        self.start_link()
                elif self.link is not None:
                    try:
                        await self.link.send(message)
                    except ConnectionClosed:
                        self.link = None
        except ConnectionClosed:
            pass
        finally:
            link, self.link = self.link, None
            if self.linking is not None:
                self.linking.cancel()
            if link is not None:
                await link.close()

    PENDING_MAX = 4 * 16000 * 4         # 4 s of float32 audio at 16 kHz

    def keep_pending(self, chunk: bytes):
        self.pending.append(chunk)
        self.pending_bytes += len(chunk)
        while self.pending_bytes > self.PENDING_MAX:
            self.pending_bytes -= len(self.pending.popleft())

    def start_link(self):
        if self.config is None:
            return                      # nothing to do until the tab says what it wants
        if self.linking is None or self.linking.done():
            self.linking = asyncio.create_task(self._link())

    async def _link(self):
        sup = self.sup
        if uses_llm(self.config) and sup.settings.get("llm_backend") != "external":
            # load the translator while the engine starts, not after it
            asyncio.ensure_future(sup.ensure_llm(str((self.config or {}).get("llm_model") or "")))
        if not sup.engine.running():
            await self.status("Starting the speech engine…")
        err = await sup.engine.ensure()
        if err:
            await self.status(err, "error")
            return
        try:
            link = await connect(f"ws://127.0.0.1:{sup.engine.port}/?token={sup.engine.token}",
                                 max_size=2**22, compression=None, open_timeout=15)
        except Exception as e:
            await self.status(f"Could not reach the speech engine ({type(e).__name__})", "error")
            return
        asyncio.create_task(self._pump(link))
        await self.forward_config(link)
        self.sent_samples = 0                  # the engine's clock starts at 0 on each new connection
        self.shown.clear()
        try:
            while self.pending:
                chunk = self.pending.popleft()
                await link.send(chunk)
                self.sent_samples += len(chunk) // 4
        except ConnectionClosed:
            return
        self.pending_bytes = 0
        self.link = link

    async def forward_config(self, link):
        async with self.cfg_lock:
            cfg = await self.sup.prepare_config(self, dict(self.config or {}))
            try:
                await link.send(json.dumps(cfg, ensure_ascii=False))
            except ConnectionClosed:
                pass

    def measure_delay(self, raw: str):
        """
        Subtitle delay as the viewer sees it: from the moment the speaker stopped (the line's "t", in
        seconds of audio the engine has received) to the moment the line's final words first reached
        the tab. If a preview already showed exactly those words, that earlier moment counts.
        """
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        now = self.sent_samples / 16000
        text, lid = msg.get("text") or "", msg.get("id")
        if not msg.get("final"):
            if text:
                self.shown.setdefault((lid, text), now)
                if len(self.shown) > 400:            # forget old lines
                    for k in list(self.shown)[:200]:
                        del self.shown[k]
            return
        if not text or msg.get("t") is None:
            return
        first = self.shown.get((lid, text), now)
        self.sup.record_delay(max(0.0, first - float(msg["t"])) * 1000, int(msg.get("ms") or 0))

    async def _pump(self, link):
        try:
            async for m in link:
                if isinstance(m, str) and m.startswith('{"type": "ready"'):
                    continue                # the tab already got our own "ready"
                await self.send(m)
                if isinstance(m, str) and m.startswith('{"type": "sub"'):
                    self.measure_delay(m)
                    if '"final": true' in m:
                        self.remember_line(m)
        except ConnectionClosed:
            pass
        finally:
            if self.link is link:
                self.link = None


# ---------------------------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------------------------
class Supervisor:
    def __init__(self, port: int, allow_any_origin: bool = False):
        self.port = port
        self.allow_any_origin = allow_any_origin
        self.settings = settings_mod.load()
        self.job = winutil.ChildJob()
        self.engine = Engine(self.job)
        self.llama = llm_runner.LlamaServer(log=log, job=self.job)
        self.tabs: set[Tab] = set()
        self.panels: set = set()
        self.last_audio = 0.0
        self.last_llm_use = 0.0
        self.delays: collections.deque = collections.deque(maxlen=500)   # (delay ms, compute ms) per finished line
        self._usage, self._usage_at = None, 0.0
        self.meter = gpuinfo.ProcessMeter()
        self.gpu = {}
        self.gpu_at = 0.0
        self.models_cache = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ready = threading.Event()
        self.ui = None                 # the control panel window (ui.ControlPanel), if any
        self.stop_event: asyncio.Event | None = None
        self.downloader = downloads.Downloader(on_change=self._downloads_changed)
        self.update_info: dict | None = None      # {"version", "url"} when GitHub has a newer Subtle
        self.pc = pcaudio.PcAudio(self, self._pc_message, log, Tab)
        self.updater = updater_mod.Updater(log, on_change=self._update_changed)
        self.update_task: asyncio.Task | None = None   # subtitles for the PC's own sound
        self.pc_status = ""
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    # -- activity / idle -----------------------------------------------------------------------
    def prewarm(self):
        """Start the engine process in the background (it loads no model until a tab asks)."""
        if not self.engine.pid() and not self.engine.lock.locked() and modelstore.whisper_ids():
            self.engine.started_at = time.monotonic()
            asyncio.ensure_future(self.engine.ensure())

    def record_delay(self, delay_ms: float, compute_ms: int):
        self.delays.append((delay_ms, compute_ms))

    def delay_stats(self) -> dict:
        if not self.delays:
            return {"count": 0}
        d = sorted(x for x, _ in self.delays)
        n = len(d)
        median = d[n // 2] if n % 2 else (d[n // 2 - 1] + d[n // 2]) / 2
        last, last_compute = self.delays[-1]
        return {"count": n, "last": round(last), "avg": round(sum(d) / n), "median": round(median),
                "recent": [round(x) for x, _ in list(self.delays)[-48:]],
                "best": round(d[0]), "worst": round(d[-1]), "compute_last": last_compute,
                "compute_avg": round(sum(c for _, c in self.delays) / n)}

    def touch(self, tab: Tab):
        now = time.monotonic()
        self.last_audio = now
        if uses_llm(tab.config):
            self.last_llm_use = now

    async def idle_loop(self):
        while True:
            await asyncio.sleep(5)
            s = self.settings
            minutes = s.get("idle_unload_min", 5)
            if s.get("keep_loaded") or minutes <= 0:
                continue
            limit = minutes * 60
            now = time.monotonic()
            if self.engine.pid() and now - max(self.last_audio, self.engine.started_at) > limit:
                log(f"[idle] nothing subtitled for {minutes} min - unloading")
                await self.unload()
            elif self.llama.pid() and now - max(self.last_llm_use, self.llama.started_at) > limit:
                log(f"[idle] translator unused for {minutes} min - stopping it")
                await self.llama.stop()

    def simple(self) -> bool:
        """Simple mode: only what the browser extension needs (always so in Subtle Lite)."""
        return edition.LITE or bool(self.settings.get("simple_mode"))

    # -- subtitles for the PC's own sound ----------------------------------------------------------
    def _pc_message(self, msg: dict):
        kind = msg.get("type")
        if kind == "status":
            self.pc_status = str(msg.get("message") or "")
        if self.ui is not None and hasattr(self.ui, "pc_message"):
            self.ui.post(self.ui.pc_message, msg)

    def pc_config(self) -> dict:
        s = self.settings
        return {"language": s.get("pc_language", "ja"), "target": s.get("pc_target", "en"),
                "mode": s.get("pc_mode", "whisper"), "asr_model": "auto", "llm_model": "",
                "page_title": "PC sound"}

    # -- one-click update -------------------------------------------------------------------------
    def _update_changed(self):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.push_state()))

    async def update_now(self) -> str:
        """Download, check and apply the newest version; Subtle closes and its updater starts it again."""
        if self.update_task is not None and not self.update_task.done():
            return ""
        self.update_task = asyncio.create_task(self._update_now())
        return ""

    async def _update_now(self):
        self.updater.start()
        while self.updater.state == "downloading":
            await asyncio.sleep(0.3)
        if self.updater.state != "ready":
            await self.push_state()
            return
        await self.pc.stop()
        await self.unload()                         # engine and translator off before files change
        if self.updater.launch_apply():
            await self.push_state()
            return
        await self.push_state()
        await asyncio.sleep(0.5)
        self.stop_event.set()                       # quit; apply_update.py starts the new version

    async def check_update_now(self) -> dict:
        """'Check for updates' (window or popup): ask GitHub now instead of waiting for the daily check."""
        info = await asyncio.to_thread(check_update)
        if info is None:
            return {"error": "Could not reach GitHub. Check the internet connection and try again."}
        self.update_info = info
        await self.push_state()
        return {"ok": True, "update": info or None, "version": VERSION}

    # -- update notice ---------------------------------------------------------------------------
    RELEASES_URL = "https://api.github.com/repos/ddsaamir/subtle/releases/latest"

    async def update_loop(self):
        """Once a day, ask GitHub which Subtle version is the newest (if "check_updates" is on). Only that
        one request is made: no account, no IDs, nothing about what you watch."""
        await asyncio.sleep(20)                   # not during start-up
        while True:
            if self.settings.get("check_updates", True):
                info = await asyncio.to_thread(check_update)
                if info is not None:
                    changed = info != self.update_info
                    self.update_info = info
                    if changed:
                        if info.get("version"):
                            log(f"[update] Subtle {info['version']} is available: {info['url']}")
                        await self.push_state()
            await asyncio.sleep(24 * 3600)

    async def unload(self):
        await self.pc.stop()                      # it would only load everything again
        for tab in list(self.tabs):
            link, tab.link = tab.link, None
            if link is not None:
                await link.close()
        await self.engine.stop()
        await self.llama.stop()
        await self.push_state()

    # -- translator ----------------------------------------------------------------------------
    def llm_list(self) -> list[dict]:
        s = self.settings
        return modelstore.llm_models(True, s.get("scan_lmstudio", True), s.get("extra_llm_dirs"))

    def pick_llm(self, wanted: str) -> tuple[str, str | None]:
        """(model id, path) for the built-in translator: the wanted one, the default, or the best installed."""
        models = self.llm_list()
        by_id = {m["id"]: m for m in models}
        for mid in (wanted, self.settings.get("llm_model")):
            if mid and mid in by_id:
                return mid, by_id[mid]["path"]
        order = [m["id"] for m in downloads.load_catalog() if m["kind"] == "llm"]
        for mid in reversed(order[:2]):          # the recommended default first, then the small one
            if mid in by_id:
                return mid, by_id[mid]["path"]
        return (models[0]["id"], models[0]["path"]) if models else ("", None)

    async def ensure_llm(self, wanted: str = "", tab: Tab | None = None) -> tuple[str, str, str]:
        """Start the translator if needed -> (url, model id, error)."""
        s = self.settings
        if s.get("llm_backend") == "external":
            return s.get("external_url", ""), wanted or s.get("llm_model", ""), ""
        model_id, path = self.pick_llm(wanted)
        if not path:
            return "", "", "No translator model yet - download one on the Models page of the Subtle window"
        if not (self.llama.state == "ready" and self.llama.model_path == path) and tab is not None:
            await tab.status(f"Starting the translator ({model_id})…")
        err = await self.llama.ensure(model_id, path, s)
        await self.push_state()
        return self.llama.url, model_id, err

    async def prepare_config(self, tab: Tab, cfg: dict) -> dict:
        cfg.pop("llm_url", None)
        cfg["previews"] = self.settings.get("previews", "normal")
        cfg["asr_defaults"] = {k: self.settings.get(f"asr_default_{k}", "auto") for k in ASR_DEFAULT_LANGS}
        if uses_llm(cfg):
            self.last_llm_use = time.monotonic()
            url, model_id, err = await self.ensure_llm(str(cfg.get("llm_model") or ""), tab)
            cfg["llm_url"] = url or "http://127.0.0.1:9/v1"
            cfg["llm_model"] = model_id
            if err:
                await tab.status(f"Translator: {err}", "error")
        return cfg

    # -- info for the extension popup ------------------------------------------------------------
    async def info(self) -> dict:
        s = self.settings
        asr = await asyncio.to_thread(modelstore.whisper_ids)
        llm_error = ""
        if s.get("llm_backend") == "external":
            try:
                llms = await asyncio.to_thread(llm_client.list_llm_models, s.get("external_url", ""))
            except Exception as e:
                llms, llm_error = [], f"External translator server not reachable at {s.get('external_url')} ({type(e).__name__})"
        else:
            llms = [m["id"] for m in await asyncio.to_thread(self.llm_list)]
            if llm_runner.llama_exe() is None:
                llm_error = "The built-in translator is missing - run Install Subtle.bat again"
            elif not llms:
                llm_error = "No translator model yet - download one on the Models page of the Subtle window"
        if not asr:
            llm_error = llm_error or "No speech model yet - download one on the Models page of the Subtle window"
        gpu = await self.gpu_info()
        return {"type": "info", "version": VERSION, "transcript_dir": str(TRANSCRIPT_DIR),
                "asr_models": asr, "asr_loaded": self.engine.loaded, "llm_models": llms, "llm_error": llm_error,
                "llm_default": self.pick_llm("")[0] if s.get("llm_backend") != "external" else s.get("llm_model", ""),
                "gpu": gpu.get("name", "CPU"), "backend": s.get("llm_backend"),
                "panel_url": f"http://127.0.0.1:{self.port}/",
                "update": self.update_info if s.get("check_updates", True) else None,
                "simple": self.simple(), "lite": edition.LITE, "can_update": True,
                "updater": self.updater.snapshot()}

    async def gpu_info(self) -> dict:
        if time.monotonic() - self.gpu_at > 4.0:      # nvidia-smi is a process start: not every refresh
            self.gpu = await asyncio.to_thread(gpuinfo.gpu)
            self.gpu_at = time.monotonic()
        return self.gpu

    # -- control panel -----------------------------------------------------------------------------
    def _downloads_changed(self):
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._after_download_change)

    def _after_download_change(self):
        if any(j["state"] == "done" for j in self.downloader.status()):
            self.models_cache = None
        asyncio.ensure_future(self.push_state())

    def models(self) -> dict:
        if self.models_cache is None:
            spk = modelstore.speaker_model_path()
            self.models_cache = {
                "whisper": modelstore.whisper_models(with_size=True),
                "llm": self.llm_list(),
                "speaker": [{"id": Path(spk).name, "name": Path(spk).name, "path": str(spk),
                             "source": "folder" if modelstore.SPEAKER_DIR in Path(spk).parents else "hf-cache",
                             "size": Path(spk).stat().st_size}] if spk else [],
            }
        return self.models_cache

    async def usage_cached(self) -> dict:
        """Per-part memory, measured at most every 4 s (counting a process's private memory is not free)."""
        now = time.monotonic()
        if now - self._usage_at > 4.0 or self._usage is None:
            self._usage = await asyncio.to_thread(self.usage)
            self._usage_at = now
        return self._usage

    def usage(self) -> dict:
        vram = self.meter.vram_by_pid()
        out = {}
        for name, pid in (("supervisor", os.getpid()), ("engine", self.engine.pid()), ("translator", self.llama.pid())):
            # the supervisor's own row is just itself: its children have their own rows
            tree = [pid] if name == "supervisor" else winutil.process_tree(pid)
            out[name] = {"pid": pid, "ram": sum(self.meter.ram(p) for p in tree),
                         "vram": sum(vram.get(p, 0) for p in tree)}
        return out

    async def state(self, full: bool = False) -> dict:
        st = {"type": "state", "version": VERSION, "settings": self.settings, "gpu": await self.gpu_info(),
              "usage": await self.usage_cached(), "engine": self.engine.snapshot(),
              "translator": self.llama.snapshot(), "tabs": len(self.tabs), "downloads": self.downloader.status(),
              "idle_for": round(time.monotonic() - self.last_audio) if self.last_audio else None,
              "pc": dict(self.pc.snapshot(), status=self.pc_status),
              "simple": self.simple(), "lite": edition.LITE,
              "updater": self.updater.snapshot(),
              "delay": self.delay_stats(), "update": self.update_info if self.settings.get("check_updates", True) else None}
        if full:
            st["models"] = await asyncio.to_thread(self.models)
            have = {"whisper": {w["name"].lower() for w in st["models"]["whisper"]},
                    "llm": {m["id"].lower() for m in st["models"]["llm"]},
                    "speaker": {"wespeaker-resnet34"} if st["models"]["speaker"] else set()}
            st["catalog"] = [dict(m, installed=downloads.is_installed(m) or m["id"].lower() in have.get(m["kind"], set()))
                             for m in downloads.load_catalog()]
            st["paths"] = {"root": str(ROOT), "models": str(modelstore.MODELS_DIR), "extension": str(EXTENSION_DIR),
                           "lmstudio": str(modelstore.lmstudio_models_dir() or "")}
            st["autostart"] = winutil.startup_shortcut().exists()
        return st

    async def push_state(self, full: bool = False):
        if not self.panels:
            return
        msg = json.dumps(await self.state(full), ensure_ascii=False)
        for ws in list(self.panels):
            try:
                await ws.send(msg)
            except Exception:
                self.panels.discard(ws)

    async def panel(self, ws):
        self.panels.add(ws)
        try:
            await ws.send(json.dumps(await self.state(full=True), ensure_ascii=False))
            async for message in ws:
                try:
                    msg = json.loads(message)
                except ValueError:
                    continue
                reply = await self.panel_command(msg)
                if reply is not None:
                    await ws.send(json.dumps(dict(reply, type="reply", cmd=msg.get("cmd"), rid=msg.get("rid")),
                                             ensure_ascii=False))
        except ConnectionClosed:
            pass
        finally:
            self.panels.discard(ws)

    async def panel_command(self, msg: dict) -> dict | None:
        cmd = msg.get("cmd")
        if cmd == "state":
            return {"state": await self.state(full=True)}
        if cmd == "settings":
            old = dict(self.settings)
            self.settings = settings_mod.save(msg.get("changes") or {})
            if (old.get("scan_lmstudio") != self.settings.get("scan_lmstudio")
                    or old.get("extra_llm_dirs") != self.settings.get("extra_llm_dirs")):
                self.models_cache = None
            restart_llm = any(old.get(k) != self.settings.get(k) for k in
                              ("llm_gpu_layers", "llm_context", "llm_kv_quant", "llm_backend"))
            if restart_llm and self.llama.pid():
                await self.llama.stop()
            if any(old.get(f"asr_default_{k}") != self.settings.get(f"asr_default_{k}") for k in ASR_DEFAULT_LANGS):
                for tab in list(self.tabs):          # tabs that are running switch model right away
                    if tab.link is not None and tab.config is not None:
                        await tab.forward_config(tab.link)
            await self.push_state(full=True)
            return {"ok": True}
        if cmd == "download":
            return {"error": self.downloader.start(str(msg.get("id")))}
        if cmd == "cancel_download":
            self.downloader.cancel(str(msg.get("id")))
            return {"ok": True}
        if cmd == "clear_downloads":
            self.downloader.clear_finished()
            await self.push_state()
            return {"ok": True}
        if cmd == "delete_model":
            path = str(msg.get("path"))
            if path in (self.llama.model_path,) and self.llama.pid():
                await self.llama.stop()
            if msg.get("kind") == "whisper" and self.engine.pid():
                await self.unload()
            err = downloads.delete_model(str(msg.get("kind")), path)
            self.models_cache = None
            await self.push_state(full=True)
            return {"error": err}
        if cmd == "rescan":
            self.models_cache = None
            await self.push_state(full=True)
            return {"ok": True}
        if cmd == "llm_start":
            url, model_id, err = await self.ensure_llm(str(msg.get("model") or ""))
            self.last_llm_use = time.monotonic()
            return {"error": err, "model": model_id}
        if cmd == "llm_stop":
            await self.llama.stop()
            await self.push_state()
            return {"ok": True}
        if cmd == "llm_test":
            url, model_id, err = await self.ensure_llm(str(msg.get("model") or ""))
            if err:
                return {"error": err}
            self.last_llm_use = time.monotonic()
            t0 = time.perf_counter()
            try:
                out = await asyncio.to_thread(llm_client.llm_translate, url, model_id, str(msg.get("text") or ""),
                                              msg.get("source") or None, str(msg.get("target") or "en"), [], 60)
            except Exception as e:
                return {"error": f"{type(e).__name__}: {e}"}
            return {"text": out, "ms": int((time.perf_counter() - t0) * 1000), "model": model_id}
        if cmd in ("pc_start", "pc_stop", "pc_update"):
            if cmd == "pc_stop":
                await self.pc.stop()
                return {"ok": True}
            if cmd == "pc_update":
                await self.pc.update(self.pc_config())
                return {"ok": True}
            if edition.LITE:
                return {"error": "Subtitles for any app are in the full version of Subtle, not in Subtle Lite."}
            self.pc_status = ""
            err = await self.pc.start(self.pc_config(), str(self.settings.get("pc_device") or ""))
            return {"error": err} if err else {"ok": True}
        if cmd == "pc_devices":
            return {"devices": await asyncio.to_thread(pcaudio.output_devices),
                    "programs": await asyncio.to_thread(pcaudio.programs)}
        if cmd == "check_update":
            return await self.check_update_now()
        if cmd == "update_now":
            await self.update_now()
            return {"ok": True}
        if cmd == "reset_delay":
            self.delays.clear()
            await self.push_state()
            return {"ok": True}
        if cmd == "unload":
            await self.unload()
            return {"ok": True}
        if cmd == "open":
            target = {"models": modelstore.MODELS_DIR, "whisper": modelstore.WHISPER_DIR, "llm": modelstore.LLM_DIR,
                      "speaker": modelstore.SPEAKER_DIR, "extension": EXTENSION_DIR, "transcripts": TRANSCRIPT_DIR,
                      "logs": LOG_DIR}.get(str(msg.get("what")))
            if target is None:
                return {"error": "unknown folder"}
            target.mkdir(parents=True, exist_ok=True)
            return {"ok": winutil.open_path(target)}
        if cmd == "logs":
            return {"lines": list(LOG)[-300:], "llm": list(self.llama.lines)[-100:]}
        if cmd == "autostart":
            link = winutil.startup_shortcut()
            if msg.get("on"):
                ok = await asyncio.to_thread(winutil.make_shortcut, link, winutil.windowless_python(),
                                             f'"{SERVER_DIR / "supervisor.py"}" --hidden', str(ROOT),
                                             str(ASSETS / "subtle.ico"))
                return {"ok": ok, "error": "" if ok else "could not create the startup shortcut"}
            try:
                link.unlink()
            except OSError:
                pass
            return {"ok": True}
        if cmd == "quit":
            self.loop.call_soon(self.stop_event.set)
            return {"ok": True}
        return {"error": f"unknown command {cmd!r}"}

    # -- WebSocket front door (+ two tiny HTTP endpoints) -------------------------------------------
    def process_request(self, connection, request):
        host = request.headers.get("Host", "")
        if host not in self.hosts:                       # DNS-rebinding guard: only this PC's own names
            return connection.respond(403, "Forbidden")
        path = urllib.parse.urlsplit(request.path).path
        if request.headers.get("Upgrade", "").lower() == "websocket":
            origin = request.headers.get("Origin", "")
            if path == "/panel":                         # automation / tests: same-origin local tools only
                if origin not in self.origins:
                    return connection.respond(403, "Forbidden")
            elif path == "/":
                if not (self.allow_any_origin or origin.startswith(("chrome-extension://", "moz-extension://"))):
                    return connection.respond(403, "extension origin required")
            else:
                return connection.respond(404, "Not found")
            return None
        if path == "/api/ping":
            return self._http(200, json.dumps({"app": "subtle", "version": VERSION}).encode())
        if path == "/api/show" and request.headers.get("X-Subtle") == "show":
            # A second start of Subtle asks the running one to show its window. The custom header
            # can't be sent by a web page without a CORS preflight, which is never answered.
            self.show_window()
            return self._http(200, b'{"ok": true}')
        return connection.respond(404, "Not found")

    def _http(self, status: int, body: bytes) -> Response:
        headers = Headers([("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                           ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff")])
        return Response(status, "OK" if status == 200 else "Error", headers, body)

    def show_window(self):
        if self.ui is not None:
            self.ui.post(self.ui.show)
        else:
            log("[ui] no window in this mode (--no-window)")

    async def handler(self, ws):
        path = urllib.parse.urlsplit(ws.request.path).path
        if path == "/panel":
            await self.panel(ws)
            return
        tab = Tab(self, ws)
        self.tabs.add(tab)
        await self.push_state()
        try:
            await tab.run()
        finally:
            self.tabs.discard(tab)
            await self.push_state()

    async def state_loop(self):
        while True:
            await asyncio.sleep(1.5)
            if self.panels:
                await self.push_state()

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.stop_event = asyncio.Event()
        modelstore.ensure_dirs()
        llm_runner.kill_orphans()
        try:
            async with serve(self.handler, "127.0.0.1", self.port, process_request=self.process_request,
                             max_size=2**22, compression=None):
                log(f"[subtle] v{VERSION} running (extension connects to ws://127.0.0.1:{self.port})")
                self.ready.set()
                tasks = [asyncio.create_task(self.idle_loop()), asyncio.create_task(self.state_loop()),
                         asyncio.create_task(self.update_loop())]
                await self.stop_event.wait()
                for t in tasks:
                    t.cancel()
                await self.unload()
        finally:
            self.ready.set()
        log("[subtle] stopped")

    def request_stop(self):
        if self.loop is not None and self.stop_event is not None:
            self.loop.call_soon_threadsafe(self.stop_event.set)

    def request_unload(self):
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self.unload(), self.loop)


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == "subtle"
    except Exception:
        return False


def show_running(port: int):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/show", headers={"X-Subtle": "show"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Subtle (tray app + control panel window)")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-tray", action="store_true", help="run without the tray icon")
    ap.add_argument("--no-window", action="store_true", help="run without the control panel window (console / tests)")
    ap.add_argument("--hidden", action="store_true", help="start in the tray without showing the window")
    ap.add_argument("--allow-any-origin", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    s = settings_mod.load()
    port = args.port or int(s.get("port", 8765))
    if already_running(port):
        show_running(port)                 # second start: bring the running Subtle's window up
        return
    if port == 8765:                       # the extension's "Subtle app" button can start this Subtle
        winutil.register_native_host(SERVER_DIR / "native_host.py", ROOT / "runtime" / "native-host", EXTENSION_DIR)
        winutil.register_url_protocol(SERVER_DIR / "supervisor.py", ASSETS / "subtle.ico")
    sup = Supervisor(port, args.allow_any_origin)
    show = not args.hidden and (s.get("open_panel_on_start", True) or not s.get("first_run_done"))

    ui = None
    if not args.no_window:
        try:
            # Qt draws smoothly; the Tk window (ui.py) is the fallback when Qt can't be loaded here.
            try:
                import ui_qt as ui_mod
            except ImportError as e:
                log(f"[ui] Qt not available ({e}); using the basic window")
                import ui as ui_mod
            ui = ui_mod.ControlPanel(sup, show=False)
            sup.ui = ui
        except Exception as e:
            log(f"[ui] no window ({type(e).__name__}: {e})")
    tray = None
    # macOS only allows menu-bar icons from the main thread (Qt's): no tray there; starting Subtle again
    # brings its window back.
    if not args.no_tray and sys.platform != "darwin":
        try:
            import tray as tray_mod
            tray = tray_mod.start(sup, port)
        except Exception as e:
            log(f"[tray] no tray icon ({type(e).__name__}: {e})")

    try:
        if ui is None:
            asyncio.run(sup.run())
        else:
            # The window needs the main thread; the supervisor runs on a background thread.
            def serve_thread():
                try:
                    asyncio.run(sup.run())
                except Exception as e:
                    log(f"[subtle] stopped with an error: {type(e).__name__}: {e}")
                finally:
                    ui.post(ui.quit)
            t = threading.Thread(target=serve_thread, name="supervisor", daemon=True)
            t.start()
            sup.ready.wait(30)
            if show:
                ui.show()
            ui.mainloop()                  # until Quit (window or tray)
            sup.request_stop()
            t.join(15)
    except KeyboardInterrupt:
        pass
    finally:
        sup.engine.kill_now()
        sup.llama.kill_now()
        if tray is not None:
            tray.stop()


if __name__ == "__main__":
    main()
