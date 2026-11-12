"""
Speech engines for Subtle.

WhisperEngine  - Whisper (whisper_core.py, no transformers): feature extraction, encoder, prompt
                 prefill and the greedy decoder step run as CUDA graphs on the GPU, or eagerly
                 on the CPU / if capture is not possible.
ParakeetEngine - Parakeet-TDT-CTC 0.6B (parakeet_core.py, no NeMo): Japanese only, writes speech down
                 in one pass (about 7 ms per line as a CUDA graph). The default for Japanese with the
                 Best engine when it is installed.
SenseVoiceEngine - SenseVoice-Small (sensevoice_core.py, no FunASR): Chinese, Cantonese, English,
                 Japanese, Korean in one pass (about 6 ms per line). The default for Chinese with the
                 Best engine when it is installed.
SpeakerEncoder - optional voiceprints for "Identify speakers".
The translator client (llm_translate) lives in llm_client.py and is re-exported here.
"""

import gc
import os

os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")   # Apple GPU: run the few unsupported ops on the processor
import threading
import time

import numpy as np
import torch

from llm_client import Cancelled, LANG_NAMES, LLMCall, lang_name, list_llm_models, llm_translate  # noqa: F401 (re-exported)
from modelstore import model_languages, model_source, short_name, speaker_model_path, whisper_ids
from parakeet_core import ParakeetRunner, prepare as prepare_parakeet, prepared as parakeet_prepared
from sensevoice_core import SenseVoiceRunner, prepare as prepare_sensevoice, prepared as sensevoice_prepared
from whisper_core import WhisperRunner, WhisperWeights

SR = 16000
GPU_LOCK = threading.Lock()  # one model call on the GPU at a time

# ---------------------------------------------------------------------------------------------
# Model discovery (see modelstore.py: models/ folders + the Hugging Face cache)
# ---------------------------------------------------------------------------------------------
def pick_device() -> str:
    """NVIDIA (or AMD through ROCm) GPU, else the Apple GPU (Experimental), else the processor.
    SUBTLE_CPU=1 forces the processor; SUBTLE_MPS=0 keeps Macs on the processor."""
    if os.environ.get("SUBTLE_CPU"):
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    try:
        if os.environ.get("SUBTLE_MPS", "1") != "0" and torch.backends.mps.is_available():
            return "mps"
    except AttributeError:
        pass
    return "cpu"


def device_name(device: str) -> str:
    if device == "cuda":
        return torch.cuda.get_device_name(0)
    return "Apple GPU" if device == "mps" else "CPU"


def cached_whisper_models() -> list[str]:
    """Ids of the complete speech models in models/whisper and the Hugging Face cache (no downloads)."""
    return whisper_ids()


def is_parakeet(model_id: str) -> bool:
    return "parakeet" in short_name(model_id)


def is_sensevoice(model_id: str) -> bool:
    return "sensevoice" in short_name(model_id)


def writes_down_only(model_id: str) -> bool:
    """Parakeet and SenseVoice: speech in a fixed set of languages -> text in that language. No translation, no language detection."""
    return is_parakeet(model_id) or is_sensevoice(model_id)


def model_fits(model_id: str, language: str | None, use_llm: bool) -> bool:
    """
    Whether a model picked in the popup can be used. Whisper: always (as before). Parakeet and
    SenseVoice only write speech down, so they need the translator to follow (the Best engine, or a
    subtitle language other than English) and a spoken language they know; otherwise Subtle would show
    Chinese or Japanese text where English was asked for.
    """
    if not writes_down_only(model_id):
        return True
    return use_llm and language in (model_languages(model_id) or ())


def default_fits(model_id: str, language: str | None, use_llm: bool) -> bool:
    """
    Whether a default chosen in Settings (per spoken language) can do the whole job: a model made for
    one language only for that language, and with the Fast engine only a model that can translate.
    """
    if not model_fits(model_id, language, use_llm):
        return False
    name, langs = short_name(model_id), model_languages(model_id)
    if langs is not None and language not in langs:
        return False
    return use_llm or (_can_translate(name) and (langs is None or "bilingual" in name))


def needs_preparing(model_id: str) -> bool:
    """A Parakeet / SenseVoice download that has not been turned into Subtle's own files yet (first use only)."""
    folder = model_source(model_id)
    return (is_parakeet(model_id) and not parakeet_prepared(folder)) or (is_sensevoice(model_id) and not sensevoice_prepared(folder))


