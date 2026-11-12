"""
Streams a raw 16 kHz mono float32 file to the server in real time, like the extension does,
and prints each subtitle with the time it arrived, the server's compute time (ms) and its lag:
how long after the line's last bit of speech was played the subtitle showed up.

  python test_client.py speech.f32 --language ja [--pause-at 3] [--seek-at 6]

Make a .f32 file with:  ffmpeg -i clip.mp4 -ac 1 -ar 16000 -f f32le clip.f32
"""
import argparse
import asyncio
import json
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
from websockets.asyncio.client import connect

SR = 16000
CHUNK = 512  # 32 ms (one VAD frame), same as the extension


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--url", default="ws://127.0.0.1:8765")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--target", default="en")
    ap.add_argument("--mode", default="whisper", choices=["whisper", "llm"])
    ap.add_argument("--asr-model", default="auto")
    ap.add_argument("--llm-model", default="")
    ap.add_argument("--quiet", action="store_true", help="only print finished lines")
    ap.add_argument("--save-transcript", action="store_true", help="ask the server to save a transcript")
    ap.add_argument("--speakers", action="store_true", help="identify speakers")
    ap.add_argument("--pause-at", type=float, help="send pause at this many seconds, resume 2 s later")
    ap.add_argument("--seek-at", type=float, help="send seek at this many seconds")
    args = ap.parse_args()

    audio = np.fromfile(args.file, dtype="<f4")
    audio = np.concatenate([audio, np.zeros(SR, dtype=np.float32)])  # trailing silence ends the last line
    t0 = time.perf_counter()
    sent = []  # (samples sent so far, time): the clock that turns a line's position "t" into a lag

    def lag(msg, now):
        if "t" not in msg:
            return ""
        pos = msg["t"] * SR
        for n, when in sent:
            if n >= pos:
                return f", lag {(now - (when - (n - pos) / SR)) * 1000:.0f} ms"
        return ""

    async with connect(args.url, origin="chrome-extension://test-client") as ws:
        await ws.send(json.dumps({
            "type": "config", "language": args.language, "target": args.target, "mode": args.mode,
            "asr_model": args.asr_model, "llm_model": args.llm_model,
            "save_transcript": args.save_transcript, "page_title": "test_client", "speakers": args.speakers,
        }))

        async def receive():
            async for raw in ws:
                msg = json.loads(raw)
                now = time.perf_counter() - t0
                if msg["type"] == "sub":
                    if args.quiet and not msg["final"]:
                        continue
                    kind = "FINAL  " if msg["final"] else "partial"
                    src = f"  [{msg['src']}]" if msg.get("src") else ""
                    who = f"[S{msg['speaker']}] " if msg.get("speaker") else ("[S?] " if args.speakers and msg["final"] else "")
                    print(f"{now:6.2f}s  {kind} #{msg['id']} ({msg['ms']} ms{lag(msg, now)}): {who}{msg['text']}{src}")
                else:
                    print(f"{now:6.2f}s  {msg}")

        rx = asyncio.create_task(receive())
        paused_done = False
        seek_done = False
        start = time.perf_counter()
        held = 0.0  # time spent paused
        for i in range(0, audio.size, CHUNK):
            t = i / SR
            if args.pause_at is not None and not paused_done and t >= args.pause_at:
                paused_done = True
                print(f"{time.perf_counter() - t0:6.2f}s  >> pause")
                await ws.send(json.dumps({"type": "pause"}))
                await asyncio.sleep(2)
                held += 2
                print(f"{time.perf_counter() - t0:6.2f}s  >> play")
                await ws.send(json.dumps({"type": "play"}))
            if args.seek_at is not None and not seek_done and t >= args.seek_at:
                seek_done = True
                print(f"{time.perf_counter() - t0:6.2f}s  >> seek")
                await ws.send(json.dumps({"type": "seek"}))
            # a chunk goes out when its last sample has "played", on a steady clock like the browser's
            await asyncio.sleep(max(0.0, start + held + (i + CHUNK) / SR - time.perf_counter()))
            await ws.send(audio[i:i + CHUNK].tobytes())
            sent.append((min(i + CHUNK, audio.size), time.perf_counter() - t0))
        await asyncio.sleep(1.5)
        rx.cancel()
    print(f"audio length {audio.size / SR:.1f}s")


asyncio.run(main())
