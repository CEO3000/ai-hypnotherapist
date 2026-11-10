// Patient-facing single-page app. All dynamic text is inserted as text nodes, never as HTML.

import { createAvatar } from "./avatar_image.js"; // the practice's own picture (a green android)
import { createRain } from "./matrix.js";
import * as voice from "./voice.js";
import { createVoiceMeter } from "./voice_meter.js";

const STORAGE_KEY = "aiconsultator.session";
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE_RE = /^[0-9+()\-\s]{5,40}$/;
const LANGUAGE_KEY = "aiconsultator.language";
const app = document.getElementById("app");

let strings = {};
let config = null;
let language = "en"; // the client's language: every screen, the consultant's voice and the microphone follow it
let sessionId = null;
let pollTimer = null;
let teardown = null; // cleans up the current screen's microphone, avatar and animations

function t(key, vars = {}) {
  let text = strings[key] ?? key;
  for (const [name, value] of Object.entries(vars)) text = text.replace(`{${name}}`, String(value));
  return text;
}

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value === null || value === undefined) continue;
    if (key === "class") el.className = value;
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

const sleep = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

function stopPolling() {
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = null;
}

function render(...nodes) {
  stopPolling();
  voice.stopSpeaking();
  if (teardown) {
    const cleanup = teardown;
    teardown = null;
    cleanup();
  }
  app.replaceChildren(...nodes);
  window.scrollTo(0, 0);
}

// --- storage & URL (storage can be unavailable, so every access is guarded) ---

