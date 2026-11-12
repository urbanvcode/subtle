"""Extract N FLEURS test utterances per language to 16 kHz float32 .npy + a refs.json."""
import io, json, os, sys
import numpy as np, pyarrow.parquet as pq, soundfile as sf
N = 120
out = {}
for L in ["ja_jp", "cmn_hans_cn", "ko_kr"]:
    t = pq.read_table(f"data/{L}.parquet").to_pylist()
    seen, rows = set(), []
    for r in t:                       # FLEURS repeats sentences with different speakers: keep one each
        if r["raw_transcription"] in seen:
            continue
        seen.add(r["raw_transcription"])
        rows.append(r)
        if len(rows) == N:
            break
    os.makedirs(f"data/{L}", exist_ok=True)
    items = []
    for i, r in enumerate(rows):
        a = r["audio"]
        wav, sr = sf.read(io.BytesIO(a["bytes"]), dtype="float32") if a.get("bytes") else sf.read(a["path"], dtype="float32")
        if wav.ndim > 1:
            wav = wav.mean(1)
        assert sr == 16000, sr
        np.save(f"data/{L}/{i:03d}.npy", wav.astype(np.float32))
        items.append({"file": f"data/{L}/{i:03d}.npy", "ref": r["raw_transcription"], "sec": round(len(wav) / sr, 2)})
    out[L] = items
    print(L, len(items), "utterances,", round(sum(x["sec"] for x in items) / 60, 1), "min audio; e.g.", items[0]["ref"][:60])
json.dump(out, open("data/refs.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
