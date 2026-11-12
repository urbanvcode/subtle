"""
Where Subtle finds its models. No torch here: the supervisor (tray + control panel) imports it too.

  models/whisper/<name>/   a speech model: a Hugging Face Whisper folder (config.json + *.safetensors +
                           tokenizer files), or Parakeet / SenseVoice (the files as downloaded, or
                           the files Subtle makes from them on first use; see parakeet_core.py and
                           sensevoice_core.py)
  models/llm/*.gguf        translator models for the built-in llama.cpp server
  models/speaker/*.onnx    voiceprint model for "Identify speakers"

Models already in the Hugging Face cache (~/.cache/huggingface/hub) are found as well, so an
existing setup keeps working. A model in models/ wins over a cached one with the same name.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
WHISPER_DIR = MODELS_DIR / "whisper"
LLM_DIR = MODELS_DIR / "llm"
SPEAKER_DIR = MODELS_DIR / "speaker"

SPEAKER_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
SPEAKER_FILE = "voxceleb_resnet34_LM.onnx"

READMES = {
    WHISPER_DIR: (
        "Speech models (Whisper) go here, one folder per model.\n\n"
        "The easiest way is the Models page of the Subtle window (Download button).\n"
        "To add one yourself, download a Whisper model from huggingface.co in the Transformers\n"
        "format and put its whole folder here, e.g.\n"
        "    models\\whisper\\whisper-large-v3\\config.json, model.safetensors, tokenizer.json, ...\n"
        "Then press Rescan on the Models page.\n"
    ),
    LLM_DIR: (
        "Translator models (for the 'Best' engine) go here as .gguf files.\n\n"
        "The easiest way is the Models page of the Subtle window (Download button).\n"
        "Any instruct/chat model in GGUF format works, e.g. Qwen3-4B-Instruct-2507-Q4_K_M.gguf.\n"
        "Smaller files use less video memory (VRAM). Then press Rescan on the Models page.\n"
    ),
    SPEAKER_DIR: (
        "Voiceprint model for 'Identify speakers' goes here:\n"
        f"    {SPEAKER_FILE}  (26.5 MB, from {SPEAKER_REPO} on huggingface.co)\n"
        "The Models page of the Subtle window can download it for you.\n"
    ),
}


def ensure_dirs():
    for d, text in READMES.items():
        try:
            d.mkdir(parents=True, exist_ok=True)
            readme = d / "READ ME.txt"
            if not readme.exists():
                readme.write_text(text, encoding="utf-8")
        except OSError:
            pass


def hf_hub_dir() -> Path:
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    return Path(home) / "hub"


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size      # follows the HF cache's links to the real blobs
        except OSError:
            pass
    return total


def _is_whisper_folder(d: Path) -> bool:
    return (d / "config.json").is_file() and any(d.glob("model*.safetensors"))


def _is_parakeet_folder(d: Path) -> bool:
    return ((d / "parakeet.json").is_file() and (d / "model.safetensors").is_file()) or any(d.glob("*.nemo"))


SENSEVOICE_SOURCE = ("model.pt", "am.mvn", "chn_jpn_yue_eng_ko_spectok.bpe.model")     # as downloaded
SENSEVOICE_LANGS = ("zh", "yue", "en", "ja", "ko")


def _is_sensevoice_folder(d: Path) -> bool:
    return (((d / "sensevoice.json").is_file() and (d / "model.safetensors").is_file())
            or all((d / f).is_file() for f in SENSEVOICE_SOURCE))


def model_languages(model_id: str) -> tuple | None:
    """
    The spoken languages a speech model is for, or None for any language (multilingual Whisper).
    Parakeet and SenseVoice by what they are, Whisper fine-tunes by the "language" of their catalog entry.
    """
    name = short_name(model_id)
    if "parakeet" in name:
        return ("ja",)
    if "sensevoice" in name:
        return SENSEVOICE_LANGS
    lang = _catalog_languages().get(name)
    return (lang,) if lang else None


_langs_cache: tuple[float, dict] | None = None


def _catalog_languages() -> dict[str, str]:
    """{model id or folder name (lower case): language} of the single-language speech models in the catalog,
    read again only when catalog.json changes (Settings asks for every installed model on each refresh)."""
    global _langs_cache
    path = MODELS_DIR / "catalog.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    if _langs_cache is not None and _langs_cache[0] == mtime:
        return _langs_cache[1]
    out: dict[str, str] = {}
    try:
        import json
        with open(path, encoding="utf-8") as f:
            for m in json.load(f).get("models", []):
                if m.get("kind") == "whisper" and m.get("language"):
                    for key in (str(m.get("target", "")).lower(), m["id"].lower()):
                        if key:
                            out.setdefault(key, m["language"])
    except (OSError, ValueError, KeyError):
        return {}
    _langs_cache = (mtime, out)
    return out


def whisper_models(with_size: bool = False) -> list[dict]:
    """[{id, name, path, source, size}] for every complete speech model (Whisper, Parakeet, SenseVoice), models/ folder first."""
    found: dict[str, dict] = {}
    if WHISPER_DIR.is_dir():
        for d in sorted(WHISPER_DIR.iterdir()):
            if d.is_dir() and not d.name.endswith(".partial") and (
                    _is_whisper_folder(d) or _is_parakeet_folder(d) or _is_sensevoice_folder(d)):
                found[d.name.lower()] = {"id": d.name, "name": d.name, "path": str(d), "source": "folder"}
    hub = hf_hub_dir()
    if hub.is_dir():
        for d in sorted(hub.glob("models--*")):
            repo = d.name[len("models--"):].replace("--", "/")
            name = repo.split("/")[-1]
            if "whisper" not in repo.lower() or name.lower() in found:
                continue
            snaps = [s for s in (d / "snapshots").glob("*") if s.is_dir() and _is_whisper_folder(s)]
            if snaps:
                found[name.lower()] = {"id": repo, "name": name, "path": str(snaps[-1]), "source": "hf-cache"}
    out = list(found.values())
    if with_size:
        for m in out:
            m["size"] = _dir_size(Path(m["path"]))
    return out


def whisper_ids() -> list[str]:
    return [m["id"] for m in whisper_models()]


def model_source(model_id: str) -> str:
    """Local folder for a Whisper id (from whisper_models), else the id itself (HF cache, offline)."""
    for m in whisper_models():
        if m["id"] == model_id:
            return m["path"]
    return model_id


def short_name(model_id: str) -> str:
    return model_id.split("/")[-1].lower()


def lmstudio_models_dir() -> Path | None:
    """LM Studio's download folder, if LM Studio is installed (its .gguf files can be reused as they are)."""
    home = Path(os.path.expanduser("~"))
    try:
        with open(home / ".lmstudio" / "settings.json", encoding="utf-8") as f:
            import json
            custom = json.load(f).get("downloadsFolder")
        if custom and Path(custom).is_dir():
            return Path(custom)
    except (OSError, ValueError, AttributeError):
        pass
    for d in (home / ".lmstudio" / "models", home / ".cache" / "lm-studio" / "models"):
        if d.is_dir():
            return d
    return None


