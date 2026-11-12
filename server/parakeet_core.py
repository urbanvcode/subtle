"""
Parakeet-TDT-CTC 0.6B (NVIDIA, Japanese) without NeMo: the model's own weights run with plain tensor
ops, like whisper_core.py does for Whisper.

Why it is worth a second engine: Whisper writes a line one word-piece at a time, Parakeet reads the
whole line in one pass. Measured on 289 Japanese lines cut the way Subtle cuts them (FLEURS test set,
RTX 5090): 6.2 % character errors at 7 ms per line, against 7.0 % at 43 ms for Whisper turbo and
6.1 % at 98 ms for Whisper large-v3. It only writes down Japanese, so it is used for the Best engine.

  prepare        one-time: NVIDIA's .nemo download -> model.safetensors (what the CTC route needs, in
                 fp16, with the constant folding below already done), vocab.txt and parakeet.json
  ParakeetRunner features : NeMo's log-mel recipe (pre-emphasis, 512-point STFT every 10 ms, 80 bands,
                            log, per-band mean/variance over the line)
                 encoder  : FastConformer, 24 layers, one output frame per 80 ms
                 read-out : CTC, the best piece per frame, repeats and blanks dropped
                 On the GPU the encoder is one CUDA graph; on the CPU (or if capture fails, or for a
                 line longer than the graph's buffer) the same maths runs step by step.

Folded in at prepare time, none of it changes the result: batch-norm into the depthwise convolution,
query/key/value into one matrix, the positional projection of every layer into a table.
The model also has a TDT read-out (its main one). On Subtle-style lines it was no more accurate than
CTC (6.3 % vs 6.2 %) and needs a loop per word-piece, so it is left out.
"""

import gc
import json
import math
import os
import shutil
import tarfile
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

FORMAT = 1
MAX_T = 512                           # output frames the positional tables cover (41 s of audio)
N_FFT, HOP, WIN = 512, 160, 400


# ---------------------------------------------------------------------------------------------
# One-time preparation
# ---------------------------------------------------------------------------------------------
def prepared(folder) -> bool:
    d = Path(folder)
    return all((d / f).is_file() for f in ("parakeet.json", "model.safetensors", "vocab.txt"))


def prepare(folder, log=print):
    """
    Turns the downloaded .nemo (a tar holding a PyTorch checkpoint, 2.4 GB) into the three files the
    runner loads, then deletes the .nemo: 1.2 GB stay on disk. The checkpoint is read with
    weights_only=True (tensors only, nothing in it is executed) from a file whose checksum the
    downloader has verified.
    """
    d = Path(folder)
    tmp = d / "weights.tmp"
    if tmp.is_file():                                  # left behind if an earlier run was interrupted
        try:
            tmp.unlink()
        except OSError:
            pass
    if prepared(d):
        return
    nemo = next(iter(sorted(d.glob("*.nemo"))), None)
    if nemo is None:
        raise FileNotFoundError(f"no Parakeet model in {d} (download it on the Models page of the Subtle window)")
    from safetensors.torch import save_file

    log(f"[model] preparing {d.name} for first use (about 15 seconds, once)")
    try:
        with tarfile.open(nemo) as tar:
            members = {os.path.basename(m.name): m for m in tar.getmembers() if m.isfile()}
            vocab = next(m for n, m in members.items() if n.endswith("_tokenizer.vocab"))
            pieces = [line.split("\t")[0] for line in tar.extractfile(vocab).read().decode("utf-8").split("\n") if line]
            with tar.extractfile(members["model_weights.ckpt"]) as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 22)
        sd = torch.load(tmp, map_location="cpu", weights_only=True, mmap=True)
        out = _fold(sd)
        save_file(out, str(d / "model.safetensors.part"))
        info = {"format": FORMAT, "model": "parakeet-tdt_ctc", "source": nemo.name, "max_t": MAX_T,
                "layers": sum(1 for k in out if k.endswith(".bias_u")), "d_model": out["ctc.w"].shape[1],
                "heads": out["L0.bias_u"].shape[0], "kernel": out["L0.dw.w"].shape[-1], "mels": out["feat.fb"].shape[0]}
        del sd, out
        gc.collect()                                   # lets go of the memory-mapped checkpoint
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    (d / "vocab.txt").write_text("\n".join(pieces) + "\n", encoding="utf-8")
    os.replace(d / "model.safetensors.part", d / "model.safetensors")
    (d / "parakeet.json").write_text(json.dumps(info, indent=1), encoding="utf-8")     # last: marks the folder as ready
    try:
        nemo.unlink()
    except OSError:
        pass


