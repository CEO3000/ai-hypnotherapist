// Admin dashboard (the practice only, after signing in). All dynamic text is inserted as text nodes, never as
// HTML.

import * as voice from "./voice.js";

const view = document.getElementById("view");
const tabs = document.getElementById("tabs");
let selectedSession = null;

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

async function api(path, options = {}) {
  // The header tells the server the request comes from this dashboard (other sites cannot add it).
  const response = await fetch(path, {
    ...options, headers: { "Content-Type": "application/json", "X-Requested-With": "admin" },
  });
  if (response.status === 401) { location.assign("/sign-in"); throw new Error("Please sign in again."); }
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "");
const json = (value) => h("pre", { class: "json" }, JSON.stringify(value, null, 2));

const STATUS_CLASS = {
  consultation_stopped: "alert",
  consultation_incomplete: "action",
  screening_stopped: "",
  pending_review: "action",
  generation_failed: "action",
  extraction_failed: "action",
  human_requested: "action",
  ready: "ok",
};

const money = (pence, currency) => `${currency === "GBP" ? "£" : `${currency} `}${(pence / 100).toFixed(2)}`;

function statusBadge(session) {
  let cls = STATUS_CLASS[session.status] ?? "";
  const outcome = session.screening_outcome?.outcome;
  if (outcome === "crisis" || session.stop_detail?.level === "crisis") cls = "alert";
  const label = session.stop_detail?.level ? `${session.status} (${session.stop_detail.level})`
    : session.status === "screening_stopped" ? `screening: ${outcome}` : session.status;
  return h("span", { class: `badge ${cls}` }, label);
}

// --- tabs ---

const TABS = { sessions: showSessions, lessons: showLessons, voice: showVoice, books: showBooks, admin: showAdmin };

function renderTabs(active) {
  tabs.replaceChildren(...Object.keys(TABS).map((name) =>
    h("button", { type: "button", class: name === active ? "active" : "", onclick: () => open(name) },
      name[0].toUpperCase() + name.slice(1))));
}

function open(name) {
  clearTimeout(booksTimer);
  renderTabs(name);
  TABS[name]().catch((error) => view.replaceChildren(h("div", { class: "error" }, error.message)));
}

// --- sessions ---

async function showSessions() {
  const sessions = await api("/api/clinician/sessions");
  const detail = h("section", {}, h("p", { class: "muted" }, "Select a session."));
  const rows = sessions.map((s) => {
    const row = h("tr", { class: `row ${s.id === selectedSession ? "selected" : ""}` },
      h("td", {}, fmtTime(s.created_at)),
      h("td", {}, statusBadge(s)),
      // the issue in at most three lines (the whole summary is in the session's details), its flags below it
      h("td", {}, h("div", { class: "issue" }, s.presenting_issue || ""),
        s.risk_events ? h("span", { class: "badge alert" }, `${s.risk_events} risk`) : "",
        s.safety_checks ? h("span", { class: "badge action" }, "safety question asked") : "",
        s.inconsistencies ? h("span", { class: "badge action" }, `${s.inconsistencies} contradiction(s)`) : ""));
    row.addEventListener("click", () => {
      selectedSession = s.id;
      for (const other of row.parentElement.children) other.classList.remove("selected");
      row.classList.add("selected");
      showDetail(s.id, detail);
    });
    return row;
  });
  view.replaceChildren(h("div", { class: "layout" },
    h("section", {},
      h("h2", {}, `Sessions (${sessions.length})`),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Created"), h("th", {}, "Status"),
        h("th", {}, "Issue"))), h("tbody", {}, rows))),
    detail));
  if (selectedSession && sessions.some((s) => s.id === selectedSession)) showDetail(selectedSession, detail);
}

