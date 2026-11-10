// Speech output and input. Output is the practice voice (the clinician's own recorded voice, generated on the
// server) when it is available, otherwise the device's voices (Web Speech API; Microsoft Edge offers natural
// neural voices such as "Ryan"). Input uses the browser's speech recogniser.

const PREFS_KEY = "aiconsultator.voice";
const SHORT_PAUSE_MS = 1200; // "..." in a script
const LONG_PAUSE_MS = 3500; // "(pause)" in a script
const PARAGRAPH_PAUSE_MS = 1800;
const SESSION_SENTENCE_PAUSE_MS = 450; // between the sentences of one passage in a session
const END_OF_ANSWER_SILENCE_MS = 2200; // how long the client can pause before their answer counts as finished

const deviceVoice = typeof window !== "undefined" && "speechSynthesis" in window;
const Recognition = typeof window !== "undefined" && (window.SpeechRecognition || window.webkitSpeechRecognition);

// Whether this device can record audio at all. Recording is what the apps use now (the audio is transcribed on
// the server), so this (not the Web Speech API) is what decides if a client can answer aloud. The Web Speech
// API is only a fallback for browsers that cannot record, and only Edge offers it reliably.
const MediaRecorderAvailable = () => typeof window !== "undefined"
  && "MediaRecorder" in window
  && Boolean(navigator.mediaDevices?.getUserMedia);

// Listening needs either a recorder (any modern browser, any phone) or the browser's own speech recognition.
export const canListen = MediaRecorderAvailable() || Boolean(Recognition);
// Whether the page is shown inside a phone app (an Android WebView, or an iPhone app's web view) rather than in a
// browser. There the app, not an address bar, decides about the microphone, so the client is told to look there.
export const inPhoneApp = typeof navigator !== "undefined" && (
  /; wv\)/.test(navigator.userAgent) // Android WebView
  || (/iPhone|iPad|iPod/.test(navigator.userAgent) && !/Safari\//.test(navigator.userAgent))); // iOS, not a browser

// Whether the browser can turn speech into words by itself (needed when the server has no speech to text).
export const canRecognise = Boolean(Recognition);

// The one practice voice, chosen by the clinician in the dashboard; clients cannot change it.
// An empty voiceName means "the best voice on this device" (natural British male first). If the chosen voice
// is not installed on a device, the best available one is used instead.
export const prefs = { voiceName: "", rate: 0.9, pitch: 0.9, sessionRate: 0.8 };

export function applyPracticeVoice(settings) {
  prefs.voiceName = settings.voice_name || "";
  prefs.rate = settings.rate;
  prefs.pitch = settings.pitch;
  prefs.sessionRate = settings.session_rate;
}

// Settings the clinician tried in this browser before the practice voice existed (earlier test versions).
export function legacyLocalSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "null");
    if (!saved) return null;
    return { voice_name: saved.voiceName || "", rate: saved.rate, pitch: saved.pitch, session_rate: saved.sessionRate };
  } catch {
    return null;
  }
}

// The Web Speech API does not report gender, so male voices are recognised by name.
const MALE_NAMES = /\b(Ryan|Thomas|Guy|Christopher|Eric|Roger|Steffan|Andrew|Brian|Davis|Tony|Jason|William|Liam|Connor|Mitchell|Prabhat|Luke|Wayne|Ken|Neil|Darren|Duncan|Elliot|Oliver|Alfie|Noah|Ethan|George|Daniel|David|Mark|James|Conrad|Killian|Florian|Kasper|Ralf|Bernd|Christoph|Klaus|Stefan|Markus|Hans|Jonas|Henri|Remy|Rémy|Alain|Claude|Jerome|Jérôme|Maurice|Yves|Fabrice|Paul|Alvaro|Álvaro|Jorge|Pablo|Arnau|Dario|Darío|Gerardo|Saul|Teo|Diego|Male)\b/i;

export const isNatural = (voice) => /natural|neural/i.test(voice.name);
export const isMale = (voice) => MALE_NAMES.test(voice.name);

