# Subtle

**Real-time subtitles and translation for any video in your browser, generated on your own PC.**

Japanese, Chinese, Korean and ~95 other languages, turned into subtitles while you watch, on any
website. Speech recognition and translation both run locally on your graphics card: nothing you
watch is sent to the internet.

```
video in a browser tab ─► Subtle extension ─► Subtle app on your PC ─► subtitles over the video
                                              ├─ Fast: Whisper translates straight to English
                                              └─ Best: Whisper writes the speech down, a built-in
                                                       translator model turns it into any language
```

---

## What you need

| | Minimum | Recommended |
|---|---|---|
| System | Windows 10 or 11, 64-bit | |
| Browser | Brave or Chrome | |
| Graphics card | NVIDIA GTX 16-series / RTX 20-series or newer, **6 GB** video memory | NVIDIA RTX 3060 / 4060 or better, **8 GB+** |
| Memory (RAM) | 8 GB | 16 GB |
| Disk | 8 GB + models (2-10 GB) | |
| Internet | only for the install and model downloads | |

- **Older NVIDIA cards (GTX 10-series)** work, but more slowly.
- **AMD / Intel graphics, or no graphics card**: Subtle still installs. Speech recognition then runs
  on the processor, which is slow, so use the small or medium speech model and the Fast engine.
- Keep your NVIDIA driver reasonably up to date. The installer tells you if it's too old.

## Install (about 10 minutes)

1. **Download Subtle** and unzip it somewhere with free space, e.g. `D:\Subtle`.
2. **Double-click `Install Subtle.bat`.** It sets up everything inside that folder (a private
   Python, the translator engine) and creates a **Subtle** shortcut on the desktop and in the Start
   menu. Nothing is installed system-wide.
3. The **Subtle window** opens. Follow its **Setup guide**:
   - **Download the recommended models.** It picks them for your graphics card, one click.
   - **Add the browser extension:** open `brave://extensions` (or `chrome://extensions`), turn on
     **Developer mode**, click **Load unpacked**, and choose the `extension` folder inside Subtle.
     (The Setup guide has a button that copies the exact path.)
4. Open a video, click the Subtle icon in the browser toolbar, then **Start subtitles on this tab**
   (or press **Alt+Shift+S**).

From then on, just start **Subtle** from the desktop shortcut. Closing its window keeps it running
in the tray (bottom right, next to the clock). It uses no video memory until you turn subtitles on;
click the tray icon to open the window again. Want it always ready?
**Settings → Start Subtle when Windows starts.**

> Windows SmartScreen or your antivirus may ask about `Install Subtle.bat` the first time, because
> it's a new, unsigned download. The installer only downloads from GitHub (the `uv` Python manager
> and llama.cpp, both checksum-checked), PyTorch and PyPI (pinned package versions). Models come from
> Hugging Face at pinned versions and are checksum-checked too.

## Which models should I use?

The **Models** page and the Setup guide recommend a set for your graphics card automatically.

| Your graphics card | Speech (Whisper) | Translator (for Best) | Video memory in use |
|---|---|---|---|
| 4 GB | medium | - (use Fast) | ~2 GB |
| 6 GB | large-v3 + turbo | Qwen3.5 2B | Fast ~4.1 GB · Best ~4.3 GB |
| **8-11 GB (recommended)** | large-v3 + turbo | **Qwen3 4B Instruct 2507** | Fast ~4.1 GB · Best ~5.7 GB |
| 12-15 GB | large-v3 + turbo | Qwen3.5 9B | Best ~8.7 GB |
| 16 GB+ | large-v3 + turbo | Gemma 4 12B | Best ~10 GB |

And by spoken language (Subtle picks these by itself when they are installed):

| Spoken language | Fast | Best |
|---|---|---|
| Japanese | Whisper large-v3 | **Parakeet 0.6B** + translator |
| Chinese | Whisper large-v3 | **SenseVoice Small** + translator |
| Korean and every other language | Whisper large-v3 | Whisper large-v3 turbo + translator |

These come from our own benchmark (120 test sentences per language, cut into lines the way Subtle
cuts them; wrong characters, lower is better):

| | Japanese | Chinese | Korean | Speech step per line |
|---|---|---|---|---|
| Whisper large-v3 | 6.1 % | 10.5 % | 5.4 % | ~95 ms |
| Whisper large-v3 turbo | 7.0 % | 11.7 % | 6.3 % | ~40 ms |
| Parakeet 0.6B (Japanese) | 6.2 % | - | - | ~8 ms |
| SenseVoice Small | 11.7 % | 9.3 % | 9.4 % | ~6 ms |

