"""
Model downloads for the control panel (standard library only, runs in the supervisor).

Files come from huggingface.co at the exact revision pinned in models/catalog.json, are written to
a ".partial" name first (so a half-finished download is never mistaken for a model), resume where
they stopped, and large files are checked against their sha256 before they are put in place.
"""

import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from modelstore import LLM_DIR, MODELS_DIR, SPEAKER_DIR, WHISPER_DIR

CATALOG_FILE = MODELS_DIR / "catalog.json"
CHUNK = 1 << 20
USER_AGENT = "Subtle-model-downloader"


def load_catalog() -> list[dict]:
    try:
        with open(CATALOG_FILE, encoding="utf-8") as f:
            return json.load(f).get("models", [])
    except (OSError, ValueError):
        return []


def catalog_entry(model_id: str) -> dict | None:
    return next((m for m in load_catalog() if m["id"] == model_id), None)


def destination(entry: dict) -> Path:
    if entry["kind"] == "whisper":
        return WHISPER_DIR / entry["target"]
    if entry["kind"] == "llm":
        return LLM_DIR / entry["target"]
    return SPEAKER_DIR / entry["target"]


def is_installed(entry: dict) -> bool:
    """In Subtle's models folder (the panel also counts the same model found in the HF cache / LM Studio)."""
    dest = destination(entry)
    if entry["kind"] == "whisper":
        return dest.is_dir() and all((dest / f["name"]).is_file() for f in entry["files"])
    return dest.is_file()


class Cancelled(Exception):
    pass


class Job:
    def __init__(self, entry: dict):
        self.entry = entry
        self.id = entry["id"]
        self.total = entry["size"]
        self.done = 0
        self.state = "queued"         # queued | downloading | verifying | done | error | cancelled
        self.error = ""
        self.started = time.monotonic()
        self.speed = 0.0
        self.cancel_event = threading.Event()

    def snapshot(self) -> dict:
        return {"id": self.id, "name": self.entry["name"], "state": self.state, "error": self.error,
                "done": self.done, "total": self.total, "speed": round(self.speed)}


