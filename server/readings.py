"""
Learning mode: the original line split into words, each with a reading.

- Japanese: furigana (hiragana over words with kanji) or romaji, words from fugashi + unidic-lite
- Chinese: pinyin with tone marks, words from pypinyin's own word list
- Korean: Revised Romanization (letter by letter, without the sound-change rules)

annotate() returns [[surface, reading, lemma], ...]; reading is "" where none is shown (kana, punctuation),
lemma is the dictionary form for looking the word up. Unsupported languages return [].
"""

import re
import threading

_lock = threading.Lock()
_tagger = None
_PUNCT = re.compile(r"^[\s\W_]+$", re.UNICODE)
_KANJI = re.compile(r"[㐀-䶿一-鿿豈-﫿々〆ヵヶ]")

# ---- Japanese ---------------------------------------------------------------------------------
_ROMAJI = {
    "あ": "a", "い": "i", "う": "u", "え": "e", "お": "o", "か": "ka", "き": "ki", "く": "ku", "け": "ke", "こ": "ko",
    "さ": "sa", "し": "shi", "す": "su", "せ": "se", "そ": "so", "た": "ta", "ち": "chi", "つ": "tsu", "て": "te", "と": "to",
    "な": "na", "に": "ni", "ぬ": "nu", "ね": "ne", "の": "no", "は": "ha", "ひ": "hi", "ふ": "fu", "へ": "he", "ほ": "ho",
    "ま": "ma", "み": "mi", "む": "mu", "め": "me", "も": "mo", "や": "ya", "ゆ": "yu", "よ": "yo",
    "ら": "ra", "り": "ri", "る": "ru", "れ": "re", "ろ": "ro", "わ": "wa", "ゐ": "i", "ゑ": "e", "を": "o", "ん": "n",
    "が": "ga", "ぎ": "gi", "ぐ": "gu", "げ": "ge", "ご": "go", "ざ": "za", "じ": "ji", "ず": "zu", "ぜ": "ze", "ぞ": "zo",
    "だ": "da", "ぢ": "ji", "づ": "zu", "で": "de", "ど": "do", "ば": "ba", "び": "bi", "ぶ": "bu", "べ": "be", "ぼ": "bo",
    "ぱ": "pa", "ぴ": "pi", "ぷ": "pu", "ぺ": "pe", "ぽ": "po", "ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o",
    "ゔ": "vu",
}
_YOON = {"ゃ": "a", "ゅ": "u", "ょ": "o"}


def _hira(s: str) -> str:
    return "".join(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in s)


def kana_to_romaji(kana: str) -> str:
    s = _hira(kana)
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        nxt = s[i + 1] if i + 1 < len(s) else ""
        if ch == "っ":                                # double the next consonant
            r = _ROMAJI.get(nxt, "")
            out.append("t" if r.startswith("ch") else r[:1] if r and r[0] not in "aiueon" else "")
            i += 1
            continue
        if ch == "ー":                                # long vowel: repeat the last vowel
            last = next((c for c in reversed("".join(out)) if c in "aiueo"), "")
            out.append(last)
            i += 1
            continue
        r = _ROMAJI.get(ch)
        if r is None:
            out.append(ch)
            i += 1
            continue
        if nxt in _YOON and r.endswith("i") and len(r) > 1:   # きゃ -> kya, しゃ -> sha, ちゃ -> cha, じゃ -> ja
            stem = r[:-1]
            stem = stem if stem in ("sh", "ch", "j") else stem + "y"
            out.append(stem + _YOON[nxt])
            i += 2
            continue
        if ch == "ん" and nxt and _ROMAJI.get(nxt, "x")[0] in "aiueoy":
            out.append("n'")
        else:
            out.append(r)
        i += 1
    return "".join(out)


