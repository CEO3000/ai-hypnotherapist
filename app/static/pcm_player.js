// Audio worklet: plays the practice voice as one continuous stream. Pieces of 16-bit PCM arrive from the server
// (already turned into floats) and are played seamlessly one after another. Separate audio buffers played back to
// back leave a one-sample step at every join, which is heard as a faint click; a single stream has no joins.
//
// Messages in:  {samples: Float32Array} (queue audio), {end: id} (a sentence ends here), {clear: true} (stop now).
// Messages out: {ended: id} when playback passes an end marker, {pong: id} in reply to {ping: id},
// {underrun: true} when the voice ran dry mid-sentence (for the app's audio health count).
// A sentence only starts once PREBUFFER samples (or its end) have arrived, so the stream does not run dry mid-word.

const PREBUFFER = 0.15; // seconds
const RAMP = 96; // samples (4 ms): the soft end and start around a moment the voice ran dry

class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.queue = [];
    this.offset = 0; // position within queue[0]
    this.queued = 0; // samples waiting
    this.playing = false;
    this.fadeIn = 0; // samples of soft start still to apply
    this.port.onmessage = ({ data }) => {
      if (data.clear) {
        this.queue = [];
        this.offset = 0;
        this.queued = 0;
        this.playing = false;
      } else if (data.samples) {
        this.queue.push(data.samples);
        this.queued += data.samples.length;
      } else if (data.end !== undefined) {
        this.queue.push({ end: data.end });
      } else if (data.ping !== undefined) {
        this.port.postMessage({ pong: data.ping });
      }
    };
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!this.playing) {
      const complete = this.queue.some((piece) => piece.end !== undefined);
      if (this.queued < PREBUFFER * sampleRate && !complete) return true; // silence until enough has arrived
      this.playing = true;
    }
    let written = 0;
    while (written < out.length && this.queue.length) {
      const head = this.queue[0];
      if (head.end !== undefined) {
        this.port.postMessage({ ended: head.end });
        this.queue.shift();
        this.playing = this.queue.length > 0;
        continue;
      }
      const count = Math.min(out.length - written, head.length - this.offset);
      out.set(head.subarray(this.offset, this.offset + count), written);
      for (let i = 0; this.fadeIn > 0 && i < count; i += 1, this.fadeIn -= 1) {
        out[written + i] *= 1 - this.fadeIn / RAMP; // coming back after running dry: no hard start
      }
      written += count;
      this.offset += count;
      this.queued -= count;
      if (this.offset >= head.length) {
        this.queue.shift();
        this.offset = 0;
      }
    }
    if (this.playing && written < out.length && !this.queue.length) {
      // The voice ran dry in the middle of a sentence (it arrived late): end softly rather than with a click,
      // and wait for enough to arrive before carrying on, so it does not stutter.
      const ramp = Math.min(written, RAMP);
      for (let i = 0; i < ramp; i += 1) out[written - ramp + i] *= 1 - (i + 1) / ramp;
      this.playing = false;
      this.fadeIn = RAMP;
      this.port.postMessage({ underrun: true });
    }
    return true;
  }
}

registerProcessor("pcm-player", PcmPlayer);
