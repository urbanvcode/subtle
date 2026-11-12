"""Cut every test utterance the way Subtle's engine does (its own voice detector + rules, lines of 5 s or less),
   so each model can also be scored on the short pieces it would really get inside Subtle.
   Run with Subtle's own Python (it has the voice detector):  server\.venv\Scripts\python.exe -I -X utf8 segment.py"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import numpy as np
import server as S

class Seg(S.Session):
    def __init__(self):
        super().__init__(None, None)
        self.cfg.partial_every = None
        self.out = []
    def start_preview(self, frames, pos, full):
        pass
    def start_final(self, job, candidate=None):
        if job.audio is not None:
            self.out.append(job.audio)

refs = json.load(open("data/refs.json", encoding="utf-8"))
for L, items in refs.items():
    os.makedirs(f"data/segs/{L}", exist_ok=True)
    n = dur = kept = 0
    lens = []
    for i, it in enumerate(items):
        a = np.load(it["file"])
        s = Seg()
        s.feed(np.concatenate([a, np.zeros(16000, np.float32)]))
        segs = s.out
        np.savez(f"data/segs/{L}/{i:03d}.npz", *segs)
        n += len(segs); dur += a.size / 16000; kept += sum(x.size for x in segs) / 16000
        lens += [x.size / 16000 for x in segs]
    lens.sort()
    print(f"{L}: {len(items)} sentences -> {n} lines, {kept / dur * 100:.0f}% of the audio kept, "
          f"line length median {lens[len(lens) // 2]:.1f} s, max {lens[-1]:.1f} s, under 1 s: {sum(x < 1 for x in lens)}", flush=True)