async function showDetail(sessionId, container) {
  container.replaceChildren(h("div", { class: "spinner" }));
  let data;
  try { data = await api(`/api/clinician/sessions/${encodeURIComponent(sessionId)}`); }
  catch (error) { container.replaceChildren(h("div", { class: "error" }, error.message)); return; }
  const { session, messages, risk_events: risks, script, human_request: human, feedback, opening } = data;
  const payments = data.payments || [];
  const analyses = data.answer_analyses || {};
  const parts = [];

  parts.push(h("div", { class: "card" },
    h("h2", {}, "Session"),
    h("dl", { class: "meta" },
      h("dt", {}, "Status"), h("dd", {}, statusBadge(session)),
      h("dt", {}, "Created"), h("dd", {}, fmtTime(session.created_at)),
      h("dt", {}, "Country"), h("dd", {}, session.country),
      h("dt", {}, "Session id"), h("dd", {}, session.id),
      h("dt", {}, "Client email"), h("dd", {}, session.client_email || "-",
        session.client_email ? ` (reminders ${session.emails_ok ? "on" : "stopped"})` : ""),
      h("dt", {}, "Baseline severity"), h("dd", {}, session.baseline_severity ?? "-"),
      h("dt", {}, "Estimated AI cost"), h("dd", {},
        data.estimated_cost_usd === null ? "unknown model pricing" : `$${data.estimated_cost_usd.toFixed(4)}`))));

  parts.push(h("div", { class: "card" },
    h("h2", {}, "Screening"),
    h("p", {}, `Outcome: ${session.screening_outcome.outcome}`,
      session.screening_outcome.matched.length ? `, matched: ${session.screening_outcome.matched.join(", ")}` : ""),
    h("details", {}, h("summary", {}, "All answers"), json(session.screening_answers))));

  if (risks.length) {
    const stopped = risks.some((r) => r.level !== "check");
    parts.push(h("div", { class: `card ${stopped ? "danger" : "warn"}` },
      h("h2", {}, "Safety events"),
      risks.map((r) => h("div", {},
        h("p", {}, h("strong", {}, r.level === "check" ? "SAFETY QUESTION ASKED " : `${r.level.toUpperCase()} `),
          `(${r.source}) ${fmtTime(r.created_at)}`),
        h("p", {}, `Categories: ${r.categories.join(", ") || "-"}`),
        h("p", {}, `Evidence: ${r.evidence || "-"}`),
        h("p", {}, `Rationale: ${r.rationale}`),
        r.keyword_hits.length ? h("p", {}, `Keyword matches: ${r.keyword_hits.join(", ")}`) : null))));
  }

  if (messages.length) {
    parts.push(h("div", { class: "card" },
      h("h2", {}, "Transcript"),
      h("div", { class: "chat transcript" }, messages.flatMap((m) => [
        h("div", { class: `bubble ${m.role}` },
          h("span", { class: "who" }, `${m.role === "patient" ? "Client" : "Assistant"} · ${fmtTime(m.created_at)}`),
          m.content),
        m.voice?.summary ? h("p", { class: "small muted voice-cue" }, m.voice.summary) : null,
        analyses[String(m.id)] ? analysisBlock(analyses[String(m.id)]) : null,
      ]))));
  }

  if (session.intake) {
    const review = session.intake.consistency_review || [];
    const unresolved = review.filter((item) => !item.clarified).length;
    parts.push(h("div", { class: `card ${unresolved ? "warn" : ""}` },
      h("h2", {}, review.length
        ? `Consistency check: ${review.length} contradiction(s), ${unresolved} unresolved`
        : "Consistency check: no contradictions found"),
      session.intake.reliability_note ? h("p", {}, session.intake.reliability_note) : null,
      review.map((item) => h("div", { class: "notice" },
        h("p", {}, h("strong", {}, item.clarified ? "Clarified: " : "Unresolved: "), item.description),
        h("p", { class: "small" }, `Earlier: “${item.earlier_statement}”`),
        h("p", { class: "small" }, `Later: “${item.later_statement}”`),
        item.clarified && item.clarification ? h("p", { class: "small" }, `Client's explanation: ${item.clarification}`)
          : null))));
    parts.push(h("div", { class: "card" },
      h("h2", {}, "Intake record"),
      session.coverage_note ? h("p", {}, `Consultation coverage note: ${session.coverage_note}`) : null,
      h("p", {}, `Self-guided suitability: ${session.intake.suitability_for_self_guided_session.suitable ? "suitable" : "NOT suitable"}`,
        session.intake.suitability_for_self_guided_session.reasons.length
          ? `: ${session.intake.suitability_for_self_guided_session.reasons.join("; ")}` : ""),
      session.intake.hypnotherapist_issue && session.intake.hypnotherapist_issue !== "none"
        ? h("p", {}, `For you (regression work): ${session.intake.hypnotherapist_issue.replace("_", " ")}`) : null,
      h("details", { open: true }, h("summary", {}, "Full intake"), json(session.intake))));
  }

  const suitability = session.intake?.suitability_for_self_guided_session;
  if (script && suitability && !suitability.suitable) {
    parts.push(h("div", { class: "notice" }, h("strong", {}, "Check before approving: "),
      "the consultation raised concerns about a self-guided session for this client",
      suitability.reasons.length ? ` (${suitability.reasons.join("; ")})` : "",
      ". The client chose it themselves; it always comes to you for review first."));
  }
  if (payments.length) {
    const test = payments.some((payment) => payment.provider === "test");
    parts.push(h("div", { class: "card" },
      h("h2", {}, "Payments"),
      test ? h("p", { class: "muted small" }, "Test payments: no money was taken.") : null,
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "For"), h("th", {}, "Amount"), h("th", {}, "Status"),
        h("th", {}, "Card saved"), h("th", {}, "Paid at"))),
      h("tbody", {}, payments.map((payment) => h("tr", {},
        h("td", {}, payment.purpose === "consultation" ? "Consultation" : "Full AI session"),
        h("td", {}, money(payment.amount_pence, payment.currency)),
        h("td", {}, payment.status),
        h("td", {}, payment.save_card ? "yes" : "no"),
        h("td", {}, payment.paid_at ? fmtTime(payment.paid_at) : "-"))))),
      payments.filter((payment) => payment.acknowledgement).map((payment) => h("p", { class: "small muted" },
        `${payment.purpose === "consultation" ? "Consultation" : "Session"}: the client confirmed “${payment.acknowledgement}”`))));
    if (session.status === "consultation_stopped" && payments.some((payment) => payment.status === "paid")) {
      parts.push(h("div", { class: "notice" }, h("strong", {}, "Paid, then stopped for safety: "),
        "this client paid and the consultation was stopped by the safety check. The fee is not refunded under the "
        + "practice's rule (they confirmed their safety answers before paying); refund it in Stripe only if the app "
        + "was at fault."));
    }
    if (session.status === "consultation_incomplete" && payments.some((payment) => payment.status === "paid")) {
      parts.push(h("div", { class: "notice" }, h("strong", {}, "Paid, too few answers: "),
        "the consultation ended before the client had said enough to write a session, so none was written. They "
        + "were offered to continue it (once) or a session with you. Consider a refund if they do neither."));
    }
  }

  if (opening && !script) {
    parts.push(h("div", { class: "card" }, h("h2", {}, "Session opening (the free preview is made from it)"),
      h("p", { class: "muted small" }, `Written ${fmtTime(opening.created_at)} by ${opening.model}. The rest is `
        + "written only after the client pays for the full session."),
      opening.validation_issues.length
        ? h("p", { class: "small" }, `Checks: ${opening.validation_issues.join("; ")}`) : null,
      h("textarea", { class: "script-editor", readonly: true }, opening.text)));
  }
  if (script) parts.push(scriptCard(session, script, container));
  else if (session.status === "generation_failed") parts.push(regenerateCard(session.id, container));

  if (human) {
    parts.push(h("div", { class: "card highlight" },
      h("h2", {}, "Hypnotherapist request"),
      h("dl", { class: "meta" },
        h("dt", {}, "Full name"), h("dd", {}, human.name),
        h("dt", {}, "Email"), h("dd", {}, human.email),
        h("dt", {}, "Mobile"), h("dd", {}, human.phone || "-"),
        h("dt", {}, "Description"), h("dd", {}, human.description || human.preferred_times || "-"),
        h("dt", {}, "Requested from"), h("dd", {}, human.origin),
        h("dt", {}, "Requested at"), h("dd", {}, fmtTime(human.created_at)))));
  }

  const emails = data.emails || [];
  if (emails.length) {
    const labels = { receipt_consultation: "Receipt: consultation", receipt_session: "Receipt: full session",
      session_ready: "Your session is ready", follow_up: "Follow-up: how are you doing?" };
    parts.push(h("div", { class: "card" }, h("h2", {}, "Emails to the client"),
      h("ul", {}, emails.map((email) => h("li", {},
        `${labels[email.kind] || email.kind.replace(/^preview_reminder_(\d+)$/, "Preview reminder $1")} · `
          + `${email.status} · ${fmtTime(email.created_at)}`)))));
  }
  if (data.followup) {
    parts.push(h("div", { class: "card highlight" }, h("h2", {}, "Follow-up"),
      h("p", {}, `How much the issue affects them: ${session.baseline_severity ?? "?"}/10 before the session → `
        + `${data.followup.severity}/10 at the follow-up (${fmtTime(data.followup.created_at)})`),
      data.followup.comments ? h("p", {}, data.followup.comments) : null));
  }

  if (feedback) {
    parts.push(h("div", { class: "card" },
      h("h2", {}, "Client feedback"),
      feedback.rating != null
        ? h("p", {}, `Rating ${feedback.rating}/5 · severity ${session.baseline_severity ?? "?"} → ${feedback.severity_after ?? "?"}/10`)
        : null,
      feedback.comments ? h("p", {}, feedback.comments) : null));
  }

  if (data.usage.length) {
    parts.push(h("details", { class: "card" }, h("summary", {}, "AI usage"), json(data.usage)));
  }
  container.replaceChildren(...parts);
}