function saveSessionId(id) {
  try { localStorage.setItem(STORAGE_KEY, id); } catch { /* private mode */ }
  tellWebsite(id);
}
function loadSessionId() { try { return localStorage.getItem(STORAGE_KEY); } catch { return null; } }
function forgetSessionId() {
  try { localStorage.removeItem(STORAGE_KEY); } catch { /* private mode */ }
  tellWebsite(null);
}
// A session's private link: #s=<id>, optionally with an action from an email (&followup or &stop-emails).
function sessionIdFromUrl() {
  const match = location.hash.match(/^#s=([A-Za-z0-9_-]{20,64})(?:&(followup|stop-emails))?$/);
  return match ? match[1] : null;
}
function actionFromUrl() {
  const match = location.hash.match(/&(followup|stop-emails)$/);
  return match ? match[1] : null;
}

function startOver() {
  sessionId = null;
  forgetSessionId();
  history.replaceState(null, "", location.pathname);
  showWelcome();
}

// --- API ---

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (!response.ok) {
    let detail = "";
    try { detail = (await response.json()).detail; } catch { /* not JSON */ }
    const error = new Error(typeof detail === "string" ? detail : `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

const sessionUrl = (suffix = "") => `/api/sessions/${encodeURIComponent(sessionId)}${suffix}`;

async function* readSSE(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) event = line.slice(7);
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      yield { event, data: data ? JSON.parse(data) : {} };
    }
  }
}

// --- routing ---

function route(id, view) {
  sessionId = id;
  saveSessionId(id);
  history.replaceState(null, "", `#s=${id}`);
  switch (view.status) {
    case "screening_stopped": return showScreeningOutcome(view);
    case "awaiting_consultation_payment": return showPayment(view);
    case "consulting": return showConsultation(view);
    case "consultation_stopped":
      return view.stop_level === "crisis" ? showCrisis(view.crisis_resources) : showConcern(view);
    case "consultation_incomplete": return showIncomplete(view);
    case "extracting": return showWaiting("extracting", view.status, 3000);
    case "extraction_failed": return showExtractionFailed();
    case "awaiting_choice": return showChoice(view);
    case "awaiting_session_payment": return showPayment(view);
    case "generating": return view.audio ? showSessionPage(view) : showWaiting("generating", view.status, 5000);
    case "generation_failed": return showGenerationFailed();
    case "pending_review": return showWaiting("review", view.status, 20000);
    case "ready": return view.audio ? showSessionPage(view) : showReady(view);
    case "expired": return showExpired();
    case "rejected": return showRejected();
    case "human_requested": return showHumanDone();
    default: return showFatal();
  }
}

// hold(): while it returns true the page stays as it is even if the status has changed (checked again next time).
function pollUntilChanged(currentStatus, intervalMs, hold = () => false) {
  pollTimer = setTimeout(async () => {
    try {
      const view = await api(sessionUrl());
      if (view.status !== currentStatus && !hold()) {
        route(sessionId, view);
        return;
      }
    } catch { /* keep polling through brief network problems */ }
    pollUntilChanged(currentStatus, intervalMs, hold);
  }, intervalMs);
}

// --- screens ---

function startOverButton() {
  return h("button", { class: "link", type: "button", onclick: startOver }, t("app.start_over"));
}

// The languages the client can choose, each in its own language: changing it redraws the welcome page.
function languagePicker() {
  if (config.languages.length < 2) return null;
  // Its heading is in every language offered, so a visitor sees it is there for them before reading anything else.
  return h("section", { class: "language-choice", role: "group", "aria-label": t("welcome.language") },
    h("p", { class: "language-heading" }, h("span", { "aria-hidden": "true" }, "🌐 "),
      "Choose your language · Sprache wählen · Choisissez votre langue · Elija su idioma"),
    h("div", { class: "languages" }, config.languages.map((option) => h("button", {
      type: "button", lang: option.code, class: option.code === language ? "language current" : "language",
      "aria-pressed": String(option.code === language),
      onclick: async () => {
        if (option.code === language) return;
        try {
          await useLanguage(option.code);
          rememberLanguage(option.code);
        } catch { return showFatal(); }
        showWelcome();
      },
    }, option.name))));
}

function showWelcome() {
  const prices = config.prices;
  const select = h("select", { id: "country" },
    config.countries.map((c) => h("option", { value: c.code }, c.name)));
  render(
    languagePicker(),
    h("h1", {}, t("welcome.title")),
    h("p", {}, t("welcome.intro")),
    h("div", { class: "notice" }, t("welcome.prepare")),
    h("div", { class: "notice" }, t("welcome.honesty")),
    h("div", { class: "notice" }, t("welcome.ai_disclosure"),
      config.practice_voice ? ` ${t("welcome.voice_disclosure")}` : ""),
    h("div", { class: "card highlight" },
      h("h2", {}, t("welcome.prices_title")),
      h("p", {}, t("welcome.price_ai", { total: prices.total, consultation: prices.consultation,
                                          session: prices.ai_session })),
      h("p", {}, t("welcome.price_human", { from: prices.human_session_from, to: prices.human_session_to }))),
    h("h2", {}, t("welcome.steps_title")),
    h("ol", { class: "steps" }, h("li", {}, t("welcome.step1")),
      h("li", {}, t("welcome.step2", { consultation: prices.consultation })), h("li", {}, t("welcome.step3")),
      h("li", {}, t("welcome.step4", { session: prices.ai_session, from: prices.human_session_from,
                                       to: prices.human_session_to }))),
    h("label", { for: "country" }, t("welcome.country")),
    select,
    h("p", {}),
    h("button", { type: "button", onclick: () => showScreening(select.value) }, t("welcome.begin")),
  );
}

async function showScreening(country) {
  let screening;
  try { screening = await api(`/api/screening?language=${encodeURIComponent(language)}`); } catch { return showFatal(); }
  const questions = screening.questions.filter((q) => q.kind === "question");
  const consents = screening.questions.filter((q) => q.kind === "consent");
  const errorBox = h("div", { class: "error hidden", role: "alert" }, t("screening.incomplete"));
  const submit = h("button", { type: "submit" }, t("screening.submit"));

  const form = h("form", { onsubmit: onSubmit },
    questions.map((q) => h("fieldset", { class: "question" },
      h("legend", { class: "screen-reader-only" }, q.text),
      h("div", { class: "question-text" }, q.text),
      h("div", { class: "options" },
        h("label", { for: `${q.id}-yes` }, h("input", { id: `${q.id}-yes`, type: "radio", name: q.id, value: "yes" }), t("screening.yes")),
        h("label", { for: `${q.id}-no` }, h("input", { id: `${q.id}-no`, type: "radio", name: q.id, value: "no" }), t("screening.no"))))),
    h("h2", {}, t("screening.consent_title")),
    consents.map((q) => h("div", { class: "consent" },
      h("label", { for: q.id }, h("input", { id: q.id, type: "checkbox", name: q.id }), q.text))),
    errorBox,
    submit,
  );
  render(h("h1", {}, t("screening.title")), h("p", { class: "muted" }, t("screening.intro")), form);

  async function onSubmit(event) {
    event.preventDefault();
    const answers = {};
    for (const q of questions) {
      const picked = form.querySelector(`input[name="${q.id}"]:checked`);
      if (!picked) { errorBox.classList.remove("hidden"); return; }
      answers[q.id] = picked.value === "yes";
    }
    for (const q of consents) {
      const box = form.querySelector(`input[name="${q.id}"]`);
      if (!box.checked) { errorBox.classList.remove("hidden"); return; }
      answers[q.id] = true;
    }
    submit.disabled = true;
    try {
      const view = await api("/api/sessions", {
        method: "POST",
        body: JSON.stringify({ country, language, answers }),
      });
      route(view.session_id, view);
    } catch {
      submit.disabled = false;
      showFatal();
    }
  }
}

function showScreeningOutcome(view) {
  const outcome = view.screening_outcome;
  if (outcome === "crisis") return showCrisis(view.crisis_resources);
  if (outcome === "human_only") {
    return render(h("h1", {}, t("outcome.human_only.title")), h("p", {}, t("outcome.human_only.body")),
      humanRequestForm(() => null, false));
  }
  render(h("h1", {}, t(`outcome.${outcome}.title`)), h("p", {}, t(`outcome.${outcome}.body`)), startOverButton());
}

function showCrisis(resources = []) {
  render(h("div", { class: "card danger", role: "alert" },
    h("h1", {}, t("outcome.crisis.title")),
    h("p", {}, t("outcome.crisis.body")),
    h("ul", { class: "resources" },
      resources.map((r) => h("li", {}, h("strong", {}, r.name), r.detail)))));
}

// Stopped during the consultation for something the client said they have: the practice's hypnotherapist for what
// he works with himself, a doctor for any other safety reason. (A crisis has its own page, with no word of fees.)
function showConcern(view) {
  const fee = view.stop_level === "handoff" ? [] : noRefund(view); // a handoff is not about the client's answers
  if (view.referral === "doctor") {
    return render(h("h1", {}, t("referral.doctor.title")), h("p", {}, t("referral.doctor.body")), ...fee,
      startOverButton());
  }
  render(h("h1", {}, t("stopped.concern.title")), h("p", {}, t("stopped.concern.body")), ...fee,
    humanRequestForm(() => null));
}

// Why the consultation fee is not refunded (the client confirmed their safety answers before paying).
const noRefund = (view) => (view.consultation_fee
  ? [h("p", { class: "muted" }, t("referral.no_refund", { amount: view.consultation_fee }))] : []);

function bubble(role, content) {
  const el = h("div", { class: `bubble ${role}` },
    h("span", { class: "who" }, t(role === "patient" ? "consult.you" : "consult.assistant")),
    h("span", { class: "text" }, content));
  if (role === "assistant" && voice.supported()) {
    el.append(h("button", { type: "button", class: "replay", title: t("voice.replay"), "aria-label": t("voice.replay"),
      onclick: () => voice.speakReply(el.querySelector(".text").textContent) }, "▶"));
  }
  return el;
}

// Keeps the phone's screen on during the consultation: a phone dims and locks after a minute without a touch, which
// would stop the client in the middle of an answer. Browsers use the Screen Wake Lock (taken again whenever the page
// is shown again); the practice's Android app has its own switch (window.HypnoApp), as its WebView has no wake lock.
// Returns the function that lets the screen sleep again; leaving the page lets it sleep too.
function keepScreenOn() {
  let on = true;
  let lock = null;
  const app = (value) => { try { window.HypnoApp?.keepScreenOn(value); } catch { /* not the Android app */ } };
  const take = async () => {
    if (!on || document.visibilityState !== "visible") return;
    app(true);
    if (lock || !navigator.wakeLock) return;
    try {
      const taken = await navigator.wakeLock.request("screen");
      if (!on) { taken.release().catch(() => {}); return; }
      lock = taken;
      lock.addEventListener("release", () => { lock = null; });
    } catch { /* not allowed here (e.g. battery saver): the screen may dim as usual */ }
  };
  const release = () => {
    if (!on) return;
    on = false;
    document.removeEventListener("visibilitychange", take);
    window.removeEventListener("pagehide", release);
    lock?.release().catch(() => {});
    lock = null;
    app(false);
  };
  document.addEventListener("visibilitychange", take);
  window.addEventListener("pagehide", release); // the app was closed on the website page, or the tab left
  take();
  return release;
}

// The consultation fills the whole screen, like a video call: in a browser the address bar and tabs are hidden too
// (on a phone and on a computer; Esc or the back gesture leaves full screen, and the consultation carries on either
// way). Asked for only from the client's own press of a button, as browsers require. Not in the Android app (it is
// full screen already) nor inside the website's page, which makes its own window full screen when it opens the app
// (here, only the frame would fill the screen, hiding the website's close button). iPhones allow full screen for
// videos only: there the consultation fills the browser's window instead.
function goFullScreen() {
  if (embedded || voice.inPhoneApp || document.fullscreenElement || document.webkitFullscreenElement) return;
  const root = document.documentElement;
  try {
    const asked = (root.requestFullscreen || root.webkitRequestFullscreen)?.call(root, { navigationUI: "hide" });
    asked?.catch?.(() => {}); // refused (an older browser, or a setting): the page still fills the window
  } catch { /* not supported */ }
}

function leaveFullScreen() {
  if (embedded || !(document.fullscreenElement || document.webkitFullscreenElement)) return;
  try { (document.exitFullscreen || document.webkitExitFullscreen)?.call(document)?.catch?.(() => {}); } catch { /* */ }
}

// Live captions for the consultant: each word appears as it is spoken, in its place (the words still to come
// already take their space, invisibly, so nothing below moves). The device voice reports each word as it starts;
// the practice voice reports none, so its words follow its own speaking pace, learnt sentence by sentence, and a
// sentence is always complete the moment the voice has finished it. Screen readers hear the whole question once.
let captionPace = 2.6; // words per second, adjusted to the practice voice as it speaks
// The app's own "are you sure?" box. (A browser's confirm() pop-up is not used: inside the website's page, and in
// some phones and apps, browsers skip it without asking, as if the client had said yes or no.)
function askToConfirm(message, yes, no) {
  return new Promise((resolve) => {
    const finish = (answer) => { box.remove(); resolve(answer); };
    const [title, ...rest] = message.split("\n\n");
    const noButton = h("button", { type: "button", onclick: () => finish(false) }, no);
    const box = h("div", { class: "modal-backdrop" },
      h("div", { class: "modal", role: "alertdialog", "aria-modal": "true", "aria-label": title },
        h("p", { class: "modal-title" }, title),
        rest.map((text) => h("p", {}, text)),
        h("div", { class: "modal-actions" },
          h("button", { type: "button", class: "danger", onclick: () => finish(true) }, yes), noButton)));
    document.body.append(box);
    noButton.focus(); // the safe answer is the one ready to press
  });
}

function liveCaption(element, text) {
  const ends = [...text.matchAll(/\S+/g)].map((m) => m.index + m[0].length);
  const spoken = h("span");
  const unspoken = h("span", { class: "unspoken" });
  element.replaceChildren(spoken, unspoken);
  element.setAttribute("aria-busy", "true");
  let shown = 0; // words on screen
  let from = 0; // the sentence being spoken: words from..to
  let to = 0;
  let heardWords = 0; // word events in this sentence (device voice)
  let soundAt = null;
  let exact = false; // this sentence has word events
  let timer = null;
  const render = (count) => {
    shown = Math.max(shown, Math.min(count, ends.length));
    const cut = shown ? ends[shown - 1] : 0;
    spoken.textContent = text.slice(0, cut);
    unspoken.textContent = text.slice(cut);
    // A long question scrolls inside its box: keep the line being spoken in view.
    const bottom = spoken.offsetTop + spoken.offsetHeight;
    if (bottom > element.scrollTop + element.clientHeight) element.scrollTop = bottom - element.clientHeight;
  };
  const stop = () => { clearInterval(timer); timer = null; };
  const follow = (startedAt = performance.now()) => {
    stop();
    soundAt = startedAt;
    const tick = () => render(Math.min(to, from + 1 + Math.floor(((performance.now() - soundAt) / 1000) * captionPace)));
    tick();
    timer = setInterval(tick, 60);
  };
  render(0);
  return {
    sentence(sentence) {
      stop();
      from = Math.max(to, shown);
      to = Math.min(ends.length, from + (sentence.match(/\S+/g) || []).length);
      heardWords = 0;
      soundAt = null;
      exact = false;
    },
    sound() { if (!exact) follow(); },
    noWords(startedAt) { if (!exact) follow(startedAt); },
    word() {
      exact = true;
      stop();
      heardWords += 1;
      render(Math.min(to, from + heardWords));
    },
    sentenceEnd() {
      if (!exact && soundAt !== null) { // learn the voice's pace from how long this sentence took
        const seconds = (performance.now() - soundAt) / 1000;
        if (seconds > 0.5 && to - from > 1) {
          captionPace = Math.min(4.5, Math.max(1.5, captionPace * 0.5 + ((to - from) / seconds) * 0.5));
        }
      }
      stop();
      render(to);
    },
    done() {
      stop();
      spoken.textContent = text;
      unspoken.textContent = "";
      element.removeAttribute("aria-busy");
    },
  };
}

// The spoken consultation: the avatar speaks each reply, then listens for the client's answer (hands-free),
// with typing available as an alternative.
function showConsultation(view) {
  const chat = h("div", { class: "chat" }, [...view.messages].reverse().map((m) => bubble(m.role, m.content)));
  const avatarBox = h("div", { class: "avatar-box" });
  const status = h("p", { class: "stage-status", role: "status" });
  const lastSaid = [...view.messages].reverse().find((m) => m.role === "assistant");
  const question = h("p", { class: "question-now", "aria-live": "polite" }, lastSaid ? lastSaid.content : "");
  const caption = h("p", { class: "caption" }); // the client's words as they speak
  const note = h("div", { class: "notice hidden" });
  const errorBox = h("div", { class: "error hidden", role: "alert" });
  const talk = h("button", { type: "button", class: "mic" });
  const typeToggle = h("button", { type: "button", class: "secondary" }, t("stage.type"));
  const input = h("textarea", { rows: 2, maxlength: 4000, placeholder: t("consult.placeholder"),
    "aria-label": t("consult.placeholder") });
  const send = h("button", { type: "submit" }, t("consult.send"));
  const composer = h("form", { class: "composer hidden" }, input, send);
  // The client can pause (the microphone and their clock stop, as after a long silence) or end the consultation.
  const pauseButton = h("button", { type: "button", class: "secondary" }, t("stage.pause_consultation"));
  const endButton = h("button", { type: "button", class: "link end-consultation" }, t("stage.end"));
  const controls = h("div", { class: "stage-controls" }, talk, typeToggle, pauseButton);
  const clock = h("span", { class: "timer-clock" });
  const fill = h("div", { class: "timer-fill" });
  const timer = h("div", { class: "timer" }, clock, h("div", { class: "timer-bar" }, fill));
  const voiceInUse = h("p", { class: "muted small hidden" });

  // The consultation fills the screen like a video call: the avatar takes the space that is left, and the question,
  // any message, the typing box and the buttons always stay in view (the conversation so far is below it).
  render(
    h("section", { class: "stage" }, timer, avatarBox, status, question, caption, note, errorBox, composer, controls,
      endButton, voiceInUse),
    h("details", { class: "transcript" }, h("summary", {}, t("stage.transcript")), chat),
    startOverButton(),
  );
  document.body.classList.add("in-consultation");

  // Consultation clock: time used out of the planned length. The client's time starts with their first answer
  // (not when they press Start), and stands still while the consultation is paused; the server keeps the same clock.
  const totalSeconds = (view.consultation_minutes || 20) * 60;
  let paused = Boolean(view.consultation_paused);
  let clockStarted = Boolean(view.consultation_clock_started);
  let usedBefore = view.consultation_elapsed_s || 0; // seconds used up to runningSince
  let runningSince = clockStarted && !paused ? performance.now() : null;
  const usedSeconds = () => usedBefore + (runningSince === null ? 0 : (performance.now() - runningSince) / 1000);
  const runClock = (on) => {
    if (on && runningSince === null) runningSince = performance.now();
    if (!on && runningSince !== null) { usedBefore = usedSeconds(); runningSince = null; }
  };
  const mmss = (s) => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  const tick = () => {
    const seconds = usedSeconds();
    clock.textContent = clockStarted ? t("stage.timer", { elapsed: mmss(seconds), total: mmss(totalSeconds) })
      : t("stage.timer_waiting", { total: Math.round(totalSeconds / 60) });
    fill.style.width = `${Math.min(100, (seconds / totalSeconds) * 100)}%`;
  };
  tick();
  const clockTimer = setInterval(tick, 1000);

  const rainCanvas = h("canvas", { class: "rain-bg", "aria-hidden": "true" });
  document.body.append(rainCanvas);
  const rain = createRain(rainCanvas, { fade: 0.1 });
  const avatar = createAvatar(avatarBox);
  avatar.setMouthSource(voice.mouthLevel); // the practice voice moves the mouth by its loudness
  avatar.setListenSource(() => (meter ? meter.level() : -120)); // it nods when the client finishes a phrase
  let alive = true;
  let letScreenSleep = paused ? () => {} : keepScreenOn(); // until it ends or pauses (its time, or the client leaves)
  // Answering aloud needs the microphone and something to turn speech into words: this server or the browser.
  const canAnswerAloud = voice.canListen && (Boolean(config.transcribe) || voice.canRecognise);
  let handsFree = canAnswerAloud && !paused;
  let busy = false; // an answer is being sent and replied to
  // The consultant is saying goodbye: the server has already moved on to the summary, but the page waits for the
  // last words to be spoken before it changes (it used to cut them off mid-sentence).
  let closing = false;
  const reload = async () => route(sessionId, await api(sessionUrl())); // after a stop: where they go
  let loopRunning = false;
  let listener = null;
  let meter = null; // measures how the client speaks (tone cues); opened on first listen
  let meterOpening = null;
  let useServerTranscription = Boolean(config.transcribe); // off for the rest of the visit if the server can't
  let micFailures = 0; // failures in a row; a passing hiccup is retried without bothering the client
  const ensureMeter = () => {
    meterOpening ??= createVoiceMeter()
      .then((m) => { if (alive) meter = m; else m.close(); })
      .catch(() => {}); // no microphone access for measuring: the consultation continues without tone cues
    return meterOpening;
  };

  teardown = () => {
    alive = false;
    document.body.classList.remove("in-consultation");
    leaveFullScreen(); // the call has ended
    letScreenSleep();
    clearInterval(clockTimer);
    clearInterval(silenceTimer);
    clearInterval(voiceWatch);
    listener?.stop();
    meter?.close();
    avatar.dispose();
    rain.stop();
    rainCanvas.remove();
  };

  const setStatus = (key) => { status.textContent = key ? t(`stage.status.${key}`) : ""; };
  const updateTalk = () => {
    talk.textContent = paused ? t("stage.resume") : handsFree ? t("stage.pause") : t("stage.talk");
    talk.classList.toggle("active", handsFree || paused);
    pauseButton.classList.toggle("hidden", paused);
  };
  const showNote = (text) => { note.textContent = text; note.classList.remove("hidden"); };
  const showError = (code) => {
    const key = `consult.error.${code}`;
    errorBox.textContent = t(key) === key ? t("consult.error.generic") : t(key);
    errorBox.classList.remove("hidden");
  };
  // Shows the typing box. Only a click on "Type instead" moves the focus there: focusing it on its own would scroll
  // the page away from the microphone (and open the keyboard on a phone) while the client is still talking.
  const showComposer = (focus = false) => {
    composer.classList.remove("hidden");
    typeToggle.classList.add("hidden");
    if (focus) {
      input.focus({ preventScroll: true });
      setTimeout(() => composer.scrollIntoView({ block: "nearest" }), 350); // once the keyboard is up
    }
  };

  const mouthHandlers = {
    onWord: (word) => avatar.speakWord(word, voice.prefs.rate),
    onNoWords: (words, startedAt) => avatar.speakWords(words, voice.prefs.rate, startedAt),
    onSentenceEnd: () => avatar.closeMouth(),
  };

  // Silence. A minute after the consultant's question with no answer, it gently checks in; after two minutes the
  // consultation pauses (the microphone and the client's clock stop, and the screen may sleep) until they press
  // Continue. Both are fixed phrases in the practice voice: nothing is sent to the AI. Writing an answer is not silence.
  const CHECK_IN_AFTER_MS = 60_000;
  const PAUSE_AFTER_MS = 120_000;
  let quietSince = null; // since the consultant finished a question (null: not waiting for an answer)
  let checkedIn = false;
  let speaking = false;
  const waitForAnswer = () => { quietSince = performance.now(); checkedIn = false; };
  // Someone talking is never "quiet": while the microphone hears their voice (or their words appear), the minute
  // starts again, so a long answer (even ten minutes) is never interrupted by "take your time".
  const VOICE_DB = -45; // as SPEECH_DB in voice.js: louder than this is the client speaking
  const voiceWatch = setInterval(() => {
    if (quietSince !== null && !speaking && meter && meter.level() > VOICE_DB) waitForAnswer();
  }, 300);
  async function sayAside(text) { // spoken without replacing the question on screen
    if (!text) return;
    speaking = true;
    setStatus("speaking");
    caption.textContent = text;
    try {
      await voice.speakReply(text, { ...mouthHandlers, onStart: () => avatar.setState("speaking"),
                                     onEnd: () => avatar.setState("idle") });
    } finally {
      speaking = false;
      if (alive && caption.textContent === text) caption.textContent = "";
    }
  }
  async function checkSilence() {
    if (!alive || paused || busy || speaking || quietSince === null) return;
    if (input.value.trim()) { waitForAnswer(); return; } // they are writing their answer
    const quiet = performance.now() - quietSince;
    if (quiet >= PAUSE_AFTER_MS) {
      await pauseForSilence();
    } else if (quiet >= CHECK_IN_AFTER_MS && !checkedIn) {
      checkedIn = true;
      const listening = handsFree;
      speaking = true; // the microphone rests while the consultant speaks
      handsFree = false;
      listener?.stop();
      await sayAside(config.silence?.silence_check_in);
      if (!alive || paused || busy) return;
      handsFree = listening;
      if (handsFree) listenLoop();
    }
  }
  // manual: the client pressed "Pause consultation" (it stops at once, without the consultant saying so)
  async function pauseForSilence(manual = false) {
    quietSince = null;
    if (manual) voice.stopSpeaking();
    paused = true;
    speaking = true; // the microphone rests while the consultant speaks
    handsFree = false;
    listener?.stop();
    runClock(false);
    updateTalk();
    try { await api(sessionUrl("/pause"), { method: "POST" }); } catch { /* the server's clock pauses on its own check */ }
    if (!manual) await sayAside(config.silence?.silence_pause);
    if (!alive || !paused) return;
    letScreenSleep();
    letScreenSleep = () => {};
    avatar.setState("idle");
    setStatus("");
    showNote(t("stage.paused_silence"));
  }
  // Back from a pause. The client's clock stays stopped until they answer again (an answer resumes it on the server
  // too), so the minutes spent hearing the question again are never theirs.
  function resumeAfterPause() {
    if (!paused) return;
    paused = false;
    note.classList.add("hidden");
    letScreenSleep = keepScreenOn();
    updateTalk();
  }
  // The question to ask again after a pause: only its question sentences ("Thanks for answering that." again would
  // sound odd), after a short welcome back.
  const questionAgain = (text) => {
    const parts = voice.splitSentences(text);
    let first = parts.length;
    while (first > 0 && parts[first - 1].includes("?")) first -= 1;
    if (first === parts.length) {
      const last = parts.map((part) => part.includes("?")).lastIndexOf(true);
      if (last >= 0) first = last;
    }
    const asked = first < parts.length ? parts.slice(first).join(" ") : text;
    return [config.silence?.silence_resume, asked].filter(Boolean).join(" ");
  };
  const silenceTimer = setInterval(checkSilence, 2000);

  async function say(text, delivery = "warm") {
    setStatus("speaking");
    const live = liveCaption(question, text); // the words appear as they are spoken, and stay until the next question
    try {
      await voice.speakReply(text, {
        onWord: (word) => { mouthHandlers.onWord(word); live.word(); },
        onNoWords: (words, startedAt) => { mouthHandlers.onNoWords(words, startedAt); live.noWords(startedAt); },
        onSentenceEnd: () => { mouthHandlers.onSentenceEnd(); live.sentenceEnd(); },
        onSentence: live.sentence,
        onSound: live.sound,
        onStart: () => {
          avatar.setState("speaking");
          if (config.engine === "claude-code") {
            const name = voice.practiceVoiceActive() ? t("stage.practice_voice") : voice.currentVoiceName();
            voiceInUse.textContent = t("stage.voice_in_use", { name });
            voiceInUse.classList.remove("hidden");
          }
        },
        onEnd: () => avatar.setState("idle"),
      }, delivery);
    } finally {
      live.done(); // the whole question, also when the client interrupted or there is no voice at all
    }
    if (alive) caption.textContent = "";
    if (alive && !paused) waitForAnswer();
  }

  // Failures that are worth retrying by themselves: the recorder or recogniser stopped, one recording could not be
  // transcribed, or the browser's recogniser lost its connection. A blocked microphone is not.
  const RETRYABLE = new Set(["recorder-failed", "start-failed", "transcribe-failed", "transcribe-unavailable",
                             "network", "audio-capture", "no-speech", "aborted"]);
  const MAX_MIC_RETRIES = 3;

  // A problem while listening. Returns true if listening should simply carry on (the problem was dealt with).
  function recoverFromMic(code) {
    if (code === "transcribe-unavailable" && useServerTranscription) {
      // The server cannot turn speech into words: the browser's own recogniser takes over, where there is one.
      // It opens the microphone itself, so the meter's stream is closed (two at once can clash on phones).
      useServerTranscription = false;
      meter?.close();
      meter = null;
      if (voice.canRecognise) return true;
    }
    if (!RETRYABLE.has(code) || micFailures >= MAX_MIC_RETRIES) return false;
    micFailures += 1;
    // A microphone stream that ended (a headset unplugged, the device switched) cannot record again: open a new one.
    if (meter && !meter.live?.()) {
      meter.close();
      meter = null;
      meterOpening = null;
    }
    return true;
  }

  function micFailed(code) {
    handsFree = false;
    updateTalk();
    avatar.setState("idle");
    setStatus("paused");
    // "not-allowed": the browser or the client refused the microphone. Anything else (the recorder stopped, or
    // the server could not transcribe the answer) can be retried with Talk, or the answer typed.
    const blocked = code === "not-allowed" || code === "service-not-allowed";
    const message = blocked ? (voice.inPhoneApp ? "stage.mic_blocked_app" : "stage.mic_blocked")
      : code === "speech-unavailable" ? "stage.mic_unavailable" : "stage.mic_error";
    showNote(t(message));
    showComposer();
  }

  // Sends a recorded answer to the server to be turned into words (app/transcribe.py). Rejects with an error
  // code for real failures, so the caller can show the same messages as before.
  async function transcribe(blob) {
    let response;
    try {
      response = await fetch(sessionUrl("/transcribe"), {
        method: "POST",
        headers: { "Content-Type": blob.type || "audio/webm" },
        body: blob,
      });
    } catch {
      throw "transcribe-failed";
    }
    if (response.status === 503) throw "transcribe-unavailable";
    if (!response.ok) throw "transcribe-failed";
    const data = await response.json();
    return (data.text || "").trim();
  }

  // Keeps listening for answers while hands-free is on; each answer is sent and the reply spoken before listening again.
  async function listenLoop() {
    if (loopRunning || !canAnswerAloud) return;
    loopRunning = true;
    // The meter and recorder must share one stream. Opening the meter in parallel with the recorder can make
    // browsers prompt twice or reject one of the two microphone consumers. Recognition owns its microphone.
    if (useServerTranscription) await ensureMeter();
    try {
      while (alive && handsFree && !busy) {
        avatar.setState("listening");
        setStatus("listening");
        caption.textContent = "";
        meter?.start();
        listener = voice.listenForAnswer({ lang: voice.languageTag(view.language),
                                           onInterim: (text) => { caption.textContent = text; if (text) waitForAnswer(); },
                                           submit: useServerTranscription ? transcribe : null,
                                           microphoneStream: meter?.stream?.() || null });
        let heard;
        try {
          heard = await listener.promise;
        } catch (code) {
          listener = null;
          meter?.stop("");
          if (alive && handsFree && !busy && recoverFromMic(code)) {
            await sleep(500);
            if (useServerTranscription && !meter) await ensureMeter();
            continue;
          }
          micFailed(code);
          return;
        }
        micFailures = 0;
        listener = null;
        const features = meter ? meter.stop(heard) : null;
        if (!alive || !handsFree || busy) break;
        if (heard) {
          loopRunning = false;
          await submit(heard, features);
          return;
        }
        await sleep(300); // silence: listen again
      }
    } finally {
      loopRunning = false;
    }
    if (alive && !busy && !speaking) {
      avatar.setState("idle");
      setStatus(handsFree ? "" : "paused");
    }
  }

  // Sends one answer, then speaks the reply. Afterwards hands-free listening resumes.
  async function submit(text, features = null) {
    if (busy || !alive) return;
    busy = true;
    quietSince = null;
    if (paused) { // an answer resumes the consultation (on the server too)
      paused = false;
      note.classList.add("hidden");
      letScreenSleep = keepScreenOn();
    }
    clockStarted = true; // the client's time starts with their first answer
    runClock(true);
    listener?.stop();
    voice.stopSpeaking();
    errorBox.classList.add("hidden");
    send.disabled = true;
    const mine = bubble("patient", text);
    const reply = bubble("assistant", "");
    reply.classList.add("typing");
    chat.prepend(reply, mine); // the newest at the top
    caption.textContent = "";
    avatar.setState("thinking");
    setStatus("thinking");
    voice.speakReply(voice.acknowledgement(), mouthHandlers); // a person responds at once while they think

    let failure = null;
    let complete = false;
    let delivery = "warm";
    let redirect = null;
    try {
      const response = await fetch(sessionUrl("/messages"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(features ? { text, voice: features } : { text }),
      });
      if (response.status === 409) {
        const current = await api(sessionUrl());
        if (current.status !== "consulting") redirect = () => route(sessionId, current);
        else failure = "busy";
      } else if (!response.ok) {
        failure = "generic";
      } else {
        for await (const { event, data } of readSSE(response)) {
          if (event === "delta") {
            reply.classList.remove("typing");
            reply.querySelector(".text").append(document.createTextNode(data.text));
          } else if (event === "risk") {
            redirect = data.level === "crisis" ? () => showCrisis(data.crisis_resources) : reload;
          } else if (event === "handoff") {
            redirect = reload;
          } else if (event === "error") {
            failure = data.code;
          } else if (event === "done") {
            complete = data.consultation_complete;
            delivery = data.delivery || "warm";
          }
        }
      }
    } catch {
      failure = "generic";
    }
    busy = false;
    send.disabled = false;
    if (!alive) return;
    if (redirect) { redirect(); return; }
    if (failure) {
      mine.remove();
      reply.remove();
      input.value = text; // nothing is lost: the answer can be sent again
      showComposer();
      showError(failure);
      handsFree = false;
      updateTalk();
      avatar.setState("idle");
      setStatus("paused");
      return;
    }
    reply.classList.remove("typing");
    closing = Boolean(complete);
    await say(reply.querySelector(".text").textContent, delivery);
    if (!alive) return;
    if (complete) { // the goodbye has been heard: the call ends, and the page says they need not wait
      showWaiting("extracting", "extracting", 3000);
      return;
    }
    if (handsFree) listenLoop();
    else setStatus("paused");
  }

  talk.addEventListener("click", async () => {
    if (paused) { // Continue: the consultant welcomes them back and asks its question again, then listens
      goFullScreen();
      const asked = question.textContent;
      resumeAfterPause();
      handsFree = canAnswerAloud;
      updateTalk();
      if (!busy) {
        await sayAside(questionAgain(asked));
        if (alive && !paused) waitForAnswer();
        if (alive && handsFree && !busy && !paused) listenLoop();
      }
      return;
    }
    handsFree = !handsFree;
    updateTalk();
    note.classList.add("hidden");
    if (handsFree) {
      micFailures = 0;
      errorBox.classList.add("hidden");
      voice.stopSpeaking(); // pressing Talk while the consultant speaks means "I want to answer now"
      if (!busy) listenLoop();
    } else {
      listener?.stop();
      if (!busy) { avatar.setState("idle"); setStatus("paused"); }
    }
  });
  typeToggle.addEventListener("click", () => showComposer(true));
  pauseButton.addEventListener("click", () => {
    if (paused || busy) return; // (while an answer is on its way, the pause waits for the reply)
    pauseForSilence(true);
  });
  endButton.addEventListener("click", async () => {
    if (busy) return;
    const sure = await askToConfirm(t("stage.end_confirm", { amount: config.prices.consultation }),
                                    t("stage.end_yes"), t("stage.end_no"));
    if (!sure || !alive) return;
    endButton.disabled = true;
    voice.stopSpeaking();
    listener?.stop();
    try {
      const ended = await api(sessionUrl("/end-consultation"), { method: "POST" });
      route(sessionId, ended);
    } catch {
      endButton.disabled = false;
      route(sessionId, await api(sessionUrl()).catch(() => view)); // (it may have ended already)
    }
  });
  composer.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    submit(text);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); composer.requestSubmit(); }
  });

  updateTalk();
  // The server owns the consultation clock. This also catches a consultation that expired while the browser was
  // closed, so returning to the tab cannot leave the client in an old active consultation. While a reply is on its
  // way or the goodbye is being spoken, the page stays (the server has moved on already, the client not yet).
  pollUntilChanged("consulting", 5000, () => busy || closing);
  if (!canAnswerAloud) {
    handsFree = false;
    talk.classList.add("hidden");
    showNote(t("stage.mic_unavailable"));
    showComposer();
  }

  // The consultation begins when the client presses Start (browsers also only allow sound and the microphone
  // after a click). After a page reload during the consultation, Start carries on where it was.
  const start = async (returning = false) => {
    question.classList.remove("before-start");
    if (paused) { // paused for a silence: Continue (the Talk button) carries on
      handsFree = false;
      updateTalk();
      setStatus("");
      showNote(t("stage.paused_silence"));
      return;
    }
    const last = view.messages[view.messages.length - 1];
    if (last?.role === "assistant" && returning) { // back after a pause: welcome back, and the question again
      await sayAside(questionAgain(last.content));
      if (alive && !paused) waitForAnswer();
    } else if (last?.role === "assistant") {
      question.replaceChildren(h("span", { class: "unspoken" }, last.content)); // its words appear as they are said
      await say(last.content);
    }
    if (alive && handsFree) listenLoop();
    else if (alive) setStatus(canAnswerAloud ? "paused" : "");
  };
  if (!view.consultation_started || (navigator.userActivation && !navigator.userActivation.hasBeenActive)) {
    const begin = h("button", { type: "button", class: "mic active" },
      t(view.consultation_started ? "stage.continue" : "stage.start"));
    controls.prepend(begin);
    talk.classList.add("hidden");
    question.classList.add("before-start"); // the consultant's words appear only as it says them
    begin.addEventListener("click", async () => {
      goFullScreen(); // first, while the press still counts as the client's own
      begin.disabled = true;
      if (!view.consultation_started) {
        try { await api(sessionUrl("/start-consultation"), { method: "POST" }); } catch { /* noted only */ }
        view.consultation_started = true;
      }
      const returning = paused;
      if (paused) {
        resumeAfterPause();
        handsFree = canAnswerAloud;
        updateTalk();
      }
      begin.remove();
      window.scrollTo(0, 0); // the avatar, the question and the buttons: the whole consultation in view
      if (canAnswerAloud) talk.classList.remove("hidden");
      start(returning);
    }, { once: true });
  } else {
    start();
  }
}

