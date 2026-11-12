"""
One-click update ("Update now" on the Home page).

In the app (this file): download the newest release's zip from GitHub, check it against the SHA-256 that
GitHub publishes for it, unpack it into runtime/update/<version>/, then start that version's own
installer/apply_update.py and quit. apply_update.py waits for Subtle to close, backs up the program files,
copies the new ones over (models, settings, the private Python and logs are never touched), installs new
packages if the version needs them, and starts Subtle again; if anything fails it puts the old files back.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

import edition
import winutil
from version import VERSION

ROOT = Path(__file__).resolve().parent.parent
UPDATE_DIR = ROOT / "runtime" / "update"
RELEASES_URL = "https://api.github.com/repos/ddsaamir/subtle/releases/latest"
HEADERS = {"User-Agent": f"Subtle/{VERSION}", "Accept": "application/vnd.github+json"}


def version_tuple(v: str) -> tuple:
    nums = []
    for part in str(v).lstrip("vV").split("-")[0].split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    return tuple(nums + [0] * (3 - len(nums)))


def latest_release() -> dict | None:
    """{"version", "url", "zip": {"name", "url", "size", "sha256"}} of the newest release, or None."""
    try:
        with urllib.request.urlopen(urllib.request.Request(RELEASES_URL, headers=HEADERS), timeout=15) as r:
            data = json.loads(r.read())
    except Exception:
        return None
    version = str(data.get("tag_name") or "").lstrip("vV")
    if not version:
        return None
    want = f"Subtle-Lite-{version}.zip" if edition.LITE else f"Subtle-{version}.zip"
    zip_asset = None
    for a in data.get("assets") or []:
        if a.get("name") == want:
            digest = str(a.get("digest") or "")
            zip_asset = {"name": want, "url": a.get("browser_download_url"), "size": int(a.get("size") or 0),
                         "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else ""}
    return {"version": version, "url": str(data.get("html_url") or ""), "zip": zip_asset}


class Updater:
    def __init__(self, log=print, on_change=None):
        self.log = log
        self.on_change = on_change or (lambda: None)
        self.state = "idle"            # idle | downloading | ready | applying | error
        self.error = ""
        self.done = 0
        self.total = 0
        self.version = ""
        self.src: Path | None = None   # the unpacked new Subtle folder
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        return {"state": self.state, "error": self.error, "done": self.done, "total": self.total,
                "version": self.version}

    def _set(self, state: str, error: str = ""):
        self.state, self.error = state, error
        self.on_change()

    def start(self) -> str:
        """Begin downloading the newest release (in a thread). Returns an error message or ""."""
        if self.state in ("downloading", "applying"):
            return ""
        self._set("downloading")
        self.thread = threading.Thread(target=self._prepare, daemon=True, name="update")
        self.thread.start()
        return ""

    def _prepare(self):
        try:
            info = latest_release()
            if info is None:
                return self._set("error", "Could not reach GitHub to check for the new version.")
            if version_tuple(info["version"]) <= version_tuple(VERSION):
                return self._set("error", f"You already have the newest version ({VERSION}).")
            z = info.get("zip")
            if not z or not z.get("url") or not z.get("sha256"):
                return self._set("error", "This release has no download for automatic updates. Use the download page.")
            self.version, self.total, self.done = info["version"], z["size"], 0
            UPDATE_DIR.mkdir(parents=True, exist_ok=True)
            path = UPDATE_DIR / z["name"]
            part = path.with_suffix(".partial")
            h = hashlib.sha256()
            req = urllib.request.Request(z["url"], headers={"User-Agent": HEADERS["User-Agent"]})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    self.done += len(chunk)
            if h.hexdigest() != z["sha256"].lower():
                part.unlink(missing_ok=True)
                return self._set("error", "The download was damaged (checksum mismatch). Please try again.")
            part.replace(path)
            dest = UPDATE_DIR / info["version"]
            shutil.rmtree(dest, ignore_errors=True)
            with zipfile.ZipFile(path) as zf:
                for name in zf.namelist():                  # never write outside the update folder
                    target = (dest / name).resolve()
                    if not str(target).startswith(str(dest.resolve())):
                        return self._set("error", "The download contains unexpected paths.")
                zf.extractall(dest)
            src = dest / "Subtle"
            ver_file = src / "server" / "version.py"
            if not ver_file.is_file() or f'"{info["version"]}"' not in ver_file.read_text(encoding="utf-8"):
                return self._set("error", "The download is not the expected Subtle version.")
            if not (src / "installer" / "apply_update.py").is_file():
                return self._set("error", "The new version can't be applied automatically. Use the download page.")
            path.unlink(missing_ok=True)
            self.src = src
            self.log(f"[update] Subtle {info['version']} downloaded and checked")
            self._set("ready")
        except Exception as e:
            self._set("error", f"Update failed: {type(e).__name__}: {e}")

    def launch_apply(self) -> str:
        """Start the new version's apply_update.py; the caller then quits Subtle."""
        if self.state != "ready" or self.src is None:
            return "The update is not ready."
        py = winutil.console_python()
        args = [py, "-u", str(self.src / "installer" / "apply_update.py"), "--src", str(self.src), "--dst", str(ROOT),
                "--pid", str(os.getpid()), "--from-version", VERSION]
        kwargs = {"cwd": str(self.src)}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x00000010            # CREATE_NEW_CONSOLE: the user sees the progress
        else:
            kwargs["start_new_session"] = True
            kwargs["stdout"] = kwargs["stderr"] = subprocess.DEVNULL
        try:
            subprocess.Popen(args, **kwargs)
        except OSError as e:
            self._set("error", f"Could not start the update: {e}")
            return self.error
        self.log(f"[update] applying {self.version}: Subtle closes and starts again")
        self._set("applying")
        return ""