function analysisBlock(analysis) {
  const row = (label, items) => (items && items.length
    ? h("p", { class: "small" }, h("strong", {}, `${label}: `), items.join(" · ")) : null);
  return h("details", { class: "analysis" },
    h("summary", {}, "AI analysis of this answer"),
    row("Facts", analysis.facts),
    row("Feelings", analysis.feelings),
    row("Beliefs / conclusions", analysis.beliefs_or_conclusions),
    row("Links to earlier answers", analysis.links_to_earlier_answers),
    row("Inconsistencies", analysis.inconsistencies),
    row("Gaps", analysis.gaps),
    analysis.next_focus ? h("p", { class: "small" }, h("strong", {}, "Next focus: "), analysis.next_focus) : null,
    analysis.delivery ? h("p", { class: "small" }, h("strong", {}, "Tone chosen for the reply: "), analysis.delivery)
      : null);
}

function scriptCard(session, script, container) {
  const reviewable = session.status === "pending_review";
  const editor = h("textarea", { class: "script-editor", readonly: !reviewable },
    script.final_text ?? script.draft);
  const notes = h("textarea", { placeholder: "Review notes (these feed the learning loop)", maxlength: 5000 });
  const errorBox = h("div", { class: "error hidden", role: "alert" });
  const card = h("div", { class: "card" }, h("h2", {}, "Session script"),
    h("p", { class: "muted small" }, `Generated ${fmtTime(script.created_at)} by ${script.model} · prompt ${script.prompt_version}`));

  if (script.validation_issues.length) {
    card.append(h("div", { class: "notice" }, h("strong", {}, "Automatic checks flagged:"),
      h("ul", {}, script.validation_issues.map((issue) => h("li", {}, issue)))));
  } else {
    card.append(h("p", { class: "small" }, h("span", { class: "badge ok" }, "Automatic checks passed")));
  }
  if (script.review_decision) {
    card.append(h("p", {}, `Review: ${script.review_decision} at ${fmtTime(script.reviewed_at)}`,
      script.review_notes ? `: ${script.review_notes}` : ""));
  }
  card.append(editor);

  if (reviewable) {
    const act = async (path, body) => {
      errorBox.classList.add("hidden");
      try {
        await api(`/api/clinician/sessions/${encodeURIComponent(session.id)}/${path}`, {
          method: "POST", body: body ? JSON.stringify(body) : undefined,
        });
        showDetail(session.id, container);
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.classList.remove("hidden");
      }
    };
    card.append(notes, errorBox, h("div", { class: "actions" },
      h("button", { type: "button", onclick: () => act("review", {
        decision: "approve", final_text: editor.value, notes: notes.value,
      }) }, "Approve (with any edits)"),
      h("button", { type: "button", class: "danger", onclick: () => act("review", {
        decision: "reject", notes: notes.value,
      }) }, "Reject and refer to a hypnotherapist"),
      h("button", { type: "button", class: "secondary", onclick: () => act("regenerate") }, "Write a new draft")));
  }

  card.append(h("details", {}, h("summary", {}, `Knowledge used (${script.knowledge_sources.length})`),
    h("ul", {}, script.knowledge_sources.map((s) =>
      h("li", { class: "small" }, `${s.kind}: ${s.path}${s.section ? `, ${s.section}` : ""} (${s.sha})`)))));
  return card;
}

function regenerateCard(sessionId, container) {
  const errorBox = h("div", { class: "error hidden", role: "alert" });
  return h("div", { class: "card" }, h("h2", {}, "Script generation failed"),
    h("p", {}, "The AI service could not write the script. Check the server log, then try again."),
    errorBox,
    h("button", { type: "button", onclick: async () => {
      try {
        await api(`/api/clinician/sessions/${encodeURIComponent(sessionId)}/regenerate`, { method: "POST" });
        showDetail(sessionId, container);
      } catch (error) { errorBox.textContent = error.message; errorBox.classList.remove("hidden"); }
    } }, "Try again"));
}

