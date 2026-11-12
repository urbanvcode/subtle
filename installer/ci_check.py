"""
Checks run by GitHub Actions after ./install.sh (Linux and macOS): download a small speech model with
Subtle's own downloader, run the smoke test, start Subtle and ask it for its version, and start the
built-in translator binary.

usage: runtime/venv/bin/python installer/ci_check.py
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

import downloads  # noqa: E402

MODEL = "whisper-small"


def step(text):
    print(f"\n==> {text}", flush=True)


step(f"Downloading {MODEL} with Subtle's downloader")
dl = downloads.Downloader()
err = dl.start(MODEL)
if err:
    sys.exit(f"download refused: {err}")
t0 = time.time()
while True:
    st = next((j for j in dl.status() if j.get("id") == MODEL), {})
    if st.get("state") in ("done", "error", "cancelled"):
        break
    if time.time() - t0 > 1200:
        sys.exit("download took too long")
    time.sleep(2)
print(st, flush=True)
if st.get("state") != "done":
    sys.exit("download failed")

step("Smoke test (speech engine)")
subprocess.run([sys.executable, "smoke_test.py", "whisper-small"], cwd=SERVER, check=True)

step("Learning-mode readings")
import readings  # noqa: E402
print(readings.annotate("東京に行きましょう。", "ja", "romaji"))
print(readings.annotate("我们去吃饭吧。", "zh", "pinyin"))

step("Built-in translator binary")
exe = ROOT / "bin" / "llama" / "llama-server"
out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=60)
print((out.stdout + out.stderr).strip()[-300:])
if out.returncode != 0:
    sys.exit("llama-server does not start")

step("Starting Subtle (no window) and asking for its version")
sup = subprocess.Popen([sys.executable, "supervisor.py", "--port", "8799", "--no-tray", "--no-window"], cwd=SERVER)
try:
    for _ in range(60):
        time.sleep(1)
        try:
            with urllib.request.urlopen("http://127.0.0.1:8799/api/ping", timeout=2) as r:
                print(json.loads(r.read()))
                break
        except OSError:
            continue
    else:
        sys.exit("Subtle did not answer")
finally:
    sup.terminate()
print("\nAll checks passed")
