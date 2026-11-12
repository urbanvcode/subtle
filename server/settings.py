"""
Subtle's settings (settings.json in the Subtle folder), changed from the control panel.
No torch here: the supervisor imports it too.
"""

import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = ROOT / "settings.json"

DEFAULTS = {
    # Translator for the "Best" engine
    "llm_backend": "builtin",            # "builtin" (llama.cpp shipped with Subtle) | "external" (LM Studio, Ollama...)
    "llm_model": "",                     # builtin: a .gguf id from the Models page; external: the server's model name
    "external_url": "http://127.0.0.1:1234/v1",
    "llm_gpu_layers": 999,               # all layers on the GPU (fastest); lower it to save VRAM
    "llm_context": 2048,                 # a subtitle prompt is ~400 tokens; more only costs VRAM
    "llm_kv_quant": True,                # 8-bit KV cache: half the cache memory, same quality here
    # Memory
    "idle_unload_min": 5,                # free all VRAM/RAM this long after the last tab stops (0 = never)
    "keep_loaded": False,                # keep models in memory even when idle (instant start)
    "previews": "normal",                # "normal" (every 0.5 s) | "slow" (every 1 s) | "off" (finished lines only)
    # Speech model per spoken language when the popup says "Auto": "auto" (Subtle chooses) or a model id
    "asr_default_zh": "auto",
    "asr_default_ja": "auto",
    "asr_default_ko": "auto",
    "asr_default_other": "auto",
    # App
    "open_panel_on_start": True,
    "check_updates": True,               # once a day: ask GitHub for the newest Subtle version (nothing else is sent)
    "first_run_done": False,
    "scan_lmstudio": True,               # also list .gguf models already downloaded with LM Studio
    "extra_llm_dirs": [],
    "port": 8765,
}

_lock = threading.Lock()


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def save(changes: dict) -> dict:
    """Merge known keys (type-checked against the defaults) into settings.json; returns the new settings."""
    with _lock:
        data = load()
        for k, v in changes.items():
            if k not in DEFAULTS:
                continue
            d = DEFAULTS[k]
            if isinstance(d, bool):
                v = bool(v)
            elif isinstance(d, int):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
            elif isinstance(d, str):
                v = str(v)
            elif isinstance(d, list) and not isinstance(v, list):
                continue
            data[k] = v
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, SETTINGS_FILE)
        return data