The test sentences are read aloud (FLEURS), not film or anime dialogue, so treat the numbers as a
guide. With the Best engine the translator adds about 60 ms per line.

- **Fast** (Whisper only): straight to English, the least memory, about 70 ms per line.
- **Best** (Whisper + translator): more natural lines, uses the previous lines as context, and works
  for **any** subtitle language, at about 95 ms per line.
- **Japanese** with Best: download **Parakeet 0.6B (Japanese)** on the Models page and Subtle uses it
  by itself. It reads a whole line in one pass (about 8 ms instead of 43 ms for the speech step) and
  was a little more accurate than Whisper turbo on test sentences. Any other speech model can still
  be picked in the popup at any time.
- **Chinese** with Best: download **SenseVoice Small (Chinese)** and Subtle uses it by itself. It was
  clearly more accurate than Whisper turbo on Chinese test sentences (9.3 % against 11.7 % wrong
  characters) and takes about 6 ms instead of 40 ms for the speech step.
- Parakeet and SenseVoice only write speech down; they can't translate. So they are used where the
  translator follows (Best). With **Fast**, Subtle keeps Whisper large-v3, which translates by itself.
- **Settings → Speech model for each language** changes what "Auto" means for Chinese, Japanese,
  Korean and all other languages. A model picked in the popup still wins.
- You can add your own models. Put a Whisper model folder into `models\whisper\`, or any instruct
  `.gguf` file into `models\llm\`, then press **Rescan**. Models you already downloaded with
  **LM Studio** are found automatically, so there's no need to download them twice.

## How Subtle saves memory

Subtle only uses memory while you're actually watching something with subtitles:

| State | Video memory (VRAM) | Memory (RAM) |
|---|---|---|
| Waiting in the tray (window open or closed) | **0** | ~40 MB |
| Fast (Whisper large-v3) | ~4.1 GB | ~0.1 GB |
| Best (Whisper turbo + Qwen3 4B) | ~5.7 GB | ~0.15 GB |
| Best, Japanese (Parakeet + Qwen3 4B) | ~5.0 GB | ~0.15 GB |
| Best, Chinese (SenseVoice + Qwen3 4B) | ~4.8 GB | ~0.15 GB |

- Nothing is loaded until a tab starts subtitles. Starting takes about 3-5 seconds; the sentence
  being spoken meanwhile still gets its subtitle.
- The Subtle window is a plain Windows window (no browser inside), about 15 MB.
- Starting the speech engine takes about 1.5 GB of RAM for a moment. A few seconds after a model has
  loaded, the engine hands that start-up memory back to Windows and keeps about 50 MB in use.
- The speech model is loaded into exactly-sized blocks of video memory, without the gaps a normal
  load leaves behind (0.3-0.5 GB less).
- **Five minutes after the last subtitle everything is unloaded** (Settings → Memory: 1-30 minutes,
  or keep models loaded for instant starts). **Free memory now** on the Home page or in the tray
  menu does it at once.
- Only one speech model is in memory at a time, and the translator only runs for the Best engine.
- The translator is set up for subtitles: a small context and a compact 8-bit cache. That's about
  25% less video memory than a general chat setup.
- **Previews** (the line updating while someone speaks) are the main GPU work. Settings → Memory can
  make them less frequent, or turn them off.
- The Home page shows exactly how much video memory and RAM each part of Subtle is using.

## Privacy

- **Nothing you watch leaves your PC.** The extension only talks to the Subtle app on your PC
  (`127.0.0.1`), and the app talks to nothing else. The Subtle window and its connection only work on this PC.
- **Subtitles live in memory.** No subtitle text or audio is written to disk, unless you turn on
  **Save transcripts** in the extension popup (one text file per session in `transcripts\`).
- **Speaker voiceprints** (Identify speakers, off by default) are kept in memory only.
- **Error log:** `logs\errors.log` keeps errors only, never subtitle text (about 1 MB at most).
- The internet is used only by `Install Subtle.bat` and by the **Download** buttons on the Models page.

## Troubleshooting

| Problem | What to do |
|---|---|
| The popup says "Subtle isn't running" | Start **Subtle** from the desktop or Start-menu shortcut. Look for its icon in the tray. |
| The first subtitle takes a few seconds | Normal: models load when you start (3-5 s). After that lines take ~0.1 s. To skip it, turn on Settings → Keep models loaded. |
| "No translator model" | Download one on the **Models** page (Best needs a translator; Fast doesn't). |
| Netflix, Prime Video, Disney+... show nothing with Auto-start | DRM video can't be read from the page. Click the Subtle icon (or press Alt+Shift+S) to use tab capture instead. |
| Out of video memory / games stutter | Use Fast, pick a smaller translator, set Previews to "every second", or press **Free memory now**. |
| Installer: "NVIDIA driver too old" | Update it from [nvidia.com/drivers](https://www.nvidia.com/drivers) or the NVIDIA app, then run `Install Subtle.bat` again. |
| Anything else | Subtle window → Settings → **Show activity log**, and the install log in `logs\install.log`. |

**Update:** replace the Subtle files with the new version (keep `models\`, `runtime\` and
`settings.json`), run `Install Subtle.bat` again, and reload the extension in `brave://extensions`.