// --- lessons ---

async function showLessons() {
  const lessons = await api("/api/clinician/lessons");
  const status = h("p", { class: "muted" });
  const propose = h("button", { type: "button", onclick: async () => {
    propose.disabled = true;
    status.textContent = "Analysing recent reviews and client feedback… (this can take a minute)";
    try {
      const result = await api("/api/clinician/lessons/propose", { method: "POST" });
      status.textContent = result.created.length
        ? `${result.created.length} new lesson(s) proposed.` : "No new lessons were supported by the evidence yet.";
      setTimeout(() => open("lessons"), 1200);
    } catch (error) {
      status.textContent = error.message;
      propose.disabled = false;
    }
  } }, "Propose lessons from recent reviews");

  const decide = async (id, decision) => {
    await api(`/api/clinician/lessons/${id}/decision`, { method: "POST", body: JSON.stringify({ decision }) });
    open("lessons");
  };

  view.replaceChildren(
    h("h2", {}, "Learning loop"),
    h("p", {}, "Lessons are learned from your script edits, rejections, review notes and client feedback. "
      + "A lesson only changes future scripts after you approve it."),
    propose, status,
    lessons.length ? lessons.map((l) => h("div", { class: `card ${l.status === "approved" ? "highlight" : ""}` },
      h("p", {}, h("span", { class: `badge ${l.status === "approved" ? "ok" : l.status === "proposed" ? "action" : ""}` },
        l.status), " ", fmtTime(l.created_at)),
      h("p", {}, h("strong", {}, l.lesson)),
      h("p", { class: "muted" }, l.rationale),
      h("p", { class: "small muted" }, `Evidence: script ${l.evidence.join(", ")}`),
      l.status === "proposed" ? h("div", { class: "actions" },
        h("button", { type: "button", onclick: () => decide(l.id, "approve") }, "Approve"),
        h("button", { type: "button", class: "secondary", onclick: () => decide(l.id, "reject") }, "Reject"))
        : null))
      : h("p", { class: "muted" }, "No lessons yet."));
}

// --- practice voice ---

const VOICE_SAMPLE = "Hello, and welcome. Take a slow, comfortable breath, and let yourself settle as we begin.";
const SESSION_SAMPLE = "And now... let your eyes close... (pause) Breathing slowly... and deeply... "
  + "letting every breath out... take you deeper... and deeper still.";

function slider(id, label, min, max, step, value) {
  const shown = h("span", { class: "range-value" }, Number(value).toFixed(2));
  const input = h("input", { type: "range", id, min, max, step, value });
  input.addEventListener("input", () => { shown.textContent = Number(input.value).toFixed(2); });
  return { input, node: h("div", {}, h("label", { for: id }, label, " ", shown), input) };
}

// The client languages the voice is ready in; clients in any other language hear their device's own voice, and
// their sessions have no MP3 (python -m app.speech prepare ... --languages de fr es adds them).
const LANGUAGE_NAMES = { en: "English", de: "German", fr: "French", es: "Spanish" };
function languagesNote(state) {
  const ready = (state.languages || ["en"]).map((code) => LANGUAGE_NAMES[code] || code);
  const missing = Object.keys(LANGUAGE_NAMES).filter((code) => !(state.languages || ["en"]).includes(code))
    .map((code) => LANGUAGE_NAMES[code]);
  return h("p", { class: "muted small" }, `Speaks: ${ready.join(", ")}.`,
    missing.length ? ` Not prepared yet: ${missing.join(", ")} (those clients hear their device's voice, and their `
      + "sessions have no MP3)." : "");
}

// The practice voice made from a consenting speaker's recording (generated on this computer with Pocket TTS),
// with a box to hear any words in it: as in the consultation (natural speed), or as in the session at the session
// speed currently set below (saved or not).
function recordedVoiceCard(state, speeds, reverb) {
  if (!state.available) {
    const reason = !state.installed ? "The voice software (Pocket TTS) is not installed."
      : !state.prepared ? "No recording has been turned into the practice voice yet (one setup step)."
        : `It could not be loaded: ${state.problem}`;
    return h("div", { class: "card" }, h("h2", {}, "Recorded practice voice"),
      h("p", {}, h("span", { class: "badge action" }, "Not in use"), " ", reason),
      h("p", { class: "muted small" }, "Until then, clients hear the device voice below."));
  }
  const text = h("textarea", { id: "v-preview", rows: 3, maxlength: 600 }, VOICE_SAMPLE);
  const note = h("p", { class: "muted small" });
  const finished = (heard) => {
    voice.usePracticeVoice(null);
    voice.setReverb(0);
    note.textContent = heard ? "" : "Stopped.";
  };
  const hearConsultation = async () => {
    await voice.setReverb(0); // the consultation voice is dry
    voice.usePracticeVoice(() => "/api/clinician/voice/speak");
    note.textContent = "Speaking…";
    finished(await voice.speakReply(text.value));
  };
  const hearSession = async () => {
    await voice.setReverb(reverb()); // the session voice with the reverb set below
    voice.usePracticeVoice(() => "/api/clinician/voice/speak", () => ({ rate: speeds().session_rate }));
    note.textContent = "Speaking…";
    voice.createSessionPlayer(text.value, { onDone: () => finished(true) }).play();
  };
  return h("div", { class: "card highlight" }, h("h2", {}, "Recorded practice voice"),
    h("p", {}, h("span", { class: "badge ok" }, "In use"), " Every client hears this voice, generated by AI from the "
      + "recording provided (used with the speaker's consent). Clients are told this before they start."),
    languagesNote(state),
    h("label", { for: "v-preview" }, "Type anything to hear it"), text,
    h("div", { class: "actions" },
      h("button", { type: "button", onclick: hearConsultation }, "Hear as in the consultation"),
      h("button", { type: "button", class: "secondary", onclick: hearSession }, "Hear at session speed")),
    note);
}

