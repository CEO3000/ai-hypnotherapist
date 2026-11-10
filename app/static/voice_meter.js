// Measures HOW the client speaks, on their own device: loudness, pitch, pitch variation, pauses and pace.
// No audio is recorded or sent anywhere; only these numbers go to the server with the answer.

const FRAME_MS = 40;
const PAUSE_MIN_S = 0.5;
const MIN_PITCH_HZ = 70;
const MAX_PITCH_HZ = 400;

// Autocorrelation pitch estimate on a downsampled copy of the frame (cheap enough for old laptops and phones).
function estimatePitch(frame, sampleRate) {
  const step = Math.max(1, Math.floor(sampleRate / 11025));
  const rate = sampleRate / step;
  const data = [];
  for (let i = 0; i < frame.length; i += step) data.push(frame[i]);
  const n = data.length;
  let energy = 0;
  for (let i = 0; i < n; i += 1) energy += data[i] * data[i];
  if (energy === 0) return 0;
  const minLag = Math.floor(rate / MAX_PITCH_HZ);
  const maxLag = Math.min(Math.floor(rate / MIN_PITCH_HZ), n - 1);
  let bestLag = 0;
  let best = 0;
  for (let lag = minLag; lag <= maxLag; lag += 1) {
    let sum = 0;
    for (let i = 0; i < n - lag; i += 1) sum += data[i] * data[i + lag];
    const normalised = sum / (energy * (n - lag) / n);
    if (normalised > best) { best = normalised; bestLag = lag; }
  }
  return best > 0.55 && bestLag ? rate / bestLag : 0;
}

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length ? sorted[Math.floor(sorted.length / 2)] : 0;
};
const mean = (values) => (values.length ? values.reduce((a, b) => a + b, 0) / values.length : 0);

export function summarise(frames, transcript) {
  if (frames.length < 10) return null;
  const levels = frames.map((f) => f.db);
  const floor = [...levels].sort((a, b) => a - b)[Math.floor(levels.length * 0.1)];
  const threshold = Math.max(floor + 10, -60);
  const voiced = frames.map((f) => f.db > threshold);
  const first = voiced.indexOf(true);
  const last = voiced.lastIndexOf(true);
  if (first < 0) return null;
  const speaking = frames.slice(first, last + 1);
  const speakingVoiced = voiced.slice(first, last + 1);

  let pauses = 0;
  let longest = 0;
  let run = 0;
  for (const isVoiced of speakingVoiced) {
    if (isVoiced) {
      if (run * FRAME_MS / 1000 >= PAUSE_MIN_S) {
        pauses += 1;
        longest = Math.max(longest, run * FRAME_MS / 1000);
      }
      run = 0;
    } else {
      run += 1;
    }
  }
  const voicedFrames = speaking.filter((_, i) => speakingVoiced[i]);
  const pitches = voicedFrames.map((f) => f.pitch).filter((p) => p > 0);
  const pitchMean = mean(pitches);
  const pitchSd = Math.sqrt(mean(pitches.map((p) => (p - pitchMean) ** 2)));
  const seconds = (speaking.length * FRAME_MS) / 1000;
  const words = (transcript.match(/\S+/g) || []).length;
  const round = (value, digits = 2) => Math.round(value * 10 ** digits) / 10 ** digits;
  return {
    duration_s: round(seconds, 1),
    speech_ratio: round(voicedFrames.length / speaking.length),
    loudness_db: round(Math.max(-120, Math.min(0, mean(voicedFrames.map((f) => f.db)))), 1),
    pitch_hz: round(median(pitches), 0),
    pitch_variation: round(pitchMean ? pitchSd / pitchMean : 0),
    pause_count: pauses,
    longest_pause_s: round(longest, 1),
    words_per_minute: round(seconds > 0 ? Math.min(600, words / (seconds / 60)) : 0, 0),
  };
}

// Resolves to a meter, or rejects if the microphone cannot be opened (the consultation then simply has no cues).
export async function createVoiceMeter() {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: false },
  });
  const context = new AudioContext();
  await context.resume();
  const analyser = context.createAnalyser();
  analyser.fftSize = 2048;
  context.createMediaStreamSource(stream).connect(analyser);
  const buffer = new Float32Array(analyser.fftSize);
  let frames = [];
  let timer = null;

  const sample = () => {
    analyser.getFloatTimeDomainData(buffer);
    let sum = 0;
    for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
    const db = 10 * Math.log10(sum / buffer.length + 1e-12);
    frames.push({ db, pitch: db > -50 ? estimatePitch(buffer, context.sampleRate) : 0 });
  };

  return {
    start() {
      clearInterval(timer);
      frames = [];
      timer = setInterval(sample, FRAME_MS);
    },
    stop(transcript) {
      clearInterval(timer);
      timer = null;
      return summarise(frames, transcript);
    },
    // The stream is shared with the answer recorder when possible. Opening a second microphone stream can
    // conflict with permissions or hardware on some phones and browsers.
    stream() {
      return stream;
    },
    close() {
      clearInterval(timer);
      stream.getTracks().forEach((track) => track.stop());
      context.close();
    },
    // Whether the microphone is still delivering sound (a stream ends if a headset is unplugged, for example).
    live() {
      return stream.getAudioTracks().some((track) => track.readyState === "live");
    },
    // The client's loudness at this moment (dB), so the avatar can nod when they finish a phrase.
    level() {
      analyser.getFloatTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
      return 10 * Math.log10(sum / buffer.length + 1e-12);
    },
  };
}
