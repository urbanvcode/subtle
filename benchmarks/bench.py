"""
Speech-model benchmark for Subtle: Chinese / Japanese / Korean, FLEURS test sentences (120 per language).
Each model is scored twice: on whole sentences, and on the short lines Subtle's own voice detector cuts
them into (data/segs, made by segment.py), which is what a model really gets inside Subtle.

    venv/Scripts/python.exe -I -X utf8 bench.py <model key> [--limit N] [--langs ja,zh,ko] [--threads 4]

One model per process, so the RAM / VRAM numbers are that model alone. Results: results/<key>.json
"""
import argparse
import json
import os
import statistics as st
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server")
sys.path.insert(0, HERE)
sys.path.insert(1, SERVER)
import warnings

warnings.filterwarnings("ignore")
import numpy as np
import psutil

import gpuinfo
from score import cer, chrf

M = os.path.join(HERE, "models")
LANGS = {"ja": "ja_jp", "zh": "cmn_hans_cn", "ko": "ko_kr"}
MODELS = {
    # key: (kind, where, languages)
    "whisper-large-v3": ("whisper", "openai/whisper-large-v3", "ja,zh,ko"),
    "whisper-turbo": ("whisper", "openai/whisper-large-v3-turbo", "ja,zh,ko"),
    "kotoba-v2.0": ("whisper", "kotoba-whisper-v2.0", "ja"),
    "kotoba-v2.2": ("whisper", "kotoba-whisper-v2.2", "ja"),
    "belle-turbo-zh": ("whisper", "belle-whisper-large-v3-turbo-zh", "zh"),
    "belle-zh-punct": ("whisper", "belle-whisper-large-v3-zh-punct", "zh"),
    "turbo-korean": ("whisper", "whisper-large-v3-turbo-korean", "ko"),
    "anime-whisper": ("whisper", "anime-whisper", "ja"),
    "kotoba-bilingual": ("whisper", "kotoba-whisper-bilingual-v1.0", "ja"),
    "qwen3-asr-0.6b": ("qwen", "qwen3-asr-0.6b", "ja,zh,ko"),
    "qwen3-asr-1.7b": ("qwen", "qwen3-asr-1.7b", "ja,zh,ko"),
    "sensevoice": ("sherpa", "sensevoice-2024", "ja,zh,ko"),   # the original SenseVoice-Small export
    "sensevoice-ort": ("svgpu", "sensevoice-2024", "ja,zh,ko"),   # same model file, own runner on onnxruntime CUDA (sv_gpu.py)
    "funasr-nano": ("sherpa", "funasr-nano", "ja,zh"),          # this export has no Korean
    "dolphin-small": ("sherpa", "dolphin-small", "ja,zh,ko"),
    "firered2": ("sherpa", "firered2", "zh"),
    "paraformer-zh": ("sherpa", "paraformer-zh", "zh"),
    "reazon-zipformer": ("sherpa", "reazon-zipformer", "ja"),
    "parakeet-ja": ("sherpa", "parakeet-ja", "ja"),
    "parakeet-subtle": ("whisper", os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "parakeet-engine-test"), "ja"),   # Subtle's own Parakeet engine
    "sensevoice-subtle": ("whisper", "sensevoice-small", "zh,ja,ko"),   # the SenseVoice engine built into Subtle
    "parakeet-ja-fp32": ("sherpa", "parakeet-ja-fp32", "ja"),   # full-size export (OpenVoiceOS), for the GPU
}
ap = argparse.ArgumentParser()
ap.add_argument("key")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--langs", default="")
ap.add_argument("--threads", type=int, default=4)
ap.add_argument("--tag", default="")
ap.add_argument("--only", default="", choices=["", "full", "segs"],
                help="whisper family: segs keeps Subtle's own token limits (real timing), full raises them (long sentences)")
ap.add_argument("--provider", default="cpu", choices=["cpu", "cuda"], help="sherpa-onnx models: cuda needs venv-gpu")
ap.add_argument("--task", default="transcribe", choices=["transcribe", "translate"],
                help="translate: Whisper family only, straight to English, scored with chrF against the English FLEURS sentences")