def _can_translate(name: str) -> bool:
    return "turbo" not in name and not name.startswith("distil")   # turbo/distil only transcribe


def default_asr_model(available: list[str], use_llm: bool, loaded: list[str] | None = None,
                      language: str | None = None) -> str:
    """
    Direct translation needs large-v3 (turbo can't translate); transcription is fine with turbo,
    which needs half the video memory (2.5 GB instead of 4.3 GB). Only one model is kept on the
    GPU, so switching between Fast and Best swaps them (a ~2 s reload) instead of keeping both.
    Japanese with the Best engine goes to Parakeet when it is installed: more accurate than turbo
    on Subtle-style lines (6.2 % vs 7.0 % character errors) and about six times as fast.
    Chinese with the Best engine goes to SenseVoice when it is installed (9.3 % vs 11.7 %, about
    seven times as fast). Both only write speech down, so the Fast engine keeps Whisper large-v3.
    """
    if use_llm and language == "ja":
        for m in available:
            if is_parakeet(m):
                return m
    if use_llm and language in ("zh", "yue"):
        for m in available:
            if is_sensevoice(m):
                return m
    available = [m for m in available if not writes_down_only(m)]
    by_name = {short_name(m): m for m in available}
    if use_llm:
        prefs = ["whisper-large-v3-turbo", "whisper-large-v3", "whisper-medium", "whisper-small"]
    else:
        prefs = ["whisper-large-v3", "whisper-large-v2", "whisper-medium", "whisper-small"]
    for p in prefs:
        if p in by_name:
            return by_name[p]
    usable = [m for m in available if use_llm or _can_translate(short_name(m))]
    return (usable or available or ["openai/whisper-large-v3"])[0]


# ---------------------------------------------------------------------------------------------
# Whisper
# ---------------------------------------------------------------------------------------------
class EngineClosed(Exception):
    """Raised by an engine's run when the model was unloaded meanwhile (get it from the cache again)."""


class Engine:
    """What the two speech engines share: one call on the GPU at a time, cancellable while waiting."""

    model_id = ""
    closed = False
    runner = None

    def _decode(self, audio, language, task, cancel) -> str:
        raise NotImplementedError

    def run(self, audio: np.ndarray, language: str | None, task: str, cancel=None) -> str:
        """
        task 'translate' -> English text; 'transcribe' -> text in the spoken language.
        language None -> detect it. `cancel` (optional callable, polled while waiting for the GPU
        and between tokens) aborts the call with Cancelled once it returns True, so that a preview
        never holds up a finished line.
        """
        if cancel is None:
            GPU_LOCK.acquire()
        else:
            while not GPU_LOCK.acquire(timeout=0.01):
                if cancel():
                    raise Cancelled()
        try:
            if self.closed:
                raise EngineClosed(self.model_id)
            if cancel is not None and cancel():
                raise Cancelled()
            return self._decode(audio, language, task, cancel).strip()
        finally:
            GPU_LOCK.release()

    def warmup(self):
        for _ in range(2):
            self.run(np.zeros(SR * 2, dtype=np.float32), "ja", "transcribe")

    def close(self):
        """Free the model, its CUDA graphs and their buffers (call with GPU_LOCK held)."""
        self.closed = True
        self.runner = None