class Downloader:
    """One download at a time (keeps the disk and the connection from thrashing); others queue."""

    def __init__(self, on_change=None):
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.queue: list[Job] = []
        self.worker: threading.Thread | None = None
        self.on_change = on_change or (lambda: None)

    def status(self) -> list[dict]:
        with self.lock:
            return [j.snapshot() for j in self.jobs.values()]

    def start(self, model_id: str) -> str:
        entry = catalog_entry(model_id)
        if entry is None:
            return "unknown model"
        if is_installed(entry):
            return "already installed"
        need = entry["size"] - self._partial_bytes(entry) + 200_000_000
        free = shutil.disk_usage(MODELS_DIR).free
        if free < need:
            return f"not enough disk space: needs {need / 1e9:.1f} GB, {free / 1e9:.1f} GB free"
        with self.lock:
            job = self.jobs.get(model_id)
            if job is not None and job.state in ("queued", "downloading", "verifying"):
                return ""
            job = Job(entry)
            self.jobs[model_id] = job
            self.queue.append(job)
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._run, daemon=True, name="downloads")
                self.worker.start()
        self.on_change()
        return ""

    def cancel(self, model_id: str):
        with self.lock:
            job = self.jobs.get(model_id)
        if job is not None:
            job.cancel_event.set()
            if job.state == "queued":
                job.state = "cancelled"
        self.on_change()

    def clear_finished(self):
        with self.lock:
            for k in [k for k, j in self.jobs.items() if j.state in ("done", "error", "cancelled")]:
                del self.jobs[k]

    # ------------------------------------------------------------------------------------------
    def _partial_bytes(self, entry: dict) -> int:
        dest = destination(entry)
        total = 0
        if entry["kind"] == "whisper":
            tmp = dest.with_name(dest.name + ".partial")
            for f in entry["files"]:
                for p in (tmp / f["name"], tmp / (f["name"] + ".part")):
                    if p.is_file():
                        total += p.stat().st_size
        else:
            p = dest.with_name(dest.name + ".partial")
            if p.is_file():
                total += p.stat().st_size
        return total

    def _run(self):
        while True:
            with self.lock:
                job = next((j for j in self.queue if j.state == "queued"), None)
                if job is None:
                    self.queue.clear()
                    return
            try:
                job.state = "downloading"
                self.on_change()
                self._download(job)
                job.state = "done"
            except Cancelled:
                job.state = "cancelled"
            except Exception as e:
                job.state = "error"
                job.error = f"{type(e).__name__}: {e}"
            self.on_change()

    def _download(self, job: Job):
        entry = job.entry
        dest = destination(entry)
        dest.parent.mkdir(parents=True, exist_ok=True)
        job.done = self._partial_bytes(entry)
        if entry["kind"] == "whisper":
            tmp_dir = dest.with_name(dest.name + ".partial")
            tmp_dir.mkdir(exist_ok=True)
            for f in entry["files"]:
                final = tmp_dir / f["name"]
                if final.is_file() and final.stat().st_size == f["size"]:
                    continue
                self._fetch(job, entry, f, tmp_dir / (f["name"] + ".part"))
                os.replace(tmp_dir / (f["name"] + ".part"), final)
            if dest.exists():
                shutil.rmtree(dest)
            os.replace(tmp_dir, dest)
        else:
            f = entry["files"][0]
            part = dest.with_name(dest.name + ".partial")
            self._fetch(job, entry, f, part)
            os.replace(part, dest)

    def _fetch(self, job: Job, entry: dict, f: dict, part: Path):
        url = (f"https://huggingface.co/{entry['repo']}/resolve/{entry['revision']}/"
               f"{urllib.parse.quote(f['name'])}")
        have = part.stat().st_size if part.is_file() else 0
        if have > f["size"]:
            part.unlink()
            job.done -= have
            have = 0
        if have < f["size"]:
            headers = {"User-Agent": USER_AGENT}
            if have:
                headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                if have and resp.status != 206:      # server ignored the range: start over
                    job.done -= have
                    have = 0
                mode = "ab" if have else "wb"
                last_t, last_done = time.monotonic(), job.done
                with open(part, mode) as out:
                    while True:
                        if job.cancel_event.is_set():
                            raise Cancelled()
                        chunk = resp.read(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
                        job.done += len(chunk)
                        now = time.monotonic()
                        if now - last_t >= 0.5:
                            job.speed = (job.done - last_done) / (now - last_t)
                            last_t, last_done = now, job.done
                            self.on_change()
        if part.stat().st_size != f["size"]:
            raise IOError(f"{f['name']}: download incomplete ({part.stat().st_size} of {f['size']} bytes)")
        if f.get("sha256"):
            job.state = "verifying"
            self.on_change()
            h = hashlib.sha256()
            with open(part, "rb") as fh:
                while True:
                    if job.cancel_event.is_set():
                        raise Cancelled()
                    b = fh.read(CHUNK * 8)
                    if not b:
                        break
                    h.update(b)
            if h.hexdigest() != f["sha256"]:
                part.unlink()
                raise IOError(f"{f['name']}: checksum mismatch (file removed, please download again)")
            job.state = "downloading"


def delete_model(kind: str, path: str) -> str:
    """Delete a model, but only from Subtle's own models/ folders (never the HF cache or LM Studio)."""
    p = Path(path).resolve()
    root = {"whisper": WHISPER_DIR, "llm": LLM_DIR, "speaker": SPEAKER_DIR}.get(kind)
    if root is None or root.resolve() not in p.parents:
        return "only models in Subtle's models folder can be deleted here"
    try:
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()
    except OSError as e:
        return f"could not delete: {e}"
    return ""