function showWaiting(kind, status, intervalMs) {
  render(h("h1", {}, t(`${kind}.title`)), h("p", { class: "muted" }, t(`${kind}.body`)), h("div", { class: "spinner" }));
  pollUntilChanged(status, intervalMs);
}

function showGenerationFailed() {
  const retry = h("button", { type: "button", onclick: async () => {
    retry.disabled = true;
    try {
      await api(sessionUrl("/retry-generation"), { method: "POST" });
      showWaiting("generating", "generating", 5000);
    } catch { retry.disabled = false; }
  } }, t("generation_failed.retry"));
  render(h("h1", {}, t("generation_failed.title")), h("p", {}, t("generation_failed.body")), retry);
}

function showExtractionFailed() {
  const retry = h("button", { type: "button", onclick: async () => {
    retry.disabled = true;
    try {
      await api(sessionUrl("/retry-extraction"), { method: "POST" });
      showWaiting("extracting", "extracting", 3000);
    } catch { retry.disabled = false; }
  } }, t("extraction_failed.retry"));
  render(h("h1", {}, t("extraction_failed.title")), h("p", {}, t("extraction_failed.body")), retry);
}

function rangeField(id, labelText, initial) {
  const value = h("span", { class: "range-value" }, String(initial));
  const input = h("input", { type: "range", id, min: 0, max: 10, step: 1, value: initial });
  input.addEventListener("input", () => { value.textContent = input.value; });
  return { input, node: h("div", {}, h("label", { for: id }, labelText, " ", value), input) };
}

