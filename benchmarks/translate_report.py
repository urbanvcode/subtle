import glob, json, os, sys
sys.stdout.reconfigure(encoding="utf-8")
rows = []
for f in sorted(glob.glob("results/translate-*.json"), key=os.path.getmtime):
    d = json.load(open(f, encoding="utf-8"))
    L = d["langs"]
    if not L: continue
    avg = lambda k: sum(L[x][k] for x in L) / len(L)
    rows.append((d["tag"], " ".join(f"{x} {L[x]['chrf']:.1f}" for x in L), avg("chrf"), avg("ms"), avg("prompt_ms"), avg("write_ms"), avg("tokens_out"), avg("ms_per_token"), d.get("vram_mb", 0), d.get("ram_mb", 0)))
print(f"{'setup':22s} {'chrF per language':26s} {'avg':>5s} {'ms/line':>8s} {'prompt':>7s} {'writing':>8s} {'tokens':>7s} {'ms/tok':>7s} {'VRAM':>6s} {'RAM':>6s}")
for r in rows:
    print(f"{r[0]:22s} {r[1]:26s} {r[2]:5.1f} {r[3]:8.1f} {r[4]:7.1f} {r[5]:8.1f} {r[6]:7.1f} {r[7]:7.2f} {r[8]:6d} {r[9]:6d}")
