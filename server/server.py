"""
Subtle - local GPU subtitle server (a universal open-source video subtitle extension).

Receives 16 kHz mono float32 PCM from the browser extension over a WebSocket, cuts it into
speech segments with Silero VAD and turns each segment into a subtitle:

  whisper mode : Whisper translates the speech straight to English          (fastest)
  llm mode     : Whisper writes down the speech, an LLM (LM Studio) translates it into
                 any target language, using the previous lines as context    (best quality)

Protocol (ws://127.0.0.1:8765):
  client -> server  binary : float32 little-endian PCM, 16 kHz, mono
  client -> server  text   : {"type": "pause" | "play" | "seek" | "reset"}
                             {"type": "config", "language", "target", "mode", "asr_model", "llm_model", "llm_url"}
                             {"type": "info", "llm_url"?}         -> list available models
  server -> client  text   : {"type": "sub", "text", "src"?, "final", "id", "ms", "t"}
                               ms = compute time of the line, t = position (seconds of audio
                               received on this connection) where the line's speech ended
                             {"type": "clear"} | {"type": "status", "message", "level"}
                             {"type": "ready"} | {"type": "info", ...}

How a line is produced (see Session):
  * while someone is speaking, a preview of the line so far is shown every PARTIAL_EVERY seconds;
  * as soon as the speaker has been quiet for EARLY_SILENCE the whole line is translated and
    shown as a preview; if the silence lasts until END_SILENCE that same result becomes the
    finished line without being computed again;
  * previews are cancelled the moment a finished line needs the GPU or the LLM.
"""

import argparse
import asyncio
import collections
import ctypes
import datetime
import hmac
import itertools
import json
import logging
import logging.handlers
import os
import re
import sys
import threading
import time
import urllib.parse
import warnings
from pathlib import Path

from version import VERSION     # noqa: E402  (keep in step with extension/manifest.json)

ROOT = Path(__file__).resolve().parent.parent
TRANSCRIPT_DIR = ROOT / "transcripts"
LOG_DIR = ROOT / "logs"

warnings.filterwarnings("ignore")  # library deprecation noise, not actionable here
# Japanese/Chinese/Korean text in the log must never crash a line (Windows defaults to cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np
import torch
from silero_vad import load_silero_vad
from websockets.asyncio.server import serve

from engines import (Cancelled, EngineCache, EngineClosed, LLMCall, SpeakerEncoder, SpeakerTracker,
                     cached_whisper_models, default_asr_model, default_fits, list_llm_models, llm_translate,
                     model_fits, needs_preparing)

SR = 16000
FRAME = 512                       # Silero VAD frame size at 16 kHz (32 ms)
FRAME_S = FRAME / SR

# Segmentation tuning (seconds)
START_PROB = 0.4                  # VAD probability that starts speech
KEEP_PROB = 0.28                  # probability that keeps speech going

# Automatic gain, so whispers and quiet scenes are heard (applied before VAD and Whisper).
AGC_TARGET = 0.08                 # RMS the recent loudest audio is raised to
AGC_MAX_GAIN = 8.0                # at most +18 dB
AGC_DECAY = 0.996                 # per 32 ms frame: the reference level halves in ~5 s of quiet
AGC_FLOOR = 0.002                 # below this the input is treated as silence (not boosted further)
SEGMENT_RMS = 0.1                 # each line is normalized to this loudness before Whisper
END_SILENCE = 0.30                # silence that ends a segment (short, so speaker turns split)
EARLY_SILENCE = 0.12              # silence after which the line is already translated (as a preview)
TAIL_FRAMES = 4                   # frames of trailing silence kept, so the last word isn't clipped
MAX_SEGMENT = 5.0                 # cut long, unbroken speech at its quietest point around here
MIN_SPEECH = 0.35                 # ignore blips shorter than this
PREROLL = 0.25                    # audio kept from before speech starts
PARTIAL_EVERY = 0.5               # how often to show an in-progress line
PREVIEW_RATES = {"normal": PARTIAL_EVERY, "slow": 1.0, "off": None}   # control panel "Previews" setting
PARTIAL_MIN = 0.9                 # only show partials once speech is this long
CONTEXT_LINES = 4                 # earlier lines given to the LLM
MAX_BACKLOG = 8.0                 # a finished line that waited this long (slow LLM) is dropped, not shown late
TRIM_AFTER = 8.0                  # seconds after a model has loaded until its start-up memory is handed back
VERBOSE = False                   # --verbose also logs the in-progress previews
DEFAULT_LLM_URL = "http://127.0.0.1:1234/v1"

EARLY_FRAMES = max(1, round(EARLY_SILENCE / FRAME_S))

# Privacy: subtitle text lives in memory only. The one file written by default is an error log
# (logs/errors.log, capped at ~1 MB) that never contains subtitle text. Transcripts are written
# only when the user turns on "Save transcripts" in the popup.
_errlog = logging.getLogger("subtle.errors")
_errlog.propagate = False