// After the consultation: the free five-minute preview (only the opening of the session is written until the
// client decides), then the choice: the full AI session, or a qualified hypnotherapist.
// The consultation ended with too few answers to write a session from: nothing was written (and nothing spent on
// it). The client can continue it once, for a few more minutes, or ask for the practice's qualified hypnotherapist.
function showIncomplete(view) {
  const humanSlot = h("div", {});
  const errorBox = h("div", { class: "error hidden", role: "alert" }, t("consult.error.generic"));
  const buttons = [];
  if (view.can_continue) {
    const go = h("button", { type: "button", onclick: async () => {
      go.disabled = true;
      try {
        route(sessionId, await api(sessionUrl("/continue-consultation"), { method: "POST" }));
      } catch { go.disabled = false; errorBox.classList.remove("hidden"); }
    } }, t("incomplete.continue", { minutes: view.extra_minutes }));
    buttons.push(go);
  }
  const human = h("button", { type: "button", class: view.can_continue ? "secondary" : "", onclick: () => {
    human.disabled = true;
    humanSlot.replaceChildren(humanRequestForm(() => null));
    humanSlot.scrollIntoView({ behavior: "smooth" });
  } }, t("incomplete.human"));
  buttons.push(human);
  // A consultation the client stopped is over: the website forgets it ("Start your consultation" again), and a new
  // one can be started here.
  if (view.ended_early) {
    tellWebsite(null);
    buttons.push(h("button", { type: "button", class: "secondary", onclick: startOver }, t("incomplete.start_new")));
  }
  render(
    h("h1", {}, t("incomplete.title")),
    h("p", {}, view.ended_early ? t("incomplete.ended_body")
      : t(view.can_continue ? "incomplete.body" : "incomplete.body_final", { minutes: view.extra_minutes })),
    h("div", { class: "stage-controls" }, ...buttons),
    errorBox,
    humanSlot,
  );
}

