> Setup notes from the original project README. The practice's knowledge base, prompts and question sets are private; this repository ships short stand-ins.

# AI Hypnotherapist

A spoken hypnotherapy consultation (the client talks with an animated consultant), followed by a personal
hypnotherapy session written from it, with an admin dashboard for the practice. FastAPI server in `app/`,
browser app in `app/static/`.

## Requirements

- Windows 10/11 (the `.bat` launchers; the server itself also runs on macOS and Linux)
- Python 3.12 or newer
- The practice's knowledge base folder (`KNOWLEDGE_ROOT` in `.env`); the app will not start without it
- For session audio files: ffmpeg 7 or newer (`winget install ffmpeg`)
- Optional, for the practice voice and for speech to text on this computer: the packages in
  `requirements-voice.txt` (several GB, including PyTorch)

## Setup

In the project folder:

```bat
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-voice.txt   (optional, see above)
copy .env.example .env
```

Then fill in `.env` (at least `ANTHROPIC_API_KEY`), and set the admin password with `set-admin-password.bat`.
`requirements.lock.txt` pins the exact versions of everything `requirements.txt` installs.

## Running

Double-click `start.bat`. It starts the server and opens the app in Microsoft Edge:

- Client app: http://127.0.0.1:8000
- Admin dashboard: http://127.0.0.1:8000/admin

Or: `.venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000`

## Spoken answers and the microphone

- Browsers only allow the microphone on `https://` pages or on this computer (`http://127.0.0.1` or
  `http://localhost`). Opening the app from another device at `http://<this computer's address>:8000` will not
  work; put it behind HTTPS (for example a reverse proxy) and set `PUBLIC_URL` to that address.
- With `requirements-voice.txt` installed, answers are recorded in the browser and turned into words on this
  computer (faster-whisper), in any browser. Its model (about 150 MB) is downloaded the first time the app starts.
  Without it, the browser's own speech recognition is used (works best in Edge and Chrome; not in Firefox).
- When the app is shown inside the practice's website, the website's `<iframe>` needs
  `allow="microphone; autoplay"` (see `EMBED_ORIGINS` in `.env.example`).
- If the microphone briefly fails, listening carries on by itself; only a blocked microphone or repeated failures
  pause it and offer typing instead.

## Languages

Clients choose English, German, French or Spanish on the first page (their browser's language is chosen for them
when it is one of these). Every screen, the consultant, the safety questions, helplines, emails and the session
itself are then in that language; the practice's side (summary, clinician notes, emails to the practice) stays in
English, and the qualified hypnotherapist is offered in English only. `LANGUAGES` in `.env` sets which are offered.

- Speech to text: English uses a small English model; the other languages use Whisper's multilingual `small` model
  (about 480 MB, downloaded the first time the app starts).
- The practice voice speaks each language from its own Pocket TTS model, prepared once from the same recording.
  Where the English voice is already prepared, add the others with
  `.venv\Scripts\python.exe -m app.speech prepare "data\voice\Clear sound.mp3" --languages de fr es`, then restart
  the app and listen to `data\voice\preview.<language>.wav` for each. A language without a prepared voice uses the
  device's own voice, and its sessions have no MP3.
- Before launch, a native speaker should check the translations, above all the safety questions and the legal
  wording, and the helpline numbers for Germany, Austria, Switzerland, France and Spain must be checked (see
  `app/content/crisis_resources.json`).

## Tests

```bat
.venv\Scripts\python.exe -m pytest
```

Tests that call the real Claude API are skipped unless `RUN_LIVE_TESTS=1`.
