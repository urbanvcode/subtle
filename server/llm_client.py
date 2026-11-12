"""
Client for the translator: any OpenAI-compatible chat server (Subtle's built-in llama.cpp server,
LM Studio, Ollama, ...). Standard library only, so the supervisor can use it without torch.
"""

import http.client
import json
import re
import socket
import threading
import time
import urllib.parse


class Cancelled(Exception):
    """Raised by WhisperEngine.run / llm_translate when the caller no longer wants the result."""



LANG_NAMES = {
    "auto": "the spoken language", "en": "English", "ja": "Japanese", "zh": "Chinese", "ko": "Korean",
    "es": "Spanish", "fr": "French", "de": "German", "it": "Italian", "pt": "Portuguese", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "hi": "Hindi", "ar": "Arabic", "tr": "Turkish",
    "ur": "Urdu", "nl": "Dutch", "pl": "Polish", "uk": "Ukrainian", "ms": "Malay", "tl": "Tagalog",
    "bn": "Bengali", "fa": "Persian", "he": "Hebrew", "sv": "Swedish",
}


def lang_name(code: str | None) -> str:
    return LANG_NAMES.get(code or "auto", code or "the spoken language")


# ---------------------------------------------------------------------------------------------
# LLM translation (OpenAI-compatible: LM Studio, Ollama, llama.cpp server, ...)
# ---------------------------------------------------------------------------------------------
THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class LLMCall:
    """Handle for one llm_translate call, so another thread can abort it (closing the socket also
    makes LM Studio / llama.cpp stop generating)."""

    def __init__(self):
        self.cancelled = False
        self._conn = None
        self._lock = threading.Lock()

    def attach(self, conn):
        with self._lock:
            self._conn = conn
            if self.cancelled:
                self._close()

    def detach(self) -> bool:
        """The request is over: let go of the connection. False if it was cancelled meanwhile (its
        socket is then shut down and must not be used again)."""
        with self._lock:
            self._conn = None
            return not self.cancelled

    def cancel(self):
        with self._lock:
            self.cancelled = True
            self._close()

    def _close(self):
        conn, self._conn = self._conn, None
        sock = getattr(conn, "sock", None)
        try:
            if sock is not None:
                sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Connections:
    """
    Connections to the translator that are kept open between calls. Opening a new one costs about
    11 ms with llama-server (measured), a kept one about 1 ms, and Subtle asks for a translation
    several times per spoken line. A connection is used by one call at a time; calls that overlap
    simply get one each.
    """

    MAX_IDLE = 4          # kept per server
    MAX_AGE = 60.0        # seconds unused; servers close idle connections themselves (a retry covers that)

    def __init__(self):
        self._idle: dict[tuple, list] = {}       # server -> [(connection, time it was put back)]
        self._lock = threading.Lock()

    def take(self, key):
        now = time.monotonic()
        stale = []
        try:
            with self._lock:
                for k in list(self._idle):       # also forgets servers that are gone (the translator restarted)
                    fresh = [(c, t) for c, t in self._idle[k] if now - t <= self.MAX_AGE]
                    stale += [c for c, t in self._idle[k] if now - t > self.MAX_AGE]
                    if fresh:
                        self._idle[k] = fresh
                    else:
                        del self._idle[k]
                conns = self._idle.get(key)
                return conns.pop()[0] if conns else None
        finally:
            for c in stale:
                c.close()

    def give(self, key, conn):
        with self._lock:
            conns = self._idle.setdefault(key, [])
            if len(conns) < self.MAX_IDLE:
                conns.append((conn, time.monotonic()))
                return
        conn.close()


_CONNECTIONS = _Connections()


