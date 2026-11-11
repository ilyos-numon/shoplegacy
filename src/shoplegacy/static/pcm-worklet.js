// AudioWorklet that converts microphone audio to 16 kHz PCM16 frames.
// Input float32 at the context sample rate is linearly resampled to 16 kHz,
// converted to Int16 and posted to the main thread every 4096 samples.
var TARGET_RATE = 16000;
var FRAME_SAMPLES = 4096;

class Pcm16Worklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / TARGET_RATE;
    this.pos = 0;          // read position (in input samples) relative to this.prev
    this.prev = 0;         // last sample of the previous input block
    this.out = new Int16Array(FRAME_SAMPLES);
    this.outLen = 0;
  }

  push(value) {
    var v = value < -1 ? -1 : value > 1 ? 1 : value;
    this.out[this.outLen++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.outLen === FRAME_SAMPLES) {
      var buf = this.out.buffer;
      this.port.postMessage(buf, [buf]);
      this.out = new Int16Array(FRAME_SAMPLES);
      this.outLen = 0;
    }
  }

  process(inputs) {
    var input = inputs[0];
    if (!input || input.length === 0 || !input[0]) {
      return true;
    }
    var ch = input[0];
    var n = ch.length;
    // Sample index -1 is this.prev, 0..n-1 is the current block.
    var pos = this.pos;
    while (pos < n - 1) {
      var i = Math.floor(pos);
      var frac = pos - i;
      var a = i < 0 ? this.prev : ch[i];
      var b = ch[i + 1];
      this.push(a + (b - a) * frac);
      pos += this.ratio;
    }
    this.pos = pos - n;
    this.prev = ch[n - 1];
    return true;
  }
}

registerProcessor("pcm16-worklet", Pcm16Worklet);
