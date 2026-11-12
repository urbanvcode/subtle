"""Character error rate with the same clean-up for every model: NFKC, lowercase, punctuation / symbols /
   spaces removed, Chinese converted to Simplified."""
import unicodedata

_cc = None


def norm(text: str, lang: str) -> str:
    global _cc
    t = unicodedata.normalize("NFKC", text or "").lower()
    if lang == "zh":
        if _cc is None:
            from opencc import OpenCC
            _cc = OpenCC("t2s")
        t = _cc.convert(t)
    return "".join(c for c in t if unicodedata.category(c)[0] in "LNM")


def edits(a: str, b: str) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(refs, hyps, lang):
    e = n = 0
    for r, h in zip(refs, hyps):
        r, h = norm(r, lang), norm(h, lang)
        e += edits(r, h)
        n += len(r)
    return e / max(1, n)


def chrf(refs, hyps, order: int = 6, beta: float = 2.0) -> float:
    """chrF (character n-gram F-score, 0-100; whitespace ignored) over the whole set: the usual
    automatic measure for translations when there is one reference per sentence. Higher is better."""
    from collections import Counter
    match = [0] * order
    n_hyp = [0] * order
    n_ref = [0] * order
    for r, h in zip(refs, hyps):
        r, h = "".join((r or "").lower().split()), "".join((h or "").lower().split())
        for n in range(1, order + 1):
            cr = Counter(r[i:i + n] for i in range(len(r) - n + 1))
            ch = Counter(h[i:i + n] for i in range(len(h) - n + 1))
            match[n - 1] += sum((cr & ch).values())
            n_hyp[n - 1] += sum(ch.values())
            n_ref[n - 1] += sum(cr.values())
    f = []
    for m, a, b in zip(match, n_hyp, n_ref):
        p, rc = (m / a if a else 0.0), (m / b if b else 0.0)
        f.append((1 + beta**2) * p * rc / (beta**2 * p + rc) if p + rc else 0.0)
    return 100 * sum(f) / order
