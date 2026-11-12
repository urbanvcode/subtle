# Changelog

All notable changes to Subtle are listed here. Versions follow [Semantic Versioning](https://semver.org/);
the format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

## [0.8.0] - 2026-11-12

### Added
- **Linux** (Experimental): `./install.sh` sets everything up inside the Subtle folder, for NVIDIA (CUDA 13
  or 12), AMD (ROCm: speech and translator on the graphics card), Intel (Vulkan translator) or no GPU.
  Start it from the applications menu or `./subtle.sh`.
- **macOS on Apple M-series** (Experimental): the same `./install.sh`; speech runs on the Apple GPU, the
  translator on Metal. Start it with `Subtle.command`.
- Both are installed and checked automatically on clean GitHub machines with every change
  (the *Install test* badge on the README).
- **Firefox** (Experimental): the extension for Firefox is in `extension-firefox/` and as its own download.
  It subtitles the video on the page (not DRM sites like Netflix).
- **Only one program** for *Subtitles for any app*: under *Listen to*, pick e.g. **Only Discord** and
  Subtle hears just that program (Windows 10 version 2004 or newer).
- **Subtle-Setup-<version>.exe**: an optional Windows installer that asks for a folder and sets Subtle
  up there. The zip and `Install Subtle.bat` work as before.


## [0.7.0] - 2026-11-12

### Added
- **Subtitles for any app**: Discord, Zoom, games, VLC, a film on your PC. Start it on the Home page of the
  Subtle window; Subtle listens to what your speakers play and shows subtitles at the bottom of the screen,
  on top of every window, with clicks passing through. Text size, position, box and screen can be set.
- **Learning mode** (popup → Display): the original line with readings over each word: furigana or romaji
  for Japanese, pinyin for Chinese, romanization for Korean. Click a word to see what it means in that
  sentence; the video pauses until you close the card.
- **Subtle Lite**, a second download: only the server for the browser extension, without the extra
  features and their packages (about 260 MB smaller). The full version has a matching **Simple mode**
  switch in Settings that hides the extras.


## [0.6.0] - 2026-11-12

### Added
- **Save subtitles** as `.srt` or `.vtt` from the popup. The lines are timed to the video itself (also
  after seeking or at other playback speeds), and include the original line if *Original-language line* is on.
- **Catch me up** (popup button or **Alt+Shift+U**): a short summary of the last five minutes, shown on
  the video in your subtitle language. Made by the translator on your PC from lines kept in memory.
- **Move the subtitles** by dragging them on the video. The position is remembered for each website;
  double-click puts them back.
- **Shortcuts**: **Alt+Shift+H** hides or shows the subtitles, **Alt+Shift+L** shows the last line again.
- **Update notice**: the Subtle window (and the popup) tell you when a new version is on GitHub. Once a
  day Subtle asks GitHub for the newest version number; nothing else is sent. Settings can turn it off.

### Changed
- The README and the Setup guide now list every supported browser (Chrome, Edge, Brave, Opera,
  Vivaldi and other Chromium browsers) and use `chrome://extensions`, which works in all of them.

## [0.5.0] - 2026-11-12

First public release.

### Added
- **Subtle app for Windows**: a normal window (Home, Models, Translator, Settings, Setup guide)
  plus a tray icon. No console window and no browser inside.
- **One-click install** (`Install Subtle.bat`): a private Python and pinned packages inside the
  Subtle folder, with desktop and Start-menu shortcuts. Nothing is installed system-wide.
- **Built-in translator** (llama.cpp). LM Studio is no longer needed; any OpenAI-compatible
  server still works as an "external" translator.
- **Model manager**: recommended models for your graphics card, one-click downloads (pinned and
  checksum-checked), and `models\` folders where you can drop your own Whisper folders or `.gguf` files.
- **Language-specific speech models**, picked automatically with the Best engine:
  Parakeet 0.6B for Japanese and SenseVoice Small for Chinese. **Settings → Speech model for each
  language** changes the defaults.
- **Subtitle delay counter** on the Home page: last line, average and median, in milliseconds.
- **Subtitle style options** in the extension popup: presets, text and box colours, box opacity,
  text edge, font, weight, line width and how long lines stay on screen, with a live preview.
- New extension and app icons, and a redesigned popup.

### Changed
- Whisper, Parakeet and SenseVoice run on Subtle's own loader (no transformers, NeMo or FunASR),
  with CUDA graphs on the GPU.
- Start-up takes about 3 seconds instead of 17.
- The translator is set up for subtitles: small context, 8-bit cache, deterministic output, and it
  reuses translations of lines that repeat.

### Performance
- **0 video memory while idle**: models load when a tab starts subtitles and are unloaded 5 minutes
  after the last one (adjustable, or press **Free memory now**).
- Only one speech model in memory at a time; the translator only runs for the Best engine.
- Best engine (Whisper turbo + Qwen3 4B) uses about 5.7 GB of video memory; Fast about 4.1 GB.

[Unreleased]: https://github.com/ddsaamir/subtle/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/ddsaamir/subtle/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/ddsaamir/subtle/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/ddsaamir/subtle/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/ddsaamir/subtle/releases/tag/v0.5.0