args = ap.parse_args()
kind, where, langs = MODELS[args.key]
langs = [x for x in langs.split(",") if not args.langs or x in args.langs.split(",")]
PROC, METER = psutil.Process(), gpuinfo.ProcessMeter()


def mem():
    time.sleep(1.2)
    return round(PROC.memory_full_info().uss / 2**20), round(METER.vram_by_pid().get(os.getpid(), 0) / 2**20)


def norm_audio(a):
    rms = float(np.sqrt(np.dot(a, a) / a.size))
    return np.clip(a * min(0.1 / rms, 20.0), -1.0, 1.0).astype(np.float32) if rms > 1e-5 else a


t0 = time.perf_counter()
device = "GPU"
if kind == "whisper":
    import engines

    if args.only != "segs":
        engines.WhisperEngine.MAX_CACHE, engines.WhisperEngine.MAX_NEW = 448, 440   # FLEURS sentences are long
    eng = engines.make_engine(where)
    eng.warmup()

    def make(lang):
        return lambda a: eng.run(a, lang, args.task)
elif kind == "qwen":
    import torch
    from transformers import AutoProcessor, Qwen3ASRForConditionalGeneration

    proc = AutoProcessor.from_pretrained(os.path.join(M, where))
    model = Qwen3ASRForConditionalGeneration.from_pretrained(os.path.join(M, where), dtype=torch.float16).to("cuda").eval()

    def make(lang):
        def run(a):
            inputs = proc.apply_transcription_request(audio=a, language=lang).to("cuda", torch.float16)
            with torch.inference_mode():
                out = model.generate(**inputs, max_new_tokens=256, do_sample=False)
            return proc.decode(out[0, inputs["input_ids"].shape[1]:], skip_special_tokens=True).strip()
        return run
elif kind == "svgpu":
    from sv_gpu import SenseVoiceGPU

    sv = SenseVoiceGPU(os.path.join(M, where, "model.onnx"), os.path.join(M, where, "tokens.txt"))
    device = "GPU" if sv.provider.startswith("CUDA") else "CPU"

    def make(lang):
        return lambda a: sv.run(a, lang)
else:
    if args.provider == "cuda":
        import torch  # noqa: F401  (its folder holds cuDNN 9, which the GPU build of sherpa-onnx needs)
    import sherpa_onnx as so

    device = f"CPU x{args.threads}" if args.provider == "cpu" else "GPU"
    PROV = args.provider
    d, R, T = os.path.join(M, where), so.OfflineRecognizer, args.threads

    def p(f):
        return os.path.join(d, f)

    def build(lang):
        if where == "sensevoice-2024":
            return R.from_sense_voice(p("model.onnx"), p("tokens.txt"), num_threads=T, language=lang, use_itn=True, provider=PROV)
        if where == "funasr-nano":
            return R.from_funasr_nano(p("encoder_adaptor.onnx"), p("llm.fp32.onnx"), p("embedding.onnx"), p("Qwen3-0.6B"), num_threads=T, provider=PROV)
        if where == "dolphin-small":
            return R.from_dolphin_ctc(p("model.onnx"), p("tokens.txt"), num_threads=T, provider=PROV)
        if where == "firered2":
            return R.from_fire_red_asr(p("encoder.int8.onnx"), p("decoder.int8.onnx"), p("tokens.txt"), num_threads=T, provider=PROV)
        if where == "paraformer-zh":
            return R.from_paraformer(p("model.onnx"), p("tokens.txt"), num_threads=T, provider=PROV)
        if where == "reazon-zipformer":
            return R.from_transducer(p("encoder-epoch-99-avg-1.int8.onnx"), p("decoder-epoch-99-avg-1.onnx"),
                                     p("joiner-epoch-99-avg-1.onnx"), p("tokens.txt"), num_threads=T, provider=PROV)
        if where == "parakeet-ja-fp32":
            return R.from_nemo_ctc(p("model.sherpa.onnx"), p("tokens.txt"), num_threads=T, provider=PROV)
        if where == "parakeet-ja":
            return R.from_nemo_ctc(p("model.int8.onnx"), p("tokens.txt"), num_threads=T, provider=PROV)
        raise KeyError(where)

    _rec = {}

    def make(lang):
        key = lang if where == "sensevoice-2024" else ""
        if key not in _rec:
            _rec[key] = build(lang)
        rec = _rec[key]

        def run(a):
            s = rec.create_stream()
            s.accept_waveform(16000, a)
            rec.decode_stream(s)
            return s.result.text.strip()
        return run