class WhisperEngine(Engine):
    MAX_CACHE = 128   # decoder positions (prompt + output tokens)
    MAX_NEW = 100     # output tokens per line

    def __init__(self, model_id: str):
        t0 = time.time()
        self.model_id = model_id
        # SUBTLE_CPU=1 / SUBTLE_NO_GRAPH=1 force the fallback paths (for testing them).
        self.device = pick_device()
        # fp16 halves memory and is fast on RTX cards; GTX 10-series and older run fp16 very slowly.
        fast_fp16 = self.device == "mps" or (self.device == "cuda" and torch.cuda.get_device_capability(0)[0] >= 7)
        self.dtype = torch.float16 if fast_fp16 else torch.float32
        self.closed = False
        folder = model_source(model_id)
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"{model_id} is not downloaded (Models page of the Subtle window)")
        self.W = WhisperWeights(folder, self.device, self.dtype)
        self.eot = self.W.token("<|endoftext|>")
        self.sot = self.W.token("<|startoftranscript|>")
        # Bilingual fine-tunes (kotoba-whisper-bilingual) translate when told the language to write in
        # (<|en|> + translate), not the language that is spoken as in OpenAI's Whisper.
        self.translate_into_en = "bilingual" in model_id.lower()
        use_graphs = self.device == "cuda" and not os.environ.get("SUBTLE_NO_GRAPH")
        with GPU_LOCK:
            try:
                self.runner = WhisperRunner(self.W, self.device, self.MAX_CACHE, self.eot, use_graphs)
            except Exception as e:  # stay on the slower, always-working path
                if not use_graphs:
                    raise
                print(f"[model] CUDA graph unavailable for {model_id} ({type(e).__name__}: {e}); running without", flush=True)
                self.runner = WhisperRunner(self.W, self.device, self.MAX_CACHE, self.eot, False)
        self.W.t.clear()               # the runner holds the weights it uses (views into the blocks W loaded)
        gc.collect()
        name = device_name(self.device)
        mode = "CUDA graph" if self.runner.graphs else "eager"
        print(f"[model] {model_id} ready on {name} ({mode}) in {time.time() - t0:.1f}s", flush=True)

    # -- helpers --------------------------------------------------------------------------
    def _token(self, text: str) -> int:
        i = self.W.token(text)
        if i is None:
            raise ValueError(f"{text} is not supported by {self.model_id}")
        return i

    def _decode(self, audio, language, task, cancel) -> str:
        g = self.runner
        task_id, nots = self._token(f"<|{task}|>"), self._token("<|notimestamps|>")
        lang = self._token(f"<|{language}|>") if language else None
        if task == "translate" and self.translate_into_en:
            lang = self._token("<|en|>")
        if lang is None and not g.can_detect:
            lang = self._token("<|en|>")
        out = []
        with torch.inference_mode():
            g.encode(audio)
            if lang is None:
                lang = g.detect_language(self.sot)
            nxt = g.first_token([self.sot, lang, task_id, nots])
            for _ in range(min(self.MAX_NEW, self.MAX_CACHE - g.N_PROMPT - 1)):
                if nxt == self.eot:
                    break
                out.append(nxt)
                if len(out) >= 12 and out[-6:] == out[-12:-6]:  # stuck in a loop
                    break
                if cancel is not None and cancel():
                    raise Cancelled()
                nxt = g.next_token()
        return self.W.decode(out)

    def close(self):
        super().close()
        self.W = None


class ParakeetEngine(Engine):
    """Japanese speech written down in one pass (parakeet_core.py). Language and task are fixed:
    model_fits() keeps it to Japanese with the Best engine."""

    def __init__(self, model_id: str):
        t0 = time.time()
        self.model_id = model_id
        self.device = pick_device()
        fast_fp16 = self.device == "mps" or (self.device == "cuda" and torch.cuda.get_device_capability(0)[0] >= 7)
        self.dtype = torch.float16 if fast_fp16 else torch.float32
        folder = model_source(model_id)
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"{model_id} is not downloaded (Models page of the Subtle window)")
        prepare_parakeet(folder)                       # first use only: NVIDIA's download -> Subtle's own files
        use_graph = self.device == "cuda" and not os.environ.get("SUBTLE_NO_GRAPH")
        with GPU_LOCK:
            try:
                self.runner = ParakeetRunner(folder, self.device, self.dtype, use_graph)
            except Exception as e:  # stay on the slower, always-working path
                if not use_graph:
                    raise
                print(f"[model] CUDA graph unavailable for {model_id} ({type(e).__name__}: {e}); running without", flush=True)
                self.runner = ParakeetRunner(folder, self.device, self.dtype, False)
        gc.collect()
        name = device_name(self.device)
        mode = "CUDA graph" if self.runner.graph is not None else "eager"
        print(f"[model] {model_id} ready on {name} ({mode}) in {time.time() - t0:.1f}s", flush=True)

    def _decode(self, audio, language, task, cancel) -> str:
        return self.runner.transcribe(audio)


