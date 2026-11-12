"""
Native messaging host: lets the browser extension start Subtle with one click, with no question from the
browser (the extension's "Subtle app" and "Start Subtle" buttons).

The browser runs this program and sends one message on stdin (a 4-byte length, then JSON):
  {"cmd": "start"}   Subtle running: show its window. Not running: start it (fully detached from the
                     browser, so it keeps running after this program ends).
It answers {"ok": true, "running": bool, "started": bool} or {"ok": false, "error": "..."} and exits.

Subtle registers this host for Chrome, Edge, Brave, Chromium and Firefox each time it starts
(winutil.register_native_host); only Subtle's own extension may call it.
"""

import json
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SERVER_DIR))


def read_message():
    raw = sys.stdin.buffer.read(4)
    if len(raw) < 4:
        return None
    size = struct.unpack("<I", raw)[0]
    if size > 1 << 16:
        return None
    try:
        return json.loads(sys.stdin.buffer.read(size).decode("utf-8"))
    except ValueError:
        return None


def send_message(obj):
    data = json.dumps(obj).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)) + data)
    sys.stdout.buffer.flush()


def port() -> int:
    try:
        import settings
        return int(settings.load().get("port", 8765))
    except Exception:
        return 8765


def running(p: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{p}/api/ping", timeout=2) as r:
            return json.loads(r.read()).get("app") == "subtle"
    except Exception:
        return False


def show(p: int):
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{p}/api/show", headers={"X-Subtle": "show"})
        urllib.request.urlopen(req, timeout=3).read()
    except Exception:
        pass


def in_job() -> bool:
    """Whether this helper (or the Python launcher that started it) runs inside a Windows job object."""
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.OpenProcess.restype = ctypes.c_void_p
    handles = [k32.GetCurrentProcess()]
    try:                                              # a venv's python.exe is a launcher: check it too
        import os
        parent = k32.OpenProcess(0x1000, False, os.getppid())        # PROCESS_QUERY_LIMITED_INFORMATION
        if parent:
            handles.append(parent)
    except Exception:
        pass
    for h in handles:
        r = ctypes.c_int(0)
        if k32.IsProcessInJob(ctypes.c_void_p(h), None, ctypes.byref(r)) and r.value:
            return True
    return False


def start() -> str:
    """Start Subtle as its own program (not a child the browser could end with this host)."""
    import winutil
    args = [winutil.windowless_python(), str(SERVER_DIR / "supervisor.py")]
    kw = dict(cwd=str(SERVER_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        detached = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        if in_job():
            # Started inside a job (some browsers run helpers in one): it could end Subtle together with this
            # helper. Windows' management service (WMI) starts it instead, outside any job.
            cmd = subprocess.list2cmdline(args).replace("'", "''")
            cwd = str(SERVER_DIR).replace("'", "''")
            ps = ("$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
                  f"@{{CommandLine='{cmd}'; CurrentDirectory='{cwd}'}}; exit [int]$r.ReturnValue")
            try:
                r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                                   capture_output=True, timeout=20, creationflags=0x08000000)   # CREATE_NO_WINDOW
                if r.returncode == 0:
                    return ""
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                subprocess.Popen(args, creationflags=detached | 0x01000000, **kw)   # CREATE_BREAKAWAY_FROM_JOB
                return ""
            except OSError:
                pass
        try:
            subprocess.Popen(args, creationflags=detached, **kw)
            return ""
        except OSError as e:
            return f"{type(e).__name__}: {e}"
    try:
        subprocess.Popen(args, start_new_session=True, **kw)
        return ""
    except OSError as e:
        return f"{type(e).__name__}: {e}"


def main():
    msg = read_message() or {}
    if msg.get("cmd") != "start":
        send_message({"ok": False, "error": "unknown command"})
        return
    p = port()
    if running(p):
        show(p)
        send_message({"ok": True, "running": True, "started": False})
        return
    err = start()
    send_message({"ok": not err, "running": False, "started": not err, "error": err})


if __name__ == "__main__":
    main()