def _http_json(url: str, body: dict | None = None, timeout: float = 30, call: LLMCall | None = None):
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    key = (u.scheme, u.hostname, u.port)
    path = u.path + (f"?{u.query}" if u.query else "")
    payload = None if body is None else json.dumps(body).encode()
    for attempt in (0, 1):
        conn = _CONNECTIONS.take(key) if attempt == 0 else None
        reused = conn is not None
        try:
            if reused:
                conn.sock.settimeout(timeout)
            else:
                conn = cls(u.hostname, u.port, timeout=timeout)
                conn.connect()
                conn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if call is not None:
                call.attach(conn)
            if payload is None:
                conn.request("GET", path)
            else:
                conn.request("POST", path, payload, {"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
        except Exception as e:
            if conn is not None:
                conn.close()
            if call is not None and call.cancelled:
                raise Cancelled() from None
            # A kept connection that the server closed while it was idle fails right away, before
            # anything was answered: ask once more on a new connection. Timeouts are real errors.
            if reused and isinstance(e, (ConnectionError, http.client.HTTPException)) and not isinstance(e, TimeoutError):
                continue
            raise
        alive = call.detach() if call is not None else True
        if alive and not resp.will_close:
            _CONNECTIONS.give(key, conn)
        else:
            conn.close()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status}: {data[:200].decode('utf-8', 'replace')}")
        return json.loads(data)


def list_llm_models(base_url: str) -> list[str]:
    data = _http_json(base_url.rstrip("/") + "/models", timeout=3)
    ids = [m.get("id", "") for m in data.get("data", [])]
    return [i for i in ids if i and "embed" not in i.lower()]


def llm_summary(base_url: str, model: str, lines: list[str], language: str, timeout: float = 90) -> str:
    """"Catch me up": a few short bullet points about what was said in `lines` (subtitles, oldest first)."""
    name = lang_name(language)
    system = (
        f"You help a viewer who missed part of a video. Read its subtitles and tell them, in {name}, what "
        f"happened and what it was about: 2 to 4 short bullet points starting with '- ', in order, at most 70 "
        f"words in total. Summarise in your own words, like a friend explaining it: who wants what, what was "
        f"decided, what changed. Do not quote or copy the lines. Use only what is in the subtitles and never "
        f"invent names or events. Subtitles can contain recognition mistakes: ignore lines that make no sense. "
        f"No heading, no introduction."
    )
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 240,
        "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": "Subtitles of the last few minutes:\n" + "\n".join(lines)}],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = _http_json(base_url.rstrip("/") + "/chat/completions", body, timeout=timeout)
    out = data["choices"][0]["message"].get("content") or ""
    out = THINK.sub("", out).strip()
    if "</think>" in out:
        out = out.split("</think>")[-1].strip()
    return out


def llm_define(base_url: str, model: str, word: str, context: str, source: str | None, target: str,
               surface: str = "", reading: str = "", timeout: float = 60) -> str:
    """Learning mode: what one word of a subtitle means, for a learner, in the subtitle language."""
    src_name, tgt_name = lang_name(source), lang_name(target)
    system = (
        f"You are a friendly {src_name} teacher. A learner clicked a word in a subtitle. Answer in {tgt_name}, "
        f"in at most three short lines and 45 words: 1) the word in its dictionary form with its reading"
        f"{' (kana and romaji)' if source == 'ja' else ' (pinyin)' if source in ('zh', 'yue') else ' (romanization)' if source == 'ko' else ''}"
        f" and word type; 2) what it means in this sentence; 3) only if useful, a short note on grammar, "
        f"politeness or slang. If a reading is given, it comes from a dictionary: use exactly that reading. "
        f"No heading, no markdown."
    )
    word_line = f"Word: {surface or word}"
    if surface and surface != word:
        word_line += f" (dictionary form: {word})"
    if reading:
        word_line += f"\nReading: {reading}"
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 160,
        "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": f"Sentence: {context}\n{word_line}"}],
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = _http_json(base_url.rstrip("/") + "/chat/completions", body, timeout=timeout)
    out = THINK.sub("", data["choices"][0]["message"].get("content") or "").strip()
    if "</think>" in out:
        out = out.split("</think>")[-1].strip()
    return out.replace("**", "")


def llm_translate(base_url: str, model: str, text: str, source: str | None, target: str,
                  context: list[tuple[str, str]], timeout: float = 20, call: LLMCall | None = None) -> str:
    src_name, tgt_name = lang_name(source), lang_name(target)
    system = (
        f"You translate live video subtitles from {src_name} into natural, concise {tgt_name}. "
        f"This is subtitling for films and TV: translate faithfully, keeping profanity, slurs, insults, slang, "
        f"sexual and violent language at the same intensity and register as the original. Never censor, soften, "
        f"euphemise, bleep or asterisk words, and never refuse or add warnings. Match each speaker's rudeness or "
        f"politeness; keep names as they are. Earlier lines are given only as context. "
        f"Reply with the {tgt_name} translation of the new line only - no quotes, notes or explanations."
    )
    user = ""
    if context:
        user += "Earlier lines:\n" + "\n".join(f"- {s} => {t}" for s, t in context) + "\n\n"
    user += f"New line: {text}"
    body = {
        "model": model,
        # No randomness: the same words always get the same translation, so a preview does not change
        # its wording while nothing new was said. Measured on 885 lines (ja/zh/ko -> en): same quality
        # and speed as 0.2, where two identical runs agreed on only two sentences in three.
        "temperature": 0,
        "max_tokens": 160,
        "stream": False,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        # Ask reasoning models not to think (ignored by servers/models that don't support it).
        "chat_template_kwargs": {"enable_thinking": False},
    }
    data = _http_json(base_url.rstrip("/") + "/chat/completions", body, timeout=timeout, call=call)
    out = data["choices"][0]["message"].get("content") or ""
    out = THINK.sub("", out).strip()
    if "</think>" in out:
        out = out.split("</think>")[-1].strip()
    return out.strip().strip('"“”「」').strip()