class SenseVoiceEngine(Engine):
    """Chinese (and Cantonese, English, Japanese, Korean) written down in one pass (sensevoice_core.py).
    Always full precision: its numbers do not fit into fp16."""

    def __init__(self, model_id: str):
        t0 = time.time()
        self.model_id = model_id
        self.device = pick_device()
        folder = model_source(model_id)
        if not os.path.isdir(folder):
            raise FileNotFoundError(f"{model_id} is not downloaded (Models page of the Subtle window)")
        prepare_sensevoice(folder)                     # first use only: FunASR's download -> Subtle's own files
        use_graph = self.device == "cuda" and not os.environ.get("SUBTLE_NO_GRAPH")
        with GPU_LOCK:
            try:
                self.runner = SenseVoiceRunner(folder, self.device, torch.float32, use_graph)
            except Exception as e:  # stay on the slower, always-working path
                if not use_graph:
                    raise
                print(f"[model] CUDA graph unavailable for {model_id} ({type(e).__name__}: {e}); running without", flush=True)
                self.runner = SenseVoiceRunner(folder, self.device, torch.float32, False)
        gc.collect()
        name = device_name(self.device)
        mode = "CUDA graph" if self.runner.graphs else "eager"
        print(f"[model] {model_id} ready on {name} ({mode}) in {time.time() - t0:.1f}s", flush=True)

    def _decode(self, audio, language, task, cancel) -> str:
        return self.runner.transcribe(audio, language)

    def warmup(self):
        for _ in range(2):
            self.run(np.zeros(SR * 2, dtype=np.float32), "zh", "transcribe")


def make_engine(model_id: str) -> Engine:
    if is_parakeet(model_id):
        return ParakeetEngine(model_id)
    return SenseVoiceEngine(model_id) if is_sensevoice(model_id) else WhisperEngine(model_id)


def free_gpu_memory():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()


class EngineCache:
    """
    Loads speech models on first use. Only `keep` models stay on the GPU (default 1): loading
    another one first frees the oldest, so switching models never piles up VRAM.
    """

    def __init__(self, keep: int = 1):
        self.engines: dict[str, Engine] = {}          # oldest first
        self.keep = max(1, keep)
        self.lock = threading.Lock()

    def get(self, model_id: str, in_use: set[str] | frozenset = frozenset()) -> Engine:
        """
        in_use: models other tabs are using right now. Those are never unloaded to make room (two
        tabs in different modes would otherwise keep swapping each other's model out); they're
        freed by trim() once no tab needs them.
        """
        with self.lock:
            eng = self.engines.pop(model_id, None)
            if eng is None:
                for old in [m for m in self.engines if m not in in_use]:
                    if len(self.engines) < self.keep:
                        break
                    self._unload(old)
                eng = make_engine(model_id)
                eng.warmup()
            self.engines[model_id] = eng              # most recently used last
            return eng

    def trim(self, in_use: set[str]):
        """Free models no tab is using any more (beyond the `keep` most recent)."""
        with self.lock:
            extra = [m for m in self.engines if m not in in_use]
            for m in extra[:max(0, len(self.engines) - self.keep)]:
                self._unload(m)

    def _unload(self, model_id: str):
        eng = self.engines.pop(model_id)
        with GPU_LOCK:                                # never while a line is being decoded
            eng.close()
        del eng
        free_gpu_memory()
        print(f"[model] unloaded {model_id.split('/')[-1]}", flush=True)

    def unload_all(self):
        with self.lock:
            for model_id in list(self.engines):
                self._unload(model_id)

    def loaded(self, model_id: str) -> bool:
        return model_id in self.engines

    def loaded_ids(self) -> list[str]:
        return list(self.engines)


