# Subtle speech-model benchmark (Chinese, Japanese, Korean)

Run on 2026-11-12, RTX 5090, Windows 11, Subtle 0.5.0.

## What was measured

- **Test set:** 120 unique sentences per language from the FLEURS test split (Google, CC-BY-4.0):
  `ja_jp`, `cmn_hans_cn`, `ko_kr`, about 25 minutes each. Read-aloud Wikipedia-style sentences, not
  film or anime dialogue. The audio is not included here; get it from FLEURS on Hugging Face (google/fleurs).
- **Two ways per model:**
  - *whole sentences* (about 12 s each), and
  - *Subtle-style lines*: every sentence cut by Subtle's own voice detector and rules into lines of
    5 s or less (`segment.py`, about 290-300 lines per language), which is what a model really gets in Subtle.
- **Accuracy:** character error rate (CER) after the same clean-up for every model: NFKC, lowercase,
  punctuation / symbols / spaces removed, Chinese converted to Simplified (`score.py`).
- **Translation to English:** chrF against the English FLEURS sentence with the same id (`data/en_refs.json`).
- **Speed:** milliseconds per Subtle-style line, model warmed up, one line at a time.
  "Text appears after the speaker stops" = 128 ms (Subtle waits for 4 quiet voice-detector frames) + time per line.
- **Memory:** private RAM and dedicated video memory of the benchmark process, one model per process.

## Files

| File | What it is |
|---|---|
| `prep.py`, `fetch.py` | Extract the test sentences; download the non-Whisper models |
| `segment.py` | Cuts the sentences into Subtle-style lines (uses Subtle's `server.py` logic) |
| `bench.py` | The benchmark: `bench.py <model key> [--only segs|full] [--task translate] [--provider cuda]` |
| `sv_gpu.py` | SenseVoice-Small on the GPU through onnxruntime (own feature extraction and read-out) |
| `best_translate.py` | Subtle's Best mode (Whisper turbo + translator) scored as a translator |
| `score.py`, `report.py` | CER / chrF; builds `results/report.md` from `results/*.json` |
| `results/*.json` | Per model: scores, timings, memory and every transcript (`full_hyp`, `seg_hyp`) |
| `results/*.log` | Console output of each queue |
| `data/`, `models/`, `venv*/` | Not included: `prep.py` rebuilds `data/` from FLEURS and `fetch.py` downloads the models. Some models ran through sherpa-onnx (CPU or CUDA build) |

Result file naming: `<key>.json` = Subtle-style lines (Whisper family: with Subtle's own token limits);
`<key>-full.json` = whole sentences; `<key>-translate.json` = straight to English; `<key>-gpu.json` =
sherpa-onnx CUDA build; `<key>-new.json` = regression re-run after Subtle's loader change (identical text).

## Things to know when reading the numbers

- The first round (Whisper family lines, Qwen, all CPU runs) ran with two queues in parallel, so the
  Qwen and CPU timings are a little inflated. Later rounds ran alone.
- Whisper-family VRAM in `<key>.json` is from before Subtle's block loader (2026-11-12);
  `<key>-new.json` has the lower figures (large-v3 4.1 GB, turbo 2.4 GB).
- SenseVoice: the 2025-09-09 sherpa-onnx export is a Cantonese-tuned variant (wrong on Japanese and
  Korean); the benchmark uses the 2024-07-17 export of SenseVoice-Small.
- Fun-ASR-Nano (sherpa-onnx export 2025-12-30) has no Korean. Its GPU run covers Japanese only (stopped early).
- Paraformer-zh on the sherpa-onnx CUDA build failed on some lines (broadcast error in the decoder), so
  its GPU row is not valid; the CPU row is.
- FireRedASR2, ReazonSpeech and the first Parakeet row are int8 exports made for CPUs. Parakeet also ran
  as a full-size export on the GPU (`parakeet-ja-fp32`).
- whisper-large-v3-turbo-korean (ghost613) states no license: personal testing only.