def _fold(sd: dict) -> dict:
    """Checkpoint -> the tensors the runner uses (fp16, features fp32)."""
    h = lambda name: sd[name].to(torch.float16)                                        # noqa: E731
    out = {"feat.window": sd["preprocessor.featurizer.window"].float().clone(),
           "feat.fb": sd["preprocessor.featurizer.fb"][0].float().clone()}
    for n, i in enumerate((0, 2, 3, 5, 6)):            # conv, (depthwise, 1x1), (depthwise, 1x1)
        out[f"pre.conv{n}.w"], out[f"pre.conv{n}.b"] = h(f"encoder.pre_encode.conv.{i}.weight"), h(f"encoder.pre_encode.conv.{i}.bias")
    out["pre.out.w"], out["pre.out.b"] = h("encoder.pre_encode.out.weight"), h("encoder.pre_encode.out.bias")
    D = out["pre.out.w"].shape[0]
    # relative positions MAX_T-1 ... -(MAX_T-1) as sinusoids; each layer projects them with its own matrix
    pos = torch.arange(MAX_T - 1, -MAX_T, -1, dtype=torch.float32).unsqueeze(1)
    div = torch.exp(torch.arange(0, D, 2, dtype=torch.float32) * -(math.log(10000.0) / D))
    pe = torch.zeros(2 * MAX_T - 1, D)
    pe[:, 0::2], pe[:, 1::2] = torch.sin(pos * div), torch.cos(pos * div)
    i = 0
    while f"encoder.layers.{i}.norm_out.weight" in sd:
        p, q = f"encoder.layers.{i}.", f"L{i}."
        for a, b in (("ln_ff1", "norm_feed_forward1"), ("ln_att", "norm_self_att"), ("ln_conv", "norm_conv"),
                     ("ln_ff2", "norm_feed_forward2"), ("ln_out", "norm_out")):
            out[q + a + ".w"], out[q + a + ".b"] = h(p + b + ".weight"), h(p + b + ".bias")
        for a in ("feed_forward1", "feed_forward2"):
            b = "ff1" if a.endswith("1") else "ff2"
            out[q + b + ".w1"], out[q + b + ".b1"] = h(p + a + ".linear1.weight"), h(p + a + ".linear1.bias")
            out[q + b + ".w2"], out[q + b + ".b2"] = h(p + a + ".linear2.weight"), h(p + a + ".linear2.bias")
        out[q + "qkv.w"] = torch.cat([h(p + f"self_attn.linear_{x}.weight") for x in "qkv"])
        out[q + "qkv.b"] = torch.cat([h(p + f"self_attn.linear_{x}.bias") for x in "qkv"])
        H = sd[p + "self_attn.pos_bias_u"].shape[0]
        out[q + "bias_u"], out[q + "bias_v"] = h(p + "self_attn.pos_bias_u"), h(p + "self_attn.pos_bias_v")
        table = F.linear(pe, sd[p + "self_attn.linear_pos.weight"].float())            # [2*MAX_T-1, D]
        out[q + "pos"] = table.view(-1, H, D // H).permute(1, 2, 0).to(torch.float16).contiguous()   # [H, D/H, 2*MAX_T-1]
        out[q + "out.w"], out[q + "out.b"] = h(p + "self_attn.linear_out.weight"), h(p + "self_attn.linear_out.bias")
        out[q + "pw1.w"], out[q + "pw1.b"] = h(p + "conv.pointwise_conv1.weight"), h(p + "conv.pointwise_conv1.bias")
        # batch-norm (fixed numbers after training) folded into the depthwise convolution before it
        scale = sd[p + "conv.batch_norm.weight"].double() / torch.sqrt(sd[p + "conv.batch_norm.running_var"].double() + 1e-5)
        out[q + "dw.w"] = (sd[p + "conv.depthwise_conv.weight"].double() * scale.view(-1, 1, 1)).to(torch.float16)
        out[q + "dw.b"] = ((sd[p + "conv.depthwise_conv.bias"].double() - sd[p + "conv.batch_norm.running_mean"].double()) * scale
                           + sd[p + "conv.batch_norm.bias"].double()).to(torch.float16)
        out[q + "pw2.w"], out[q + "pw2.b"] = h(p + "conv.pointwise_conv2.weight"), h(p + "conv.pointwise_conv2.bias")
        i += 1
    out["ctc.w"], out["ctc.b"] = h("ctc_decoder.decoder_layers.0.weight")[:, :, 0].contiguous(), h("ctc_decoder.decoder_layers.0.bias")
    return out


# ---------------------------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------------------------
class ParakeetRunner:
    """
    Batch 1. A line costs about 1,100 small GPU steps whatever its length, so nearly all of the time
    would be the cost of starting each step from Python (14 ms). The CUDA graph replays them in one go
    (7 ms) but needs fixed sizes: the line is put into a buffer of GRAPH_T output frames and everything
    past its real length is masked exactly where a shorter input would have had zero padding or nothing
    at all (after each reduction stage, before the depthwise convolution, and as attention keys). The
    frames that count then see the same numbers as without the buffer: in fp32 the text is identical
    line for line, in fp16 it differs only where two pieces are a rounding error apart.
    """

    GRAPH_T = 72          # 5.76 s; Subtle's lines are 5 s plus a little lead-in at most
    BLOCK = 512 * 2**20   # bytes per block of weights (see WhisperWeights._load for why blocks)

    def __init__(self, folder: str, device: str, dtype: torch.dtype, use_graph: bool):
        from safetensors import safe_open

        d = Path(folder)
        info = json.loads((d / "parakeet.json").read_text(encoding="utf-8"))
        if info.get("format") != FORMAT:
            raise ValueError(f"{d.name} was prepared by another version of Subtle - delete the folder and download it again")
        self.dev, self.dt = device, dtype
        self.D, self.H, self.K = info["d_model"], info["heads"], info["kernel"]
        self.pieces = (d / "vocab.txt").read_text(encoding="utf-8").split("\n")[:-1]
        self.pieces[0] = ""                            # <unk>
        self.blank = len(self.pieces)

        # weights: exactly-sized blocks, every tensor a view (no allocator gaps)
        f = safe_open(str(d / "model.safetensors"), framework="pt", device="cpu")
        names = [k for k in f.keys() if not k.startswith("feat.")]
        shapes = {k: tuple(f.get_slice(k).get_shape()) for k in names}
        item = torch.empty(0, dtype=dtype).element_size()
        align, t, block, used = 256 // item, {}, None, 0
        for k in names:
            n = int(np.prod(shapes[k]))
            if block is None or used + n > block.shape[0]:
                rest = sum(-(-int(np.prod(shapes[x])) // align) * align for x in names[names.index(k):])
                block, used = torch.empty(min(rest, max(self.BLOCK // item, n)), dtype=dtype, device=device), 0
            t[k] = block[used:used + n].view(shapes[k])
            t[k].copy_(f.get_tensor(k))
            used = -(-(used + n) // align) * align
        self.window = f.get_tensor("feat.window").to(device)
        self.fb = f.get_tensor("feat.fb").to(device)
        del f
        if device == "cuda":
            torch.cuda.empty_cache()

        H, hd = self.H, self.D // self.H
        pair = lambda k: (t[k + ".w"], t[k + ".b"])                                    # noqa: E731
        self.pre = [pair(f"pre.conv{i}") for i in range(5)]
        self.pre_out = pair("pre.out")
        self.layers = []
        for i in range(info["layers"]):
            q = f"L{i}."
            self.layers.append(dict(
                ln_ff1=pair(q + "ln_ff1"), ff1=(t[q + "ff1.w1"], t[q + "ff1.b1"], t[q + "ff1.w2"], t[q + "ff1.b2"]),
                ln_att=pair(q + "ln_att"), qkv=pair(q + "qkv"),
                bias_u=t[q + "bias_u"].view(H, 1, hd), bias_v=t[q + "bias_v"].view(H, 1, hd), pos=t[q + "pos"],
                out=pair(q + "out"), ln_conv=pair(q + "ln_conv"), pw1=pair(q + "pw1"), dw=pair(q + "dw"), pw2=pair(q + "pw2"),
                ln_ff2=pair(q + "ln_ff2"), ff2=(t[q + "ff2.w1"], t[q + "ff2.b1"], t[q + "ff2.w2"], t[q + "ff2.b2"]),
                ln_out=pair(q + "ln_out"),
            ))
        self.ctc = pair("ctc")
        self.graph = None
        if use_graph and device == "cuda":
            self._capture()

    # -- features -----------------------------------------------------------------------------------
    def _features(self, audio: np.ndarray, n: int) -> torch.Tensor:
        """16 kHz float audio -> [mels, n] normalised log-mel (fp32); n = frames that hold real audio."""
        x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).to(self.dev)
        x = torch.cat((x[:1], x[1:] - 0.97 * x[:-1]))
        s = torch.stft(x, N_FFT, HOP, WIN, window=self.window, center=True, return_complex=True)
        m = torch.log(self.fb @ torch.view_as_real(s).pow(2).sum(-1) + 2.0 ** -24)[:, :n]
        return (m - m.mean(1, keepdim=True)) / (m.std(1, keepdim=True) + 1e-5)

    # -- the maths ----------------------------------------------------------------------------------
    def _encode(self, feats: torch.Tensor, T: int, masks=None) -> torch.Tensor:
        """
        feats [mels, 8*T-ish] -> best piece per output frame [T]. masks (graph only): what is real in
        the fixed-size buffer, as (after stage 1, after stage 2, attention keys, frames to keep).
        """
        D, H, hd = self.D, self.H, self.D // self.H
        x = feats.to(self.dt).t().unsqueeze(0).unsqueeze(0)                          # [1, 1, frames, mels]
        (w0, b0), (w1, b1), (w2, b2), (w3, b3), (w4, b4) = self.pre
        x = F.relu(F.conv2d(x, w0, b0, stride=2, padding=1))
        if masks is not None:
            x = x * masks[0]
        x = F.relu(F.conv2d(F.conv2d(x, w1, b1, stride=2, padding=1, groups=w1.shape[0]), w2, b2))
        if masks is not None:
            x = x * masks[1]
        x = F.relu(F.conv2d(F.conv2d(x, w3, b3, stride=2, padding=1, groups=w3.shape[0]), w4, b4))
        x = F.linear(x[0].permute(1, 0, 2).reshape(T, -1), *self.pre_out) * math.sqrt(D)      # [T, D]
        lo, hi = MAX_T - T, MAX_T + T - 1                                            # relative positions T-1 ... -(T-1)
        scale = hd ** -0.5
        for L in self.layers:
            h = F.layer_norm(x, (D,), *L["ln_ff1"])
            x = x + 0.5 * F.linear(F.silu(F.linear(h, L["ff1"][0], L["ff1"][1])), L["ff1"][2], L["ff1"][3])

            h = F.layer_norm(x, (D,), *L["ln_att"])
            qkv = F.linear(h, *L["qkv"]).view(T, 3, H, hd).permute(1, 2, 0, 3)       # [3, H, T, hd]
            q, k, v = qkv[0], qkv[1], qkv[2]
            ac = torch.matmul(q + L["bias_u"], k.transpose(1, 2))                    # what is said      [H, T, T]
            bd = torch.matmul(q + L["bias_v"], L["pos"][:, :, lo:hi])                # how far apart     [H, T, 2T-1]
            bd = F.pad(bd, (1, 0)).view(H, 2 * T, T)[:, 1:].reshape(H, T, 2 * T - 1)[:, :, :T]   # lined up with the keys
            s = (ac + bd) * scale
            if masks is not None:
                s = s + masks[2]
            x = x + F.linear(torch.matmul(torch.softmax(s, -1), v).transpose(0, 1).reshape(T, D), *L["out"])

            h = F.glu(F.conv1d(F.layer_norm(x, (D,), *L["ln_conv"]).t().unsqueeze(0), *L["pw1"]), dim=1)   # [1, D, T]
            if masks is not None:
                h = h * masks[3]
            h = F.silu(F.conv1d(h, L["dw"][0], L["dw"][1], padding=self.K // 2, groups=D))
            x = x + F.conv1d(h, *L["pw2"])[0].t()

            h = F.layer_norm(x, (D,), *L["ln_ff2"])
            x = x + 0.5 * F.linear(F.silu(F.linear(h, L["ff2"][0], L["ff2"][1])), L["ff2"][2], L["ff2"][3])
            x = F.layer_norm(x, (D,), *L["ln_out"])
        return F.linear(x, *self.ctc).argmax(-1)

    # -- graph --------------------------------------------------------------------------------------
    def _static(self):
        Tm = self.GRAPH_T
        n1 = torch.div(self.s_n - 1, 2, rounding_mode="floor") + 1                   # real frames after each halving
        n2 = torch.div(n1 - 1, 2, rounding_mode="floor") + 1
        n3 = torch.div(n2 - 1, 2, rounding_mode="floor") + 1
        valid = self.s_ar[2] < n3
        masks = ((self.s_ar[0] < n1).to(self.dt).view(1, 1, -1, 1), (self.s_ar[1] < n2).to(self.dt).view(1, 1, -1, 1),
                 torch.where(valid, self.s_zero, self.s_neg).view(1, 1, Tm), valid.to(self.dt).view(1, 1, Tm))
        return self._encode(self.s_feats, Tm, masks)

    def _capture(self):
        Tm, dev = self.GRAPH_T, self.dev
        self.s_feats = torch.zeros(self.fb.shape[0], 8 * Tm, device=dev, dtype=torch.float32)
        self.s_n = torch.ones(1, dtype=torch.long, device=dev)                       # mel frames that hold the line
        self.s_ar = [torch.arange(8 * Tm >> k, device=dev) for k in (1, 2, 3)]
        self.s_neg = torch.tensor(float("-inf"), device=dev, dtype=self.dt)
        self.s_zero = torch.tensor(0.0, device=dev, dtype=self.dt)
        with torch.inference_mode():
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream):
                for _ in range(3):                     # let cuDNN / cuBLAS set themselves up before capturing
                    self._static()
            torch.cuda.current_stream().wait_stream(stream)
            torch.cuda.synchronize()
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph):
                self.s_ids = self._static()
        self.graph = graph

    # -- driving it ---------------------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray) -> str:
        audio = audio[: (MAX_T - 1) * 8 * HOP]
        n = audio.shape[0] // HOP
        if n < 16:                                     # under 0.16 s: nothing to read
            return ""
        T = (((n - 1) // 2 + 1 - 1) // 2 + 1 - 1) // 2 + 1                           # output frames for n mel frames
        with torch.inference_mode():
            feats = self._features(audio, n)
            if self.graph is not None and T <= self.GRAPH_T:
                self.s_feats.zero_()
                self.s_feats[:, :n].copy_(feats)
                self.s_n.fill_(n)
                self.graph.replay()
                ids = self.s_ids[:T].tolist()
            else:
                ids = self._encode(feats, T).tolist()
        return "".join(self.pieces[t] for i, t in enumerate(ids)
                       if t != self.blank and (i == 0 or t != ids[i - 1])).replace("▁", " ").strip()


__all__ = ["ParakeetRunner", "prepare", "prepared"]