# ---------------------------------------------------------------------------------------------
# Speaker identification (optional, off by default). Voiceprints live in memory only.
# ---------------------------------------------------------------------------------------------
class SpeakerEncoder:
    """WeSpeaker ResNet34 (26.5 MB ONNX): one line of audio -> a 256-number voiceprint (~3 ms on GPU)."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "SpeakerEncoder":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def release(cls):
        """Free the voiceprint model (nobody has 'Identify speakers' on any more)."""
        with cls._lock:
            inst, cls._instance = cls._instance, None
        if inst is not None:
            with inst.run_lock:
                inst.sess = None
            del inst
            gc.collect()
            print("[speakers] voiceprint model unloaded", flush=True)

    def __init__(self):
        import onnxruntime as ort
        import torchaudio.compliance.kaldi as kaldi

        path = speaker_model_path()
        if path is None:
            raise FileNotFoundError(
                "speaker model not found - download it on the Models page of the Subtle window "
                "(or put voxceleb_resnet34_LM.onnx into models\\speaker)"
            )
        ort.set_default_logger_severity(3)
        opts = ort.SessionOptions()
        opts.enable_cpu_mem_arena = False       # the arena kept ~650 MB of RAM for a 26 MB model
        opts.enable_mem_pattern = False
        opts.intra_op_num_threads = 2           # ~30 ms per line on the CPU; don't take every core for it
        cuda = ("CUDAExecutionProvider", {"arena_extend_strategy": "kSameAsRequested"})
        gpu_ok = torch.cuda.is_available() and "CUDAExecutionProvider" in ort.get_available_providers()
        providers = [cuda, "CPUExecutionProvider"] if gpu_ok else ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(str(path), sess_options=opts, providers=providers)
        self.input = self.sess.get_inputs()[0].name
        self.kaldi = kaldi
        self.run_lock = threading.Lock()
        self.embed(np.zeros(SR, dtype=np.float32) + 1e-4)  # warm-up
        print(f"[speakers] voiceprint model ready ({self.sess.get_providers()[0]})", flush=True)

    def embed(self, audio: np.ndarray) -> np.ndarray:
        w = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0) * 32768
        f = self.kaldi.fbank(w, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0,
                             sample_frequency=SR, window_type="hamming", use_energy=False)
        f = f - f.mean(0, keepdim=True)
        with self.run_lock:
            if self.sess is None:
                raise EngineClosed("speaker model")
            e = self.sess.run(None, {self.input: f.unsqueeze(0).numpy()})[0][0]
        return e / (np.linalg.norm(e) + 1e-9)


class SpeakerTracker:
    """
    Learns voices during one session. Each finished line's voiceprint is compared with the
    voices heard so far: a close match is that speaker (and refines their profile), otherwise
    it is a new speaker. Profiles that turn out to be the same voice are merged. Short lines
    carry too little voice to judge, so they only get a speaker on a very clear match.
    """

    MATCH = 0.50        # same speaker (same voice measured 0.64-0.80, different voices <= 0.48)
    SHORT_MATCH = 0.62  # stricter bar for short lines
    MIN_LONG = 1.0      # seconds; shorter lines never create a new speaker
    MERGE = 0.72        # two profiles this similar are one person
    MAX_SPEAKERS = 8
    MAX_WEIGHT = 20     # how many lines a profile averages over (later lines still adjust it)

    def __init__(self):
        self.profiles: list[list] = []   # [speaker id, centroid, line count]
        self.next_id = 1

    def assign(self, emb: np.ndarray, seconds: float) -> int | None:
        if not self.profiles:
            return self._new(emb) if seconds >= self.MIN_LONG else None
        sims = [float(p[1] @ emb) for p in self.profiles]
        i = int(np.argmax(sims))
        best = sims[i]
        if seconds < self.MIN_LONG:
            return self.profiles[i][0] if best >= self.SHORT_MATCH else None
        if best >= self.MATCH or len(self.profiles) >= self.MAX_SPEAKERS:
            self._update(i, emb)
            return self.profiles[i][0] if i < len(self.profiles) else self._closest(emb)
        return self._new(emb)

    def _new(self, emb):
        sid = self.next_id
        self.next_id += 1
        self.profiles.append([sid, emb.copy(), 1])
        return sid

    def _update(self, i, emb):
        p = self.profiles[i]
        w = min(p[2], self.MAX_WEIGHT)
        c = p[1] * w + emb
        p[1] = c / (np.linalg.norm(c) + 1e-9)
        p[2] += 1
        self._merge()

    def _closest(self, emb):
        return max(self.profiles, key=lambda p: float(p[1] @ emb))[0]

    def _merge(self):
        merged = True
        while merged and len(self.profiles) > 1:
            merged = False
            for a in range(len(self.profiles)):
                for b in range(a + 1, len(self.profiles)):
                    pa, pb = self.profiles[a], self.profiles[b]
                    if float(pa[1] @ pb[1]) >= self.MERGE:
                        keep, drop = (pa, pb) if pa[0] < pb[0] else (pb, pa)  # keep the older number
                        c = keep[1] * keep[2] + drop[1] * drop[2]
                        keep[1] = c / (np.linalg.norm(c) + 1e-9)
                        keep[2] += drop[2]
                        self.profiles.remove(drop)
                        merged = True
                        break
                if merged:
                    break

    def reset(self):
        self.profiles.clear()
        self.next_id = 1