def setup_error_log():
    try:
        LOG_DIR.mkdir(exist_ok=True)
        h = logging.handlers.RotatingFileHandler(LOG_DIR / "errors.log", maxBytes=1_000_000, backupCount=1,
                                                 encoding="utf-8", delay=True)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        _errlog.addHandler(h)
        _errlog.setLevel(logging.WARNING)
    except Exception:
        pass


def report(msg: str):
    """Print an error/warning and keep it in the error log. Never pass subtitle text here."""
    print(msg, flush=True)
    try:
        _errlog.warning(msg)
    except Exception:
        pass
SEG_IDS = itertools.count(1)      # line ids keep growing across connections (the overlay compares them)
SPEAKER_USERS: set = set()        # sessions with "Identify speakers" on (the model is freed when none are)
ACTIVE_MODELS: dict = {}          # session -> Whisper model it uses (never unloaded under another tab)

# Lines Whisper is known to invent from music, noise or silence (English and Japanese).
HALLUCINATIONS = re.compile(
    r"(thank(s| you)( so much| very much)? for watching|please subscribe|subscribe to|"
    r"like and subscribe|see you (in the )?next (time|video)|transcri(bed|ption) by|"
    r"subtitles? by|amara\.org|translated by|thank you for listening|"
    r"ご視聴ありがとうございました|チャンネル登録|字幕(制作|提供|by)|"
    r"请不吝点赞|订阅|字幕由|구독|시청해 주셔서)",
    re.IGNORECASE,
)


def clean(text: str) -> str:
    text = (text or "").strip()
    if not text or not re.search(r"\w", text):
        return ""
    if HALLUCINATIONS.search(text):
        return ""
    words = text.split()
    if len(words) >= 6 and len(set(w.lower() for w in words)) / len(words) < 0.35:
        return ""  # repetition loop
    if len(text) >= 12 and len(set(text)) <= 3:
        return ""  # e.g. "ああああああ"
    return text


class Config:
    def __init__(self):
        self.language: str | None = "ja"     # spoken language; None = auto-detect
        self.target = "en"                   # subtitle language
        self.mode = "whisper"                # "whisper" | "llm"
        self.asr_model = "auto"
        self.asr_defaults: dict = {}         # Settings: speech model per spoken language ("zh", "ja", "ko", "other")
        self.llm_model = ""
        self.llm_url = DEFAULT_LLM_URL
        self.save_transcript = False         # off by default: nothing written to disk
        self.page_title = ""                 # only used as the transcript's heading
        self.speakers = False                # identify speakers (off by default; voiceprints in memory only)
        self.partial_every = PARTIAL_EVERY   # previews of a line in progress; None = off (power saver)
        self.learning = False                # learning mode: the original line comes with word readings
        self.reading = "furigana"            # "furigana" | "romaji" (Japanese); Chinese: pinyin, Korean: romanization

    @property
    def use_llm(self) -> bool:
        return self.mode == "llm" or self.target != "en"

    def update(self, msg: dict):
        if "language" in msg:
            lang = str(msg["language"] or "ja").strip().lower()
            self.language = None if lang == "auto" else lang
        if msg.get("target"):
            self.target = str(msg["target"]).strip().lower()
        if msg.get("mode") in ("whisper", "llm"):
            self.mode = msg["mode"]
        if msg.get("asr_model"):
            self.asr_model = str(msg["asr_model"])
        if isinstance(msg.get("asr_defaults"), dict):
            self.asr_defaults = {str(k): str(v) for k, v in msg["asr_defaults"].items()}
        if "llm_model" in msg:
            self.llm_model = str(msg["llm_model"] or "")
        if msg.get("llm_url"):
            self.llm_url = str(msg["llm_url"]).strip()
        if "save_transcript" in msg:
            self.save_transcript = bool(msg["save_transcript"])
        if "speakers" in msg:
            self.speakers = bool(msg["speakers"])
        if msg.get("page_title"):
            self.page_title = str(msg["page_title"])[:200]
        if "learning" in msg:
            self.learning = bool(msg["learning"])
        if msg.get("reading") in ("furigana", "romaji", "off"):
            self.reading = msg["reading"]
        if msg.get("previews") in PREVIEW_RATES:
            self.partial_every = PREVIEW_RATES[msg["previews"]]

    def describe(self, asr: str) -> str:
        src = self.language or "auto"
        if self.use_llm:
            return f"{src} -> {self.target} via {asr.split('/')[-1]} + {self.llm_model or '(no LLM picked)'}"
        return f"{src} -> en via {asr.split('/')[-1]}"