def _japanese(text: str, style: str):
    global _tagger
    with _lock:
        if _tagger is None:
            import fugashi
            _tagger = fugashi.Tagger()
        words = list(_tagger(text))
    out = []
    kanas = [(getattr(w.feature, "kana", None) or "").replace("*", "") for w in words]
    for i, w in enumerate(words):
        surface = w.surface
        f = w.feature
        kana = kanas[i]
        lemma = getattr(f, "lemma", None) or surface
        if lemma and "-" in lemma:                       # unidic adds English glosses: 行く-他動詞 etc.
            lemma = lemma.split("-")[0]
        if _KANJI.search(surface) and not _KANJI.search(lemma or ""):
            lemma = surface                              # names: unidic gives katakana there
        reading = ""
        if not _PUNCT.match(surface) and kana and kana != "*":
            if style == "romaji":
                # spelled as written (とうきょう -> toukyou); a small っ at the end doubles the next word's consonant
                nxt = next((k for k in kanas[i + 1:] if k), "")
                if kana.endswith(("ッ", "っ")) and nxt:
                    reading = kana_to_romaji(kana + nxt[:1])[:-len(kana_to_romaji(nxt[:1])) or None]
                    reading = reading if reading else kana_to_romaji(kana)
                else:
                    reading = kana_to_romaji(kana)
            elif _KANJI.search(surface):
                reading = _hira(kana)
        out.append([surface, reading, lemma if lemma != "*" else surface])
        # spaces between words are not in the tokens: keep them
        if getattr(w, "white_space", ""):
            out.append([w.white_space, "", ""])
    return out


# ---- Chinese ----------------------------------------------------------------------------------
def _chinese(text: str, style: str):
    from pypinyin import Style, pinyin
    from pypinyin.seg.mmseg import seg
    out = []
    for word in seg.cut(text):
        if _PUNCT.match(word):
            out.append([word, "", ""])
            continue
        syl = [p[0] for p in pinyin(word, style=Style.TONE, errors="ignore")]
        out.append([word, " ".join(syl) if style != "off" else "", word])
    return out


# ---- Korean -----------------------------------------------------------------------------------
_K_INITIAL = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_K_VOWEL = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu",
            "eu", "ui", "i"]
_K_FINAL = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l", "m", "p", "p", "t", "t",
            "ng", "t", "t", "k", "t", "p", "t"]


# A final consonant moves to a following syllable that starts with a vowel (밤에 bame, 있었어 isseosseo).
_K_CARRY = {1: "g", 2: "kk", 4: "n", 7: "d", 8: "r", 16: "m", 17: "b", 19: "s", 20: "ss", 22: "j", 23: "ch",
            24: "k", 25: "t", 26: "p", 27: ""}


def hangul_to_roman(word: str) -> str:
    syl = [(ord(ch) - 0xAC00) if 0 <= ord(ch) - 0xAC00 < 11172 else ch for ch in word]
    out = []
    for i, c in enumerate(syl):
        if isinstance(c, str):
            out.append(c)
            continue
        ini, vow, fin = c // 588, (c % 588) // 28, c % 28
        prev = syl[i - 1] if i else None
        if ini == 11 and isinstance(prev, int) and (prev % 28) in _K_CARRY:
            head = _K_CARRY[prev % 28]                   # this syllable takes over the previous final
        else:
            head = _K_INITIAL[ini]
        nxt = syl[i + 1] if i + 1 < len(syl) else None
        carried = isinstance(nxt, int) and nxt // 588 == 11 and fin in _K_CARRY
        out.append(head + _K_VOWEL[vow] + ("" if carried else _K_FINAL[fin]))
    return "".join(out)


def _korean(text: str, style: str):
    out = []
    for part in re.split(r"(\s+)", text):
        if not part:
            continue
        if part.isspace() or _PUNCT.match(part):
            out.append([part, "", ""])
            continue
        m = re.match(r"^(.*?)([\W_]*)$", part, re.UNICODE)     # keep trailing punctuation apart
        word, tail = m.group(1), m.group(2)
        out.append([word, hangul_to_roman(word) if style != "off" else "", word])
        if tail:
            out.append([tail, "", ""])
    return out


def annotate(text: str, language: str | None, style: str = "furigana") -> list:
    if not text or not language:
        return []
    try:
        if language == "ja":
            return _japanese(text, style)
        if language in ("zh", "yue"):
            return _chinese(text, style)
        if language == "ko":
            return _korean(text, style)
    except Exception:
        return []
    return []
