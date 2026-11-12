#!/usr/bin/env bash
# Subtle installer for Linux and macOS (Apple M-series). Everything goes inside this folder:
# a private Python (uv), Subtle's packages, and the built-in translator (llama.cpp).
#
#   ./install.sh                 detect the graphics card
#   ./install.sh --gpu nvidia    or: nvidia-old | amd | intel | cpu
#
# Run it again to repair or update. To uninstall, delete this folder (and ~/.local/share/applications/subtle.desktop).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INST="$ROOT/installer"
RUNTIME="$ROOT/runtime"
DOWNLOADS="$RUNTIME/downloads"
LOGS="$ROOT/logs"
mkdir -p "$DOWNLOADS" "$LOGS"
exec > >(tee "$LOGS/install.log") 2>&1

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nSetup stopped: %s\nThe full log is in logs/install.log\n' "$*"; exit 1; }

GPU=""
while [ $# -gt 0 ]; do
  case "$1" in
    --gpu) GPU="${2:-}"; shift 2 ;;
    *) fail "unknown option $1 (use --gpu nvidia|nvidia-old|amd|intel|cpu)" ;;
  esac
done

# ------------------------------------------------------------------------------------------------
step "Checking this computer"
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS-$ARCH" in
  Linux-x86_64)  PLATFORM="linux-x64" ;;
  Linux-aarch64) PLATFORM="linux-arm64" ;;
  Darwin-arm64)  PLATFORM="macos-arm64" ;;
  Darwin-*)      fail "Subtle needs a Mac with an Apple M-series chip." ;;
  *)             fail "This setup is for Linux (x86-64) and macOS (Apple M-series). On Windows, run Install Subtle.bat." ;;
esac
say "  $OS $ARCH"

if [ "$PLATFORM" = "macos-arm64" ]; then
  VARIANT="macos-arm64"
  say "  Apple M-series chip (Experimental: speech runs on the Apple GPU where it can)"
else
  if [ -z "$GPU" ]; then
    if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
      DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)"
      say "  NVIDIA $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1), driver $DRIVER"
      if [ "${DRIVER%%.*}" -ge 580 ]; then GPU="nvidia"; else GPU="nvidia-old"; fi
    elif command -v lspci >/dev/null 2>&1 && lspci | grep -Ei 'vga|3d|display' | grep -qi 'amd\|ati\|radeon'; then
      GPU="amd"; say "  AMD graphics (Experimental)"
    elif command -v lspci >/dev/null 2>&1 && lspci | grep -Ei 'vga|3d|display' | grep -qi 'intel'; then
      GPU="intel"; say "  Intel graphics (Experimental)"
    else
      GPU="cpu"; say "  No supported graphics card found: Subtle will run on the processor (slow; use small models and Fast)"
    fi
  fi
  case "$GPU" in
    nvidia) VARIANT="linux-cuda13" ;;
    nvidia-old) VARIANT="linux-cuda12" ;;
    amd) VARIANT="linux-rocm" ;;
    intel) VARIANT="linux-vulkan" ;;
    cpu) VARIANT="linux-cpu" ;;
    *) fail "unknown --gpu $GPU" ;;
  esac
  if [ "$PLATFORM" = "linux-arm64" ]; then VARIANT="linux-cpu"; fi
fi
say "  Engine: $VARIANT"
FREE_KB="$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')"
[ "${FREE_KB:-0}" -gt 8000000 ] || fail "Less than 8 GB free on this drive."

sha256() { if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1; else shasum -a 256 "$1" | cut -d' ' -f1; fi; }
fetch() {   # url file sha256
  if [ -f "$2" ] && [ "$(sha256 "$2")" = "$3" ]; then say "  $(basename "$2"): already downloaded"; return; fi
  say "  Downloading $(basename "$2")"
  curl -fL --retry 3 --progress-bar -o "$2.partial" "$1" || fail "Could not download $1"
  [ "$(sha256 "$2.partial")" = "$3" ] || { rm -f "$2.partial"; fail "$(basename "$2") is damaged (checksum mismatch). Try again."; }
  mv "$2.partial" "$2"
}

