"""
Translator benchmark for Subtle's Best engine: the same source lines every time, so only the translator
differs between runs.

  1. (once) data/src_lines.json: the Subtle-style lines of the test sentences written down by the speech
     model Subtle uses for that language (ja: Parakeet, zh: SenseVoice, ko: Whisper turbo).
  2. each run: starts its own llama-server (port 8791) with the given settings, translates every line with
     Subtle's own client (llm_client.llm_translate, earlier lines of the same sentence as context), and records
     chrF against the English FLEURS sentence, time per call split into prompt / writing / other, and memory.

    venv/Scripts/python.exe -I -X utf8 translate_bench.py --tag baseline
    ... --kv f16 | --extra "--flag value ..." | --model <gguf id> | --target fr | --limit 20 | --langs ja,zh
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
import psutil

import gpuinfo
import llm_client
import llm_runner
import modelstore
from score import chrf

ap = argparse.ArgumentParser()
ap.add_argument("--tag", default="baseline")
ap.add_argument("--langs", default="ja,zh,ko")
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--model", default="Qwen3-4B-Instruct-2507-Q4_K_M")
ap.add_argument("--kv", default="q8", choices=["q8", "f16"])
ap.add_argument("--context", type=int, default=2048)
ap.add_argument("--extra", default="", help="more llama-server flags")
ap.add_argument("--drop", default="", help="llama-server flags to remove from Subtle's command, comma separated (with their value)")
ap.add_argument("--target", default="en")
ap.add_argument("--port", type=int, default=8791)
ap.add_argument("--trim", action="store_true", help="hand the translator start-up RAM back to Windows after the first call")
ap.add_argument("--model-path", default="", help="a .gguf outside Subtle's model folders")
ap.add_argument("--style", default="subtle", choices=["subtle", "hymt", "hymt-ctx", "tgemma"],
                help="subtle: Subtle's own prompt; hymt / hymt-ctx: Tencent HY-MT prompt without / with earlier lines; tgemma: TranslateGemma prompt")
ap.add_argument("--body", default="", help="JSON merged into every request (e.g. sampler settings)")
args = ap.parse_args()
LANGS = {"ja": "ja_jp", "zh": "cmn_hans_cn", "ko": "ko_kr"}
SRC = os.path.join(HERE, "data", "src_lines.json")

if not os.path.exists(SRC):
    import engines
    out = {}
    for lang, model in (("ja", "parakeet-tdt_ctc-0.6b-ja"), ("zh", "sensevoice-small"), ("ko", "openai/whisper-large-v3-turbo")):
        eng = engines.make_engine(model)
        eng.warmup()
        sents = []
        for i in range(120):
            z = np.load(os.path.join(HERE, "data", "segs", LANGS[lang], f"{i:03d}.npz"))
            lines = []
            for k in z.files:
                a = z[k]
                rms = float(np.sqrt(np.dot(a, a) / a.size))
                a = np.clip(a * min(0.1 / rms, 20.0), -1.0, 1.0).astype(np.float32) if rms > 1e-5 else a
                text = eng.run(a, lang, "transcribe")
                if text:
                    lines.append(text)
            sents.append(lines)
        out[lang] = {"speech_model": model, "sentences": sents}
        with engines.GPU_LOCK:
            eng.close()
        del eng
        engines.free_gpu_memory()
    json.dump(out, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("wrote", SRC, {k: sum(len(s) for s in v["sentences"]) for k, v in out.items()}, "lines", flush=True)
src = json.load(open(SRC, encoding="utf-8"))
refs_file = "en_refs.json" if args.target == "en" else f"{args.target}_refs.json"
target_refs = json.load(open(os.path.join(HERE, "data", refs_file), encoding="utf-8"))

path = args.model_path or next(m["path"] for m in modelstore.llm_models(True, True, []) if m["id"] == args.model)
srv = llm_runner.LlamaServer()
srv.port = args.port
cmd = srv.command(path, {"llm_gpu_layers": 999, "llm_context": args.context, "llm_kv_quant": args.kv == "q8"})
for flag in [f for f in args.drop.split(",") if f]:
    if flag in cmd:
        i = cmd.index(flag)
        del cmd[i:i + (2 if i + 1 < len(cmd) and not cmd[i + 1].startswith("-") else 1)]
cmd += args.extra.split()
log = open(os.path.join(HERE, "results", f"translate-{args.tag}.llama.log"), "wb")
proc = subprocess.Popen(cmd, cwd=str(llm_runner.LLAMA_DIR), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                        creationflags=llm_runner.NO_WINDOW)
last = {}
orig = llm_client._http_json


EXTRA_BODY = json.loads(args.body) if args.body else {}


def spy(u, body=None, timeout=30, call=None):
    if body is not None and EXTRA_BODY:
        body = dict(body, **EXTRA_BODY)
    data = orig(u, body, timeout, call)
    last.clear()
    last.update(data.get("timings") or {})
    return data


llm_client._http_json = spy
EN = {"ja": "Japanese", "zh": "Chinese", "ko": "Korean", "en": "English", "fr": "French"}
ZH = {"ja": "日语", "zh": "中文", "ko": "韩语", "en": "英语", "fr": "法语"}


def translate(url, text, lang, target, ctx):
    """One line -> its translation, with the prompt style under test."""
    if args.style == "subtle":
        return llm_client.llm_translate(url, args.model, text, lang, target, ctx)
    if args.style in ("hymt", "hymt-ctx"):
        if args.style == "hymt-ctx" and ctx:
            prompt = "\n".join(s for s, _ in ctx) + f"\n参考上面的信息，把下面的文本翻译成{ZH[target]}，注意不需要翻译上文，也不要额外解释：\n{text}"
        elif "zh" in (lang, target):
            prompt = f"将以下文本翻译为{ZH[target]}，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}"
        else:
            prompt = f"Translate the following segment into {EN[target]}, without additional explanation.\n\n{text}"
        body = {"temperature": 0.7, "top_k": 20, "top_p": 0.6, "repeat_penalty": 1.05}
    else:
        prompt = (f"You are a professional {EN[lang]} ({lang}) to {EN[target]} ({target}) translator. Your goal is to accurately convey the "
                  f"meaning and nuances of the original {EN[lang]} text while adhering to {EN[target]} grammar, vocabulary, and cultural "
                  f"sensitivities.\nProduce only the {EN[target]} translation, without any additional explanations or commentary. "
                  f"Please translate the following {EN[lang]} text into {EN[target]}:\n\n\n{text}")
        body = {"temperature": 0.2}
    body.update(model=args.model, max_tokens=160, stream=False, messages=[{"role": "user", "content": prompt}])
    data = llm_client._http_json(url + "/chat/completions", body, 20)
    return (data["choices"][0]["message"].get("content") or "").strip()
try:
    t0 = time.perf_counter()
    for _ in range(360):
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{args.port}/health", timeout=1).status == 200:
                break
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("llama-server stopped: see the .llama.log")
            time.sleep(0.25)
    load_s = time.perf_counter() - t0
    url = f"http://127.0.0.1:{args.port}/v1"
    translate(url, "こんにちは。", "ja", args.target, [])
    if args.trim:
        import ctypes
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.OpenProcess.restype = ctypes.c_void_p
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        print("trim:", bool(psapi.EmptyWorkingSet(k32.OpenProcess(0x0100 | 0x0400, False, proc.pid))), flush=True)
    res = {"tag": args.tag, "model": args.model, "kv": args.kv, "extra": args.extra, "drop": args.drop, "target": args.target, "body": args.body, "style": args.style, "model_path": args.model_path,
           "load_s": round(load_s, 1), "cmd": cmd[1:], "langs": {}}
    for lang in args.langs.split(","):
        sents = src[lang]["sentences"][: args.limit or None]
        hyps, rows = [], []
        for lines in sents:
            ctx, parts = [], []
            for text in lines:
                t = time.perf_counter()
                out = translate(url, text, lang, args.target, ctx[-4:])
                ms = (time.perf_counter() - t) * 1e3
                rows.append((ms, last.get("prompt_ms", 0.0), last.get("predicted_ms", 0.0), last.get("predicted_n", 0),
                             last.get("prompt_n", 0), last.get("cache_n", 0)))
                ctx.append((text, out))
                parts.append(out)
            hyps.append(" ".join(parts))
        ref = target_refs[LANGS[lang]][: len(sents)]
        col = lambda i: [r[i] for r in rows]                                           # noqa: E731
        x = res["langs"][lang] = dict(
            chrf=round(chrf(ref, hyps), 1), calls=len(rows), ms=round(st.mean(col(0)), 1), ms_median=round(st.median(col(0)), 1),
            ms_p95=round(sorted(col(0))[int(len(rows) * 0.95)], 1), prompt_ms=round(st.mean(col(1)), 1),
            write_ms=round(st.mean(col(2)), 1), other_ms=round(st.mean(col(0)) - st.mean(col(1)) - st.mean(col(2)), 1),
            tokens_out=round(st.mean(col(3)), 1), ms_per_token=round(sum(col(2)) / max(1, sum(col(3))), 2),
            prompt_new=round(st.mean(col(4)), 1), prompt_cached=round(st.mean(col(5)), 1), hyp=hyps)
        print(f"{args.tag:14s} {lang}: chrF {x['chrf']:5.1f} | {x['ms']:5.1f} ms per line (median {x['ms_median']}, p95 {x['ms_p95']}) = "
              f"prompt {x['prompt_ms']} + writing {x['write_ms']} ({x['tokens_out']} tokens, {x['ms_per_token']} ms each) + other {x['other_ms']}"
              f" | prompt tokens new {x['prompt_new']} / cached {x['prompt_cached']}", flush=True)
    time.sleep(1.3)
    p = psutil.Process(proc.pid)
    res["vram_mb"] = round(gpuinfo.ProcessMeter().vram_by_pid().get(proc.pid, 0) / 2**20)
    res["ram_mb"] = round(p.memory_full_info().uss / 2**20)
    print(f"{args.tag:14s} translator memory: VRAM {res['vram_mb']} MB, RAM {res['ram_mb']} MB, load {res['load_s']} s", flush=True)
    if not args.limit:
        json.dump(res, open(os.path.join(HERE, "results", f"translate-{args.tag}.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
finally:
    proc.kill()
    log.close()
