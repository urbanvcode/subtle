"""
The built-in translator: llama.cpp's llama-server.exe (bin/llama) running a .gguf from models/llm.

It is started only when a tab needs the Best engine (or from the control panel) and stopped when
idle, so the Fast engine never costs any translator memory. Settings keep it small: a subtitle
prompt is ~400 tokens, so the context is 2048 with an 8-bit KV cache and one slot.
"""

import asyncio
import collections
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import winutil

ROOT = Path(__file__).resolve().parent.parent
LLAMA_DIR = ROOT / "bin" / "llama"
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW


def llama_exe() -> Path | None:
    for name in ("llama-server.exe", "llama-server"):
        p = LLAMA_DIR / name
        if p.is_file():
            return p
    return None


def free_port(preferred: int) -> int:
    for port in (preferred, 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise OSError("no free port")


def _get(url: str, timeout: float = 2.0):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


class LlamaServer:
    def __init__(self, log=print, job=None):
        self.job = job                  # winutil.ChildJob: killed with the supervisor, even on a crash
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.model_id = ""
        self.model_path = ""
        self.state = "stopped"          # stopped | starting | ready | error
        self.error = ""
        self.started_at = 0.0
        self.lines = collections.deque(maxlen=200)   # llama-server's own log (memory only)
        self.log = log
        self.lock = asyncio.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def pid(self) -> int | None:
        return self.proc.pid if self.proc is not None and self.proc.poll() is None else None

    def snapshot(self) -> dict:
        return {"state": self.state, "model": self.model_id, "error": self.error, "pid": self.pid(),
                "installed": llama_exe() is not None, "url": self.url if self.state == "ready" else ""}

    def command(self, path: str, cfg: dict) -> list[str]:
        cmd = [str(llama_exe()), "-m", path, "--host", "127.0.0.1", "--port", str(self.port),
               "-ngl", str(cfg.get("llm_gpu_layers", 999)), "-c", str(cfg.get("llm_context", 2048)),
               "--parallel", "1", "-fa", "on", "-b", "512", "-ub", "256",
               "--jinja", "--no-webui", "--no-warmup",
               # no copies of old prompts in RAM: subtitle prompts all start the same, so the copies were never
               # used, and they grew the translator's RAM from 0.4 GB to over 1 GB in a session
               "--cache-ram", "0"]
        if cfg.get("llm_kv_quant", True):
            cmd += ["--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
        return cmd

    @staticmethod
    def _trim(proc: subprocess.Popen):
        """Hand the translator's start-up memory back to Windows, a little after it has loaded (as the
        engine does for itself in server.py): about 0.4 GB of RAM it never touches again."""
        if sys.platform != "win32" or proc.poll() is not None:
            return
        try:
            import ctypes
            psapi = ctypes.WinDLL("psapi")
            psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
            psapi.EmptyWorkingSet(int(proc._handle))             # Popen keeps the process handle on Windows
        except Exception:
            pass

    async def ensure(self, model_id: str, path: str, cfg: dict) -> str:
        """Make sure llama-server is running `path`. Returns "" when ready, else an error message."""
        async with self.lock:
            if self.state == "ready" and self.model_path == path and self.proc and self.proc.poll() is None:
                return ""
            await self._stop()
            exe = llama_exe()
            if exe is None:
                self.state, self.error = "error", "llama-server is not installed (run Install Subtle.bat again)"
                return self.error
            if not path or not Path(path).is_file():
                self.state, self.error = "error", "translator model not found - pick one on the Models page"
                return self.error
            self.port = free_port(8766)
            self.model_id, self.model_path = model_id, path
            self.state, self.error = "starting", ""
            self.started_at = time.monotonic()
            self.lines.clear()
            self.log(f"[llm] starting llama-server with {Path(path).name}")
            try:
                self.proc = subprocess.Popen(
                    self.command(path, cfg), cwd=str(LLAMA_DIR), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **winutil.popen_kwargs(),
                )
            except OSError as e:
                self.state, self.error = "error", f"could not start llama-server: {e}"
                return self.error
            if self.job is not None:
                self.job.add(self.proc)
            asyncio.get_running_loop().run_in_executor(None, self._pump, self.proc)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    tail = " | ".join(list(self.lines)[-3:])
                    self.state = "error"
                    self.error = f"llama-server stopped (code {self.proc.returncode}). {tail}"[:400]
                    self.proc = None
                    return self.error
                try:
                    status, _ = await asyncio.to_thread(_get, f"http://127.0.0.1:{self.port}/health", 1.0)
                    if status == 200:
                        self.state = "ready"
                        self.log(f"[llm] ready in {time.monotonic() - self.started_at:.1f}s on port {self.port}")
                        asyncio.get_running_loop().call_later(15, self._trim, self.proc)
                        return ""
                except (urllib.error.URLError, OSError, ValueError):
                    pass
                await asyncio.sleep(0.25)
            await self._stop()
            self.state, self.error = "error", "llama-server took too long to load the model"
            return self.error

    def _pump(self, proc: subprocess.Popen):
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self.lines.append(line)
        except Exception:
            pass

    async def stop(self):
        async with self.lock:
            await self._stop()

    async def _stop(self):
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                proc.kill()
            self.log("[llm] llama-server stopped - its memory is free again")
        if self.state != "error":
            self.state = "stopped"

    def kill_now(self):
        """For exit paths outside the event loop."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
        self.proc = None


def kill_orphans():
    """
    llama-server.exe processes from our bin folder left behind by a crash: only those whose parent
    (the Subtle that started them) is gone. A translator owned by another running Subtle (e.g. a test
    copy on another port) is left alone.
    """
    try:
        import psutil
    except ImportError:
        return
    exe = llama_exe()
    if exe is None:
        return
    for p in psutil.process_iter(["exe", "ppid"]):
        try:
            if not (p.info["exe"] and Path(p.info["exe"]).resolve() == exe.resolve()):
                continue
            parent = psutil.Process(p.info["ppid"]) if p.info["ppid"] else None
            # Windows reuses process ids: a live parent only counts if it started before the child.
            if parent is not None and parent.is_running() and parent.create_time() <= p.create_time():
                continue
            p.kill()
        except psutil.NoSuchProcess:
            try:
                p.kill()                     # its parent is gone: an orphan
            except psutil.Error:
                pass
        except (psutil.Error, OSError):
            pass
