"""
SenseVoice-Small on the GPU without sherpa-onnx: the same ONNX file run by onnxruntime's CUDA provider, with
the feature steps (kaldi fbank -> stack 7 frames every 6 -> mean/variance) and the CTC read-out done here.
Used to see how fast a non-Whisper model gets on the GPU; results are checked against sherpa-onnx (CPU).
"""
import numpy as np
import onnxruntime as ort
import torch
import torchaudio.compliance.kaldi as kaldi


class SenseVoiceGPU:
    def __init__(self, model: str, tokens: str, provider: str = "cuda"):
        so = ort.SessionOptions()
        so.log_severity_level = 3
        # HEURISTIC: no exhaustive cuDNN search every time a line has a new length (lines all differ in length)
        cuda = ("CUDAExecutionProvider", {"cudnn_conv_algo_search": "HEURISTIC"})
        providers = ([cuda] if provider == "cuda" else []) + ["CPUExecutionProvider"]
        self.sess = ort.InferenceSession(model, so, providers=providers)
        self.provider = self.sess.get_providers()[0]
        meta = self.sess.get_modelmeta().custom_metadata_map
        self.m, self.n = int(meta["lfr_window_size"]), int(meta["lfr_window_shift"])
        self.neg_mean = np.array([float(x) for x in meta["neg_mean"].split(",")], dtype=np.float32)
        self.inv_std = np.array([float(x) for x in meta["inv_stddev"].split(",")], dtype=np.float32)
        self.lang = {k[5:]: int(v) for k, v in meta.items() if k.startswith("lang_")}
        self.itn = int(meta["with_itn"])
        self.scale = 1.0 if meta.get("normalize_samples", "0") == "1" else 32768.0
        self.tok = {}
        for line in open(tokens, encoding="utf-8"):
            piece, idx = line.rstrip("\n").rsplit(" ", 1)
            self.tok[int(idx)] = piece

    def features(self, audio: np.ndarray) -> np.ndarray:
        w = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0) * self.scale
        f = kaldi.fbank(w, num_mel_bins=80, frame_length=25, frame_shift=10, dither=0.0, sample_frequency=16000,
                        window_type="hamming", use_energy=False).numpy()
        m, n = self.m, self.n
        T = f.shape[0]
        f = np.concatenate([np.repeat(f[:1], (m - 1) // 2, 0), f])
        T_lfr = int(np.ceil(T / n))
        need = (T_lfr - 1) * n + m
        if need > f.shape[0]:
            f = np.concatenate([f, np.repeat(f[-1:], need - f.shape[0], 0)])
        idx = np.arange(T_lfr)[:, None] * n + np.arange(m)[None, :]
        x = f[idx].reshape(T_lfr, -1)
        return ((x + self.neg_mean) * self.inv_std).astype(np.float32)

    def run(self, audio: np.ndarray, lang: str) -> str:
        x = self.features(audio)[None]
        logits = self.sess.run(None, {"x": x, "x_length": np.array([x.shape[1]], np.int32),
                                      "language": np.array([self.lang.get(lang, 0)], np.int32),
                                      "text_norm": np.array([self.itn], np.int32)})[0][0]
        ids = logits.argmax(-1)[4:]                     # the first four frames are language / emotion / event / itn tags
        keep = ids[np.insert(ids[1:] != ids[:-1], 0, True)]
        text = "".join(self.tok[i] for i in keep if i != 0)
        return text.replace("▁", " ").strip()