// The session's sound: the music under it (a file from data\voice, and how loud) and the reverb on the voice.
// The voice and the music together, as a client hears the session: the practice voice at the session speed with the
// reverb set below, over the chosen music at its volume.
const WITH_MUSIC_SAMPLE = "Hello, and welcome. Take a slow, comfortable breath... and let yourself settle. (pause) "
  + "Now, let your eyes close... and let your breathing slow down, all by itself. Each breath a little softer, "
  + "a little deeper... and you can let go.";

function backgroundCard(data, reverbSlider, speeds) {
  const select = h("select", { id: "bg-file" },
    h("option", { value: "", selected: !data.file }, "No music"),
    ...data.files.map((name) => h("option", { value: name, selected: name === data.file }, name)));
  const volume = slider("bg-volume", "Music volume (under the voice; 1 = as recorded)", 0, 2, 0.01, data.volume);
  const status = h("p", { class: "muted" });
  const listen = h("button", { type: "button", class: "secondary" }, "Listen");
  // Heard through a gain (an audio element's own volume stops at 1), so above 1 sounds as it will in the session.
  let audio = null;
  let listening = null;
  const stopListening = () => {
    audio?.pause();
    listening?.close().catch(() => {});
    audio = listening = null;
    listen.textContent = "Listen";
  };
  listen.addEventListener("click", () => {
    if (audio) { stopListening(); return; }
    if (!select.value) return;
    audio = new Audio(`/api/clinician/background/audio?file=${encodeURIComponent(select.value)}`);
    listening = new AudioContext();
    const gain = listening.createGain();
    gain.gain.value = Number(volume.input.value);
    listening.createMediaElementSource(audio).connect(gain).connect(listening.destination);
    listening.gain = gain;
    audio.play().catch(() => stopListening());
    listen.textContent = "Stop";
  });
  volume.input.addEventListener("input", () => {
    if (listening) listening.gain.gain.value = Number(volume.input.value);
  });
  select.addEventListener("change", stopListening);
  const together = h("button", { type: "button", class: "secondary" }, "Hear the voice with the music");
  let player = null;
  const stopTogether = () => {
    player?.stop();
    player = null;
    stopListening();
    voice.usePracticeVoice(null);
    voice.setReverb(0);
    together.textContent = "Hear the voice with the music";
  };
  together.addEventListener("click", async () => {
    if (player) { stopTogether(); return; }
    if (select.value && !audio) listen.click(); // the music, at the volume set above
    listen.textContent = "Listen";
    await voice.setReverb(Number(reverbSlider.input.value)); // the session voice's reverb (the music has none)
    voice.usePracticeVoice(() => "/api/clinician/voice/speak", () => ({ rate: speeds().session_rate }));
    together.textContent = "Stop";
    player = voice.createSessionPlayer(WITH_MUSIC_SAMPLE, { onDone: () => setTimeout(stopTogether, 2500) });
    player.play();
  });
  const save = async () => {
    try {
      await api("/api/clinician/background", { method: "PUT",
        body: JSON.stringify({ file: select.value || null, volume: Number(volume.input.value) }) });
      await api("/api/clinician/reverb", { method: "PUT",
        body: JSON.stringify({ amount: Number(reverbSlider.input.value) }) });
      status.textContent = "Saved. Every session now plays with this music and reverb.";
    } catch (error) {
      status.textContent = `Could not save: ${error.message}`;
    }
  };
  return h("div", { class: "card" }, h("h2", {}, "Session sound"),
    h("label", { for: "bg-file" }, "Music (audio files in the data\\voice folder)"), select, volume.node,
    h("p", { class: "muted small" }, "Plays quietly under the voice during the session only (not the consultation): "
      + "it fades in when the session starts, pauses with it, repeats if the session is longer, and fades out at the "
      + "end. 0.90 puts a quiet ambient track about 12 LU below the voice."),
    reverbSlider.node,
    h("p", { class: "muted small" }, "Reverb: a warm hall around the session voice only; the consultation stays dry. "
      + "0 = none, 0.50 = clearly there without blurring the words. \"Hear at session speed\" above plays it."),
    h("div", { class: "actions" }, together, listen, h("button", { type: "button", onclick: save }, "Save")),
    h("p", { class: "muted small" }, "Reverb is on the voice only, never on the music. The music volume and reverb are "
      + "heard as set here even before Save; Save makes them the ones every session uses."), status);
}

