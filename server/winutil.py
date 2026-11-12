"""Small operating-system helpers for the supervisor: Windows first, with Linux and macOS versions."""

import ctypes
import os
import subprocess
import sys
from pathlib import Path

NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class ChildJob:
    """
    A Windows job object that the supervisor's child processes (engine, llama-server) are put in.
    When the supervisor exits for any reason, even a crash, Windows closes the job and kills them,
    so no orphan is ever left holding VRAM.
    """

    def __init__(self):
        self.handle = None
        if sys.platform != "win32":
            return
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = ctypes.c_void_p
        k32.OpenProcess.restype = ctypes.c_void_p
        self.k32 = k32
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in
                        ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                        ("SchedulingClass", ctypes.c_uint32)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        if ok:
            self.handle = job

    def add(self, proc: subprocess.Popen):
        if self.handle is None:
            return
        try:
            h = int(proc._handle)                                 # Popen keeps the process handle on Windows
            self.k32.AssignProcessToJobObject(ctypes.c_void_p(self.handle), ctypes.c_void_p(h))
        except Exception:
            pass


def _die_with_parent():
    """Linux: the child gets SIGTERM when Subtle exits, even on a crash (like the Windows job object)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 15)     # PR_SET_PDEATHSIG, SIGTERM
    except Exception:
        pass


def popen_kwargs() -> dict:
    """Extra subprocess.Popen arguments for a background child (engine, translator)."""
    if sys.platform == "win32":
        return {"creationflags": NO_WINDOW}
    if sys.platform.startswith("linux"):
        return {"preexec_fn": _die_with_parent}
    return {}


def process_tree(pid: int | None) -> list[int]:
    """pid and all its descendants. (A venv's python.exe on Windows is a launcher that runs the real
    interpreter as a child, so the memory and the work are in the child.)"""
    if not pid:
        return []
    try:
        import psutil
        p = psutil.Process(pid)
        return [pid] + [c.pid for c in p.children(recursive=True)]
    except Exception:
        return [pid]


def kill_tree(proc: subprocess.Popen, timeout: float = 5.0):
    """Stop a child process and everything it started (blocking; call from a thread)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        import psutil
        parent = psutil.Process(proc.pid)
        kids = parent.children(recursive=True)
    except Exception:
        kids = []
    for k in kids:
        try:
            k.terminate()
        except Exception:
            pass
    proc.terminate()
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
    for k in kids:
        try:
            k.wait(timeout)
        except Exception:
            try:
                k.kill()
            except Exception:
                pass


def console_python() -> str:
    """python.exe next to the running interpreter (the tray runs under pythonw.exe)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return str(exe.parent / "python.exe")
    return str(exe)


def windowless_python() -> str:
    exe = Path(sys.executable)
    w = exe.parent / "pythonw.exe"
    return str(w) if w.is_file() else str(exe)


def register_url_protocol(supervisor: Path, icon: Path | None = None) -> bool:
    """Windows: make subtle:// links start Subtle (the extension's "Subtle app" button uses one when Subtle
    isn't running; the browser asks first). Per user, no admin rights. The link's own text is never passed
    on: it only starts Subtle, or shows the window of the one already running."""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        command = f'"{windowless_python()}" "{supervisor}"'
        base = r"Software\Classes\subtle"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base) as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Subtle")
            winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
        if icon is not None and icon.is_file():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\DefaultIcon") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(icon))
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base + r"\shell\open\command") as k:
            try:
                if winreg.QueryValueEx(k, "")[0] == command:
                    return True            # already there (Subtle checks at every start)
            except OSError:
                pass
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command)
        return True
    except OSError:
        return False


NATIVE_HOST = "io.github.ddsaamir.subtle"
FIREFOX_EXTENSION_ID = "subtle@ddsaamir.github.io"
# where each browser looks for native messaging hosts (per user)
NATIVE_HOST_KEYS = {
    "chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "chromium": r"Software\Chromium\NativeMessagingHosts",
    "edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    "firefox": r"Software\Mozilla\NativeMessagingHosts",
}


def unpacked_extension_id(folder: Path) -> str:
    """The id Chromium browsers give an extension loaded unpacked from this folder (hash of its path)."""
    p = str(Path(folder).resolve())
    if sys.platform == "win32" and len(p) > 1 and p[1] == ":" and p[0].islower():
        p = p[0].upper() + p[1:]
    data = p.encode("utf-16-le") if sys.platform == "win32" else p.encode("utf-8")
    import hashlib
    return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(data).hexdigest()[:32])


def installed_extension_ids(folder: Path) -> set[str]:
    """Ids of Subtle's extension in the browsers on this PC: the one for `folder`, and any other unpacked
    copy of it the browsers know (loaded from another folder)."""
    ids = {unpacked_extension_id(folder)}
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for browser in ("Google/Chrome", "Chromium", "Microsoft/Edge", "BraveSoftware/Brave-Browser",
                    "Vivaldi", "Opera Software/Opera Stable"):
        root = base / browser / "User Data"
        prefs = list(root.glob("*/Secure Preferences")) + list(root.glob("*/Preferences")) if root.is_dir() else []
        for pref in prefs:
            try:
                import json
                with open(pref, encoding="utf-8") as f:
                    settings = (json.load(f).get("extensions") or {}).get("settings") or {}
                for eid, info in settings.items():
                    path = Path(str(info.get("path") or ""))
                    if info.get("location") == 4 and path.is_absolute() and (path / "manifest.json").is_file():
                        with open(path / "manifest.json", encoding="utf-8") as f:
                            if str(json.load(f).get("name", "")).startswith("Subtle"):
                                ids.add(eid)
            except (OSError, ValueError, AttributeError):
                continue
    return ids


def register_native_host(host_script: Path, out_dir: Path, extension_dir: Path) -> bool:
    """Windows: register native_host.py with Chrome, Chromium, Edge, Brave and Firefox (per user, no admin
    rights), so the extension can start Subtle without asking. Only Subtle's extension may use it."""
    if sys.platform != "win32":
        return False
    try:
        import json
        import winreg
        out_dir.mkdir(parents=True, exist_ok=True)
        launcher = out_dir / "subtle-host.bat"
        python = str(Path(windowless_python()).with_name("python.exe"))      # stdin/stdout: not pythonw
        text = f'@echo off\r\n"{python}" "{host_script}" %*\r\n'
        if not launcher.is_file() or launcher.read_text(encoding="utf-8") != text:
            launcher.write_text(text, encoding="utf-8", newline="")
        common = {"name": NATIVE_HOST, "description": "Starts Subtle for its browser extension",
                  "path": str(launcher), "type": "stdio"}
        manifests = {
            "chromium": dict(common, allowed_origins=sorted(f"chrome-extension://{i}/"
                                                            for i in installed_extension_ids(extension_dir))),
            "firefox": dict(common, allowed_extensions=[FIREFOX_EXTENSION_ID]),
        }
        files = {}
        for kind, m in manifests.items():
            f = out_dir / f"{NATIVE_HOST}.{kind}.json"
            data = json.dumps(m, indent=2)
            if not f.is_file() or f.read_text(encoding="utf-8") != data:
                f.write_text(data, encoding="utf-8")
            files[kind] = str(f)
        for browser, key in NATIVE_HOST_KEYS.items():
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key + "\\" + NATIVE_HOST) as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, files["firefox" if browser == "firefox" else "chromium"])
        return True
    except OSError:
        return False