function showChoice(view) {
  const severity = rangeField("severity", t("choice.severity"), 5);
  const prices = config.prices;
  const humanSlot = h("div", {});
  const errorBox = h("div", { class: "error hidden", role: "alert" }, t("consult.error.generic"));
  const preview = previewCard();

  const aiCard = h("div", { class: "card highlight" },
    h("h2", {}, t("choice.ai.title")),
    h("p", {}, t("choice.ai.body")),
    h("div", { class: "price" }, t("choice.price", { amount: prices.ai_session })));
  if (view.ai_available) {
    const choose = h("button", { type: "button", onclick: async () => {
      choose.disabled = true;
      try {
        await api(sessionUrl("/ai-session"), {
          method: "POST", body: JSON.stringify({ baseline_severity: Number(severity.input.value) }),
        });
        route(sessionId, await api(sessionUrl()));
      } catch { choose.disabled = false; errorBox.classList.remove("hidden"); }
    } }, t("choice.ai.button", { amount: prices.ai_session }));
    aiCard.append(choose);
  }

  const humanButton = h("button", { type: "button", class: "secondary", onclick: () => {
    humanButton.disabled = true;
    humanSlot.replaceChildren(humanRequestForm(() => Number(severity.input.value)));
    humanSlot.scrollIntoView({ behavior: "smooth" });
  } }, t("choice.human.button"));
  const humanCard = h("div", { class: "card" },
    h("h2", {}, t("choice.human.title")),
    h("p", {}, t("choice.human.body")),
    h("div", { class: "price" }, t("choice.price_range", { from: prices.human_session_from,
                                                         to: prices.human_session_to })),
    humanButton);

  const summary = view.summary || {};
  if (view.ai_not_suitable && view.referral === "doctor") { // a safety reason: their doctor first, not a session
    render(h("h1", {}, t("referral.doctor.title")), h("p", {}, t("referral.doctor.body")), ...noRefund(view),
      startOverButton());
    return;
  }
  if (view.ai_not_suitable) { // the practice's hypnotherapist works with this himself: no AI session is offered
    render(
      h("h1", {}, t("choice.not_suitable_title")),
      h("div", { class: "notice" }, t("choice.not_suitable")),
      ...noRefund(view),
      severity.node,
      humanCard,
      humanSlot,
    );
    return;
  }
  render(
    h("h1", {}, t("choice.title")),
    h("div", { class: "card" },
      h("h2", {}, t("choice.summary_title")),
      h("p", {}, h("strong", {}, t("choice.issue"), ": "), summary.presenting_issue || ""),
      h("p", {}, h("strong", {}, t("choice.goal"), ": "), summary.goal || "")),
    preview.node,
    severity.node,
    h("div", { class: "choices" }, aiCard, humanCard),
    errorBox,
    humanSlot,
  );
  preview.update(view);
  watchSession(view, ["awaiting_choice"], (latest) => { preview.update(latest); return previewSettled(latest); });
}