async function showVoice() {
  const [data, background, reverb] = await Promise.all([api("/api/clinician/voice"), api("/api/clinician/background"),
    api("/api/clinician/reverb")]);
  const reverbSlider = slider("bg-reverb", "Reverb on the session voice", 0, 1, 0.05, reverb.amount);
  const earlier = data.saved ? null : voice.legacyLocalSettings();
  // Earlier test versions sometimes stored a basic (robotic) voice picked before the natural voices had loaded:
  // keep the pitch and speeds, but let the voice fall back to automatic.
  if (earlier && !/natural|neural/i.test(earlier.voice_name)) earlier.voice_name = "";
  const initial = earlier || data.settings;
  const own = data.practice_voice.available;
  const select = h("select", { id: "v-voice" });
  const hint = h("p", { class: "notice hidden" },
    "This browser only has basic, robotic-sounding voices. Open the dashboard in Microsoft Edge to choose a natural one.");
  const rate = slider("v-rate", own ? "Conversation speed (device voice only)" : "Conversation speed",
    0.5, 1.4, 0.05, initial.rate);
  const pitch = slider("v-pitch", "Pitch (lower = deeper)", 0.3, 1.5, 0.05, initial.pitch);
  const sessionRate = slider("v-session", "Session speed (the hypnosis recording)", 0.4, 1.2, 0.05, initial.session_rate);
  const status = h("p", { class: "muted" });
  let wanted = initial.voice_name;

  const fill = () => {
    const voices = voice.englishVoices();
    select.replaceChildren(
      h("option", { value: "", selected: !wanted }, "Automatic: the best natural male voice on each device"),
      ...voices.map((v) => h("option", { value: v.name, selected: v.name === wanted },
        `${voice.isNatural(v) ? "Natural" : "Basic (robotic)"} · ${v.name} (${v.lang})`)));
    hint.classList.toggle("hidden", voices.some(voice.isNatural));
  };
  select.addEventListener("change", () => { wanted = select.value; });
  voice.whenVoicesReady(fill);
  fill();

  const current = () => ({
    voice_name: select.value, rate: Number(rate.input.value), pitch: Number(pitch.input.value),
    session_rate: Number(sessionRate.input.value),
  });
  const deviceTest = (run) => () => { voice.usePracticeVoice(null); voice.applyPracticeVoice(current()); run(); };
  const testConversation = deviceTest(() => voice.speakReply(VOICE_SAMPLE));
  const testSession = deviceTest(() => voice.createSessionPlayer(SESSION_SAMPLE).play());
  const save = async () => {
    try {
      await api("/api/clinician/voice", { method: "PUT", body: JSON.stringify(current()) });
      status.textContent = "Saved. Every client now hears this voice and speed.";
    } catch (error) {
      status.textContent = `Could not save: ${error.message}`;
    }
  };

  view.replaceChildren(
    h("h2", {}, "Practice voice"),
    h("p", {}, "Every client hears this voice and speed. Clients cannot change it."),
    earlier ? h("p", { class: "notice" }, "Loaded from the settings you used earlier in this browser. "
      + "Press Save to use them for every client.") : null,
    recordedVoiceCard(data.practice_voice, current, () => Number(reverbSlider.input.value)),
    h("div", { class: "card" }, h("h2", {}, "Speed"),
      rate.node, sessionRate.node,
      h("p", { class: "muted small" }, own
        ? "In the consultation the recorded voice speaks exactly as it is, at its natural speed. The session speed "
          + "slows only the session (1.00 = natural), with studio-quality time-stretching that keeps the pitch; "
          + "nothing else about the voice is changed."
        : "1.00 is the voice's own speed. Slowing does not change the pitch."),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: save }, "Save")), status),
    backgroundCard(background, reverbSlider, current),
    h("h2", {}, own ? "Device voice (used only if the recorded voice can't be played)" : "Device voice"),
    h("div", { class: "card" },
      h("label", { for: "v-voice" }, "Voice"), select, hint, pitch.node,
      h("p", { class: "muted small" }, "A chosen voice must be installed on the client's device; if it isn't, the "
        + "closest natural male voice is used. Microsoft Edge on Windows has the natural voices."),
      h("p", { class: "notice small" }, "Natural voices start to sound artificial when the pitch is lowered a lot "
        + "(below about 0.8). For a deeper sound, choose a naturally deep voice and keep the pitch near 1.0."),
      h("div", { class: "actions" },
        h("button", { type: "button", class: "secondary", onclick: testConversation }, "Test conversation voice"),
        h("button", { type: "button", class: "secondary", onclick: testSession }, "Test session voice"),
        h("button", { type: "button", onclick: save }, "Save"))));
}

// --- books: the practice adds PDF books to improve the AI (see app/books.py) ---

let booksTimer = null; // refreshes the list while the app is working on a book

const BOOK_STATUS = {
  reading: "Reading the PDF…",
  unreadable: "Can't be used",
  read: "Ready to add",
  checking: "The AI is checking what the book is…",
  refused: "Not added",
  writing: "The AI is writing notes",
  review: "Notes ready for you to read",
  approved: "In use by the AI",
  failed: "Stopped",
};
const BOOK_BUSY = ["reading", "checking", "writing"];

function uploadPdf(file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/clinician/books?name=${encodeURIComponent(file.name)}`);
    xhr.setRequestHeader("Content-Type", "application/pdf");
    xhr.setRequestHeader("X-Requested-With", "admin"); // the dashboard's own request (see api())
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((100 * event.loaded) / event.total));
    };
    xhr.onload = () => {
      if (xhr.status === 401) { location.assign("/sign-in"); return; }
      let body = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* not JSON */ }
      if (xhr.status < 300) resolve(body);
      else reject(new Error(body?.detail || `The upload failed (HTTP ${xhr.status}).`));
    };
    xhr.onerror = () => reject(new Error("The upload failed. Please check the connection and try again."));
    xhr.send(file);
  });
}

function bookEstimate(book, engine) {
  const e = book.estimate;
  if (!e) return null;
  const requests = `about ${e.requests} AI request${e.requests === 1 ? "" : "s"}`;
  return h("p", { class: "muted small" }, engine === "claude-code"
    ? `Adding it takes ${requests}. On the test engine this uses your Claude subscription's allowance.`
    : `Adding it takes ${requests}, costing about $${e.usd.toFixed(2)} with the AI service.`);
}

