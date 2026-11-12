"""
SenseVoice-Small (FunAudioLLM) without FunASR, sherpa-onnx or onnxruntime: the model's own weights run
with plain tensor ops, like whisper_core.py and parakeet_core.py.

It writes down Chinese, Cantonese, English, Japanese and Korean in one pass over the line (no word-by-word
loop). Measured on 303 Chinese lines cut the way Subtle cuts them (FLEURS test set): 9.3 % character errors,
against 11.7 % for Whisper turbo and 10.5 % for Whisper large-v3; for Japanese and Korean Whisper is better.
It cannot translate, so it is used where the translator does that (the Best engine).

  prepare          one-time: FunASR's model.pt + am.mvn + the sentencepiece file -> model.safetensors
                   (fp16), vocab.txt and sensevoice.json; the 0.9 GB model.pt is then deleted
  SenseVoiceRunner features : Kaldi-style log-mel (25 ms Hamming frames every 10 ms, 80 bands), 7 frames
                              stacked every 6 (one input frame per 60 ms), mean/variance from am.mvn
                   encoder  : 70 SAN-M layers (self-attention plus an 11-frame memory convolution);
                              four query frames in front say: language, "text with punctuation and digits"
                   read-out : CTC, the best piece per frame, repeats and blanks dropped
                   On the GPU the encoder is one CUDA graph over a fixed-size buffer; on the CPU (or if
                   capture fails, or for a line longer than the buffer) the same maths runs step by step.
"""

import gc
import json
import math
import os
import re
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

FORMAT = 1
MAX_T = 2048                          # input frames the position table covers (2 minutes)
LANGUAGES = {"zh": 3, "en": 4, "yue": 7, "ja": 11, "ko": 12}      # rows of the query table (0 = let it guess)
WITH_ITN = 14                         # query: punctuation and digits in the text
WIN, HOP, N_FFT, LFR_M, LFR_N = 400, 160, 512, 7, 6
SOURCE_FILES = ("model.pt", "am.mvn", "chn_jpn_yue_eng_ko_spectok.bpe.model")


# ---------------------------------------------------------------------------------------------
# One-time preparation
# ---------------------------------------------------------------------------------------------
def prepared(folder) -> bool:
    d = Path(folder)
    return all((d / f).is_file() for f in ("sensevoice.json", "model.safetensors", "vocab.txt"))


def _varint(b: bytes, i: int):
    n = shift = 0
    while True:
        n |= (b[i] & 0x7F) << shift
        i += 1
        if not b[i - 1] & 0x80:
            return n, i
        shift += 7


