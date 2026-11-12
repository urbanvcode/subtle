"""
Puts a downloaded Subtle version in place (started by the app's "Update now", from the new version's
unpacked folder). Steps:

  1. wait until the running Subtle has closed
  2. back up the program files that will be replaced (runtime/update/backup-<old version>/)
  3. copy the new program files over; never touched: models/, runtime/, bin/, logs/, transcripts/,
     settings.json and edition.txt
  4. if the new version needs other packages or another translator build, run the installer unattended
  5. start Subtle again
If step 3 or 4 fails, the old files are put back and the old version starts again.
"""

import argparse
import filecmp
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

KEEP = {"models", "runtime", "bin", "logs", "transcripts", "settings.json", "edition.txt", ".git"}
INSTALL_INPUTS = ["installer/requirements.txt", "installer/requirements-extras.txt", "installer/versions.json",
                  "installer/versions-unix.json"]

ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True)
ap.add_argument("--dst", required=True)
ap.add_argument("--pid", type=int, default=0)
ap.add_argument("--from-version", default="old")
ap.add_argument("--no-restart", action="store_true")
args = ap.parse_args()
SRC, DST = Path(args.src).resolve(), Path(args.dst).resolve()
LOG = DST / "logs" / "update.log"
LOG.parent.mkdir(parents=True, exist_ok=True)
logf = open(LOG, "a", encoding="utf-8")


def say(msg: str):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(msg, flush=True)
    logf.write(line + "\n")
    logf.flush()


def new_version() -> str:
    for line in (SRC / "server" / "version.py").read_text(encoding="utf-8").splitlines():
        if line.startswith("VERSION"):
            return line.split('"')[1]
    return "?"


def program_files(root: Path) -> list[Path]:
    """Files of a Subtle folder that belong to the program (relative paths)."""
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if rel.parts[0] in KEEP or "__pycache__" in rel.parts or not p.is_file():
            continue
        out.append(rel)
    return out


def wait_for_exit(pid: int, timeout: float = 60):
    if not pid:
        return
    try:
        import psutil
    except ImportError:
        time.sleep(5)
        return
    t0 = time.time()
    while time.time() - t0 < timeout:
        if not psutil.pid_exists(pid):
            break
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.Error:
            break
        time.sleep(0.3)
    time.sleep(1.0)                          # its engine and translator close with it


def needs_installer(backup: Path) -> bool:
    for rel in INSTALL_INPUTS:
        new, old = SRC / rel, backup / rel
        if new.is_file() != old.is_file() or (new.is_file() and not filecmp.cmp(new, old, shallow=False)):
            return True
    return False


def current_variant() -> str:
    try:
        return (DST / "bin" / "llama" / "subtle-build.txt").read_text(encoding="utf-8").split()[1]
    except (OSError, IndexError):
        return ""


def run_installer() -> bool:
    variant = current_variant()
    if sys.platform == "win32":
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DST / "installer" / "install.ps1"),
               "-Unattended", "-NoShortcuts", "-NoLaunch"] + (["-Variant", variant] if variant else [])
    else:
        gpu = {"linux-cuda13": "nvidia", "linux-cuda12": "nvidia-old", "linux-rocm": "amd", "linux-vulkan": "intel",
               "linux-cpu": "cpu"}.get(variant)
        cmd = ["bash", str(DST / "install.sh")] + (["--gpu", gpu] if gpu else [])
    say("Installing what the new version needs (this can take a few minutes)...")
    return subprocess.run(cmd, cwd=str(DST)).returncode == 0


def start_subtle():
    if args.no_restart:
        return
    if sys.platform == "win32":
        pyw = DST / "runtime" / "venv" / "Scripts" / "pythonw.exe"
        if not pyw.is_file():
            pyw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pyw), str(DST / "server" / "supervisor.py")], cwd=str(DST / "server"),
                         creationflags=0x00000008 | 0x00000200)        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        subprocess.Popen(["bash", str(DST / "subtle.sh")], cwd=str(DST), start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def copy_tree(files: list[Path], src: Path, dst: Path):
    for rel in files:
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src / rel, target)


def main():
    ver = new_version()
    say(f"Updating Subtle {args.from_version} -> {ver}. Please don't close this window.")
    wait_for_exit(args.pid)

    backup = DST / "runtime" / "update" / f"backup-{args.from_version}"
    shutil.rmtree(backup, ignore_errors=True)
    old_files = program_files(DST)
    copy_tree(old_files, DST, backup)
    say(f"Backed up {len(old_files)} program files.")

    try:
        new_files = program_files(SRC)
        copy_tree(new_files, SRC, DST)
        if sys.platform != "win32":
            for sh in ("install.sh", "subtle.sh"):
                if (DST / sh).is_file():
                    os.chmod(DST / sh, 0o755)
        say(f"Copied {len(new_files)} new program files.")
        if needs_installer(backup) and not run_installer():
            raise RuntimeError("the installer reported an error (see logs/install.log)")
    except Exception as e:
        say(f"Update failed: {e}. Putting the old version back...")
        copy_tree(old_files, backup, DST)
        say("The old version is back.")
        start_subtle()
        if sys.platform == "win32":
            input("Press Enter to close this window.")
        return 1

    say(f"Subtle {ver} is installed. Starting it...")
    start_subtle()
    say("Done. In your browser, the Subtle extension updates itself the next time you open it.")
    time.sleep(3)
    return 0


sys.exit(main())
