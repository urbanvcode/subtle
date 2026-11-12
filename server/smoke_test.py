"""
Quick check that the speech engine works on this PC: loads a Whisper model with Subtle's own
loader and decodes a second of silence and of noise. Usage:

    python server\\smoke_test.py [model id, default: the Best engine's model]
"""

import sys
import time

import numpy as np

import engines

available = engines.cached_whisper_models()
if not available:
    sys.exit("No Whisper model found - download one on the Models page of the Subtle window.")
model = sys.argv[1] if len(sys.argv) > 1 else engines.default_asr_model(available, True)
print("models found:", ", ".join(available))
t = time.perf_counter()
eng = engines.WhisperEngine(model)
eng.warmup()
print(f"loaded {model} in {time.perf_counter() - t:.1f}s")
for name, audio in (("silence", np.zeros(16000, np.float32)),
                    ("noise", (np.random.RandomState(0).randn(16000) * 0.02).astype(np.float32))):
    t = time.perf_counter()
    out = eng.run(audio, "en", "transcribe")
    print(f"{name}: {(time.perf_counter() - t) * 1000:.0f} ms -> {out!r}")
print("OK")