// Checks the session every few seconds while `keep` returns false for the latest view: the page is updated in
// place (a preview that is playing is never interrupted); if the session moves to another step, that step's page
// is shown instead.
function watchSession(view, statuses, apply) {
  let timer = null;
  const check = async () => {
    let latest = null;
    try { latest = await api(sessionUrl()); } catch { /* try again next time */ }
    if (latest && !statuses.includes(latest.status)) { route(sessionId, latest); return; }
    if (latest && apply(latest)) return;
    timer = setTimeout(check, 5000);
  };
  if (!apply(view)) timer = setTimeout(check, 5000);
  teardown = () => clearTimeout(timer);
}

const previewSettled = (view) => !view.audio || view.audio.preview_ready || view.audio.expired
  || view.preview_state === "failed" || view.audio.failed;

// The free preview: the first five minutes of the client's own session.
function previewCard() {
  const card = h("div", { class: "card highlight" });
  let shown = null; // what the card shows, so a playing preview is never replaced
  const update = (view) => {
    const audio = view.audio;
    const state = !audio ? "unavailable" : audio.expired ? "expired" : audio.preview_ready ? "ready"
      : view.preview_state === "failed" || audio.failed ? "failed" : "preparing";
    if (state === shown) return;
    shown = state;
    card.replaceChildren(h("h2", {}, t("preview.title")));
    if (state === "ready") {
      card.append(h("p", { class: "muted" }, t("preview.body")),
        h("audio", { controls: true, preload: "metadata", controlslist: "nodownload noplaybackrate noremoteplayback",
          src: sessionUrl("/preview") }));
    } else if (state === "failed") {
      const retry = h("button", { type: "button", class: "secondary", onclick: async () => {
        retry.disabled = true;
        try {
          await api(sessionUrl("/retry-preview"), { method: "POST" });
          route(sessionId, await api(sessionUrl()));
        } catch { retry.disabled = false; }
      } }, t("preview.retry"));
      card.append(h("p", {}, t("preview.failed")), retry);
    } else if (state === "preparing") {
      card.append(h("p", {}, t("preview.preparing")), h("div", { class: "spinner" }));
    } else {
      card.append(h("p", {}, t(state === "expired" ? "audio.expired" : "preview.unavailable",
        { days: config.audio_retention_days })));
    }
  };
  return { node: card, update };
}

// The full session to download: written once it is paid for, then spoken and mixed, then the download button.
function downloadCard() {
  const card = h("div", { class: "card" });
  let shown = null;
  const update = (view) => {
    const audio = view.audio || {};
    const state = audio.expired ? "expired" : audio.failed ? "failed" : view.status === "generating" ? "writing"
      : audio.download_ready ? "ready" : "making";
    if (state === "making" && shown === "making") {
      card.querySelector(".progress-fill").style.width = `${audio.progress}%`;
      card.querySelector("p").textContent = t("download.preparing", { progress: audio.progress });
      return;
    }
    if (state === shown) return;
    shown = state;
    card.replaceChildren(h("h2", {}, t("download.title")));
    if (state === "ready") {
      card.append(h("p", { class: "muted" }, t("download.body")),
        h("a", { class: "button", href: sessionUrl("/download"), download: "Hypnotherapy session.mp3" },
          t("download.button", { minutes: audio.minutes })),
        h("p", { class: "muted small" }, t("download.keep", { days: config.audio_retention_days })));
    } else if (state === "writing") {
      card.append(h("p", {}, t("download.writing")), h("div", { class: "spinner" }));
    } else if (state === "making") {
      card.append(h("p", {}, t("download.preparing", { progress: audio.progress })),
        h("div", { class: "progress" }, h("div", { class: "progress-fill" })));
      card.querySelector(".progress-fill").style.width = `${audio.progress}%`;
    } else {
      card.append(h("p", {}, t(state === "expired" ? "audio.expired" : "audio.failed",
        { days: config.audio_retention_days })));
    }
  };
  return { node: card, update };
}

// The paid session: its preview, then the download once it is written and made, then feedback.
function showSessionPage(view) {
  const preview = previewCard();
  const download = downloadCard();
  const feedbackSlot = h("div", {});
  render(
    h("h1", {}, t("ready.title")),
    h("div", { class: "notice" }, t("ready.prepare")),
    h("div", { class: "notice" }, t("ready.safety")),
    h("p", { class: "muted small" }, t("ready.practice_voice_note")),
    download.node,
    preview.node,
    feedbackSlot,
    h("button", { class: "link", type: "button", onclick: startOver }, t("app.new_consultation")),
  );
  watchSession(view, ["generating", "ready"], (latest) => {
    preview.update(latest);
    download.update(latest);
    if (latest.status === "ready" && !feedbackSlot.firstChild) {
      feedbackSlot.append(latest.feedback_given ? h("p", { class: "card" }, t("feedback.thanks")) : feedbackForm());
    }
    const audio = latest.audio || {};
    return latest.status === "ready" && (audio.download_ready || audio.failed || audio.expired);
  });
}

// Payment: the consultation fee before the consultation, and the full session after the preview. With Stripe the
// client pays on Stripe's secure page and comes back here (see boot); the test checkout takes no money.
let paymentNotice = null; // shown once on the payment page, e.g. after an unfinished payment on Stripe's page

