# AI Hypnotherapist

A spoken consultation with an AI consultant, followed by a personal, self-guided hypnotherapy session written from it, with a dashboard where the practice reviews everything. It is a **clinician-support workflow, not a diagnostic or autonomous clinical system.**

**Stack:** Python 3.12, FastAPI, Claude API, SQLite, a browser client in plain JavaScript, speech to text on the server (faster-whisper), a local practice voice (Pocket TTS), Stripe Checkout and ffmpeg. About 7,100 lines of Python, 4,400 lines of front-end JavaScript and 4,500 lines of tests (294 tests).

> **About this repository.** This is a portfolio copy of a live product. The practice's own prompts, question sets and knowledge base encode its method and stay private, so this copy ships short stand-ins with the same placeholders. No client data of any kind is included. I designed the system and its safety rules and I operate it. It was built with an AI coding assistant (Claude Code) under my direction. The [engineering log](docs/ENGINEERING_LOG.md) records real problems found in use and how each was fixed.

## The client journey

1. **Screening.** Fixed questions (age, self-harm, epilepsy, psychosis, dissociation, heart, pregnancy, unchecked symptoms, intoxication). No AI is involved in these decisions. An answer can stop the journey or send the client to a human practitioner.
2. **Consultation.** About 20 minutes, spoken or typed, one question at a time. Every client message passes the safety monitor before any reply is shown.
3. **Intake.** The transcript becomes a structured intake record.
4. **Preview.** The opening of the script is written, and the first five minutes are made as a free audio preview.
5. **Payment.** Stripe Checkout. The app never sees card details.
6. **Session.** The rest of the script is written, checked automatically, optionally reviewed by a clinician, and made into audio in the practice's voice with music. The client downloads it from a private link.

## What makes it interesting engineering

- **Safety that does not depend on the model alone.** Screening is deterministic. During the consultation a keyword net (instant, offline) and an independent AI classifier both look at every client message. If the classifier is unavailable, a keyword match is treated as a crisis and anything else is refused as "unverified", so no reply is ever produced without a safety check.
- **Speculative reply, safe by construction.** The assistant's reply is generated at the same time as the safety check and is discarded unless the safety result is clear. The client does not wait, and nothing unchecked can reach them.
- **Analysis before the reply.** Each consultation turn is structured output where the field order makes the model analyse the latest answer first and write the reply from that analysis.
- **One door to the model.** `app/llm.py` is the only module that talks to the Claude API, which keeps retries, limits, logging and tests in one place.
- **Prompt-injection hygiene.** Client text is escaped before it reaches the classifier. Voice measurements from the browser arrive as numbers only, and the wording is produced by code, so nothing a client sends can become an instruction.
- **Cost control built into the design.** The script is written in two steps, so nothing is spent on the whole session before the client has paid. Silence check-ins use fixed phrases and never call the model. A session is only written from enough of the client's own words. Books are read by a cheaper model.
- **Human-gated learning.** Clinician edits and feedback become proposed lessons, and a lesson only affects future scripts after a clinician approves it.
- **Fail-closed knowledge.** Notes can only be retrieved if the catalogue tags them as suitable, and files the catalogue does not mention are blocked.
- **Local speech.** Speech to text and the practice voice run on the server with no outside service, with the browser's own speech as a fallback.

## Safety at a glance

| Concern | What the code does |
|---|---|
| Unsuitable clients | Deterministic screening with an explicit outcome order. Crisis outranks everything. |
| Risk during the consultation | Keyword net plus an independent classifier. Five outcomes: none, check, concern, crisis, unverified. |
| Mishearing | A stated condition that would stop the consultation is confirmed once first, because speech to text can mishear. |
| Crisis | The consultation stops and the client sees verified crisis resources for their country and language. |
| Unsafe scripts | Automatic checks for required sections, safety lines, a calm emergence and length limits. |
| Wrong material | Books of covert influence, "dark psychology" or stage hypnosis are refused. Notes are approved by the practice before use. |

More in [docs/SAFETY_AND_PRIVACY.md](docs/SAFETY_AND_PRIVACY.md).

## Testing

294 tests run with `pytest` and need no API key: the model is replaced by a scripted fake gateway, so every safety branch can be exercised offline. The tests cover the risk monitor (including the fail-safe paths and tag-escaping), screening, script validation, the consultation limits, payments, languages, embedding in a website, emails, admin sign-in, speech and audio. 287 pass here and 7 are skipped because they need the live Claude API, the optional voice packages, or the practice's private knowledge base.

## Honest limitations

- It has not been clinically validated, makes no claim about outcomes, and is not a medical device.
- Clinician review of each script is a switch (`REQUIRE_CLINICIAN_REVIEW`), and it is off in the sample configuration.
- The browser client (about 4,400 lines of JavaScript) has no automated tests of its own yet. Its fixes were checked by hand in browsers and on phones.
- Knowledge retrieval is keyword search (BM25) over curated notes, not embeddings.
- Consultations and sessions are available in English, German, French and Spanish, while the qualified hypnotherapist is offered in English only.
- The practice voice is a real person's recorded voice, used with their consent and read by a local model.

## Repository layout

| Path | What is there |
|---|---|
| `app/` | The server: journey, safety monitor, screening, script writing, speech, audio, payments, emails, admin |
| `app/static/` | The browser client and the clinician dashboard |
| `app/prompts/`, `app/content/` | Prompts and content (stand-ins for the practice's private ones) |
| `tests/` | The test suite |
| `docs/` | Architecture, safety and privacy, engineering log, setup notes |

## Running it

```bash
pip install -r requirements.txt
python -m pytest -q
```

Running the full product needs a Claude API key, a knowledge base folder and a configured mailbox. See [docs/SETUP.md](docs/SETUP.md).

## Licence

All rights reserved. This repository is published so that it can be read and evaluated. See [LICENSE](LICENSE).