def llm_dirs(scan_lmstudio: bool = True, extra: list | None = None) -> list[tuple[Path, str]]:
    dirs = [(LLM_DIR, "folder")]
    for e in extra or []:
        if e and Path(e).is_dir():
            dirs.append((Path(e), "custom"))
    lms = lmstudio_models_dir() if scan_lmstudio else None
    if lms is not None:
        dirs.append((lms, "lmstudio"))
    return dirs


def _gguf_parts(p: Path) -> bool:
    """Skip vision projectors and every split part but the first (model-00002-of-00003.gguf)."""
    n = p.name.lower()
    if "mmproj" in n or n.endswith(".partial"):
        return False
    import re
    m = re.search(r"-(\d{5})-of-\d{5}\.gguf$", n)
    return m is None or m.group(1) == "00001"


def llm_models(with_size: bool = True, scan_lmstudio: bool = True, extra: list | None = None) -> list[dict]:
    """[{id, name, path, size, source}] for every .gguf in models/llm (+ LM Studio's folder, + extra folders)."""
    out, seen = [], set()
    for d, source in llm_dirs(scan_lmstudio, extra):
        try:
            files = sorted(d.rglob("*.gguf"))
        except OSError:
            continue
        for p in files:
            if not _gguf_parts(p) or p.stem.lower() in seen:
                continue
            seen.add(p.stem.lower())
            m = {"id": p.stem, "name": p.stem, "path": str(p), "source": source}
            if with_size:
                try:
                    m["size"] = p.stat().st_size
                except OSError:
                    m["size"] = 0
            out.append(m)
    return out


def llm_path(model_id: str, scan_lmstudio: bool = True, extra: list | None = None) -> str | None:
    for m in llm_models(False, scan_lmstudio, extra):
        if m["id"] == model_id:
            return m["path"]
    return None


def speaker_model_path() -> Path | None:
    p = SPEAKER_DIR / SPEAKER_FILE
    if p.is_file():
        return p
    if SPEAKER_DIR.is_dir():
        for q in sorted(SPEAKER_DIR.glob("*.onnx")):
            return q
    snaps = hf_hub_dir() / ("models--" + SPEAKER_REPO.replace("/", "--")) / "snapshots"
    for q in sorted(snaps.glob(f"*/{SPEAKER_FILE}")) if snaps.is_dir() else []:
        return q
    return None
