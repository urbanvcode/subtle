"""Download benchmark model folders from Hugging Face (only the files the runners need)."""
import json, os, sys, urllib.request, urllib.parse, concurrent.futures as cf
UA = {"User-Agent": "subtle-bench"}
REPOS = {
 "sensevoice": ("csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2025-09-09", None),
 "funasr-nano": ("csukuangfj/sherpa-onnx-funasr-nano-2025-12-30", None),
 "firered2": ("csukuangfj2/sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26", None),
 "dolphin-small": ("csukuangfj/sherpa-onnx-dolphin-small-ctc-multi-lang-2025-04-02", None),
 "paraformer-zh": ("csukuangfj/sherpa-onnx-paraformer-zh-2024-03-09", ["model.onnx", "tokens.txt"]),
 "parakeet-ja": ("csukuangfj/sherpa-onnx-nemo-parakeet-tdt_ctc-0.6b-ja-35000-fp16", None),
 "reazon-zipformer": ("DeL-TaiseiOzaki/sherpa-onnx-zipformer-ja-reazonspeech-2024-08-01", None),
 "qwen3-asr-0.6b": ("Qwen/Qwen3-ASR-0.6B-hf", None),
 "qwen3-asr-1.7b": ("Qwen/Qwen3-ASR-1.7B-hf", None),
}
def files_of(repo):
    i = json.load(urllib.request.urlopen(urllib.request.Request(f"https://huggingface.co/api/models/{repo}", headers=UA), timeout=30))
    return [s["rfilename"] for s in i["siblings"]]
def fetch(name, repo, only):
    os.makedirs(f"models/{name}", exist_ok=True)
    fs = [f for f in files_of(repo) if (only is None or f in only) and not f.startswith(("test_wavs/", ".git")) and not f.endswith((".md", ".py", ".png", ".jpg"))]
    for f in fs:
        dst = f"models/{name}/{f}"
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            continue
        url = f"https://huggingface.co/{repo}/resolve/main/{urllib.parse.quote(f)}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, open(dst + ".part", "wb") as o:
            while True:
                b = r.read(1 << 20)
                if not b: break
                o.write(b)
        os.replace(dst + ".part", dst)
    return name, fs
with cf.ThreadPoolExecutor(4) as ex:
    for name, fs in ex.map(lambda kv: fetch(kv[0], *kv[1]), REPOS.items()):
        print(name, fs, flush=True)
