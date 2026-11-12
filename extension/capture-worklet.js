// Runs on the audio thread at 16 kHz. Downmixes to mono and posts 32 ms chunks: exactly one frame
// of the server's voice detector, so it never waits for audio that has already been captured.
const CHUNK = 512;

class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.chunk = new Float32Array(CHUNK);
    this.pos = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channels = input.length;
    const frames = input[0].length;
    for (let i = 0; i < frames; i++) {
      let s = 0;
      for (let c = 0; c < channels; c++) s += input[c][i];
      this.chunk[this.pos++] = s / channels;
      if (this.pos === this.chunk.length) {
        this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
        this.chunk = new Float32Array(CHUNK);
        this.pos = 0;
      }
    }
    return true;
  }
}

registerProcessor("capture", CaptureProcessor);