def startup_shortcut() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Subtle.lnk"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "LaunchAgents" / "io.github.ddsaamir.subtle.plist"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "subtle.desktop"


def make_shortcut(link: Path, target: str, args: str, workdir: str, icon: str = "") -> bool:
    if sys.platform != "win32":
        return _make_launcher(link, target, args, workdir, icon)
    return _make_lnk(link, target, args, workdir, icon)


def _make_launcher(link: Path, target: str, args: str, workdir: str, icon: str = "") -> bool:
    """Linux: a .desktop file; macOS: a LaunchAgent (both start Subtle when you log in)."""
    import shlex
    from xml.sax.saxutils import escape
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        argv = [target] + shlex.split(args)
        if link.suffix == ".plist":
            items = "".join(f"<string>{escape(a)}</string>" for a in argv)
            text = "\n".join([
                '<?xml version="1.0" encoding="UTF-8"?>',
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
                '<plist version="1.0"><dict>',
                "<key>Label</key><string>io.github.ddsaamir.subtle</string>",
                f"<key>ProgramArguments</key><array>{items}</array>",
                f"<key>WorkingDirectory</key><string>{escape(workdir)}</string>",
                "<key>RunAtLoad</key><true/>",
                "</dict></plist>", ""])
        else:
            text = "\n".join([
                "[Desktop Entry]", "Type=Application", "Name=Subtle", "Comment=Real-time subtitles",
                "Exec=" + " ".join(shlex.quote(a) for a in argv), f"Path={workdir}"]
                + ([f"Icon={icon}"] if icon else []) + ["Terminal=false", "X-GNOME-Autostart-enabled=true", ""])
        link.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


def _make_lnk(link: Path, target: str, args: str, workdir: str, icon: str = "") -> bool:
    """Create a .lnk with Windows' own shell object (via PowerShell, no extra packages)."""
    def q(s: str) -> str:
        return s.replace("'", "''")
    ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(str(link))}');"
          f"$s.TargetPath='{q(target)}';$s.Arguments='{q(args)}';$s.WorkingDirectory='{q(workdir)}';"
          + (f"$s.IconLocation='{q(icon)}';" if icon else "") + "$s.Save()")
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, timeout=20, creationflags=NO_WINDOW)
        return r.returncode == 0 and link.exists()
    except (OSError, subprocess.SubprocessError):
        return False


def open_path(path) -> bool:
    try:
        Path(path).mkdir(parents=True, exist_ok=True) if not Path(path).suffix else None
        if sys.platform == "win32":
            os.startfile(str(path))      # Explorer / default app; local only
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, AttributeError):
        return False
