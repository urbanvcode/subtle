# Changelog

All notable changes to Subtle are listed here. Versions follow [Semantic Versioning](https://semver.org/);
the format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

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

[Unreleased]: https://github.com/ddsaamir/subtle/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/ddsaamir/subtle/releases/tag/v0.5.0