// Shown inside the practice's website page (an iframe): Stripe's payment page can't be shown inside another page,
// so the website page opens it in the whole browser tab (it checks the address is Stripe's).
const embedded = window.top !== window;
document.documentElement.classList.toggle("embedded", embedded); // shown inside the practice's website page

// Inside the practice's website page, the page keeps this session's private link, so that "Continue your
// consultation" (after the client closed the app, or pressed their phone's back button) reopens this session
// instead of starting a new one. Only the websites allowed to show the app (EMBED_ORIGINS) are told: the link is
// the client's key to their session.
function tellWebsite(id) {
  if (!embedded) return;
  for (const origin of config?.embed_origins || []) {
    try {
      parent.postMessage({ type: "ai-hypnotherapist:session", id }, origin);
    } catch { /* not a valid origin in EMBED_ORIGINS: nothing is sent there */ }
  }
}

function openPaymentPage(url) {
  if (embedded) parent.postMessage({ type: "ai-hypnotherapist:payment", url }, "*");
  else location.assign(url);
}

function showPayment(view) {
  const payment = view.payment;
  const purpose = payment.purpose;
  const errorBox = h("div", { class: "error hidden", role: "alert" });
  const saveCard = h("input", { type: "checkbox", id: "save-card" });
  const email = h("input", { type: "email", id: "pay-email", maxlength: 200, autocomplete: "email" });
  const emailsOk = h("input", { type: "checkbox", id: "emails-ok", checked: true });
  const agree = h("input", { type: "checkbox", id: "pay-agree" });
  const pay = h("button", { type: "button" }, t("pay.button", { amount: payment.amount }));
  const fail = (key) => { errorBox.textContent = t(key); errorBox.classList.remove("hidden"); };
  if (paymentNotice) { fail(paymentNotice); paymentNotice = null; }
  pay.addEventListener("click", async () => {
    errorBox.classList.add("hidden");
    if (purpose === "consultation" && !EMAIL_RE.test(email.value.trim())) { fail("pay.email_invalid"); return; }
    if (!agree.checked) { fail("pay.agree_needed"); return; }
    pay.disabled = true;
    const body = { purpose, save_card: saveCard.checked, acknowledged: true, amount: payment.amount };
    if (purpose === "consultation") Object.assign(body, { email: email.value.trim(), emails_ok: emailsOk.checked });
    try {
      const next = await api(sessionUrl("/pay"), { method: "POST", body: JSON.stringify(body) });
      if (next.checkout_url) { // Stripe's secure payment page, which brings the client back here
        pay.textContent = t("pay.redirecting");
        openPaymentPage(next.checkout_url);
        return;
      }
      route(sessionId, next); // the same view as the session's own page (with its audio)
    } catch (error) {
      pay.disabled = false;
      fail(/price has changed/.test(error.message) ? "pay.price_changed"
        : error.status === 503 ? "pay.unavailable" : "consult.error.generic");
    }
  });
  const card = h("div", { class: "card highlight" },
    h("h2", {}, t(`pay.${purpose}.item`)),
    h("div", { class: "price" }, t("choice.price", { amount: payment.amount })),
    h("p", {}, t(`pay.${purpose}.body`, { session: config.prices.ai_session, consultation: config.prices.consultation })));
  if (purpose === "consultation") {
    card.append(
      h("label", { for: "pay-email" }, t("pay.email")), email,
      h("p", { class: "muted small" }, t("pay.email_note")),
      h("label", { class: "check" }, emailsOk, t("pay.emails_ok")),
      h("label", { class: "check" }, saveCard, t("pay.save_card")));
  } else {
    if (view.email) card.append(h("p", { class: "muted" }, t("pay.receipt_to", { email: view.email })));
    if (payment.saved_card) card.append(h("p", { class: "muted" }, t("pay.saved_card")));
  }
  const stripeTestNotice = payment.provider !== "test" && payment.test_mode
    ? h("div", { class: "notice" }, t("pay.stripe_test_notice")) : null;
  card.append(h("label", { class: "check" }, agree, payment.acknowledgement),
    payment.provider === "test" ? h("div", { class: "notice" }, t("pay.test_notice"))
      : h("p", { class: "muted small" }, t("pay.secure_notice")),
    ...(stripeTestNotice ? [stripeTestNotice] : []),
    errorBox, pay);
  const nodes = [h("h1", {}, t(`pay.${purpose}.title`)), card];
  if (purpose === "session") {
    const back = h("button", { type: "button", class: "link", onclick: async () => {
      back.disabled = true;
      try {
        await api(sessionUrl("/back-to-choice"), { method: "POST" });
        route(sessionId, await api(sessionUrl()));
      } catch { back.disabled = false; }
    } }, t("pay.back"));
    nodes.push(back);
  } else {
    nodes.push(startOverButton());
  }
  render(...nodes);
}

// The "How are you doing?" page, reached from the follow-up email; the same 0-10 question as before the session.
function showFollowUp(view) {
  if (view.follow_up_given) {
    render(h("h1", {}, t("followup.title")), h("p", { class: "card" }, t("followup.thanks")),
      h("button", { type: "button", class: "link", onclick: () => route(sessionId, view) }, t("followup.back")));
    return;
  }
  const severity = rangeField("fu-severity", t("followup.severity"), 5);
  const comments = h("textarea", { id: "fu-comments", maxlength: 2000, rows: 4 });
  const submit = h("button", { type: "submit" }, t("followup.submit"));
  const errorBox = h("div", { class: "error hidden", role: "alert" }, t("consult.error.generic"));
  const form = h("form", { class: "card", onsubmit: async (event) => {
    event.preventDefault();
    submit.disabled = true;
    try {
      await api(sessionUrl("/follow-up"), { method: "POST",
        body: JSON.stringify({ severity: Number(severity.input.value), comments: comments.value.trim() }) });
      form.replaceWith(h("p", { class: "card" }, t("followup.thanks")));
    } catch { submit.disabled = false; errorBox.classList.remove("hidden"); }
  } },
    h("p", {}, t("followup.intro")), severity.node,
    h("label", { for: "fu-comments" }, t("followup.comments")), comments, errorBox, h("p", {}), submit);
  render(h("h1", {}, t("followup.title")), form);
}

function showStopEmails(view) {
  const done = h("p", { class: "card hidden" }, t("stop_emails.done"));
  const stop = h("button", { type: "button", onclick: async () => {
    stop.disabled = true;
    try {
      await api(sessionUrl("/stop-emails"), { method: "POST" });
      stop.classList.add("hidden");
      done.classList.remove("hidden");
    } catch { stop.disabled = false; }
  } }, t("stop_emails.button"));
  render(h("h1", {}, t("stop_emails.title")), h("p", {}, t("stop_emails.body")), stop, done,
    h("button", { type: "button", class: "link", onclick: () => route(sessionId, view) }, t("followup.back")));
}

function showExpired() {
  render(h("h1", {}, t("expired.title")), h("p", {}, t("expired.body", { days: config.audio_retention_days })),
    startOverButton());
}

// shared: the client had a consultation, whose questions and answers go to the hypnotherapist with the request.
function humanRequestForm(getSeverity, shared = true) {
  const name = h("input", { type: "text", id: "hr-name", maxlength: 120, autocomplete: "name" });
  const email = h("input", { type: "email", id: "hr-email", maxlength: 200, autocomplete: "email" });
  const phone = h("input", { type: "tel", id: "hr-phone", maxlength: 40, autocomplete: "tel" });
  const description = h("textarea", { id: "hr-description", maxlength: 2000, rows: 4 });
  const errorBox = h("div", { class: "error hidden", role: "alert" }, t("human.invalid"));
  const submit = h("button", { type: "submit" }, t("human.submit"));
  return h("form", { class: "card", onsubmit: async (event) => {
    event.preventDefault();
    errorBox.textContent = t("human.invalid");
    if (!name.value.trim() || !EMAIL_RE.test(email.value.trim()) || !PHONE_RE.test(phone.value.trim())
        || !description.value.trim()) {
      errorBox.classList.remove("hidden");
      return;
    }
    submit.disabled = true;
    const body = {
      name: name.value.trim(), email: email.value.trim(), phone: phone.value.trim(),
      description: description.value.trim(),
    };
    const severity = getSeverity();
    if (severity !== null) body.baseline_severity = severity;
    try {
      await api(sessionUrl("/human-request"), { method: "POST", body: JSON.stringify(body) });
      showHumanDone();
    } catch {
      submit.disabled = false;
      errorBox.textContent = t("consult.error.generic");
      errorBox.classList.remove("hidden");
    }
  } },
    h("h2", {}, t("human.title")),
    t("human.language_note") ? h("div", { class: "notice" }, t("human.language_note")) : null,
    h("label", { for: "hr-name" }, t("human.name")), name,
    h("label", { for: "hr-email" }, t("human.email")), email,
    h("label", { for: "hr-phone" }, t("human.phone")), phone,
    h("label", { for: "hr-description" }, t("human.description")), description,
    shared ? h("p", { class: "muted small" }, t("human.shares_consultation")) : null,
    errorBox, h("p", {}), submit);
}