**Uninstall:** quit Subtle (tray icon → Quit), untick "Start with Windows" if you turned it on,
delete the Subtle folder and the two shortcuts, and remove the extension from the browser.

---

## Popup settings (browser extension)

| Setting | What it does |
|---|---|
| Spoken → Subtitles in | Source language (or auto-detect) and subtitle language |
| **Fast** / **Best** | See above |
| Speech model | Auto picks large-v3 for Fast and turbo for Best (with Best: Parakeet for Japanese and SenseVoice for Chinese, if installed; changeable in the Subtle window's Settings) |
| Translator | Any installed translator; the default is set on the Translator page of the Subtle window |
| Show original-language line | With Best, shows the transcribed original under the translation |
| Identify speakers | Gives each voice its own colour (26 MB voiceprint model, Models page) |
| Auto-start when a video plays | Subtitles start by themselves on any tab where a video plays with sound (not for DRM video) |

## For developers

| Part | File | Job |
|---|---|---|
| Supervisor | `server/supervisor.py` | Always-on app (no torch, ~40 MB with its window): tray, relays tabs to the engine, starts and stops the engine and translator, idle unload |
| Window | `server/ui.py`, `server/tray.py` | The Subtle window (Tkinter) and tray icon; they talk to the supervisor in-process |
| Engine | `server/server.py`, `engines.py`, `whisper_core.py`, `parakeet_core.py`, `sensevoice_core.py` | VAD segmentation, previews and finished lines. Whisper, Parakeet and SenseVoice are loaded and run by Subtle's own code (no transformers, NeMo or FunASR): CUDA graphs on the GPU, eager on the CPU. Started on demand by the supervisor |
| Translator | `server/llm_runner.py`, `llm_client.py` | Runs `bin/llama/llama-server.exe`; any OpenAI-compatible server works as "external" |
| Models | `server/modelstore.py`, `downloads.py`, `models/catalog.json` | Finds models (models/, HF cache, LM Studio); pinned, checksummed downloads |
| Extension | `extension/` | Captures tab audio and draws the overlay |
| Installer | `Install Subtle.bat`, `installer/` | uv + private Python, pinned packages (`requirements.txt`), pinned llama.cpp build (`versions.json`) |

Testing:

```
python server\smoke_test.py
python server\supervisor.py --no-window --no-tray --port 8799
python server\test_client.py clip.f32 --url ws://127.0.0.1:8799 --language ja
python server\test_client.py clip.f32 --url ws://127.0.0.1:8799 --mode llm --target fr
```

Through the supervisor, the engine and translator start by themselves. `server.py --verbose` runs
the engine alone (it then accepts extension origins on port 8765 itself). `clip.f32` is 16 kHz mono float32 PCM
(`ffmpeg -i clip.mp4 -ac 1 -ar 16000 -f f32le clip.f32`).

## Licenses

Subtle's code is MIT-licensed (see `LICENSE`). Models are downloaded from their authors on Hugging Face under their
own licenses, shown on the Models page:
- Whisper large-v3, medium, small (OpenAI): Apache-2.0; Whisper large-v3 turbo (OpenAI): MIT
- Parakeet-TDT-CTC 0.6B ja (NVIDIA): CC-BY-4.0
- SenseVoice Small (FunAudioLLM, Alibaba): FunASR Model License (see its model page)
- Kotoba-Whisper v2.0 and Bilingual v1.0 (Kotoba Technologies): Apache-2.0
- Anime-Whisper (litagin): MIT
- Qwen3 and Qwen3.5 (Alibaba Cloud): Apache-2.0
- Gemma 4 (Google): Apache-2.0
- WeSpeaker ResNet34 voiceprints (WeSpeaker): CC-BY-4.0

Subtle also uses llama.cpp (MIT) for the translator, Silero VAD (MIT) to hear when someone speaks,
and PyTorch (BSD-3-Clause). The benchmark numbers above were measured on the FLEURS test set
(Google, CC-BY-4.0).