function bookCard(book, engine, refresh) {
  const status = h("p", { class: "muted small", role: "status" });
  const act = (label, run, cls = "") => h("button", { type: "button", class: cls, onclick: async (event) => {
    event.target.disabled = true;
    status.textContent = "";
    try { await run(); await refresh(); } catch (error) { status.textContent = error.message; event.target.disabled = false; }
  } }, label);
  const remove = (label, question) => act(label, async () => {
    if (!confirm(question)) throw new Error("");
    await api(`/api/clinician/books/${book.id}`, { method: "DELETE" });
  }, "secondary");
  const notesBox = h("div", { class: "book-notes hidden" });
  const showNotes = h("button", { type: "button", class: "secondary", onclick: async () => {
    if (notesBox.classList.toggle("hidden")) return;
    notesBox.textContent = "Loading…";
    try { notesBox.textContent = (await api(`/api/clinician/books/${book.id}`)).notes || "(no notes)"; }
    catch (error) { notesBox.textContent = error.message; }
  } }, "Read the notes");

  const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
  const facts = [book.pages && plural(book.pages, "page"), book.words && plural(book.words, "word"),
    book.sections && plural(book.sections, "technique note")].filter(Boolean).join(" · ");
  const heading = book.title ? `${book.title}${book.author ? `, ${book.author}` : ""}` : book.filename;
  const card = h("div", { class: `card book ${book.status}` },
    h("div", { class: "book-head" }, h("h3", {}, heading),
      h("span", { class: `badge ${book.status === "approved" ? "ok" : ["unreadable", "refused", "failed"].includes(book.status) ? "alert" : "action"}` },
        BOOK_STATUS[book.status] || book.status)),
    book.title ? h("p", { class: "muted small" }, book.filename) : null,
    facts ? h("p", { class: "small" }, facts) : null,
    book.topics?.length ? h("p", { class: "small" }, `Topics: ${book.topics.join(", ")}`) : null);

  const actions = h("div", { class: "actions" });
  if (book.status === "writing") {
    const done = book.parts ? Math.round((100 * book.parts_done) / book.parts) : 0;
    const fill = h("div", { class: "progress-fill" });
    fill.style.width = `${done}%`; // (not a style attribute: the page's security policy blocks those)
    card.append(h("p", {}, `Part ${Math.min(book.parts_done + 1, book.parts)} of ${book.parts}`),
      h("div", { class: "progress" }, fill));
  }
  if (book.status === "unreadable" || book.status === "failed") card.append(h("p", {}, book.error || ""));
  if (book.status === "refused") card.append(h("p", {}, book.reason || ""));
  if (book.status === "read" || book.status === "failed") {
    const estimateNode = bookEstimate(book, engine);
    if (estimateNode) card.append(estimateNode);
    actions.append(act(book.status === "failed" ? "Try again" : "Add to the AI", () =>
      api(`/api/clinician/books/${book.id}/start`, { method: "POST" })));
  }
  if (book.status === "review") {
    card.append(h("p", { class: "small" }, book.sections
      ? "Read the notes the AI wrote. Approve them and the AI starts using them in the sessions it writes."
      : "The AI found nothing in this book to use for sessions."));
    if (book.sections) {
      actions.append(showNotes, act("Approve: the AI starts using it", () =>
        api(`/api/clinician/books/${book.id}/approve`, { method: "POST" })));
    }
    actions.append(remove("Discard", "Discard these notes? The book will be removed."));
  }
  if (book.status === "approved") {
    card.append(h("p", { class: "small" }, `Approved on ${fmtTime(book.approved_at)}.`));
    actions.append(showNotes, remove("Remove from the AI", "Remove this book? The AI stops using it straight away."));
  }
  if (["read", "unreadable", "refused", "failed"].includes(book.status)) {
    actions.append(remove("Remove", "Remove this book from the list?"));
  }
  if (actions.children.length) card.append(actions);
  card.append(status, notesBox);
  return card;
}

async function showBooks() {
  const [data, kb] = await Promise.all([api("/api/clinician/books"), api("/api/clinician/knowledge")]);
  const list = h("div", {});
  const refresh = async () => {
    clearTimeout(booksTimer);
    const latest = await api("/api/clinician/books");
    list.replaceChildren(...(latest.books.length ? latest.books.map((b) => bookCard(b, latest.engine, refresh))
      : [h("p", { class: "muted" }, "No books added yet.")]));
    if (latest.books.some((b) => BOOK_BUSY.includes(b.status))) booksTimer = setTimeout(refresh, 3000);
  };

  const file = h("input", { type: "file", id: "book-file", accept: "application/pdf,.pdf" });
  const uploadStatus = h("p", { class: "muted small", role: "status" });
  const upload = h("button", { type: "button", onclick: async () => {
    const chosen = file.files[0];
    if (!chosen) { uploadStatus.textContent = "Choose a PDF first."; return; }
    if (chosen.size > data.max_upload_mb * 2 ** 20) {
      uploadStatus.textContent = `This file is larger than ${data.max_upload_mb} MB.`;
      return;
    }
    upload.disabled = true;
    try {
      await uploadPdf(chosen, (percent) => { uploadStatus.textContent = `Uploading… ${percent}%`; });
      uploadStatus.textContent = "Uploaded.";
      file.value = "";
      await refresh();
    } catch (error) { uploadStatus.textContent = error.message; }
    upload.disabled = false;
  } }, "Upload");

  const blocked = Object.entries(kb.blocked_files);
  view.replaceChildren(
    h("h2", {}, "Books"),
    h("div", { class: "card" }, h("h2", {}, "Add a book to improve the AI"),
      h("p", {}, "Upload a hypnotherapy book as a PDF. The AI checks what the book is, then reads it and writes "
        + "technique notes in its own words. You read the notes and approve them; only then does the AI use them "
        + "in the sessions it writes. You can remove a book at any time."),
      h("p", { class: "muted small" }, "Books of covert influence, \"dark psychology\", manipulation or stage "
        + "hypnosis are refused. The PDF must have selectable text (scanned pages can't be read), and the file "
        + "itself is deleted once its text has been read."),
      h("label", { for: "book-file" }, "PDF file"), file,
      h("div", { class: "actions" }, upload), uploadStatus),
    list,
    h("details", { class: "card" }, h("summary", {}, `The AI's whole library: ${kb.library_files} books, `
      + `${kb.library_sections} sections`),
      h("p", { class: "small" }, `Built-in library folder: ${kb.root}`),
      h("h3", {}, `Blocked files (${blocked.length})`),
      blocked.length ? h("ul", {}, blocked.map(([name, reason]) => h("li", {}, `${name}: ${reason}`)))
        : h("p", { class: "muted" }, "None."),
      h("h3", {}, `Sections removed by the ethics filter (${kb.dropped_sections.length})`),
      h("ul", {}, kb.dropped_sections.map((d) => h("li", { class: "small" }, d)))));
  await refresh();
}