# ------------------------------------------------------------------------------------------------
step "Getting the Python manager (uv)"
UVLINE="$(grep "\"$PLATFORM\"" "$INST/versions-unix.json" | head -1)"
UV_URL="$(printf '%s' "$UVLINE" | sed -E 's/.*"url": "([^"]+)".*/\1/')"
UV_SHA="$(printf '%s' "$UVLINE" | sed -E 's/.*"sha256": "([^"]+)".*/\1/')"
fetch "$UV_URL" "$DOWNLOADS/uv-$PLATFORM.tar.gz" "$UV_SHA"
mkdir -p "$RUNTIME/uv"
tar -xzf "$DOWNLOADS/uv-$PLATFORM.tar.gz" -C "$RUNTIME/uv" --strip-components=1
UV="$RUNTIME/uv/uv"
export UV_PYTHON_INSTALL_DIR="$RUNTIME/python" UV_CACHE_DIR="$RUNTIME/uv-cache"

step "Getting Python 3.12"
"$UV" python install 3.12 || fail "Could not get Python 3.12."
VENV="$RUNTIME/venv"
[ -x "$VENV/bin/python" ] || "$UV" venv "$VENV" --python 3.12 || fail "Could not create the Python environment."
PY="$VENV/bin/python"

json() { "$PY" - "$INST/versions-unix.json" "$@" <<'EOF'
import json, sys
d = json.load(open(sys.argv[1]))
what = sys.argv[2]
if what == "torch":
    print(d["torch_index"][sys.argv[3]])
elif what == "llama":
    for f in d["llama_cpp"]["variants"][sys.argv[3]]["files"]:
        print(f["name"], f["url"], f["sha256"])
elif what == "build":
    print(d["llama_cpp"]["build"])
EOF
}

step "Installing Subtle's packages (about 3 GB the first time - this takes a while)"
INDEX="$(json torch "$VARIANT")"
TORCH_PINS="$(grep -E '^(torch|torchaudio)==' "$INST/requirements.txt" | tr '\n' ' ')"
if [ "$VARIANT" = "macos-arm64" ]; then
  "$UV" pip install --python "$PY" $TORCH_PINS || fail "Could not install PyTorch."
else
  "$UV" pip install --python "$PY" --index-url "$INDEX" $TORCH_PINS || fail "Could not install PyTorch from $INDEX."
fi
"$UV" pip install --python "$PY" -r "$INST/requirements.txt" || fail "Could not install the Python packages."
if [ "$(cat "$ROOT/edition.txt" 2>/dev/null | tr -d '[:space:]')" != "lite" ]; then
  "$UV" pip install --python "$PY" -r "$INST/requirements-extras.txt" \
    || say "  Warning: the packages for the extra features did not install. Everything else works."
fi
"$UV" cache clean >/dev/null 2>&1 || true

step "Checking the installation"
"$PY" -c "import torch, tokenizers, safetensors, websockets, silero_vad, onnxruntime; print('  torch', torch.__version__, '| NVIDIA/AMD GPU:', torch.cuda.is_available(), '| Apple GPU:', torch.backends.mps.is_available())" \
  || fail "The Python packages don't load."

# ------------------------------------------------------------------------------------------------
BUILD="$(json build)"
step "Getting the built-in translator (llama.cpp $BUILD)"
LLAMA="$ROOT/bin/llama"; STAMP="$LLAMA/subtle-build.txt"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$BUILD $VARIANT" ] && [ -x "$LLAMA/llama-server" ]; then
  say "  Already installed"
else
  rm -rf "$LLAMA"; mkdir -p "$LLAMA"
  TMP="$DOWNLOADS/llama-unpack"; rm -rf "$TMP"; mkdir -p "$TMP"
  json llama "$VARIANT" | while read -r NAME URL SHA; do
    fetch "$URL" "$DOWNLOADS/$NAME" "$SHA"
    tar -xzf "$DOWNLOADS/$NAME" -C "$TMP"
  done
  # the archives keep their files in a sub-folder: put everything next to llama-server
  find "$TMP" -type f -exec cp -a {} "$LLAMA/" \;
  find "$TMP" -type l -exec cp -a {} "$LLAMA/" \;
  rm -rf "$TMP"
  [ -x "$LLAMA/llama-server" ] || chmod +x "$LLAMA/llama-server" 2>/dev/null || true
  [ -f "$LLAMA/llama-server" ] || fail "llama-server is missing from the download."
  if [ "$VARIANT" = "macos-arm64" ]; then xattr -dr com.apple.quarantine "$LLAMA" 2>/dev/null || true; fi
  printf '%s %s' "$BUILD" "$VARIANT" > "$STAMP"
fi
"$LLAMA/llama-server" --version >/dev/null 2>&1 || say "  Warning: llama-server does not start on this system (the translator may not work)."

# ------------------------------------------------------------------------------------------------
step "Creating the Subtle launcher"
chmod +x "$ROOT/subtle.sh"
if [ "$VARIANT" = "macos-arm64" ]; then
  cat > "$ROOT/Subtle.command" <<EOF
#!/bin/bash
exec "$ROOT/subtle.sh"
EOF
  chmod +x "$ROOT/Subtle.command"
  say "  Double-click Subtle.command in this folder to start Subtle."
else
  APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$APPS"
  cat > "$APPS/subtle.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Subtle
Comment=Real-time subtitles and translation
Exec="$ROOT/subtle.sh"
Path=$ROOT
Icon=$ROOT/server/assets/subtle.png
Terminal=false
Categories=AudioVideo;Utility;
EOF
  say "  Subtle is in your applications menu (or run ./subtle.sh)."
fi

step "Done"
say "  Start Subtle, then add the browser extension: open chrome://extensions, turn on Developer mode,"
say "  click Load unpacked and choose: $ROOT/extension"
