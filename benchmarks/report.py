"""Collect results/*.json into one table per language (markdown, to stdout and results/report.md)."""
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WAIT_MS = 128     # Subtle shows a line once the speaker has been quiet for 4 voice-detector frames (EARLY_SILENCE)
NAMES = {
    "whisper-large-v3": "Whisper large-v3", "whisper-turbo": "Whisper large-v3-turbo (Subtle's Best mode today)",
    "kotoba-v2.0": "Kotoba-Whisper v2.0", "kotoba-v2.2": "Kotoba-Whisper v2.2", "belle-turbo-zh": "Belle-Whisper large-v3-turbo-zh",
    "belle-zh-punct": "Belle-Whisper large-v3-zh-punct", "turbo-korean": "Whisper large-v3-turbo-korean (ghost613)",
    "anime-whisper": "Anime-Whisper", "kotoba-bilingual": "Kotoba-Whisper bilingual v1.0",
    "qwen3-asr-0.6b": "Qwen3-ASR 0.6B", "qwen3-asr-1.7b": "Qwen3-ASR 1.7B", "sensevoice": "SenseVoice-Small",
    "sensevoice-ort": "SenseVoice-Small (own GPU runner)", "sensevoice-gpu": "SenseVoice-Small",
    "funasr-nano": "Fun-ASR-Nano", "dolphin-small": "Dolphin small", "firered2": "FireRedASR2 (int8)",
    "paraformer-zh": "Paraformer-zh", "reazon-zipformer": "ReazonSpeech Zipformer (int8)", "parakeet-ja": "Parakeet-TDT-CTC 0.6B ja (int8)",
    "parakeet-ja-fp32": "Parakeet-TDT-CTC 0.6B ja", "sensevoice-subtle": "SenseVoice-Small (Subtle engine, CUDA graph)", "parakeet-subtle": "Parakeet-TDT-CTC 0.6B ja (Subtle engine, CUDA graph)", "funasr-nano-gpu": "Fun-ASR-Nano", "dolphin-small-gpu": "Dolphin small",
    "paraformer-zh-gpu": "Paraformer-zh", "parakeet-ja-fp32-gpu": "Parakeet-TDT-CTC 0.6B ja",
}
res, trans = {}, {}
for f in sorted(glob.glob(os.path.join(HERE, "results", "*.json"))):
    if f.endswith("-new.json"):
        continue                              # regression re-runs (same numbers by design)
    d = json.load(open(f, encoding="utf-8"))
    if "key" not in d:
        continue
    if d.get("task") == "translate":
        for lang, x in d["langs"].items():
            trans.setdefault(lang, []).append((NAMES.get(d["key"], d["key"]) + ", direct", x["seg_chrf"], f"{x['seg_ms']:.0f} ms", f"{x['vram_mb'] / 1024:.1f} GB"))
        continue
    slot = res.setdefault(d["key"], {"device": d["device"], "kind": d["kind"], "langs": {}})
    for lang, x in d["langs"].items():
        y = slot["langs"].setdefault(lang, {})
        if d.get("only") != "full":          # the run with Subtle's own limits: line timing + memory as in Subtle
            y.update(seg_cer=x["seg_cer"], seg_ms=x["seg_ms"], seg_p95=x["seg_ms_p95"], ram=x["ram_mb"], vram=x["vram_mb"], lines=x["lines"])
        if d.get("only") != "segs":
            y.update(full_cer=x["full_cer"], full_ms=x["full_ms"])
best = os.path.join(HERE, "results", "best-translate.json")
if os.path.exists(best):
    d = json.load(open(best, encoding="utf-8"))
    for lang, x in d["langs"].items():
        trans.setdefault(lang, []).append((f"Best mode: Whisper turbo + {d['llm']}", x["seg_chrf"], f"{x['asr_ms'] + x['llm_ms']:.0f} ms ({x['asr_ms']:.0f} + {x['llm_ms']:.0f})", "5.7 GB"))
out = []
for lang, title in (("ja", "Japanese"), ("zh", "Chinese (Mandarin)"), ("ko", "Korean")):
    rows = [(k, v["langs"][lang], v) for k, v in res.items() if lang in v["langs"] and "seg_cer" in v["langs"][lang]]
    rows.sort(key=lambda r: r[1]["seg_cer"])
    out.append(f"\n### {title}: writing down what is said\n")
    out.append("| Model | Runs on | Errors, short lines (as in Subtle) | Errors, whole sentences | Time per line | Text appears after speaker stops | VRAM | RAM |")
    out.append("|---|---|---|---|---|---|---|---|")
    for k, x, v in rows:
        full = f"{x['full_cer']:.1f}%" if "full_cer" in x else "-"
        out.append(f"| {NAMES.get(k, k)} | {v['device']} | {x['seg_cer']:.1f}% | {full} | {x['seg_ms']:.0f} ms (95% under {x['seg_p95']:.0f}) "
                   f"| {WAIT_MS + x['seg_ms']:.0f} ms | {x['vram'] / 1024:.1f} GB | {x['ram'] / 1024:.1f} GB |")
    if lang in trans:
        out.append(f"\n### {title}: straight to English (chrF, higher is better)\n")
        out.append("| Route | chrF | Time per line | VRAM |")
        out.append("|---|---|---|---|")
        for name, score, ms, vram in sorted(trans[lang], key=lambda r: -r[1]):
            out.append(f"| {name} | {score} | {ms} | {vram} |")
text = "\n".join(out)
sys.stdout.reconfigure(encoding="utf-8")
print(text)
open(os.path.join(HERE, "results", "report.md"), "w", encoding="utf-8").write(text + "\n")