// --- admin: prices, the practice's own instructions to the AI, hypnotherapists, sign-in security ---

function priceInput(id, label, value, fallback) {
  const input = h("input", { type: "number", id, min: 1, step: 1, value, inputmode: "numeric" });
  return { input, node: h("div", {}, h("label", { for: id }, label), input,
    h("p", { class: "muted small" }, `Default (.env): £${fallback}`)) };
}

async function showAdmin() {
  const data = await api("/api/clinician/admin");
  const d = data.default_prices;
  const fields = {
    consultation: priceInput("p-consultation", "Consultation (£, paid before it starts)", data.prices.consultation,
      d.consultation),
    ai_session: priceInput("p-session", "Full AI session (£, after the free preview)", data.prices.ai_session,
      d.ai_session),
    human_from: priceInput("p-from", "Qualified hypnotherapist: from (£)", data.prices.human_from, d.human_from),
    human_to: priceInput("p-to", "Qualified hypnotherapist: to (£)", data.prices.human_to, d.human_to),
  };
  const total = h("p", { class: "muted" });
  const showTotal = () => {
    const sum = Number(fields.consultation.input.value) + Number(fields.ai_session.input.value);
    total.textContent = `The AI route costs a client £${sum} in total.`;
  };
  Object.values(fields).forEach((f) => f.input.addEventListener("input", showTotal));
  showTotal();
  const priceStatus = h("p", { class: "muted", role: "status" });
  const savePrices = async () => {
    const prices = Object.fromEntries(Object.entries(fields).map(([key, f]) => [key, Number(f.input.value)]));
    if (Object.values(prices).some((v) => !Number.isInteger(v) || v < 1)) {
      priceStatus.textContent = "Every price must be a whole number of pounds, at least £1.";
      return;
    }
    if (prices.human_from > prices.human_to) {
      priceStatus.textContent = "The hypnotherapist's lowest price must not be above the highest.";
      return;
    }
    try {
      await api("/api/clinician/prices", { method: "PUT", body: JSON.stringify(prices) });
      priceStatus.textContent = "Saved. New clients see these prices from now on.";
    } catch (error) { priceStatus.textContent = `Could not save: ${error.message}`; }
  };

  const texts = {
    consultation: h("textarea", { id: "i-consultation", class: "instructions", maxlength: 6000,
      placeholder: "For example: Always ask how they sleep. Keep your questions short." }),
    script: h("textarea", { id: "i-script", class: "instructions", maxlength: 6000,
      placeholder: "For example: Use nature imagery where the client likes it. Keep the pre-talk short." }),
  };
  texts.consultation.value = data.instructions.consultation;
  texts.script.value = data.instructions.script;
  const instructionStatus = h("p", { class: "muted", role: "status" });
  const saveInstructions = async () => {
    try {
      await api("/api/clinician/instructions", { method: "PUT", body: JSON.stringify({
        consultation: texts.consultation.value, script: texts.script.value }) });
      instructionStatus.textContent = "Saved. They apply to every new consultation and session from now on.";
    } catch (error) { instructionStatus.textContent = `Could not save: ${error.message}`; }
  };

  const signIns = data.sign_ins.length
    ? h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "When"), h("th", {}, "Address"), h("th", {}, "Result"),
        h("th", {}, "Browser"))),
      h("tbody", {}, data.sign_ins.map((s) => h("tr", {}, h("td", {}, fmtTime(s.at)), h("td", {}, s.ip),
        h("td", {}, s.ok ? "Signed in" : "Wrong email or password"), h("td", { class: "small" }, s.user_agent)))))
    : h("p", { class: "muted" }, "No sign-ins yet.");

  view.replaceChildren(
    h("h2", {}, "Admin"),
    h("div", { class: "card" }, h("h2", {}, "Prices"),
      h("div", { class: "price-grid" }, Object.values(fields).map((f) => f.node)), total,
      h("p", { class: "muted small" }, "New prices apply to payments from now on; what clients have already paid "
        + "does not change. A client whose payment page was already open is asked to reload it, so nobody is "
        + "charged a price they did not see."),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: savePrices }, "Save prices")),
      priceStatus),
    h("div", { class: "card" }, h("h2", {}, "Your instructions to the AI Hypnotherapist"),
      h("p", { class: "muted small" }, "Added to the AI Hypnotherapist's own instructions for every new "
        + "consultation and session. The safety and ethics rules always come first, so an instruction that "
        + "conflicts with them is not followed."),
      h("label", { for: "i-consultation" }, "During the consultation"), texts.consultation,
      h("label", { for: "i-script" }, "When writing sessions"), texts.script,
      h("div", { class: "actions" }, h("button", { type: "button", onclick: saveInstructions }, "Save instructions")),
      instructionStatus),
    h("div", { class: "card" }, h("h2", {}, "Hypnotherapists"),
      h("p", { class: "muted" }, "Coming later: add other qualified hypnotherapists here, so clients who choose a "
        + "hypnotherapist can be matched with one. For now, every request comes to you by email.")),
    h("div", { class: "card" }, h("h2", {}, "Sign-in security"),
      h("p", {}, `Only ${data.admin_email} can sign in, with its password and the robot check.`),
      h("p", { class: "muted small" }, "To change the password, run this in the project folder on the server: "
        + "python -m app.set_admin_password"),
      h("h3", {}, "Recent sign-ins"), signIns));
}

document.getElementById("sign-out").addEventListener("click", async () => {
  try { await api("/api/admin/sign-out", { method: "POST" }); } finally { location.assign("/sign-in"); }
});

open("sessions");
