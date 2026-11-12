
### Japanese: writing down what is said

| Model | Runs on | Errors, short lines (as in Subtle) | Errors, whole sentences | Time per line | Text appears after speaker stops | VRAM | RAM |
|---|---|---|---|---|---|---|---|
| Whisper large-v3 | GPU | 6.1% | 3.9% | 98 ms (95% under 139) | 226 ms | 4.5 GB | 1.5 GB |
| Parakeet-TDT-CTC 0.6B ja (Subtle engine, CUDA graph) | GPU | 6.2% | 6.6% | 8 ms (95% under 10) | 136 ms | 2.1 GB | 2.3 GB |
| Parakeet-TDT-CTC 0.6B ja | GPU | 6.4% | 6.6% | 43 ms (95% under 50) | 171 ms | 3.5 GB | 1.4 GB |
| Parakeet-TDT-CTC 0.6B ja (int8) | CPU x4 | 6.4% | 7.0% | 123 ms (95% under 171) | 251 ms | 0.0 GB | 0.9 GB |
| Whisper large-v3-turbo (Subtle's Best mode today) | GPU | 7.0% | 4.4% | 43 ms (95% under 52) | 171 ms | 2.7 GB | 1.5 GB |
| Qwen3-ASR 1.7B | GPU | 8.3% | 5.9% | 591 ms (95% under 996) | 719 ms | 5.1 GB | 4.2 GB |
| Kotoba-Whisper v2.0 | GPU | 8.4% | 7.0% | 29 ms (95% under 34) | 157 ms | 2.7 GB | 1.5 GB |
| Kotoba-Whisper v2.2 | GPU | 8.4% | 7.0% | 31 ms (95% under 36) | 159 ms | 2.8 GB | 1.5 GB |
| Kotoba-Whisper bilingual v1.0 | GPU | 8.5% | 7.8% | 30 ms (95% under 36) | 158 ms | 2.3 GB | 1.5 GB |
| Fun-ASR-Nano | CPU x4 | 9.0% | 7.1% | 1097 ms (95% under 1693) | 1225 ms | 0.0 GB | 4.9 GB |
| Anime-Whisper | GPU | 10.3% | 8.5% | 30 ms (95% under 36) | 158 ms | 2.3 GB | 1.5 GB |
| SenseVoice-Small | GPU | 11.7% | 8.1% | 39 ms (95% under 57) | 167 ms | 4.5 GB | 1.3 GB |
| SenseVoice-Small (own GPU runner) | GPU | 11.7% | 8.1% | 36 ms (95% under 44) | 164 ms | 2.4 GB | 1.2 GB |
| SenseVoice-Small (Subtle engine, CUDA graph) | GPU | 11.7% | 8.1% | 6 ms (95% under 8) | 134 ms | 1.9 GB | 1.2 GB |
| SenseVoice-Small | CPU x4 | 11.7% | 8.1% | 79 ms (95% under 110) | 207 ms | 0.0 GB | 3.0 GB |
| Qwen3-ASR 0.6B | GPU | 12.3% | 9.3% | 528 ms (95% under 766) | 656 ms | 2.7 GB | 2.1 GB |
| Dolphin small | GPU | 13.4% | 11.4% | 21 ms (95% under 25) | 149 ms | 2.4 GB | 1.4 GB |
| Dolphin small | CPU x4 | 13.4% | 11.4% | 93 ms (95% under 154) | 221 ms | 0.0 GB | 1.2 GB |
| ReazonSpeech Zipformer (int8) | CPU x4 | 14.2% | 8.6% | 54 ms (95% under 72) | 182 ms | 0.0 GB | 0.5 GB |

### Japanese: straight to English (chrF, higher is better)

| Route | chrF | Time per line | VRAM |
|---|---|---|---|
| Best mode: Whisper turbo + Qwen3-4B-Instruct-2507-Q4_K_M | 49.1 | 92 ms (31 + 61) | 5.7 GB |
| Whisper large-v3, direct | 44.5 | 66 ms | 4.0 GB |
| Kotoba-Whisper bilingual v1.0, direct | 43.8 | 27 ms | 2.3 GB |

### Chinese (Mandarin): writing down what is said

| Model | Runs on | Errors, short lines (as in Subtle) | Errors, whole sentences | Time per line | Text appears after speaker stops | VRAM | RAM |
|---|---|---|---|---|---|---|---|
| Fun-ASR-Nano | CPU x4 | 9.2% | 8.8% | 778 ms (95% under 1243) | 906 ms | 0.0 GB | 5.2 GB |
| SenseVoice-Small | GPU | 9.3% | 8.6% | 36 ms (95% under 44) | 164 ms | 5.5 GB | 1.4 GB |
| SenseVoice-Small (own GPU runner) | GPU | 9.3% | 8.6% | 36 ms (95% under 42) | 164 ms | 2.4 GB | 1.2 GB |
| SenseVoice-Small (Subtle engine, CUDA graph) | GPU | 9.3% | 8.6% | 6 ms (95% under 8) | 134 ms | 1.9 GB | 1.2 GB |
| SenseVoice-Small | CPU x4 | 9.3% | 8.6% | 55 ms (95% under 77) | 183 ms | 0.0 GB | 3.1 GB |
| Qwen3-ASR 0.6B | GPU | 9.8% | 8.3% | 599 ms (95% under 2144) | 727 ms | 2.8 GB | 2.1 GB |
| Qwen3-ASR 1.7B | GPU | 9.9% | 7.9% | 412 ms (95% under 692) | 540 ms | 5.2 GB | 2.0 GB |
| Whisper large-v3 | GPU | 10.5% | 8.7% | 93 ms (95% under 141) | 221 ms | 4.5 GB | 1.5 GB |
| FireRedASR2 (int8) | CPU x4 | 11.4% | 20.9% | 677 ms (95% under 1102) | 805 ms | 0.0 GB | 1.7 GB |
| Whisper large-v3-turbo (Subtle's Best mode today) | GPU | 11.7% | 9.8% | 40 ms (95% under 51) | 168 ms | 2.7 GB | 1.5 GB |
| Paraformer-zh | CPU x4 | 12.0% | 11.1% | 46 ms (95% under 68) | 174 ms | 0.0 GB | 0.9 GB |
| Belle-Whisper large-v3-zh-punct | GPU | 12.4% | 10.6% | 88 ms (95% under 132) | 216 ms | 4.0 GB | 1.5 GB |
| Belle-Whisper large-v3-turbo-zh | GPU | 12.5% | 11.7% | 37 ms (95% under 47) | 165 ms | 2.9 GB | 1.5 GB |
| Paraformer-zh | GPU | 13.4% | 15.8% | 69 ms (95% under 107) | 197 ms | 1.5 GB | 1.2 GB |
| Dolphin small | GPU | 18.3% | 12.2% | 20 ms (95% under 25) | 148 ms | 2.4 GB | 1.4 GB |
| Dolphin small | CPU x4 | 18.3% | 12.2% | 100 ms (95% under 156) | 228 ms | 0.0 GB | 1.2 GB |

### Chinese (Mandarin): straight to English (chrF, higher is better)

| Route | chrF | Time per line | VRAM |
|---|---|---|---|
| Best mode: Whisper turbo + Qwen3-4B-Instruct-2507-Q4_K_M | 51.5 | 85 ms (30 + 56) | 5.7 GB |
| Whisper large-v3, direct | 43.6 | 57 ms | 4.0 GB |

### Korean: writing down what is said

| Model | Runs on | Errors, short lines (as in Subtle) | Errors, whole sentences | Time per line | Text appears after speaker stops | VRAM | RAM |
|---|---|---|---|---|---|---|---|
| Whisper large-v3 | GPU | 5.4% | 4.3% | 92 ms (95% under 128) | 220 ms | 4.5 GB | 1.5 GB |
| Qwen3-ASR 1.7B | GPU | 5.6% | 4.3% | 634 ms (95% under 1080) | 762 ms | 5.2 GB | 2.0 GB |
| Whisper large-v3-turbo (Subtle's Best mode today) | GPU | 6.3% | 5.4% | 33 ms (95% under 40) | 161 ms | 2.7 GB | 1.5 GB |
| Qwen3-ASR 0.6B | GPU | 7.3% | 5.4% | 556 ms (95% under 868) | 684 ms | 2.8 GB | 2.1 GB |
| SenseVoice-Small | GPU | 9.4% | 8.4% | 35 ms (95% under 41) | 163 ms | 6.5 GB | 1.6 GB |
| SenseVoice-Small (own GPU runner) | GPU | 9.4% | 8.4% | 32 ms (95% under 35) | 160 ms | 2.4 GB | 1.2 GB |
| SenseVoice-Small (Subtle engine, CUDA graph) | GPU | 9.4% | 8.4% | 6 ms (95% under 8) | 134 ms | 1.9 GB | 1.2 GB |
| SenseVoice-Small | CPU x4 | 9.4% | 8.4% | 70 ms (95% under 106) | 198 ms | 0.0 GB | 3.3 GB |
| Dolphin small | GPU | 11.9% | 8.3% | 19 ms (95% under 22) | 147 ms | 3.4 GB | 1.5 GB |
| Dolphin small | CPU x4 | 11.9% | 8.3% | 101 ms (95% under 153) | 229 ms | 0.0 GB | 1.3 GB |
| Whisper large-v3-turbo-korean (ghost613) | GPU | 36.0% | 12.5% | 37 ms (95% under 48) | 165 ms | 2.9 GB | 1.5 GB |

### Korean: straight to English (chrF, higher is better)

| Route | chrF | Time per line | VRAM |
|---|---|---|---|
| Best mode: Whisper turbo + Qwen3-4B-Instruct-2507-Q4_K_M | 50.4 | 84 ms (28 + 55) | 5.7 GB |
| Whisper large-v3, direct | 44.2 | 58 ms | 4.0 GB |