function showHumanDone() {
  render(h("h1", {}, t("human.done.title")), h("p", {}, t("human.done.body")), startOverButton());
}

function showRejected() {
  render(h("h1", {}, t("rejected.title")), h("p", {}, t("rejected.body")), humanRequestForm(() => null));
}

// The clinician's music, quietly under the session: loaded only once the session is played, faded in and out,
// looped if the session is longer than the track.
function backgroundMusic(volume) {
  const audio = new Audio(sessionUrl("/background"));
  audio.preload = "none";
  audio.loop = true;
  audio.volume = 0;
  let fade = null;
  const rampTo = (target, ms, then) => {
    clearInterval(fade);
    const from = audio.volume;
    const started = performance.now();
    fade = setInterval(() => {
      const done = Math.min(1, (performance.now() - started) / ms);
      audio.volume = from + (target - from) * done;
      if (done >= 1) { clearInterval(fade); then?.(); }
    }, 50);
  };
  return {
    play() { audio.play().catch(() => {}); rampTo(Math.min(1, volume), 4000); }, // an element's volume stops at 1
    pause() { rampTo(0, 600, () => audio.pause()); },
    stop() { rampTo(0, 600, () => { audio.pause(); audio.currentTime = 0; }); },
    finish() { rampTo(0, 8000, () => audio.pause()); },
    close() { clearInterval(fade); audio.pause(); audio.removeAttribute("src"); audio.load(); },
  };
}

function sessionPlayer(plan, background, reverb = 0) {
  if (!voice.supported()) return { node: null, close() {} };
  const status = h("p", { class: "muted small" });
  const play = h("button", { type: "button" }, t("player.play"));
  const stop = h("button", { type: "button", class: "secondary" }, t("player.stop"));
  const music = background ? backgroundMusic(background.volume) : null;
  let player = null;
  const reset = () => { player = null; play.textContent = t("player.play"); };
  play.addEventListener("click", () => {
    player ??= voice.createSessionPlayer(plan, {
      onProgress: (done) => { status.textContent = t("player.progress", { percent: Math.round(done * 100) }); },
      onDone: () => { status.textContent = t("player.done"); music?.finish(); reset(); },
    });
    if (player.playing) {
      player.pause();
      music?.pause();
      play.textContent = t("player.resume");
    } else {
      voice.setReverb(reverb); // the session voice in a warm hall; the consultation stays dry
      player.play();
      music?.play();
      play.textContent = t("player.pause");
    }
  });
  stop.addEventListener("click", () => { player?.stop(); music?.stop(); reset(); status.textContent = ""; });
  return {
    node: h("div", { class: "card highlight" }, h("h2", {}, t("player.title")), h("div", { class: "actions" }, play, stop),
      status),
    close() { player?.stop(); music?.close(); voice.setReverb(0); },
  };
}

// Without the practice voice there is no audio file: the device voice reads the session live (test fallback).
function showReady(view) {
  const plan = view.session_plan?.map((item) => ({ text: item.text, pause: item.pause_ms }));
  const player = sessionPlayer(plan || view.session_script || "", view.session_background, view.session_reverb || 0);
  const nodes = [
    h("h1", {}, t("ready.title")),
    h("div", { class: "notice" }, t("ready.prepare")),
    h("div", { class: "notice" }, t("ready.safety")),
    h("p", { class: "muted small" }, t("ready.test_note")),
    player.node,
  ];
  nodes.push(view.feedback_given ? h("p", { class: "card" }, t("feedback.thanks")) : feedbackForm());
  render(...nodes);
  teardown = () => player.close(); // leaving this screen stops the session (and its checks)
}

function feedbackForm() {
  const comments = h("textarea", { id: "fb-comments", maxlength: 2000, rows: 4 });
  const submit = h("button", { type: "submit" }, t("feedback.submit"));
  const errorBox = h("div", { class: "error hidden", role: "alert" }, t("consult.error.generic"));
  const form = h("form", { class: "card", onsubmit: async (event) => {
    event.preventDefault();
    if (!comments.value.trim()) { comments.focus(); return; }
    submit.disabled = true;
    try {
      await api(sessionUrl("/feedback"), { method: "POST", body: JSON.stringify({ comments: comments.value.trim() }) });
      form.replaceWith(h("p", { class: "card" }, t("feedback.thanks")));
    } catch { submit.disabled = false; errorBox.classList.remove("hidden"); }
  } },
    h("h2", {}, t("feedback.title")),
    h("label", { for: "fb-comments" }, t("feedback.comments")), comments,
    errorBox, h("p", {}), submit);
  return form;
}

function showFatal() {
  render(h("h1", {}, t("consult.error.generic")), startOverButton());
}

// --- language ---

function rememberLanguage(code) {
  try { localStorage.setItem(LANGUAGE_KEY, code); } catch { /* not kept: chosen again next time */ }
}

// A new visitor: the language they chose here before, else their browser's language if it is offered, else English.
function preferredLanguage(codes) {
  try {
    const saved = localStorage.getItem(LANGUAGE_KEY);
    if (codes.includes(saved)) return saved;
  } catch { /* storage unavailable */ }
  for (const tag of navigator.languages || [navigator.language || ""]) {
    const code = String(tag).slice(0, 2).toLowerCase();
    if (codes.includes(code)) return code;
  }
  return "en";
}

// The language's words, with English for any not yet translated.
async function loadStrings(code) {
  const english = await (await fetch("/static/i18n/en.json")).json();
  if (code === "en") return english;
  try {
    return { ...english, ...(await (await fetch(`/static/i18n/${encodeURIComponent(code)}.json`)).json()) };
  } catch {
    return english;
  }
}

async function useLanguage(code) {
  const [words, settings] = await Promise.all([loadStrings(code),
    api(`/api/config?language=${encodeURIComponent(code)}`)]);
  strings = words;
  config = settings;
  language = code;
  document.documentElement.lang = code;
  voice.setLanguage(code);
  voice.applyPracticeVoice(config.voice);
  voice.setAcknowledgements(config.acknowledgements);
  // the practice voice when it speaks this language, otherwise the device's own voice in this language
  voice.usePracticeVoice(config.practice_voice ? () => (sessionId ? sessionUrl("/speech") : null) : null);
  document.getElementById("brand").textContent = t("app.name");
  document.getElementById("footer").replaceChildren(`${t("app.footer")} ${t("app.credits")} `,
    h("a", { class: "admin-link", href: "/sign-in", target: embedded ? "_blank" : null }, t("app.sign_in")));
  // (the sign-in is for the practice only; it never opens inside the website page)
  const notice = document.getElementById("engine-notice");
  notice.textContent = t("engine.test_notice");
  notice.classList.toggle("hidden", config.engine !== "claude-code");
}

async function boot() {
  // Resume only from an explicit private link. Do not silently reopen the last consultation merely because this
  // browser still has its old id in localStorage; returning to the app root should begin a fresh consultation.
  const id = sessionIdFromUrl();
  try {
    const offered = (await api("/api/config")).languages.map((option) => option.code);
    let code = preferredLanguage(offered);
    if (id) { // a consultation already begun keeps the language it was begun in
      try {
        const found = (await api(`/api/sessions/${encodeURIComponent(id)}`)).language;
        if (found) code = found;
      } catch { /* handled below */ }
    }
    await useLanguage(code);
  } catch {
    app.replaceChildren(h("p", { class: "error" }, "The service is unavailable. Please try again later."));
    return;
  }
  if (id) {
    try {
      sessionId = id;
      const checkout = new URLSearchParams(location.search).get("checkout");
      if (checkout) { // back from Stripe's payment page: the server checks the payment with Stripe
        history.replaceState(null, "", `${location.pathname}#s=${id}`);
        render(h("p", { class: "muted" }, t("pay.confirming")));
        try {
          return route(id, await api(sessionUrl("/confirm-payment"),
            { method: "POST", body: JSON.stringify({ checkout_id: checkout }) }));
        } catch (error) {
          paymentNotice = error.status === 409 ? "pay.not_completed" : "consult.error.generic";
        }
      }
      const view = await api(sessionUrl());
      const action = actionFromUrl();
      if (action === "followup" && view.status === "ready") return showFollowUp(view);
      if (action === "stop-emails") return showStopEmails(view);
      return route(id, view);
    } catch (error) {
      if (error.status !== 404) return showFatal();
      forgetSessionId();
      history.replaceState(null, "", location.pathname);
    }
  }
  showWelcome();
}

boot();
