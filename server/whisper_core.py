"""
Whisper without the transformers library: reads a Hugging Face Whisper folder (config.json,
generation_config.json, preprocessor_config.json, tokenizer.json, model*.safetensors) and runs it
with plain tensor ops.

Why: importing transformers' Whisper code took ~6 s and ~250 MB of RAM on every start, for a model
that is ~60 lines of maths. The model files are unchanged; any standard Whisper checkpoint works
(tiny ... large-v3, turbo, and fine-tunes that keep the architecture).

  WhisperWeights  loads the files: the weights into a few exactly-sized blocks of GPU memory, the
                  tokenizer, the configs
  WhisperRunner   log-mel features, encoder, prompt prefill and the greedy decoder step. On the GPU
                  each of the three parts is a CUDA graph over static buffers (batch 1); on the CPU
                  (or if capture fails) the same code runs eagerly.
"""

import json

from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F


# ---------------------------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------------------------
def mel_filters(n_mels: int, n_fft: int = 400, sr: int = 16000) -> np.ndarray:
    """Slaney-style mel filterbank [n_fft//2 + 1, n_mels], identical to Whisper's (and librosa's)."""
    def hz_to_mel(f):
        f = np.asarray(f, dtype=np.float64)
        mel = 3.0 * f / 200.0
        log = f >= 1000.0
        mel = np.where(log, 15.0 + np.log(np.maximum(f, 1e-10) / 1000.0) * (27.0 / np.log(6.4)), mel)
        return mel

    def mel_to_hz(m):
        m = np.asarray(m, dtype=np.float64)
        f = 200.0 * m / 3.0
        log = m >= 15.0
        return np.where(log, 1000.0 * np.exp(np.log(6.4) / 27.0 * (m - 15.0)), f)

    n_freq = n_fft // 2 + 1
    fft_freqs = np.linspace(0, sr // 2, n_freq)
    mel_pts = np.linspace(hz_to_mel(0.0), hz_to_mel(sr / 2.0), n_mels + 2)
    filt_freqs = mel_to_hz(mel_pts)
    fdiff = np.diff(filt_freqs)
    slopes = filt_freqs[None, :] - fft_freqs[:, None]               # [n_freq, n_mels + 2]
    down = -slopes[:, :-2] / fdiff[:-1]
    up = slopes[:, 2:] / fdiff[1:]
    fb = np.maximum(0.0, np.minimum(down, up))
    enorm = 2.0 / (filt_freqs[2:n_mels + 2] - filt_freqs[:n_mels])
    return (fb * enorm[None, :]).astype(np.float32)


def load_tokenizer(d: Path):
    """
    tokenizer.json if the model has one. Some fine-tunes (e.g. anime-whisper, kotoba-whisper-bilingual)
    only ship the older vocab.json + merges.txt + added_tokens.json: Whisper's tokenizer is a plain
    byte-level BPE, so the same tokenizer is rebuilt from those (the added tokens keep their ids).
    """
    from tokenizers import AddedToken, Tokenizer, decoders, models, pre_tokenizers

    if (d / "tokenizer.json").is_file():
        return Tokenizer.from_file(str(d / "tokenizer.json"))
    vocab = json.loads((d / "vocab.json").read_text(encoding="utf-8"))
    added = json.loads((d / "added_tokens.json").read_text(encoding="utf-8")) if (d / "added_tokens.json").is_file() else {}
    tok = Tokenizer(models.BPE.from_file(str(d / "vocab.json"), str(d / "merges.txt")))
    tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
    tok.decoder = decoders.ByteLevel()
    extra = sorted(((i, t) for t, i in added.items() if t not in vocab))
    if extra and extra[0][0] != len(vocab):
        raise ValueError("unexpected added token ids in this model's tokenizer files")
    import re
    for i, t in extra:
        if re.fullmatch(r"<\|\d+\.\d+\|>", t):         # timestamps count as ordinary added tokens in Whisper
            tok.add_tokens([AddedToken(t, special=False, normalized=False)])
        else:
            tok.add_special_tokens([AddedToken(t, special=True, normalized=False)])
        if tok.token_to_id(t) != i:
            raise ValueError(f"tokenizer rebuild mismatch at {t}")
    # control tokens that live in vocab.json itself (older checkpoints keep <|endoftext|> there)
    inside = [t for t in added if t in vocab and re.fullmatch(r"<\|[^|]+\|>", t) and not re.fullmatch(r"<\|\d+\.\d+\|>", t)]
    if "<|endoftext|>" in vocab and "<|endoftext|>" not in inside:
        inside.append("<|endoftext|>")
    if inside:
        tok.add_special_tokens([AddedToken(t, special=True, normalized=False) for t in inside])
    return tok


class WhisperWeights:
    BLOCK = 512 * 2**20   # bytes per block of weights

    def __init__(self, folder: str, device: str, dtype: torch.dtype):
        from tokenizers import Tokenizer

        d = Path(folder)
        self.cfg = json.loads((d / "config.json").read_text(encoding="utf-8"))
        self.gen = json.loads((d / "generation_config.json").read_text(encoding="utf-8")) \
            if (d / "generation_config.json").is_file() else {}
        self.pre = json.loads((d / "preprocessor_config.json").read_text(encoding="utf-8")) \
            if (d / "preprocessor_config.json").is_file() else {}
        self.tok = load_tokenizer(d)
        if not self.gen.get("lang_to_id"):
            # Some fine-tunes ship without generation_config.json: the language tokens are
            # <|xx|> / <|xxx|> special tokens in the tokenizer, so the list can be rebuilt from it.
            import re
            vocab = self.tok.get_vocab(with_added_tokens=True)
            langs = {t: i for t, i in vocab.items() if re.fullmatch(r"<\|[a-z]{2,3}\|>", t)}
            if langs:
                self.gen["lang_to_id"] = langs

        index = d / "model.safetensors.index.json"
        files = sorted(set(json.loads(index.read_text())["weight_map"].values())) if index.is_file() else ["model.safetensors"]
        self.t: dict[str, torch.Tensor] = {}
        self._load([str(d / f) for f in files], device, dtype)
        self.n_mels = self.cfg.get("num_mel_bins", 80)
        self.n_fft = self.pre.get("n_fft", 400)
        self.hop = self.pre.get("hop_length", 160)
        self.n_samples = self.pre.get("n_samples", 480000)

    def _load(self, paths: list[str], device: str, dtype: torch.dtype):
        """
        Reads the weights into self.t. They are not loaded one tensor at a time: the GPU allocator
        of PyTorch hands out memory in 2 MB and 20 MB segments and rounds big tensors up, which left
        300-600 MB of video memory in unusable gaps (more once the query/key/value weights were
        joined and their originals freed). Instead the sizes are added up first, a few blocks of
        exactly that size are reserved, and every weight is a view into a block. The self-attention
        query/key/value weights of a layer are written next to each other, so the runner gets them
        as one matrix ("...self_attn.qkv.weight" / ".bias") without a copy; the key projection has
        no bias in Whisper, so that third of the joined bias is zero.
        Checkpoints stored in another precision (fp32, bf16) are converted on the way.
        """
        from safetensors import safe_open

        ATT = ".self_attn."
        plan = []            # (name, shape, [(file, key or None for zeros, elements)])
        others = []          # non-float tensors (none in a standard checkpoint): loaded as they are
        files = [safe_open(p, framework="pt", device="cpu") for p in paths]
        for f in files:
            keys = set(f.keys())
            for key in f.keys():
                name = key[len("model."):] if key.startswith("model.") else key
                info = f.get_slice(key)
                shape = tuple(info.get_shape())
                n = int(np.prod(shape)) if shape else 1
                joined = ATT in name and key.replace("k_proj", "q_proj").replace("v_proj", "q_proj") in keys
                if not info.get_dtype().startswith(("F", "BF")):
                    others.append((f, key, name))
                elif joined and name.endswith(("k_proj.weight", "v_proj.weight", "k_proj.bias", "v_proj.bias")):
                    continue                                        # stored with its q_proj, below
                elif joined and name.endswith("q_proj.weight"):
                    parts = [(f, key.replace("q_proj", x), n) for x in ("q_proj", "k_proj", "v_proj")]
                    plan.append((name.replace("q_proj", "qkv"), (3 * shape[0],) + shape[1:], parts))
                elif joined and name.endswith("q_proj.bias"):
                    parts = [(f, key, n), (f, None, n), (f, key.replace("q_proj", "v_proj"), n)]
                    plan.append((name.replace("q_proj", "qkv"), (3 * n,), parts))
                else:
                    plan.append((name, shape, [(f, key, n)]))

        item = torch.empty(0, dtype=dtype).element_size()
        align = 256 // item                                         # every weight starts on a 256-byte boundary
        blocks, size = [], 0                                        # [[elements, [(offset, plan entry)]]]
        for entry in plan:
            n = sum(part[2] for part in entry[2])
            if not blocks or (size + n) * item > self.BLOCK:
                blocks.append([0, []])
                size = 0
            blocks[-1][1].append((size, entry))
            size = -(-(size + n) // align) * align
            blocks[-1][0] = size
        for total, entries in blocks:
            block = torch.empty(total, dtype=dtype, device=device)
            for offset, (name, shape, parts) in entries:
                self.t[name] = block[offset:offset + sum(part[2] for part in parts)].view(shape)
                for f, key, n in parts:
                    if key is None:
                        block[offset:offset + n].zero_()
                    else:
                        block[offset:offset + n].copy_(f.get_tensor(key).reshape(-1))
                    offset += n
        for f, key, name in others:
            self.t[name] = f.get_tensor(key).to(device)
        del files
        if device == "cuda":
            torch.cuda.empty_cache()                                # whatever the copies needed for a moment

    def token(self, text: str) -> int | None:
        return self.tok.token_to_id(text)

    def decode(self, ids: list[int]) -> str:
        return self.tok.decode(ids, skip_special_tokens=True)


# ---------------------------------------------------------------------------------------------
# Inference
# ---------------------------------------------------------------------------------------------
class WhisperRunner:
    """
    Batch 1, greedy. Three steps, each a CUDA graph on the GPU:

      encode  : log-mel features -> encoder -> cross-attention keys/values of every decoder layer
      prefill : the 4 prompt tokens in one pass -> first output token
      step    : one token -> next token; it feeds its own output back, so decoding is replay, replay, ...

    The decoder costs roughly "number of kernels" at batch 1, and this keeps it at about 21 small
    kernels per layer. It needs no cache reset between lines: positions past the current one are
    masked, never read.
    """

    N_PROMPT = 4  # <|startoftranscript|> <|lang|> <|task|> <|notimestamps|>

    def __init__(self, W: WhisperWeights, device: str, max_cache: int, eot: int, use_graphs: bool):
        t, cfg = W.t, W.cfg
        dev = self.dev = device
        dt = next(iter(t.values())).dtype
        self.D = D = cfg["d_model"]
        self.H = H = cfg["decoder_attention_heads"]
        self.He = cfg.get("encoder_attention_heads", H)
        self.hd = hd = D // H
        self.M = M = max_cache
        self.S = S = cfg.get("max_source_positions", 1500)
        self.scale = hd ** -0.5       # attention scale: an argument of baddbmm (self) / folded into xk (cross)

        def pair(prefix):
            return t[prefix + ".weight"], t[prefix + ".bias"]

        # --- encoder ---
        self.conv1, self.conv2 = pair("encoder.conv1"), pair("encoder.conv2")
        self.enc_pos = t["encoder.embed_positions.weight"][:S]
        self.enc_ln = pair("encoder.layer_norm")
        self.enc_layers = []
        for i in range(cfg["encoder_layers"]):
            p = f"encoder.layers.{i}."
            qkv_w, qkv_b = pair(p + "self_attn.qkv")     # query, key and value as one matrix (WhisperWeights._load)
            self.enc_layers.append(dict(
                ln1=pair(p + "self_attn_layer_norm"),
                qkv_w=qkv_w, qkv_b=qkv_b,
                o=pair(p + "self_attn.out_proj"), ln2=pair(p + "final_layer_norm"),
                f1=pair(p + "fc1"), f2=pair(p + "fc2"),
            ))

        # --- decoder ---
        self.layers = []
        for i in range(cfg["decoder_layers"]):
            p = f"decoder.layers.{i}."
            qkv_w, qkv_b = pair(p + "self_attn.qkv")
            so_w, so_b = pair(p + "self_attn.out_proj")
            co_w, co_b = pair(p + "encoder_attn.out_proj")
            cv_w, cv_b = pair(p + "encoder_attn.v_proj")
            f2_w, f2_b = pair(p + "fc2")
            self.layers.append(dict(
                ln1=pair(p + "self_attn_layer_norm"),
                qkv_w=qkv_w, qkv_b=qkv_b,
                so_wt=so_w.t(), so_b=so_b,
                ln2=pair(p + "encoder_attn_layer_norm"),
                cq_w=t[p + "encoder_attn.q_proj.weight"], cq_b=t[p + "encoder_attn.q_proj.bias"],
                ck_w=t[p + "encoder_attn.k_proj.weight"], cv_w=cv_w, cv_b=cv_b,   # used once per line (encode)
                co_wt=co_w.t(), co_b=co_b,
                ln3=pair(p + "final_layer_norm"),
                f1_w=t[p + "fc1.weight"], f1_b=t[p + "fc1.bias"],
                f2_wt=f2_w.t(), f2_b=f2_b,
                # caches; keys are stored transposed so the attention scores are a single (b)addbmm
                sk=torch.zeros(H, hd, M, device=dev, dtype=dt), sv=torch.zeros(H, M, hd, device=dev, dtype=dt),
                xk=torch.zeros(H, hd, S, device=dev, dtype=dt), xv=torch.zeros(H, S, hd, device=dev, dtype=dt),
            ))
        self.ln_f = pair("decoder.layer_norm")
        self.emb = t["decoder.embed_tokens.weight"]
        self.pos_emb = t["decoder.embed_positions.weight"]
        self.proj = t.get("proj_out.weight", self.emb)          # tied to the token embedding in Whisper

        # Output filters: never emit special / timestamp tokens (except end-of-text) or the suppress list.
        V = self.proj.shape[0]
        bias = torch.zeros(1, V, device=dev, dtype=dt)
        bias[0, eot + 1:] = float("-inf")
        sup = [x for x in (W.gen.get("suppress_tokens") or W.cfg.get("suppress_tokens") or []) if x < V and x != eot]
        bias[0, sup] = float("-inf")
        self.bias = bias
        lang_ids = sorted(set((W.gen.get("lang_to_id") or {}).values()))
        self.lang_bias = torch.full((1, V), float("-inf"), device=dev, dtype=dt)
        if lang_ids:
            self.lang_bias[0, lang_ids] = 0
        self.can_detect = bool(lang_ids)

        # Feature extraction (same maths as Whisper's own feature extractor), on the same device
        self.n_fft, self.hop, self.n_samples = W.n_fft, W.hop, W.n_samples
        self.window = torch.hann_window(self.n_fft, device=dev)
        self.mel = torch.from_numpy(mel_filters(W.n_mels, self.n_fft)).to(dev).T.contiguous()   # [mels, bins]
        self.wave = torch.zeros(self.n_samples, device=dev)

        # Static inputs
        self.feats = torch.zeros(1, W.n_mels, self.n_samples // self.hop, device=dev, dtype=dt)
        self.prompt = torch.zeros(self.N_PROMPT, dtype=torch.long, device=dev)
        self.prompt_pos = torch.arange(self.N_PROMPT, device=dev)
        self.ids = torch.zeros(1, dtype=torch.long, device=dev)
        self.pos = torch.zeros(1, dtype=torch.long, device=dev)
        self.ar = torch.arange(M, device=dev).view(1, 1, M)
        self.neg = torch.tensor(float("-inf"), device=dev, dtype=dt)
        self.zero = torch.tensor(0.0, device=dev, dtype=dt)
        self.graphs = False

        with torch.inference_mode():
            self._features(np.zeros(16000, dtype=np.float32))
            if use_graphs and dev == "cuda":
                self._capture()
            else:
                self.out_first = self.out_tok = self.out_lang = None

    # -- graph capture ----------------------------------------------------------------------------
    def _reset(self):
        self.ids.fill_(0)
        self.pos.fill_(self.N_PROMPT)

    def _capture(self):
        def capture(fn):
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream):
                for _ in range(3):      # let cuDNN / cuBLAS / cuFFT set themselves up before capturing
                    self._reset()
                    fn()
            torch.cuda.current_stream().wait_stream(stream)
            torch.cuda.synchronize()
            self._reset()
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph):
                out = fn()
            return graph, out

        self.g_encode, _ = capture(self._encode)
        self.g_prefill, self.out_first = capture(self._prefill)
        self.g_step, (self.out_tok, self.out_lang) = capture(self._step)
        self.graphs = True

    # -- the maths ----------------------------------------------------------------------------------
    def _encode(self):
        D, He = self.D, self.He
        hde = D // He
        x = F.gelu(F.conv1d(self.feats, *self.conv1, padding=1))
        x = F.gelu(F.conv1d(x, *self.conv2, stride=2, padding=1))
        x = x[0].transpose(0, 1) + self.enc_pos                                  # [S, D]
        S = x.shape[0]
        for L in self.enc_layers:
            h = F.layer_norm(x, (D,), *L["ln1"])
            qkv = F.linear(h, L["qkv_w"], L["qkv_b"]).view(S, 3, He, hde).permute(1, 2, 0, 3)   # [3, He, S, hd]
            # scale q first, then no scaling inside attention: the reference implementation's order
            # (the same maths, but fp16 rounding then matches it token for token)
            a = F.scaled_dot_product_attention(qkv[0] * hde ** -0.5, qkv[1], qkv[2], scale=1.0)
            x = x + F.linear(a.transpose(0, 1).reshape(S, D), *L["o"])
            h = F.layer_norm(x, (D,), *L["ln2"])
            x = x + F.linear(F.gelu(F.linear(h, *L["f1"])), *L["f2"])
        enc = F.layer_norm(x, (D,), *self.enc_ln)
        H, hd = self.H, self.hd
        for L in self.layers:
            L["xk"].copy_((F.linear(enc, L["ck_w"]) * self.scale).view(S, H, hd).permute(1, 2, 0))
            L["xv"].copy_(F.linear(enc, L["cv_w"], L["cv_b"]).view(S, H, hd).transpose(0, 1))

    def _decode(self, ids, pos):
        """ids, pos: [T] -> logits [1, vocab] for the last token. Writes the self-attention cache at pos."""
        T, D, H, hd = ids.shape[0], self.D, self.H, self.hd
        x = self.emb.index_select(0, ids) + self.pos_emb.index_select(0, pos)
        # token t may look at cache positions <= pos[t]; whatever lies beyond is stale and masked out
        mask = torch.where((self.ar > pos.view(1, T, 1)).expand(H, T, self.M), self.neg, self.zero)
        for L in self.layers:
            h = F.layer_norm(x, (D,), *L["ln1"])
            qkv = F.linear(h, L["qkv_w"], L["qkv_b"])
            q = qkv[:, :D].reshape(T, H, hd).transpose(0, 1)                       # [H, T, hd]
            L["sk"].index_copy_(2, pos, qkv[:, D:2 * D].reshape(T, H, hd).permute(1, 2, 0))
            L["sv"].index_copy_(1, pos, qkv[:, 2 * D:].reshape(T, H, hd).transpose(0, 1))
            a = torch.bmm(torch.softmax(torch.baddbmm(mask, q, L["sk"], alpha=self.scale), -1), L["sv"])
            x = torch.addmm(x, a.transpose(0, 1).reshape(T, D), L["so_wt"]) + L["so_b"]

            h = F.layer_norm(x, (D,), *L["ln2"])
            q = F.linear(h, L["cq_w"], L["cq_b"]).reshape(T, H, hd).transpose(0, 1)
            a = torch.bmm(torch.softmax(torch.bmm(q, L["xk"]), -1), L["xv"])
            x = torch.addmm(x, a.transpose(0, 1).reshape(T, D), L["co_wt"]) + L["co_b"]

            h = F.gelu(F.linear(F.layer_norm(x, (D,), *L["ln3"]), L["f1_w"], L["f1_b"]))
            x = torch.addmm(x, h, L["f2_wt"]) + L["f2_b"]
        return F.linear(F.layer_norm(x[T - 1:], (D,), *self.ln_f), self.proj)

    def _prefill(self):
        tok = (self._decode(self.prompt, self.prompt_pos) + self.bias).argmax(-1)
        self.ids.copy_(tok)
        self.pos.fill_(self.N_PROMPT)
        return tok

    def _step(self):
        logits = self._decode(self.ids, self.pos)
        tok = (logits + self.bias).argmax(-1)
        lang = (logits + self.lang_bias).argmax(-1)       # only meaningful right after <|startoftranscript|>
        self.ids.copy_(tok)
        self.pos.add_(1)
        return tok, lang

    def _features(self, audio: np.ndarray):
        n = min(audio.shape[0], self.n_samples)
        self.wave.zero_()
        self.wave[:n].copy_(torch.from_numpy(np.ascontiguousarray(audio[:n], dtype=np.float32)))
        stft = torch.stft(self.wave, self.n_fft, self.hop, window=self.window, return_complex=True)
        spec = (self.mel @ stft[..., :-1].abs().square()).clamp_(min=1e-10).log10_()
        spec = torch.maximum(spec, spec.max() - 8.0).add_(4.0).mul_(0.25)
        self.feats[0].copy_(spec)

    # -- driving it -----------------------------------------------------------------------------------
    def encode(self, audio: np.ndarray):
        self._features(audio)
        if self.graphs:
            self.g_encode.replay()
        else:
            self._encode()

    def detect_language(self, sot: int) -> int:
        self.ids.fill_(sot)
        self.pos.fill_(0)
        if self.graphs:
            self.g_step.replay()
            return int(self.out_lang)
        return int(self._step()[1])

    def first_token(self, prompt: list[int]) -> int:
        self.prompt.copy_(torch.tensor(prompt, dtype=torch.long))
        if self.graphs:
            self.g_prefill.replay()
            return int(self.out_first)
        return int(self._prefill())

    def next_token(self) -> int:
        if self.graphs:
            self.g_step.replay()
            return int(self.out_tok)
        return int(self._step()[0])


__all__ = ["WhisperWeights", "WhisperRunner", "mel_filters"]