// The client's language (set by the app), and each language's preferred accent and speech tag.
const LANGUAGE_TAGS = { en: "en-GB", de: "de-DE", fr: "fr-FR", es: "es-ES" };
let language = "en";
export function setLanguage(code) { language = LANGUAGE_TAGS[code] ? code : "en"; }
export const languageTag = (code = language) => LANGUAGE_TAGS[code] || "en-GB";

function rank(voice, code) {
  const lang = voice.lang.toLowerCase().replace("_", "-");
  return (isNatural(voice) ? 0 : 100) + (isMale(voice) ? 0 : 10) + (lang === languageTag(code).toLowerCase() ? 0 : 1);
}

export function voicesFor(code = language) {
  if (!deviceVoice) return [];
  return speechSynthesis.getVoices()
    .filter((v) => v.lang.toLowerCase().startsWith(code))
    .sort((a, b) => rank(a, code) - rank(b, code) || a.name.localeCompare(b.name));
}

export const englishVoices = () => voicesFor("en");

// Browsers load their voice lists asynchronously (Edge's natural voices arrive after the basic ones).
export function whenVoicesReady(callback) {
  if (!deviceVoice) return;
  if (speechSynthesis.getVoices().length) callback();
  speechSynthesis.addEventListener("voiceschanged", callback);
}

// Looked up at every utterance, so late-loading natural voices are picked up automatically.
// The clinician's chosen voice is an English one: in another language, the best voice for that language.
function currentVoice() {
  const voices = voicesFor(language);
  return voices.find((v) => v.name === prefs.voiceName) || voices[0] || null;
}