class Job:
    """One piece of audio to turn into a subtitle: a preview of a line in progress, or a finished line."""

    __slots__ = ("audio", "frames", "final", "full", "epoch", "seg_id", "end_t", "created",
                 "cancelled", "llm_call", "task", "ctx_version")

    def __init__(self, audio, frames: int, final: bool, full: bool, epoch: int, seg_id: int, end_t: float):
        self.audio = audio            # None: nothing to recognise, just tell the client the line is over
        self.frames = frames          # VAD frames in `audio`
        self.final = final
        self.full = full              # preview of what is probably the whole line (speaker went quiet)
        self.epoch = epoch
        self.seg_id = seg_id
        self.end_t = end_t
        self.created = time.monotonic()
        self.cancelled = False
        self.llm_call: LLMCall | None = None
        self.task: asyncio.Task | None = None
        self.ctx_version = -1         # LLM context the translation was made with

    def cancel(self):
        self.cancelled = True
        if self.llm_call is not None:
            self.llm_call.cancel()


class Session:
    """State for one browser tab connection."""

    def __init__(self, ws, cache: EngineCache):
        self.ws = ws
        self.cache = cache
        self.cfg = Config()
        self.vad = None                # loaded with the first audio (the popup's connections never send any)
        self.leftover = np.empty(0, dtype=np.float32)
        self.preroll = collections.deque(maxlen=max(1, int(PREROLL / FRAME_S)))
        self.speech: list[np.ndarray] = []
        self.probs: list[float] = []
        self.in_speech = False
        self.silence_frames = 0
        self.voiced_frames = 0
        self.samples = 0               # audio received so far, in samples (the clock for "t")
        self.paused = False
        self.epoch = 0                 # bumped on pause/seek so stale results are dropped
        self.seg_id = 0
        self.final_seg = 0             # newest line that was finished: its previews are stale from then on
        self.previewed_seg = 0         # newest line a preview was started for
        self.shown_preview = None      # (line id, text, src) of the last preview sent
        self.last_partial = 0.0
        self.preview: Job | None = None    # the preview being computed (a new one is skipped meanwhile)
        self.candidate: Job | None = None  # newest "whole line" preview; reused if the line ends there
        self.agc_level = AGC_TARGET    # recent loudness reference for automatic gain
        self.last_text = ""            # last finished translation (previews repeating it are dropped)
        # Finished lines: speech recognition runs in order (asr_lock) and can already start for the
        # next line while the LLM is still translating the previous one; out_lock keeps translation
        # (which reads the context) and sending in order.
        self.asr_lock = asyncio.Lock()
        self.out_lock = asyncio.Lock()
        self.context: collections.deque[tuple[str, str]] = collections.deque(maxlen=CONTEXT_LINES)
        self.ctx_version = 0
        # Translations made so far for the line in progress: (source text, context version or -1) -> text.
        # While someone speaks the speech model often writes down the same words again (nothing new was
        # said in the last half second); the translator is not asked again for the same words.
        self.translations: dict[tuple[str, int], str] = {}
        self.send_lock = asyncio.Lock()
        self.llm_warned = False
        self.tasks: set[asyncio.Task] = set()
        self.transcript = None         # open file while "Save transcripts" is on
        self.tracker = SpeakerTracker()  # voices heard this session (memory only)
        self.speaker_error = False
        self.feed_time = 0.0           # event-loop time spent on gain + VAD (reported with --verbose)
        self.feed_max = 0.0
        self.feed_frames = 0

    def asr_model(self) -> str:
        cfg = self.cfg
        # 1. the model picked in the popup, wherever it can do the job (Parakeet and SenseVoice only write
        #    speech down in their own languages: they need the Best engine)
        if cfg.asr_model and cfg.asr_model != "auto" and model_fits(cfg.asr_model, cfg.language, cfg.use_llm):
            return cfg.asr_model
        # 2. the default chosen in Settings for this spoken language, if it is installed and fits
        models = cached_whisper_models()
        pick = cfg.asr_defaults.get(cfg.language if cfg.language in ("zh", "ja", "ko") else "other", "auto")
        if pick and pick != "auto" and pick in models and default_fits(pick, cfg.language, cfg.use_llm):
            return pick
        # 3. Subtle's own choice
        return default_asr_model(models, cfg.use_llm, self.cache.loaded_ids(), cfg.language)

    def models_of_other_tabs(self) -> set[str]:
        return {m for s, m in ACTIVE_MODELS.items() if s is not self}

    def get_engine(self, model_id: str):
        """The loaded engine for model_id, loading it if needed without evicting another tab's model."""
        return self.cache.get(model_id, self.models_of_other_tabs())

    # ---- control -------------------------------------------------------
    def reset_audio(self):
        self.leftover = np.empty(0, dtype=np.float32)
        self.preroll.clear()
        self.speech = []
        self.probs = []
        self.in_speech = False
        self.silence_frames = 0
        self.voiced_frames = 0
        self.candidate = None
        if self.vad is not None:
            self.vad.reset_states()

    def cancel_preview(self):
        if self.preview is not None:
            self.preview.cancel()
            self.preview = None

    def clear_context(self):
        self.context.clear()
        self.ctx_version += 1
        self.translations.clear()

    async def control(self, msg: dict):
        kind = msg.get("type")
        if kind == "config":
            self.cfg.update(msg)
            self.clear_context()
            self.candidate = None      # made with the old settings
            self.llm_warned = False
            if not self.cfg.save_transcript:
                self.close_transcript()
            if self.cfg.speakers:
                SPEAKER_USERS.add(self)
                await self.ensure_speaker_model()
            else:
                self.tracker.reset()   # turning it off forgets every voiceprint
                await self.release_speaker_model()
            asr = self.asr_model()
            ACTIVE_MODELS[self] = asr
            saving = "  (saving transcript)" if self.cfg.save_transcript else ""
            print(f"[conn] {self.cfg.describe(asr)}{saving}", flush=True)
            await self.ensure_model(asr)
            await asyncio.to_thread(self.cache.trim, set(ACTIVE_MODELS.values()))
        elif kind == "info":
            await self.send(await asyncio.to_thread(self.info, msg.get("llm_url") or self.cfg.llm_url))
        elif kind == "pause":
            # Stop instantly: drop the half-heard line and anything still on the GPU.
            self.paused = True
            self.epoch += 1
            self.cancel_preview()
            self.reset_audio()
        elif kind == "play":
            if self.paused:            # 'playing' also fires after buffering; don't drop speech then
                self.paused = False
                self.reset_audio()
        elif kind in ("seek", "reset"):
            self.epoch += 1
            self.cancel_preview()
            self.clear_context()
            self.last_text = ""
            self.reset_audio()
            await self.send({"type": "clear"})

    def info(self, llm_url: str) -> dict:
        asr = cached_whisper_models()
        try:
            llms, llm_error = list_llm_models(llm_url), ""
        except Exception as e:
            llms, llm_error = [], f"LLM server not reachable at {llm_url} ({type(e).__name__})"
        return {
            "type": "info",
            "version": VERSION,
            "transcript_dir": str(TRANSCRIPT_DIR),
            "asr_models": asr,
            "asr_loaded": self.cache.loaded_ids(),
            "llm_models": llms,
            "llm_error": llm_error,
            "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU",
        }

    async def ensure_model(self, model_id: str):
        if self.cache.loaded(model_id):
            return
        name = model_id.split("/")[-1]
        first = await asyncio.to_thread(needs_preparing, model_id)
        await self.status(f"Preparing {name} for first use (about 15 seconds)…" if first else f"Loading {name}…", "busy")
        try:
            await asyncio.to_thread(self.get_engine, model_id)
            await self.status("Ready", "ok")
        except Exception as e:
            report(f"[error] could not load {model_id}: {type(e).__name__}: {e}")
            await self.status(f"Could not load {model_id}: {e}", "error")

    async def ensure_speaker_model(self):
        if SpeakerEncoder._instance is not None:
            return
        try:
            await asyncio.to_thread(SpeakerEncoder.get)
        except Exception as e:
            self.cfg.speakers = False
            report(f"[error] speaker identification unavailable: {type(e).__name__}: {e}")
            await self.status("Speaker identification unavailable - see the server window", "error")

    async def release_speaker_model(self):
        SPEAKER_USERS.discard(self)
        if not SPEAKER_USERS and SpeakerEncoder._instance is not None:
            await asyncio.to_thread(SpeakerEncoder.release)

    async def voiceprint(self, job: Job):
        if not self.cfg.speakers or job.audio is None or SpeakerEncoder._instance is None:
            return None
        try:
            return await asyncio.to_thread(SpeakerEncoder._instance.embed, job.audio)
        except EngineClosed:
            return None
        except Exception as e:
            if not self.speaker_error:
                self.speaker_error = True
                report(f"[error] voiceprint: {type(e).__name__}: {e}")
            return None

    async def status(self, message: str, level: str = "ok"):
        await self.send({"type": "status", "message": message, "level": level})

    # ---- audio ---------------------------------------------------------
    def feed(self, pcm: np.ndarray):
        if self.paused:
            self.samples += pcm.size
            return
        t0 = time.perf_counter()
        first = self.vad is None
        if first:
            self.vad = load_silero_vad()
        buf = np.concatenate([self.leftover, pcm]) if self.leftover.size else pcm
        start = self.samples - self.leftover.size       # stream position of buf[0]
        self.samples += pcm.size
        n = (buf.size // FRAME) * FRAME
        self.leftover = buf[n:].copy()
        with torch.inference_mode():
            for i in range(0, n, FRAME):
                self.frame(buf[i:i + FRAME], start + i + FRAME)
        if not first:                  # the first chunk also loads and warms up the VAD (~50 ms, once)
            dt = time.perf_counter() - t0
            self.feed_time += dt
            self.feed_max = max(self.feed_max, dt)
            self.feed_frames += n // FRAME

    def agc(self, f: np.ndarray) -> np.ndarray:
        rms = float(np.sqrt(np.dot(f, f) / f.size))
        if rms > self.agc_level:
            self.agc_level += 0.5 * (rms - self.agc_level)        # fast attack
        else:
            self.agc_level = max(AGC_FLOOR, self.agc_level * AGC_DECAY)  # slow release
        gain = min(AGC_MAX_GAIN, max(1.0, AGC_TARGET / self.agc_level))
        return np.clip(f * gain, -1.0, 1.0) if gain > 1.0 else f

    def frame(self, f: np.ndarray, pos: int):
        """f: one VAD frame; pos: stream position (samples) of its end."""
        f = self.agc(f)
        prob = self.vad(torch.from_numpy(f), SR).item()

        if not self.in_speech:
            self.preroll.append(f)
            if prob >= START_PROB:
                self.in_speech = True
                self.speech = list(self.preroll)
                self.probs = [0.0] * len(self.speech)
                self.preroll.clear()
                self.silence_frames = 0
                self.voiced_frames = 1
                self.seg_id = next(SEG_IDS)
                self.candidate = None
                # the first preview comes PARTIAL_MIN into the line, whatever the previous line did
                self.last_partial = 0.0
            return

        self.speech.append(f)
        self.probs.append(prob)
        if prob >= KEEP_PROB:
            self.voiced_frames += 1
            self.silence_frames = 0
        else:
            self.silence_frames += 1

        duration = len(self.speech) * FRAME_S
        if self.silence_frames * FRAME_S >= END_SILENCE:
            self.finish_segment(pos)
        elif duration >= MAX_SEGMENT:
            self.split_long_segment(pos)
        elif self.silence_frames == EARLY_FRAMES:
            # The speaker went quiet: translate the whole line now. If the silence lasts, this
            # result is the finished line (finish_segment cuts the audio at the same frame).
            if self.voiced_frames * FRAME_S >= MIN_SPEECH:
                keep = len(self.speech) - max(0, self.silence_frames - TAIL_FRAMES)
                self.start_preview(keep, pos, full=True)
        elif (self.cfg.partial_every is not None and self.silence_frames < EARLY_FRAMES
              and duration >= PARTIAL_MIN and self.preview is None
              and time.monotonic() - self.last_partial >= self.cfg.partial_every):
            self.start_preview(len(self.speech), pos, full=False)

    def split_long_segment(self, pos: int):
        """Unbroken speech hit MAX_SEGMENT: cut at the quietest frame in its second half, keep the rest."""
        n = len(self.speech)
        lo = n // 2
        cut = max(lo + int(np.argmin(self.probs[lo:])), lo + 1)
        head, tail, tail_probs = self.speech[:cut], self.speech[cut:], self.probs[cut:]
        self.start_final(Job(np.concatenate(head), cut, True, True, self.epoch, self.seg_id, (pos - (n - cut) * FRAME) / SR))
        self.seg_id = next(SEG_IDS)
        self.speech = tail
        self.probs = tail_probs
        self.voiced_frames = sum(p >= KEEP_PROB for p in tail_probs)
        self.silence_frames = 0
        self.last_partial = time.monotonic()

    def finish_segment(self, pos: int):
        if self.in_speech and self.voiced_frames * FRAME_S >= MIN_SPEECH:
            # Drop most of the trailing silence; keep a little so the last word isn't clipped.
            cut = max(0, self.silence_frames - TAIL_FRAMES)
            keep = len(self.speech) - cut
            job = Job(np.concatenate(self.speech[:keep]), keep, True, True, self.epoch, self.seg_id, (pos - cut * FRAME) / SR)
            cand = self.candidate
            reuse = cand is not None and cand.seg_id == job.seg_id and cand.frames == keep and not cand.cancelled
            self.start_final(job, cand if reuse else None)
        elif self.in_speech and self.previewed_seg == self.seg_id:
            # Too little speech to be a line, but a preview of it may be on screen: take it down.
            self.start_final(Job(None, 0, True, True, self.epoch, self.seg_id, pos / SR))
        self.in_speech = False
        self.speech = []
        self.probs = []
        self.silence_frames = 0
        self.voiced_frames = 0
        self.candidate = None
        self.preroll.clear()

    # ---- inference -----------------------------------------------------
    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def start_preview(self, frames: int, pos: int, full: bool):
        if full:
            self.cancel_preview()      # an older preview of this line is of no use any more
        cut = len(self.speech) - frames
        job = Job(np.concatenate(self.speech[:frames]), frames, False, full, self.epoch, self.seg_id, (pos - cut * FRAME) / SR)
        self.preview = job
        self.previewed_seg = job.seg_id
        self.last_partial = time.monotonic()
        if full:
            self.candidate = job
        job.task = self.spawn(self.run_preview(job))

    def start_final(self, job: Job, candidate: Job | None = None):
        self.final_seg = job.seg_id
        if self.preview is not None and self.preview is not candidate:
            self.cancel_preview()      # a preview must never hold up a finished line
        self.spawn(self.run_final(job, candidate))

    async def run_preview(self, job: Job):
        """Returns (src, text, ms) or None. Sends the preview unless the line has moved on meanwhile."""
        t0 = time.perf_counter()
        result = None
        try:
            src, text = await self.recognise(job)
            if self.cfg.use_llm and src:
                # Short previews get no context: with a few words the LLM tends to echo earlier lines.
                text = await self.translate_llm(src, job, with_context=job.full)
            result = (src, text, int((time.perf_counter() - t0) * 1000))
        except Cancelled:
            pass
        except Exception as e:
            report(f"[error] preview: {type(e).__name__}: {e}")
        finally:
            if self.preview is job:
                self.preview = None
        if result is None or job.cancelled or job.epoch != self.epoch or job.seg_id <= self.final_seg:
            return result
        src, text, ms = result
        if text and text.strip(" .!?。").lower() == self.last_text.strip(" .!?。").lower():
            return result              # a preview that just repeats the previous line
        self.log(job, src, text, ms)
        if text and self.shown_preview != (job.seg_id, text, src):   # unchanged previews aren't sent again
            self.shown_preview = (job.seg_id, text, src)
            await self.emit(job, src, text, ms)
        return result

    async def run_final(self, job: Job, candidate: Job | None):
        t0 = time.perf_counter()
        src = text = ""
        reused = None
        error = None
        emb = None
        await self.asr_lock.acquire()
        try:
            if job.audio is None or job.epoch != self.epoch:
                pass
            elif time.monotonic() - job.created > MAX_BACKLOG:
                report(f"[drop] line #{job.seg_id} waited {time.monotonic() - job.created:.1f}s (translation too slow)")
            else:
                if candidate is not None and candidate.task is not None:
                    reused = await candidate.task       # the same audio is already (being) translated
                    if candidate.cancelled or candidate.epoch != self.epoch:
                        reused = None
                if reused is not None:
                    src, text, _ = reused
                else:
                    src, text = await self.recognise(job)
                emb = await self.voiceprint(job)
        except Exception as e:
            error = e
        await self.out_lock.acquire()   # keep our place in line before the next line may start
        self.asr_lock.release()
        try:
            if error is not None:
                raise error
            if job.epoch != self.epoch:
                return
            if self.cfg.use_llm and src and (reused is None or candidate.ctx_version != self.ctx_version):
                text = await self.translate_llm(src, job, with_context=True)
                if job.epoch != self.epoch:
                    return
            ms = reused[2] if reused is not None and text == reused[1] else int((time.perf_counter() - t0) * 1000)
            if text:
                self.last_text = text
                if src:
                    self.context.append((src, text))
                    self.ctx_version += 1
            speaker = None
            if text and emb is not None and self.cfg.speakers:
                speaker = self.tracker.assign(emb, job.frames * FRAME_S)
            self.log(job, src, text, ms, speaker)
            if text:
                self.write_transcript(src, text, speaker)
            await self.emit(job, src, text, ms, speaker)
        except Exception as e:
            report(f"[error] line: {type(e).__name__}: {e}")
            if job.epoch == self.epoch:
                await self.emit(job, "", "", 0)     # so a preview of the failed line doesn't stay up
        finally:
            self.out_lock.release()

    async def recognise(self, job: Job) -> tuple[str, str]:
        """Whisper on the job's audio -> (src, text). In LLM mode only src is set (translation follows)."""
        cfg = self.cfg
        audio = job.audio
        rms = float(np.sqrt(np.dot(audio, audio) / audio.size))
        if rms > 1e-5:  # even out loudness so soft lines are as clear to Whisper as loud ones
            audio = np.clip(audio * min(SEGMENT_RMS / rms, 20.0), -1.0, 1.0)
        cancel = None if job.final else (lambda: job.cancelled)
        task = "transcribe" if cfg.use_llm else "translate"
        for attempt in range(3):
            model_id = self.asr_model()
            ACTIVE_MODELS[self] = model_id
            engine = self.cache.engines.get(model_id) or await asyncio.to_thread(self.get_engine, model_id)
            try:
                text = clean(await asyncio.to_thread(engine.run, audio, cfg.language, task, cancel))
                break
            except EngineClosed:       # another tab switched models meanwhile: get it again
                if attempt == 2:
                    raise
        return (text, "") if cfg.use_llm else ("", text)

    async def translate_llm(self, src: str, job: Job, with_context: bool = True) -> str:
        cfg = self.cfg
        if not cfg.llm_model:
            if not self.llm_warned:
                self.llm_warned = True
                await self.status("No translator model picked - choose one in the popup or the Subtle window "
                                  "(showing the original text for now)", "error")
            return src
        if not job.final:
            if job.cancelled:
                raise Cancelled()
            job.llm_call = LLMCall()
        job.ctx_version = self.ctx_version if with_context else -1
        key, version = (src, job.ctx_version), self.ctx_version
        if key in self.translations:   # the same words with the same context: the translator would say the same again
            return self.translations[key]
        try:
            out = await asyncio.to_thread(
                llm_translate, cfg.llm_url, cfg.llm_model, src, cfg.language, cfg.target,
                list(self.context) if with_context else [], 20, job.llm_call,
            )
            if self.llm_warned:
                self.llm_warned = False
                await self.status("Live", "ok")
            if out and version == self.ctx_version:     # (not if the settings or the context changed meanwhile)
                if len(self.translations) >= 64:
                    self.translations.clear()
                self.translations[key] = out
            return out or src
        except Cancelled:
            raise
        except Exception as e:
            if not self.llm_warned:
                self.llm_warned = True
                await self.status(f"Translator not answering ({type(e).__name__}) - showing the original text. "
                                  "Check the Translator page of the Subtle window.", "error")
            report(f"[llm] {type(e).__name__}: {e}")
            job.ctx_version = -1       # not a real translation: never reuse it for the finished line
            return src

    def log(self, job: Job, src: str, text: str, ms: int, speaker: int | None = None):
        if not (job.final or VERBOSE) or job.audio is None:
            return
        tag = "line" if job.final else ("early" if job.full else "preview")
        shown = f"{src}  =>  {text}" if src else (text or "(nothing)")
        if self.cfg.speakers and job.final:
            shown = f"S{speaker or '?'}: {shown}"
        try:
            print(f"[{tag}] {job.frames * FRAME_S:4.1f}s audio, {ms:4d} ms  {shown}", flush=True)
        except Exception:
            pass  # logging must never cost a subtitle

    async def emit(self, job: Job, src: str, text: str, ms: int, speaker: int | None = None):
        msg = {"type": "sub", "text": text, "src": src, "final": job.final, "id": job.seg_id,
               "ms": ms, "t": round(job.end_t, 3)}
        if job.final and text:
            # for subtitle files (.srt/.vtt): how long the line was spoken, and how much audio has played
            # since it ended, so the page can place it on the video's own clock
            msg["dur"] = round(job.frames * FRAME_S, 2)
            msg["age"] = round(max(0.0, self.samples / SR - job.end_t), 2)
        if speaker is not None:
            msg["speaker"] = speaker
        if self.cfg.learning and src and self.cfg.reading != "off":
            try:
                import readings
                tokens = readings.annotate(src, self.cfg.language, self.cfg.reading)
                if tokens:
                    msg["tokens"] = tokens
            except Exception as e:
                report(f"[error] readings: {type(e).__name__}: {e}")
        await self.send(msg)

    async def send(self, obj: dict):
        async with self.send_lock:
            try:
                await self.ws.send(json.dumps(obj, ensure_ascii=False))
            except Exception:
                pass

    # ---- optional transcript (off by default) ------------------------------
    def write_transcript(self, src: str, text: str, speaker: int | None = None):
        if not self.cfg.save_transcript:
            return
        try:
            if self.transcript is None:
                TRANSCRIPT_DIR.mkdir(exist_ok=True)
                now = datetime.datetime.now()
                path = TRANSCRIPT_DIR / f"{now:%Y-%m-%d_%H-%M-%S}.txt"
                self.transcript = open(path, "a", encoding="utf-8", buffering=1)
                title = f" - {self.cfg.page_title}" if self.cfg.page_title else ""
                self.transcript.write(f"# Subtle transcript{title}\n# started {now:%Y-%m-%d %H:%M:%S}\n\n")
                print(f"[transcript] saving to {path}", flush=True)
            stamp = datetime.datetime.now().strftime("%H:%M:%S")
            who = f"Speaker {speaker}: " if speaker else ""
            self.transcript.write(f"[{stamp}] {who}{text}" + (f"\n           {src}" if src and src != text else "") + "\n")
        except Exception as e:
            report(f"[error] transcript: {type(e).__name__}: {e}")
            self.close_transcript()

    def close_transcript(self):
        if self.transcript is not None:
            try:
                self.transcript.close()
            except Exception:
                pass
            self.transcript = None

    def close(self):
        self.epoch += 1                # whatever is still running has nobody to talk to
        self.cancel_preview()
        self.close_transcript()
        SPEAKER_USERS.discard(self)
        if not SPEAKER_USERS and SpeakerEncoder._instance is not None:
            SpeakerEncoder.release()
        if ACTIVE_MODELS.pop(self, None) is not None:
            # a model only this tab was using can go now (one stays loaded for the next start)
            threading.Thread(target=self.cache.trim, args=(set(ACTIVE_MODELS.values()),), daemon=True).start()
        if VERBOSE and self.feed_frames:
            print(f"[conn] closed; gain + VAD took {self.feed_time / self.feed_frames * 1e6:.0f} us per 32 ms frame "
                  f"({self.feed_time / (self.feed_frames * FRAME_S) * 100:.2f}% of real time), "
                  f"slowest chunk {self.feed_max * 1000:.1f} ms", flush=True)


def trim_working_set():
    """
    Hand the engine's start-up memory back to Windows. Starting PyTorch and CUDA and loading a model
    leaves about 1.5 GB of RAM behind that is never touched again (measured: some tens of MB are in
    use from then on). This tells Windows it can have those pages now, instead of finding that out
    by itself once memory runs short. Whatever is needed after all comes back by itself.
    SUBTLE_NO_TRIM=1 turns it off.
    """
    if sys.platform != "win32" or os.environ.get("SUBTLE_NO_TRIM"):
        return False
    try:
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        return bool(psapi.EmptyWorkingSet(k32.GetCurrentProcess()))
    except Exception:
        return False


async def trim_memory_loop(cache: EngineCache):
    """Runs trim_working_set a few seconds after the engine started or a model was loaded, in a
    pause between lines (it can hold the engine up for a fraction of a second)."""
    seen = due = None
    while True:
        await asyncio.sleep(1)
        state = (tuple(cache.loaded_ids()), SpeakerEncoder._instance is not None)
        now = time.monotonic()
        if state != seen:
            seen, due = state, now + TRIM_AFTER
        elif due is not None and now >= due:
            busy = any(s.in_speech or s.tasks for s in ACTIVE_MODELS)
            if busy and now < due + 30:
                continue                    # someone is speaking: wait for the gap (but not forever)
            due = None
            if await asyncio.to_thread(trim_working_set):
                print("[memory] start-up memory handed back to Windows", flush=True)


def make_handler(cache: EngineCache, allow_any_origin: bool, token: str = ""):
    async def handler(ws):
        if token:
            # Run by the supervisor (Subtle's tray app): only it may connect, with the token it gave us.
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(ws.request.path).query)
            if not hmac.compare_digest(query.get("token", [""])[0], token):
                await ws.close(1008, "not allowed")
                return
        else:
            origin = ws.request.headers.get("Origin", "") or ""
            if not allow_any_origin and not origin.startswith(("chrome-extension://", "moz-extension://")):
                print(f"[conn] rejected origin {origin!r}", flush=True)
                await ws.close(1008, "extension origin required")
                return
        session = Session(ws, cache)
        await session.send({"type": "ready", "version": VERSION})
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    n = len(message) & ~3      # whole float32 samples only
                    if n:
                        session.feed(np.frombuffer(message, dtype="<f4", count=n // 4).astype(np.float32))
                else:
                    try:
                        msg = json.loads(message)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        await session.control(msg)
        finally:
            session.close()

    return handler


async def main():
    ap = argparse.ArgumentParser(description="Subtle subtitle server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--preload", default="",
                    help="Whisper model to load at start (otherwise it loads when a tab starts subtitles)")
    ap.add_argument("--allow-any-origin", action="store_true", help="accept non-extension clients (for testing)")
    ap.add_argument("--verbose", action="store_true", help="also log in-progress preview translations")
    ap.add_argument("--token", default="", help=argparse.SUPPRESS)   # set by the supervisor
    args = ap.parse_args()
    global VERBOSE
    VERBOSE = args.verbose
    setup_error_log()
    print(f"[subtle] engine v{VERSION} - subtitles stay in memory unless 'Save transcripts' is on", flush=True)

    # Whisper and the LLM client run in worker threads. The CUDA-graph decoder barely touches the
    # GIL, but the generate() fallback and JSON handling do; a short switch interval keeps the
    # event loop (audio in, VAD, subtitles out) from waiting behind them.
    sys.setswitchinterval(0.0005)

    cache = EngineCache()
    available = cached_whisper_models()
    print(f"[models] Whisper in cache: {', '.join(m.split('/')[-1] for m in available) or 'none'}", flush=True)
    if args.preload and args.preload in available:
        cache.get(args.preload)
    token = args.token or os.environ.pop("SUBTLE_ENGINE_TOKEN", "")   # given by the supervisor
    # No per-message compression: audio barely compresses and it would only cost CPU.
    async with serve(make_handler(cache, args.allow_any_origin, token), args.host, args.port,
                     max_size=2**22, compression=None):
        print(f"[ready] listening on ws://{args.host}:{args.port}", flush=True)
        trimmer = asyncio.create_task(trim_memory_loop(cache))    # (kept in a variable so it isn't collected)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