fns = {x: make(x) for x in langs}
load_s = time.perf_counter() - t0
refs = json.load(open(os.path.join(HERE, "data", "refs.json"), encoding="utf-8"))
res = {"key": args.key + ("-gpu" if args.provider == "cuda" else ""), "kind": kind, "device": device, "load_s": round(load_s, 1), "only": args.only, "task": args.task, "langs": {}}
for lang in langs:
    items = refs[LANGS[lang]][: args.limit or None]
    run = fns[lang]
    first = norm_audio(np.load(os.path.join(HERE, items[0]["file"])))
    run(first[:48000])
    run(first[:32000])                                       # warm-up
    full_h, full_ms, seg_h, seg_ms, seg_sec, n_lines = [], [], [], [], [], 0
    for i, it in enumerate(items):
        a = norm_audio(np.load(os.path.join(HERE, it["file"])))
        if args.only != "segs":
            t = time.perf_counter()
            full_h.append(run(a))
            full_ms.append((time.perf_counter() - t) * 1e3)
        else:
            full_h.append("")
            full_ms.append(0.0)
        z = np.load(os.path.join(HERE, "data", "segs", LANGS[lang], f"{i:03d}.npz"))
        parts = []
        for k in (z.files if args.only != "full" else []):       # arr_0, arr_1, ... in order
            s = norm_audio(z[k])
            t = time.perf_counter()
            parts.append(run(s))
            seg_ms.append((time.perf_counter() - t) * 1e3)
            seg_sec.append(s.size / 16000)
        seg_h.append((" " if lang == "ko" or args.task == "translate" else "").join(parts))
        n_lines += len(parts)
    r = [it["ref"] for it in items]
    ram, vram = mem()
    en = json.load(open(os.path.join(HERE, "data", "en_refs.json"), encoding="utf-8"))[LANGS[lang]][: len(items)]
    x = res["langs"][lang] = dict(
        n=len(items), audio_min=round(sum(it["sec"] for it in items) / 60, 1),
        full_cer=round(cer(r, full_h, lang) * 100, 2), full_ms=round(st.mean(full_ms)),
        full_sec=round(st.mean(it["sec"] for it in items), 1),
        seg_cer=round(cer(r, seg_h, lang) * 100, 2), seg_ms=round(st.mean(seg_ms or [0]), 1),
        seg_ms_median=round(st.median(seg_ms or [0]), 1), seg_ms_p95=round(sorted(seg_ms or [0])[int(len(seg_ms) * 0.95)], 1),
        seg_sec=round(st.mean(seg_sec or [0]), 1), lines=n_lines, ram_mb=ram, vram_mb=vram, full_hyp=full_h, seg_hyp=seg_h)
    if args.task == "translate":
        x.update(full_chrf=round(chrf(en, full_h), 1), seg_chrf=round(chrf(en, seg_h), 1))
        print(f"{args.key:18s} {lang}  TRANSLATE to English: chrF whole sentences {x['full_chrf']}, Subtle-style lines {x['seg_chrf']}  |  "
              f"{x['seg_ms']} ms/line", flush=True)
        print("   en :", en[0][:90])
    print(f"{args.key:18s} {lang}  whole sentences: CER {x['full_cer']:5.2f}%  {x['full_ms']:5d} ms each ({x['full_sec']} s audio)  |  "
          f"Subtle-style lines: CER {x['seg_cer']:5.2f}%  {x['seg_ms']:6.1f} ms/line (median {x['seg_ms_median']}, p95 {x['seg_ms_p95']})  |  "
          f"{device}  RAM {ram} MB  VRAM {vram} MB", flush=True)
    print("   ref:", r[0][:70])
    print("   hyp:", full_h[0][:70])
    print("   seg:", seg_h[0][:70], flush=True)
if not args.limit:
    json.dump(res, open(os.path.join(HERE, "results", f"{args.key}{args.tag}.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
