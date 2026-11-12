"""
How good is the English that Subtle's Best mode produces for Japanese / Chinese / Korean, on the same test
sentences as the direct-translation ("Fast") runs?  Whisper turbo writes each Subtle-style line down, then
Subtle's own translator prompt (llm_client.llm_translate, earlier lines of the same sentence as context)
turns it into English with the built-in translator model. Scored with chrF against the English FLEURS text.

    venv/Scripts/python.exe -I -X utf8 best_translate.py [--langs ja,zh,ko] [--limit N]

Starts its own llama-server on port 8790 with Subtle's settings and stops it afterwards.
"""
import argparse
import json
import os
import statistics as st
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server")
sys.path.insert(0, HERE)
sys.path.insert(1, SERVER)
import warnings

warnings.filterwarnings("ignore")
import numpy as np

import engines
import llm_client
import llm_runner
import modelstore
from score import chrf

ap = argparse.ArgumentParser()
ap.add_argument("--langs", default="ja,zh,ko")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--llm", default="Qwen3-4B-Instruct-2507-Q4_K_M")
args = ap.parse_args()
LANGS = {"ja": "ja_jp", "zh": "cmn_hans_cn", "ko": "ko_kr"}
PORT = 8790

path = next(m["path"] for m in modelstore.llm_models(True, True, []) if m["id"] == args.llm)
srv = llm_runner.LlamaServer()
srv.port = PORT
proc = subprocess.Popen(srv.command(path, {"llm_gpu_layers": 999, "llm_context": 2048, "llm_kv_quant": True}),
                        cwd=str(llm_runner.LLAMA_DIR), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, creationflags=llm_runner.NO_WINDOW)
try:
    for _ in range(240):
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1).status == 200:
                break
        except Exception:
            time.sleep(0.5)
    url = f"http://127.0.0.1:{PORT}/v1"
    eng = engines.WhisperEngine("openai/whisper-large-v3-turbo")
    eng.warmup()
    refs = json.load(open(os.path.join(HERE, "data", "refs.json"), encoding="utf-8"))
    en_refs = json.load(open(os.path.join(HERE, "data", "en_refs.json"), encoding="utf-8"))
    out = {"llm": args.llm, "langs": {}}
    for lang in args.langs.split(","):
        items = refs[LANGS[lang]][: args.limit or None]
        hyps, asr_ms, llm_ms = [], [], []
        for i, it in enumerate(items):
            z = np.load(os.path.join(HERE, "data", "segs", LANGS[lang], f"{i:03d}.npz"))
            ctx, parts = [], []
            for k in z.files:
                a = z[k]
                rms = float(np.sqrt(np.dot(a, a) / a.size))
                a = np.clip(a * min(0.1 / rms, 20.0), -1.0, 1.0).astype(np.float32) if rms > 1e-5 else a
                t = time.perf_counter()
                src = eng.run(a, lang, "transcribe")
                asr_ms.append((time.perf_counter() - t) * 1e3)
                if not src:
                    continue
                t = time.perf_counter()
                text = llm_client.llm_translate(url, args.llm, src, lang, "en", ctx[-4:])
                llm_ms.append((time.perf_counter() - t) * 1e3)
                ctx.append((src, text))
                parts.append(text)
            hyps.append(" ".join(parts))
        en = en_refs[LANGS[lang]][: len(items)]
        score = round(chrf(en, hyps), 1)
        out["langs"][lang] = dict(seg_chrf=score, asr_ms=round(st.mean(asr_ms), 1), llm_ms=round(st.mean(llm_ms), 1), seg_hyp=hyps)
        print(f"best-mode (turbo + {args.llm}) {lang}: chrF {score}  |  {st.mean(asr_ms):.0f} ms speech + {st.mean(llm_ms):.0f} ms translator per line", flush=True)
        print("   en :", en[0][:100])
        print("   hyp:", hyps[0][:100], flush=True)
    if not args.limit:
        json.dump(out, open(os.path.join(HERE, "results", "best-translate.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
finally:
    proc.kill()