export function cleanForSpeech(text) {
  return text
    .split("\n")
    .filter((line) => !/^\s*(#|>)/.test(line))
    .join("\n")
    .replace(/\*+/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

// Sentences as the server splits them too (app/speech.py), so each one is recognised as the practice's own words.
export const splitSentences = (text) =>
  (cleanForSpeech(text).match(/[^.!?]+[.!?]*/g) || []).map((s) => s.trim()).filter(Boolean);

// Every stop invalidates the chains started before it, so a cancelled reply never carries on.
let generation = 0;

export function currentVoiceName() {
  const voice = currentVoice();
  return voice ? `${voice.name}${isNatural(voice) ? "" : " (basic)"}` : "";
}

// How each delivery shifts the practice voice: [pace factor, pitch factor]. The practice wants a steady,
// even hypnotic voice, so only the pace changes, and only slightly; the pitch stays constant.
const DELIVERY = {
  warm: [1, 1],
  gentle: [0.95, 1],
  reassuring: [0.96, 1],
  curious: [1, 1],
  encouraging: [1.02, 1],
  serious: [0.96, 1],
};
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const jitter = (amount) => 1 + (Math.random() * 2 - 1) * amount; // people never say two sentences identically
const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

// --- the practice voice: generated on the server, streamed and played with Web Audio --------------------------

const SPEECH_RATE = 24000; // the voice's own sample rate: the whole path runs at it, with no conversion per piece
const practice = { url: null, extra: null, output: null, ctx: null, node: null, analyser: null, samples: null,
  requests: new Set(), endings: new Map(), endId: 0, active: 0, underruns: 0 };

// How the audio is doing, for diagnosing sound problems: times the voice ran dry mid-sentence, and (where the
// browser reports it) moments its audio output could not keep up, which are heard as glitches.
export function audioHealth() {
  const stats = practice.ctx?.playoutStats;
  return {
    voiceRanDry: practice.underruns,
    outputGlitches: stats ? stats.fallbackFramesEvents ?? null : null,
    outputGlitchMs: stats ? Math.round((stats.fallbackFramesDuration ?? 0) * 1000) / 1000 : null,
    sampleRate: practice.ctx?.sampleRate ?? null,
  };
}

// `url` returns the speech endpoint of the current session (or null); pass null to use the device voice.
// `extra` optionally returns more request fields (the dashboard's trial speed).
export function usePracticeVoice(url, extra = null) { practice.url = url; practice.extra = extra; }
export const practiceVoiceActive = () => Boolean(practice.url?.());
export const supported = () => deviceVoice || practiceVoiceActive();

// --- reverb for the session voice: a warm hall, made here (no outside software) ------------------------------

// A seeded random generator, so the "room" is the same every time (and in every preview).
function mulberry32(seed) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export const HALL = { seconds: 2.4, decay: 1.6, predelayMs: 18, brightHz: 6500, darkHz: 1400 };

// The sound of the room (a stereo impulse response): a few early reflections, then a dense tail that dies away
// over `decay` seconds (-60 dB) and grows darker as it fades, as in a real hall. Left and right differ, for space.
// Normalised so that `setReverb` sets the reverb's level directly.
export function reverbImpulse(sampleRate, room = HALL) {
  const length = Math.round(room.seconds * sampleRate);
  const predelay = Math.round((room.predelayMs / 1000) * sampleRate);
  const channels = [0, 1].map((side) => {
    const random = mulberry32(1234 + side * 9973);
    const out = new Float32Array(length);
    let low = 0;
    for (let i = predelay; i < length; i += 1) {
      const t = (i - predelay) / sampleRate;
      const cutoff = room.darkHz + (room.brightHz - room.darkHz) * Math.exp(-t / (room.decay / 3)); // darker in time
      low += (1 - Math.exp((-2 * Math.PI * cutoff) / sampleRate)) * (random() * 2 - 1 - low);
      const build = Math.min(1, t / 0.03); // the tail builds up over 30 ms
      out[i] = low * Math.exp((-6.91 * t) / room.decay) * build;
    }
    const taps = [[11, 0.5], [19, 0.38], [27, 0.33], [38, 0.26], [53, 0.19], [71, 0.14]]; // early reflections
    for (const [ms, gain] of taps) {
      const at = predelay + Math.round(((ms + side * 3.7) / 1000) * sampleRate);
      if (at < length) out[at] += gain * (side ? -1 : 1) * 0.35;
    }
    let energy = 0;
    for (const v of out) energy += v * v;
    const scale = 1 / Math.sqrt(energy || 1);
    for (let i = 0; i < length; i += 1) out[i] *= scale;
    return out;
  });
  return channels;
}

const REVERB_MAX = 0.5; // the reverb's level at amount 1 (energy relative to the voice): 0.5 is -6 dB
const REVERB_HIGH_SHELF = { hz: 4000, db: -4 }; // the room's highs a little lower (same as session_audio.py)

// How much reverb the voice has, 0 (none: the consultation) to 1; changes smoothly, never with a jump.
export async function setReverb(amount) {
  const output = await audioOutput();
  if (!output) return;
  const now = output.ctx.currentTime;
  output.wet.gain.cancelScheduledValues(now);
  output.wet.gain.setValueAtTime(output.wet.gain.value, now);
  output.wet.gain.linearRampToValueAtTime(clamp(amount, 0, 1) * REVERB_MAX, now + 0.3);
}

// One continuous output stream (static/pcm_player.js) at the voice's own rate; the browser converts the whole
// stream for the speakers in one smooth pass. The voice goes out unchanged ("dry"), with the reverb alongside it
// ("wet", silent unless setReverb turns it up). Resolves null where that is not possible (then the device voice).
async function audioOutput() {
  practice.output ??= (async () => {
    try {
      const ctx = new AudioContext({ sampleRate: SPEECH_RATE });
      await ctx.audioWorklet.addModule("/static/pcm_player.js");
      const node = new AudioWorkletNode(ctx, "pcm-player");
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 256; // about 10 ms: short enough to follow single syllables
      node.connect(analyser); // for the mouth only
      node.connect(ctx.destination); // the voice itself
      const impulse = reverbImpulse(ctx.sampleRate);
      const room = ctx.createBuffer(2, impulse[0].length, ctx.sampleRate);
      impulse.forEach((channel, i) => room.copyToChannel(channel, i));
      const reverb = ctx.createConvolver();
      reverb.normalize = false;
      reverb.buffer = room;
      const darker = ctx.createBiquadFilter(); // a little less of the room's high end; mids and lows untouched
      darker.type = "highshelf";
      darker.frequency.value = REVERB_HIGH_SHELF.hz;
      darker.gain.value = REVERB_HIGH_SHELF.db;
      const wet = ctx.createGain();
      wet.gain.value = 0;
      node.connect(reverb);
      reverb.connect(darker);
      darker.connect(wet);
      wet.connect(ctx.destination);
      node.port.onmessage = ({ data }) => {
        if (data.underrun) {
          practice.underruns += 1;
          console.warn("Practice voice: the voice arrived late and paused briefly");
          return;
        }
        if (data.ended === undefined) return;
        practice.endings.get(data.ended)?.(true);
        practice.endings.delete(data.ended);
      };
      Object.assign(practice, { ctx, node, analyser, wet, samples: new Float32Array(analyser.fftSize) });
      return practice;
    } catch {
      return null;
    }
  })();
  const output = await practice.output;
  if (output?.ctx.state === "suspended") output.ctx.resume().catch(() => {});
  return output;
}

// How far the mouth should be open at this moment, 0 to 1, from the practice voice as it plays. Measured against
// the voice's recent peak, so it opens on each syllable and closes between them, and stays shut in pauses.
const mouth = { peakDb: -60, at: 0 };
export function mouthLevel() {
  if (!practice.analyser || !practice.active) { mouth.peakDb = -60; return 0; }
  practice.analyser.getFloatTimeDomainData(practice.samples);
  let sum = 0;
  for (const value of practice.samples) sum += value * value;
  const db = 10 * Math.log10(sum / practice.samples.length + 1e-10);
  const now = performance.now();
  mouth.peakDb = Math.max(db, mouth.peakDb - (now - mouth.at) * 0.015); // the reference sinks 15 dB a second
  mouth.at = now;
  if (db < Math.max(-60, mouth.peakDb - 30)) return 0;
  return 0.95 * clamp((db - (mouth.peakDb - 9)) / 9, 0, 1); // syllables dip only 2-8 dB, so the range is narrow
}

// Asks the server to speak one sentence ("conversation": natural speed; "session": the pace of the part it belongs
// to, found by its position in the session's delivery plan). The audio (16-bit mono PCM) is collected while it is
// being generated.
function requestSpeech(text, kind, position) {
  const controller = new AbortController();
  const item = { chunks: [], rate: 24000, done: false, status: 0, waiters: [], controller };
  const notify = () => item.waiters.splice(0).forEach((wake) => wake());
  item.changed = () => new Promise((resolve) => { item.waiters.push(resolve); });
  practice.requests.add(item);
  (async () => {
    try {
      const url = practice.url?.();
      if (!url) throw new Error("no practice voice");
      const own = url.startsWith("/api/clinician/") ? { "X-Requested-With": "admin" } : {}; // the dashboard's own
      const response = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json", ...own },
        body: JSON.stringify({ text, kind, position, ...(practice.extra?.() || {}) }),
        signal: controller.signal,
      });
      item.status = response.status;
      if (!response.ok) throw new Error(`speech ${response.status}`);
      item.rate = Number(response.headers.get("X-Sample-Rate")) || 24000;
      const reader = response.body.getReader();
      let carry = new Uint8Array(0); // a sample split across two network chunks
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const bytes = new Uint8Array(carry.length + value.length);
        bytes.set(carry);
        bytes.set(value, carry.length);
        const usable = bytes.length - (bytes.length % 2);
        carry = bytes.slice(usable);
        const view = new DataView(bytes.buffer, 0, usable);
        const floats = new Float32Array(usable / 2);
        for (let i = 0; i < floats.length; i += 1) floats[i] = view.getInt16(i * 2, true) / 32768;
        if (floats.length) { item.chunks.push(floats); notify(); }
      }
    } catch {
      item.failed = true;
    }
    item.done = true;
    practice.requests.delete(item);
    notify();
  })();
  return item;
}

// Plays one requested sentence while it is still arriving. Resolves true once heard, false if stopped, and null if
// the practice voice could not say it (the caller then uses the device voice). A client is never left in silence:
// if no sound has come after FIRST_SOUND_TIMEOUT_MS, the request is given up.
const FIRST_SOUND_TIMEOUT_MS = 6000;
async function playSpeech(item, mine, onSound) {
  const output = await audioOutput();
  if (!output) return null;
  const waitingSince = performance.now();
  let next = 0;
  for (;;) {
    if (mine !== generation) return false;
    if (!next && !item.done && performance.now() - waitingSince > FIRST_SOUND_TIMEOUT_MS) {
      item.controller.abort();
      return null;
    }
    if (next < item.chunks.length) {
      if (!next) { practice.active += 1; onSound?.(); } // the voice starts now (after its short prebuffer)
      output.node.port.postMessage({ samples: item.chunks[next] }); // a copy: the request keeps its audio
      next += 1;
    } else if (item.done) {
      break;
    } else {
      await (next ? item.changed() : Promise.race([item.changed(), sleep(500)])); // re-check the time limit
    }
  }
  if (!next) {
    if (item.status === 404 || item.status === 503) practice.url = null; // not available: stop asking
    return null;
  }
  if (item.rate !== output.ctx.sampleRate) return null; // not expected: the server speaks at 24 kHz
  practice.endId += 1;
  const played = new Promise((resolve) => { practice.endings.set(practice.endId, resolve); });
  output.node.port.postMessage({ end: practice.endId });
  const heard = await played;
  practice.active = Math.max(0, practice.active - 1);
  return heard && mine === generation;
}

// Speaks texts[i] in the practice voice while the next one is already being generated.
function practiceQueue(texts, kind) {
  const items = new Map();
  const fetchItem = (i) => {
    if (i < texts.length && (!items.has(i) || items.get(i).controller.signal.aborted)) {
      items.set(i, requestSpeech(texts[i], kind, i));
    }
    return items.get(i);
  };
  return {
    async say(i, mine, onSound) {
      const item = fetchItem(i);
      fetchItem(i + 1);
      const heard = await playSpeech(item, mine, onSound);
      if (heard !== false) items.delete(i);
      return heard;
    },
  };
}

// --- the device voice ---------------------------------------------------------------------------------------

// onWord(word) fires as the voice starts each word. Some voices never report words; for those,
// onNoWords(words, startedAt) fires shortly after speech starts so the timing can be estimated instead.
function utter(text, rate, { onEnd, onWord, onNoWords, onSound, pitch = prefs.pitch } = {}) {
  if (!deviceVoice) { onEnd?.(); return; }
  const utterance = new SpeechSynthesisUtterance(text);
  const voice = currentVoice();
  if (voice) { utterance.voice = voice; utterance.lang = voice.lang; } else utterance.lang = languageTag();
  utterance.rate = rate;
  utterance.pitch = pitch;
  let sawWord = false;
  let fallback = null;
  utterance.onstart = () => {
    const startedAt = performance.now();
    onSound?.();
    if (onNoWords) {
      fallback = setTimeout(() => { if (!sawWord) onNoWords(text.split(/\s+/).filter(Boolean), startedAt); }, 350);
    }
  };
  const finish = () => { clearTimeout(fallback); onEnd?.(); };
  utterance.onend = finish;
  utterance.onerror = finish;
  if (onWord) {
    utterance.onboundary = (event) => {
      if (event.name === "sentence") return;
      sawWord = true;
      const word = event.charLength
        ? text.substr(event.charIndex, event.charLength)
        : (text.slice(event.charIndex).match(/^\S+/) || [""])[0];
      onWord(word);
    };
  }
  speechSynthesis.speak(utterance);
}

const utterAsync = (text, rate, options = {}) => new Promise((resolve) => { utter(text, rate, { ...options, onEnd: resolve }); });

export function stopSpeaking() {
  generation += 1;
  if (deviceVoice) speechSynthesis.cancel();
  for (const item of practice.requests) item.controller.abort(); // the server stops generating too
  practice.node?.port.postMessage({ clear: true }); // silence at once
  for (const resolve of practice.endings.values()) resolve(false);
  practice.endings.clear();
  practice.active = 0;
}

// Speaks one reply sentence by sentence, in the given delivery, with a short breathing pause between sentences.
// Resolves true when finished, false if it was stopped.
// Handlers: onStart(), onSentence(text), onSound() (that sentence is now heard), onWord(word),
// onNoWords(words, startedAt), onSentenceEnd(), onEnd().
// Word events only come from the device voice; with the practice voice the mouth follows mouthLevel().
export async function speakReply(text, handlers = {}, delivery = "warm") {
  stopSpeaking();
  const mine = generation;
  const sentences = splitSentences(text);
  if (!supported() || !sentences.length) return true;
  const [paceFactor, pitchFactor] = DELIVERY[delivery] || DELIVERY.warm;
  const queue = practiceVoiceActive() ? practiceQueue(sentences, "conversation") : null;
  handlers.onStart?.();
  for (let i = 0; i < sentences.length && mine === generation; i += 1) {
    handlers.onSentence?.(sentences[i]);
    const heard = queue && practiceVoiceActive() ? await queue.say(i, mine, handlers.onSound) : null;
    if (heard === null && mine === generation) {
      await utterAsync(sentences[i], clamp(prefs.rate * paceFactor * jitter(0.01), 0.3, 2), {
        pitch: clamp(prefs.pitch * pitchFactor, 0.1, 2), onWord: handlers.onWord, onNoWords: handlers.onNoWords,
        onSound: handlers.onSound,
      });
    }
    handlers.onSentenceEnd?.();
    if (i < sentences.length - 1 && mine === generation) await sleep((280 + Math.random() * 240) / paceFactor);
  }
  handlers.onEnd?.();
  return mine === generation;
}

// The brief acknowledgement a person gives the moment someone finishes speaking. The phrases come from the server
// (so the practice voice has them ready); these are used until then.
let acknowledgements = ["Okay."]; // the practice's choice: a calm "Okay." after each answer
let lastAcknowledgement = "";
export function setAcknowledgements(phrases) { if (phrases?.length) acknowledgements = phrases; }
export function acknowledgement() {
  const others = acknowledgements.filter((a) => a !== lastAcknowledgement);
  const choices = others.length ? others : acknowledgements; // with a single phrase, the same one every time
  lastAcknowledgement = choices[Math.floor(Math.random() * choices.length)];
  return lastAcknowledgement;
}

// Listens for one spoken answer. Records with the MediaRecorder API and has the audio turned into words on the
// server (app/transcribe.py), so listening works in any browser (the Web Speech API only works reliably in Edge,
// and not at all over plain HTTP in Chrome). Resolves with the transcript ("" if nothing was said) once the client
// has been silent for END_OF_ANSWER_SILENCE_MS. Rejects with an error code for real failures (for example
// "not-allowed" when the microphone is blocked). Returns { promise, stop }.
// `submit` is how the recorded audio reaches the server: (blob) => Promise<string>, the transcript.
export function listenForAnswer({ lang = languageTag(), onInterim, submit, microphoneStream } = {}) {
  if (!submit || !MediaRecorderAvailable()) {
    if (!Recognition) {
      // Nothing here can turn speech into words: say so, rather than failing (or waiting) without a word.
      const promise = Promise.reject("speech-unavailable");
      promise.catch(() => {});
      return { promise, stop() {} };
    }
    return listenWithRecognition({ lang, onInterim });
  }
  return listenWithRecorder({ submit, microphoneStream });
}

// Recording, then transcription on the server. Used wherever the browser can record (every phone and desktop).
// The microphone's level decides when the answer is over: it waits while the client speaks, and finishes once
// they have been quiet for END_OF_ANSWER_SILENCE_MS after speaking. If they say nothing at all it resolves ""
// without sending anything, and the caller listens again (as the browser's recogniser did).
const SPEECH_DB = -45; // louder than this counts as speaking (noise suppression keeps a quiet room well below)
const NOTHING_SAID_MS = 8000; // no speech at all in this long: listen again
const MAX_ANSWER_MS = 120_000; // the same cap the server applies (app/transcribe.py)

function listenWithRecorder({ submit, microphoneStream = null }) {
  let settled = false;
  let resolveFn;
  let rejectFn;
  let recorder = null;
  let stream = null;
  let context = null;
  let levelTimer = null;
  let heard = false;
  let quietSince = 0;
  let startedAt = 0;
  const chunks = [];
  const promise = new Promise((resolve, reject) => { resolveFn = resolve; rejectFn = reject; });

  const release = () => {
    clearInterval(levelTimer);
    if (!microphoneStream) stream?.getTracks().forEach((track) => track.stop());
    context?.close().catch(() => {});
  };

  // The recorder hands over its last piece only after it has stopped, so wait for that before sending.
  const stopped = () => new Promise((resolve) => {
    if (!recorder || recorder.state === "inactive") { resolve(); return; }
    recorder.addEventListener("stop", () => resolve(), { once: true });
    try { recorder.stop(); } catch { resolve(); }
  });

  const finish = async (send) => {
    if (settled) return;
    settled = true;
    clearInterval(levelTimer);
    await stopped();
    const blob = new Blob(chunks, { type: recorder?.mimeType || "audio/webm" });
    release();
    if (!send || !blob.size) { resolveFn(""); return; }
    try {
      resolveFn(await submit(blob));
    } catch (code) {
      rejectFn(code);
    }
  };

  const openedStream = microphoneStream
    ? Promise.resolve(microphoneStream)
    : navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  openedStream.then(async (opened) => {
      if (settled) { if (!microphoneStream) opened.getTracks().forEach((t) => t.stop()); return; }
      stream = opened;
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (event) => { if (event.data?.size) chunks.push(event.data); };
      recorder.onerror = () => { if (!settled) { settled = true; release(); rejectFn("recorder-failed"); } };

      context = new AudioContext();
      await context.resume().catch(() => {});
      const analyser = context.createAnalyser();
      analyser.fftSize = 1024;
      context.createMediaStreamSource(stream).connect(analyser);
      const buffer = new Float32Array(analyser.fftSize);

      recorder.start(250);
      startedAt = performance.now();
      quietSince = startedAt;
      levelTimer = setInterval(() => {
        analyser.getFloatTimeDomainData(buffer);
        let sum = 0;
        for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
        const db = 10 * Math.log10(sum / buffer.length + 1e-12);
        const now = performance.now();
        if (db > SPEECH_DB) { heard = true; quietSince = now; }
        if (heard && now - quietSince > END_OF_ANSWER_SILENCE_MS) finish(true);
        else if (!heard && now - startedAt > NOTHING_SAID_MS) finish(false);
        else if (now - startedAt > MAX_ANSWER_MS) finish(true);
      }, 100);
    })
    .catch((error) => {
      if (settled) return;
      settled = true;
      release();
      rejectFn(error?.name === "NotAllowedError" || error?.name === "SecurityError" ? "not-allowed" : "start-failed");
    });

  return {
    promise,
    stop() {
      if (settled) return;
      settled = true;
      clearInterval(levelTimer);
      try { recorder?.stop(); } catch { /* already stopped */ }
      release();
      resolveFn("");
    },
  };
}

// Listens with the browser's own speech recognition. Kept for older browsers that cannot record; Microsoft Edge
// offers this and nothing else, and it is the only path that never sends audio to the server.
function listenWithRecognition({ lang, onInterim }) {
  const recognition = new Recognition();
  recognition.lang = lang;
  recognition.continuous = true;
  recognition.interimResults = true;
  let finalText = "";
  let interimText = "";
  let silenceTimer = null;
  let settled = false;
  let resolveFn;
  let rejectFn;
  const promise = new Promise((resolve, reject) => { resolveFn = resolve; rejectFn = reject; });

  const finish = () => {
    if (settled) return;
    settled = true;
    clearTimeout(silenceTimer);
    try { recognition.stop(); } catch { /* already stopped */ }
    resolveFn(`${finalText} ${interimText}`.replace(/\s+/g, " ").trim());
  };

  recognition.onresult = (event) => {
    interimText = "";
    for (let i = event.resultIndex; i < event.results.length; i += 1) {
      const piece = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalText += ` ${piece}`;
      else interimText += ` ${piece}`;
    }
    onInterim?.(`${finalText} ${interimText}`.replace(/\s+/g, " ").trim());
    clearTimeout(silenceTimer);
    silenceTimer = setTimeout(finish, END_OF_ANSWER_SILENCE_MS);
  };
  recognition.onerror = (event) => {
    if (settled) return;
    if (event.error === "no-speech" || event.error === "aborted") return; // onend follows
    settled = true;
    clearTimeout(silenceTimer);
    rejectFn(event.error);
  };
  recognition.onend = finish;
  try {
    recognition.start();
  } catch {
    settled = true;
    rejectFn("start-failed");
  }
  return {
    promise,
    stop() {
      if (settled) return;
      settled = true;
      clearTimeout(silenceTimer);
      try { recognition.abort(); } catch { /* already stopped */ }
      resolveFn("");
    },
  };
}

// Splits a session script into spoken segments, each followed by the pause its markers call for.
export function sessionSegments(script) {
  const segments = [];
  const paragraphs = script.split(/\n\s*\n/).map((p) => p.split("\n").filter((l) => !/^\s*(#|>)/.test(l)).join(" "));
  for (const paragraph of paragraphs) {
    const pieces = paragraph.split(/(\(pause\)|\.\.\.|…)/i);
    for (let i = 0; i < pieces.length; i += 2) {
      const text = pieces[i].replace(/\*+/g, "").replace(/\s+/g, " ").trim();
      const marker = (pieces[i + 1] || "").toLowerCase();
      const pause = marker === "(pause)" ? LONG_PAUSE_MS : marker ? SHORT_PAUSE_MS : 0;
      if (text) segments.push({ text, pause });
      else if (pause && segments.length) segments[segments.length - 1].pause += pause;
    }
    if (segments.length) segments[segments.length - 1].pause += PARAGRAPH_PAUSE_MS;
  }
  return segments;
}

// A script as spoken pieces with the pauses its markers call for (for text without a delivery plan from the server,
// such as the dashboard's samples).
export function scriptItems(script) {
  return sessionSegments(script).flatMap((segment) => {
    const parts = splitSentences(segment.text);
    return parts.map((text, i) => ({ text, pause: i === parts.length - 1 ? segment.pause : SESSION_SENTENCE_PAUSE_MS }));
  });
}

// Plays a session: pieces [{text, pause}] one after another, each followed by its pause in ms. For a client's
// session the server's delivery plan sets the phrasing, pauses and pace; a plain script is split here instead.
// Returns controls; onProgress(fraction done) and onDone() are optional.
export function createSessionPlayer(plan, { onProgress, onDone } = {}) {
  const lines = typeof plan === "string" ? scriptItems(plan) : plan;
  const queue = practiceQueue(lines.map((line) => line.text), "session");
  let index = 0;
  let playing = false;
  let timer = null;
  let mine = -1;

  async function step() {
    if (!playing || mine !== generation) return;
    if (index >= lines.length) { playing = false; onDone?.(); return; }
    const line = lines[index];
    onProgress?.(index / lines.length);
    const heard = practiceVoiceActive() ? await queue.say(index, mine) : null;
    if (heard === null && mine === generation) await utterAsync(line.text, prefs.sessionRate);
    if (mine !== generation || !playing) return;
    index += 1;
    timer = setTimeout(step, line.pause);
  }

  return {
    total: lines.length,
    play() {
      if (!supported() || playing) return;
      stopSpeaking();
      mine = generation;
      playing = true;
      step();
    },
    pause() {
      playing = false;
      clearTimeout(timer);
      stopSpeaking();
    },
    stop() {
      this.pause();
      index = 0;
    },
    get playing() { return playing; },
  };
}