def _fields(b: bytes):
    """(field number, wire type, value) of a protobuf message; length-delimited values as bytes."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
        elif wire == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        else:
            n = 4 if wire == 5 else 8
            v, i = b[i:i + n], i + n
        yield field, wire, v


def sentencepiece_pieces(path) -> list[str]:
    """The word-pieces of a sentencepiece .model file, in id order (its first field, repeated: {piece, score, type})."""
    data = Path(path).read_bytes()
    return [next(v for f, w, v in _fields(msg) if f == 1 and w == 2).decode("utf-8")
            for field, wire, msg in _fields(data) if field == 1 and wire == 2]


def prepare(folder, log=print):
    """
    Turns the downloaded FunASR files into the three files the runner loads, then deletes model.pt
    (0.47 GB stay on disk). model.pt is read with weights_only=True (tensors only, nothing in it is
    executed) from a file whose checksum the downloader has verified.
    """
    d = Path(folder)
    if prepared(d):
        return
    if not all((d / f).is_file() for f in SOURCE_FILES):
        raise FileNotFoundError(f"no SenseVoice model in {d} (download it on the Models page of the Subtle window)")
    from safetensors.torch import save_file
    from torchaudio.compliance.kaldi import get_mel_banks

    log(f"[model] preparing {d.name} for first use (a few seconds, once)")
    sd = torch.load(d / "model.pt", map_location="cpu", weights_only=True, mmap=True)
    sd = sd.get("state_dict", sd)
    h = lambda name: sd[name].to(torch.float16)                                        # noqa: E731
    out = {"embed": h("embed.weight")}
    names = ["encoder.encoders0.0."]
    names += [f"encoder.encoders.{i}." for i in range(sum(1 for k in sd if re.fullmatch(r"encoder\.encoders\.\d+\.norm1\.weight", k)))]
    main = len(names)
    names += [f"encoder.tp_encoders.{i}." for i in range(sum(1 for k in sd if re.fullmatch(r"encoder\.tp_encoders\.\d+\.norm1\.weight", k)))]
    for i, p in enumerate(names):
        q = f"L{i}."
        out[q + "ln1.w"], out[q + "ln1.b"] = h(p + "norm1.weight"), h(p + "norm1.bias")
        out[q + "qkv.w"], out[q + "qkv.b"] = h(p + "self_attn.linear_q_k_v.weight"), h(p + "self_attn.linear_q_k_v.bias")
        out[q + "mem.w"] = h(p + "self_attn.fsmn_block.weight")
        out[q + "out.w"], out[q + "out.b"] = h(p + "self_attn.linear_out.weight"), h(p + "self_attn.linear_out.bias")
        out[q + "ln2.w"], out[q + "ln2.b"] = h(p + "norm2.weight"), h(p + "norm2.bias")
        out[q + "ff.w1"], out[q + "ff.b1"] = h(p + "feed_forward.w_1.weight"), h(p + "feed_forward.w_1.bias")
        out[q + "ff.w2"], out[q + "ff.b2"] = h(p + "feed_forward.w_2.weight"), h(p + "feed_forward.w_2.bias")
    out["mid.w"], out["mid.b"] = h("encoder.after_norm.weight"), h("encoder.after_norm.bias")
    out["end.w"], out["end.b"] = h("encoder.tp_norm.weight"), h("encoder.tp_norm.bias")
    out["ctc.w"], out["ctc.b"] = h("ctc.ctc_lo.weight"), h("ctc.ctc_lo.bias")
    # am.mvn: two rows of 560 numbers, what to add and what to multiply by
    rows = [np.array(r.split(), dtype=np.float32) for r in re.findall(r"\[([^\]]+)\]", (d / "am.mvn").read_text())]
    rows = [r for r in rows if r.size == LFR_M * 80]
    out["feat.shift"], out["feat.scale"] = torch.from_numpy(rows[0]), torch.from_numpy(rows[1])
    fb = get_mel_banks(80, N_FFT, 16000.0, 20.0, 0.0, 100.0, -500.0, 1.0)[0]           # Kaldi's 80 mel bands, [80, 256]
    out["feat.fb"] = F.pad(fb, (0, 1)).float().contiguous()
    pieces = sentencepiece_pieces(d / SOURCE_FILES[2])
    info = {"format": FORMAT, "model": "sensevoice-small", "layers": len(names), "main_layers": main,
            "d_model": out["ctc.w"].shape[1], "heads": 4, "kernel": out["L0.mem.w"].shape[-1], "pieces": len(pieces)}
    if len(pieces) != out["ctc.w"].shape[0]:
        raise ValueError("SenseVoice: the word-piece list and the model do not match")
    save_file(out, str(d / "model.safetensors.part"))
    del sd, out
    gc.collect()                                       # lets go of the memory-mapped model.pt
    (d / "vocab.txt").write_text("\n".join(pieces) + "\n", encoding="utf-8")
    os.replace(d / "model.safetensors.part", d / "model.safetensors")
    (d / "sensevoice.json").write_text(json.dumps(info, indent=1), encoding="utf-8")   # last: marks the folder as ready
    for f in SOURCE_FILES:
        try:
            (d / f).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------------------------
class _TF32:
    """Faster fp32 matrix products on RTX cards while a SenseVoice call runs (and only then)."""

    def __enter__(self):
        self.old = torch.backends.cuda.matmul.allow_tf32
        torch.backends.cuda.matmul.allow_tf32 = True

    def __exit__(self, *exc):
        torch.backends.cuda.matmul.allow_tf32 = self.old


class SenseVoiceRunner:
    """
    Batch 1, full precision: the numbers inside this model reach about 71,000 in the later layers, more
    than half precision can hold (65,504), and with fp16 the error rate went from 9.3 % to 15.6 %.
    What it uses instead is TF32 for the matrix products (RTX cards: fp32 range, shorter mantissa inside
    the multiply, fp32 sums): 11 -> 6 ms per line, and the text was identical to plain fp32 on all 360
    test sentences. The switch is on only inside this model's calls; older cards ignore it.
    A line is about 1,000 small GPU steps, so the CUDA graph (all of them replayed in one go) is what
    makes it fast. A graph needs fixed sizes: the line goes into a buffer and what lies past its real
    length is masked where a shorter input would have had nothing: as attention keys and before the
    memory convolution (which pads with zeros anyway). The text is the same as without the buffer.
    Unlike Whisper and Parakeet this model's cost grows with the buffer (its wide feed-forward layers
    are real work, not overhead), so there are several buffer sizes and a line uses the smallest that fits.
    """

    GRAPH_TS = (64, 100)               # frames incl. the 4 queries: lines up to 3.6 s / 5.76 s (each size costs ~60 MB)
    BLOCK = 512 * 2**20   # bytes per block of weights (see WhisperWeights._load for why blocks)

    def __init__(self, folder: str, device: str, dtype: torch.dtype, use_graph: bool):
        from safetensors import safe_open

        d = Path(folder)
        info = json.loads((d / "sensevoice.json").read_text(encoding="utf-8"))
        if info.get("format") != FORMAT:
            raise ValueError(f"{d.name} was prepared by another version of Subtle - delete the folder and download it again")
        self.dev, self.dt = device, dtype
        self.D, self.H, self.K, self.main = info["d_model"], info["heads"], info["kernel"], info["main_layers"]
        self.pieces = (d / "vocab.txt").read_text(encoding="utf-8").split("\n")[:-1]
        self.skip = {i for i, p in enumerate(self.pieces) if re.fullmatch(r"<[^>]*>", p)}     # blank, <unk>, <|tags|>

        f = safe_open(str(d / "model.safetensors"), framework="pt", device="cpu")
        names = [k for k in f.keys() if not k.startswith("feat.")]
        shapes = {k: tuple(f.get_slice(k).get_shape()) for k in names}
        item = torch.empty(0, dtype=dtype).element_size()
        align = 256 // item
        sizes = [-(-int(np.prod(shapes[k])) // align) * align for k in names]
        t, block, used = {}, None, 0
        for i, k in enumerate(names):                  # exactly-sized blocks, every tensor a view (no allocator gaps)
            n = int(np.prod(shapes[k]))
            if block is None or used + n > block.shape[0]:
                block, used = torch.empty(min(sum(sizes[i:]), max(self.BLOCK // item, n)), dtype=dtype, device=device), 0
            t[k] = block[used:used + n].view(shapes[k])
            t[k].copy_(f.get_tensor(k))
            used += sizes[i]
        self.fb = f.get_tensor("feat.fb").to(device)                                  # [80, 257]
        self.shift, self.scale = f.get_tensor("feat.shift").to(device), f.get_tensor("feat.scale").to(device)
        del f
        if device == "cuda":
            torch.cuda.empty_cache()
        self.window = torch.hamming_window(WIN, periodic=False, device=device)
        self.eps = torch.tensor(torch.finfo(torch.float32).eps, device=device)

        pair = lambda k: (t[k + ".w"], t[k + ".b"])                                    # noqa: E731
        self.embed = t["embed"]
        self.layers = [dict(ln1=pair(f"L{i}.ln1"), qkv=pair(f"L{i}.qkv"), mem=t[f"L{i}.mem.w"], out=pair(f"L{i}.out"),
                            ln2=pair(f"L{i}.ln2"), ff=(t[f"L{i}.ff.w1"], t[f"L{i}.ff.b1"], t[f"L{i}.ff.w2"], t[f"L{i}.ff.b2"]))
                       for i in range(info["layers"])]
        self.mid, self.end, self.ctc = pair("mid"), pair("end"), pair("ctc")
        # positions 1..MAX_T as sinusoids (first half sines, second half cosines)
        n_in = self.embed.shape[1]
        inv = torch.exp(torch.arange(n_in // 2, dtype=torch.float32) * -(math.log(10000.0) / (n_in / 2 - 1)))
        ang = torch.arange(1, MAX_T + 1, dtype=torch.float32).unsqueeze(1) * inv.unsqueeze(0)
        self.pos = torch.cat([torch.sin(ang), torch.cos(ang)], 1).to(device=device, dtype=dtype)
        self.query = torch.tensor([0, 1, 2, WITH_ITN], dtype=torch.long, device=device)          # language, event, emotion, text style
        self.graphs = []                               # [(frames, buffers, CUDA graph)], smallest first
        if use_graph and device == "cuda":
            self.s_neg = torch.tensor(float("-inf"), device=device, dtype=dtype)
            self.s_zero = torch.tensor(0.0, device=device, dtype=dtype)
            for Tm in self.GRAPH_TS:
                self.graphs.append(self._capture(Tm))

    # -- features -----------------------------------------------------------------------------------
    def _features(self, audio: np.ndarray) -> torch.Tensor:
        """16 kHz float audio -> [frames, 560] (fp32): Kaldi fbank, 7 frames stacked every 6, mean/variance."""
        x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).to(self.dev) * 32768.0
        fr = x.unfold(0, WIN, HOP)                                                    # [N, 400]
        fr = fr - fr.mean(1, keepdim=True)
        fr = (fr - 0.97 * torch.cat((fr[:, :1], fr[:, :-1]), 1)) * self.window
        spec = torch.fft.rfft(fr, N_FFT).abs().pow(2)                                # [N, 257]
        m = torch.log(torch.maximum(spec @ self.fb.t(), self.eps))                   # [N, 80]
        N = m.shape[0]
        T = -(-N // LFR_N)
        m = torch.cat((m[:1].expand((LFR_M - 1) // 2, -1), m, m[-1:].expand(max(0, (T - 1) * LFR_N + LFR_M - N - (LFR_M - 1) // 2), -1)))
        idx = torch.arange(T, device=self.dev).unsqueeze(1) * LFR_N + torch.arange(LFR_M, device=self.dev).unsqueeze(0)
        return (m[idx].reshape(T, -1) + self.shift) * self.scale

    # -- the maths ----------------------------------------------------------------------------------
    def _encode(self, feats: torch.Tensor, T: int, keys=None, keep=None) -> torch.Tensor:
        """
        feats [T-4, 560] -> best piece per frame [T] (the first four are the query frames).
        keys / keep (graph only): attention mask [H, T, T] and frame mask [T, 1] for the real part of the buffer.
        """
        D, H, hd = self.D, self.H, self.D // self.H
        x = torch.cat((self.embed.index_select(0, self.query), feats.to(self.dt))) * math.sqrt(D) + self.pos[:T]
        scale = hd ** -0.5
        for i, L in enumerate(self.layers):
            h = F.layer_norm(x, (x.shape[1],), *L["ln1"])
            qkv = F.linear(h, *L["qkv"])                                             # [T, 3D]
            q = qkv[:, :D].reshape(T, H, hd).transpose(0, 1)                         # [H, T, hd]
            k = qkv[:, D:2 * D].reshape(T, H, hd).permute(1, 2, 0)                   # [H, hd, T]
            v = qkv[:, 2 * D:]
            if keep is not None:
                v = v * keep
            s = torch.bmm(q, k) * scale if keys is None else torch.baddbmm(keys, q, k, alpha=scale)
            a = torch.bmm(torch.softmax(s, -1), v.reshape(T, H, hd).transpose(0, 1))
            y = F.linear(a.transpose(0, 1).reshape(T, D), *L["out"])
            y = y + F.conv1d(v.t().unsqueeze(0), L["mem"], padding=self.K // 2, groups=D)[0].t() + v   # memory of the 5 frames either side
            x = y if x.shape[1] != D else x + y                                      # the first layer narrows 560 -> 512
            h = F.layer_norm(x, (D,), *L["ln2"])
            x = x + F.linear(F.relu(F.linear(h, L["ff"][0], L["ff"][1])), L["ff"][2], L["ff"][3])
            if i == self.main - 1:
                x = F.layer_norm(x, (D,), *self.mid)
        return F.linear(F.layer_norm(x, (D,), *self.end), *self.ctc).argmax(-1)

    # -- graph --------------------------------------------------------------------------------------
    def _static(self, b: dict):
        Tm, H = b["T"], self.H
        valid = b["ar"] < b["n"]
        keys = torch.where(valid.view(1, 1, Tm).expand(H, Tm, Tm), self.s_zero, self.s_neg)
        return self._encode(b["feats"], Tm, keys, valid.to(self.dt).view(Tm, 1))

    def _capture(self, Tm: int) -> dict:
        dev = self.dev
        b = dict(T=Tm, feats=torch.zeros(Tm - 4, self.embed.shape[1], device=dev, dtype=torch.float32),
                 n=torch.full((1,), Tm, dtype=torch.long, device=dev),               # frames that hold the line (+ 4 queries)
                 ar=torch.arange(Tm, device=dev))
        with torch.inference_mode(), _TF32():
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream):
                for _ in range(3):                     # let cuDNN / cuBLAS set themselves up before capturing
                    self._static(b)
            torch.cuda.current_stream().wait_stream(stream)
            torch.cuda.synchronize()
            b["graph"] = torch.cuda.CUDAGraph()
            with torch.cuda.graph(b["graph"]):
                b["ids"] = self._static(b)
        return b

    # -- driving it ---------------------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray, language: str | None) -> str:
        audio = audio[: (MAX_T - 8) * LFR_N * HOP]
        if audio.shape[0] < WIN + 15 * HOP:            # under 0.17 s: nothing to read
            return ""
        with torch.inference_mode(), _TF32():
            self.query[0] = LANGUAGES.get(language or "", 0)
            feats = self._features(audio)
            T = feats.shape[0] + 4
            b = next((b for b in self.graphs if T <= b["T"]), None)
            if b is not None:
                b["feats"].zero_()
                b["feats"][: T - 4].copy_(feats)
                b["n"].fill_(T)
                b["graph"].replay()
                ids = b["ids"][4:T].tolist()
            else:
                ids = self._encode(feats, T)[4:].tolist()
        return "".join(self.pieces[t] for i, t in enumerate(ids)
                       if t not in self.skip and (i == 0 or t != ids[i - 1])).replace("▁", " ").strip()


__all__ = ["SenseVoiceRunner", "LANGUAGES", "prepare", "prepared"]
