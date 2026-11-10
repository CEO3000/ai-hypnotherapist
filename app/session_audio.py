"""The session as an audio file the client downloads: the voice piece by piece with its pauses, a warm hall
reverb (its high frequencies lowered a little), the practice's music underneath (faded in and out), and a limiter,
encoded as MP3 by ffmpeg. Before the session is paid for, only its first five minutes are made, as a free preview.

The session lasts 60 to 85 minutes: only the pauses are lengthened or shortened to fit, never the voice.
Everything runs on this computer (ffmpeg and our own code): no outside service, no licence fees.

Files, in data/sessions/<session id>/: speech.pcm (every piece, 24 kHz 16-bit mono, one after another),
pieces.json (their words and lengths), job.json (the settings the session is made with), preview.mp3 and
session.mp3. Work that was interrupted (the app restarted) carries on from the last finished piece. When the session
file is finished, only preview.mp3, session.mp3 and job.json are kept, and those are deleted after the retention
period (AUDIO_RETENTION_DAYS, 7 by default).
"""

import json
import logging
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from .session_delivery import delivery_plan, fades_for, rate_for
from .speech import SAMPLE_RATE

log = logging.getLogger("app.session_audio")

PREVIEW_SECONDS = 300
MIN_SECONDS, MAX_SECONDS = 60 * 60, 85 * 60
PAUSE_SCALE = (0.6, 2.2)  # how far the pauses may be lengthened or shortened to fit
OUTPUT_RATE = 48_000
# Seconds of music alone before the first word (the voice never starts suddenly), and after the last word (the
# hall dies away and the music fades out over `fade`).
LEAD_IN = 5.0
SESSION_END = {"tail": 20.0, "fade": 18.0}
PREVIEW_END = {"tail": 5.0, "fade": 4.0}
MUSIC_FADE_IN = 6.0
LIMIT = 0.891  # -1 dBFS: the loudest the mix may be
HALL = {"seconds": 2.4, "decay": 1.6, "predelay_ms": 18, "bright_hz": 6500, "dark_hz": 1400}
HIGH_SHELF = {"hz": 4000, "db": -4}  # the reverb's highs, a little lower (the practice's request); Q 0.7071 as in
# the browser's shelf filter, so the file and the live player sound the same
REVERB_MAX = 0.5  # the reverb's level at amount 1 (as in static/voice.js)
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
MIX_TIMEOUT = 3600  # seconds; a mix takes a few minutes at most


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


_afir_options: dict[str, str] = {}


def afir_options(ffmpeg: str) -> str:
    """The reverb's options, leaving the hall's level as made (hall_impulse normalises it). ffmpeg 7 and later
    take irnorm=-1 for that; older versions (still common on Linux) only know gtype=-1."""
    if ffmpeg not in _afir_options:
        try:
            help_text = subprocess.run([ffmpeg, "-hide_banner", "-h", "filter=afir"], capture_output=True, text=True,
                                       timeout=30, creationflags=NO_WINDOW).stdout
        except (OSError, subprocess.SubprocessError):
            help_text = ""
        _afir_options[ffmpeg] = "dry=1:wet=1:irnorm=-1:gtype=-1" if "irnorm" in help_text else "dry=1:wet=1:gtype=-1"
    return _afir_options[ffmpeg]


def hall_impulse(sample_rate: int = OUTPUT_RATE, seed: int = 1234) -> np.ndarray:
    """The sound of the hall (stereo impulse response): early reflections, then a dense tail that dies away over
    `decay` seconds and grows darker as it fades; left and right differ, for space. The same room as the live
    preview in static/voice.js (reverbImpulse). Normalised to unit energy per channel."""
    length = round(HALL["seconds"] * sample_rate)
    predelay = round(HALL["predelay_ms"] / 1000 * sample_rate)
    rng = np.random.default_rng(seed)
    channels = []
    for side in (0, 1):
        t = np.arange(length - predelay) / sample_rate
        cutoff = HALL["dark_hz"] + (HALL["bright_hz"] - HALL["dark_hz"]) * np.exp(-t / (HALL["decay"] / 3))
        alpha = 1 - np.exp(-2 * np.pi * cutoff / sample_rate)
        noise = rng.uniform(-1, 1, len(t))
        low = np.empty_like(noise)
        state = 0.0
        for i in range(len(noise)):  # a low-pass that closes as the tail fades
            state += alpha[i] * (noise[i] - state)
            low[i] = state
        tail = low * np.exp(-6.91 * t / HALL["decay"]) * np.minimum(1, t / 0.03)
        out = np.concatenate([np.zeros(predelay), tail])
        for ms, gain in ((11, 0.5), (19, 0.38), (27, 0.33), (38, 0.26), (53, 0.19), (71, 0.14)):
            at = predelay + round((ms + side * 3.7) / 1000 * sample_rate)
            if at < length:
                out[at] += gain * (-1 if side else 1) * 0.35
        channels.append(out / np.sqrt(np.sum(out ** 2)))
    return np.stack(channels).astype(np.float32)


def write_wav(path: Path, audio: np.ndarray, sample_rate: int) -> None:
    """Float audio (channels x samples) as a 32-bit float WAV, for ffmpeg. Written here (the standard library's
    wave module only writes whole-number samples), so no extra audio package is needed."""
    frames = np.ascontiguousarray(np.atleast_2d(audio).T, dtype="<f4")
    channels = frames.shape[1]
    data = frames.tobytes()
    header = b"".join((
        b"RIFF", struct.pack("<I", 4 + 26 + 12 + 8 + len(data)), b"WAVE",
        # fmt: IEEE float (3), with the (empty) extension size the format asks for
        b"fmt ", struct.pack("<IHHIIHHH", 18, 3, channels, sample_rate, sample_rate * channels * 4, channels * 4, 32, 0),
        b"fact", struct.pack("<II", 4, frames.shape[0]),
        b"data", struct.pack("<I", len(data)),
    ))
    path.write_bytes(header + data)


class Stopped(Exception):
    """The app is closing: the session's audio carries on from here when it starts again."""


class Deferred(Exception):
    """Another client is waiting for their preview: it goes first, and this session carries on afterwards."""


def mix(ffmpeg: str, voice_pcm: Path, seconds: float, out: Path, *, hall: Path, reverb: float,
        music: Path | None, music_gain: float, end: dict, stop: threading.Event | None = None) -> None:
    """voice_pcm (24 kHz 16-bit mono, `seconds` long) + reverb + music -> MP3 (48 kHz stereo, VBR about
    190 kbit/s), `end["tail"]` seconds longer than the voice, the music fading out over its last `end["fade"]`.
    Raises Stopped (and ends ffmpeg) if `stop` is set while it runs."""
    total = seconds + end["tail"]
    voice = (f"[0:a]aresample={OUTPUT_RATE}:resampler=soxr:precision=28,pan=stereo|c0=c0|c1=c0,"
             f"apad=pad_dur={end['tail']},asplit=2[dry][send];"
             f"[send][1:a]afir={afir_options(ffmpeg)},"
             f"highshelf=f={HIGH_SHELF['hz']}:g={HIGH_SHELF['db']}:t=q:w=0.7071,volume={reverb * REVERB_MAX:.4f}[wet];"
             "[dry][wet]amix=inputs=2:normalize=0:duration=first[voice];")
    inputs = ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", str(voice_pcm), "-i", str(hall)]
    if music is not None:
        inputs += ["-stream_loop", "-1", "-i", str(music)]
        # (any music file: mono or stereo, any sample rate, is made stereo at the output rate before mixing)
        graph = voice + (f"[2:a]atrim=duration={total:.3f},asetpts=PTS-STARTPTS,aresample={OUTPUT_RATE},"
                         "aformat=channel_layouts=stereo,"
                         f"volume={music_gain:.4f},afade=t=in:d={MUSIC_FADE_IN},"
                         f"afade=t=out:st={max(0.0, total - end['fade']):.3f}:d={end['fade']}[music];"
                         "[voice][music]amix=inputs=2:normalize=0:duration=first,")
    else:
        graph = voice + "[voice]"
    graph += f"alimiter=limit={LIMIT}:level=false:latency=true[out]"
    temporary = out.with_suffix(".part.mp3")
    command = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs, "-filter_complex", graph,
               "-map", "[out]", "-c:a", "libmp3lame", "-q:a", "2", "-ar", str(OUTPUT_RATE), str(temporary)]
    stop = stop or threading.Event()
    deadline = time.monotonic() + MIX_TIMEOUT
    errors: list[bytes] = []
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          creationflags=NO_WINDOW) as process:
        reader = threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True)
        reader.start()
        while process.poll() is None:
            if stop.wait(0.2) or time.monotonic() > deadline:
                process.kill()
                process.wait()
                temporary.unlink(missing_ok=True)
                if stop.is_set():
                    raise Stopped
                raise RuntimeError(f"ffmpeg took longer than {MIX_TIMEOUT} s")
        reader.join()
    if process.returncode != 0:
        temporary.unlink(missing_ok=True)
        message = b"".join(errors).decode(errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg failed (exit {process.returncode}): {message}")
    temporary.replace(out)


def pause_scale(speech: float, pauses: float) -> float:
    """How much to lengthen or shorten the pauses so the session lasts 60 to 85 minutes (1 when it already does)."""
    if pauses <= 0 or MIN_SECONDS <= speech + pauses <= MAX_SECONDS:
        return 1.0
    margin = min(60, (MAX_SECONDS - MIN_SECONDS) / 4)  # aim a little inside the range, not at its edge
    goal = MIN_SECONDS + margin if speech + pauses < MIN_SECONDS else MAX_SECONDS - margin
    return min(PAUSE_SCALE[1], max(PAUSE_SCALE[0], (goal - speech) / pauses))


class SessionAudio:
    """Makes each session's audio in the background, one session at a time, and says how far it is.

    What is made follows the session (see Service.audio_job): before the session is paid for, only the free
    five-minute preview, and then it stops; once paid, the rest of the opening while the rest of the script is being
    written; then the whole session file. Everything spoken is kept in speech.pcm (pieces.json says which text each
    part is), so nothing is ever spoken twice. Every session's files are deleted a set number of days after they
    were last made (cleanup)."""

    SETTINGS = ("session_rate", "reverb", "music", "music_gain")  # fixed when a session's audio is first made

    def __init__(self, data_dir: Path, voice, jobs, on_expired=None, on_preview=None, on_ready=None):
        self.root = data_dir / "sessions"
        self.voice = voice
        self.jobs = jobs  # session id -> what to make now, or None (see Service.audio_job)
        self.on_expired = on_expired  # called with a session id just before its files are deleted for good
        self.on_preview = on_preview  # called with a session id when its preview file is made
        self.on_ready = on_ready  # called with a session id when its whole session file is made
        self.progress: dict[str, float] = {}
        self.failed: set[str] = set()  # not tried again until the app restarts (the log says why)
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def folder(self, session_id: str) -> Path:
        return self.root / session_id

    def files(self, session_id: str) -> dict:
        folder = self.folder(session_id)
        state = self._read(folder / "job.json") or {}
        preview, session = folder / "preview.mp3", folder / "session.mp3"
        return {"preview": preview if preview.exists() else None, "session": session if session.exists() else None,
                "session_key": state.get("session_key"), "minutes": state.get("minutes")}

    def download(self, session_id: str) -> Path | None:
        """The finished session file, if it is made from the session's current script."""
        job = self.jobs(session_id)
        files = self.files(session_id)
        if job is None or job["phase"] != "full" or files["session_key"] != job["key"]:
            return None
        return files["session"]

    def status(self, session_id: str) -> dict:
        files = self.files(session_id)
        done = self.download(session_id) is not None
        return {"preview_ready": files["preview"] is not None, "download_ready": done,
                "progress": 100 if done else round(100 * self.progress.get(session_id, 0)),
                "minutes": files["minutes"] if done else None, "failed": session_id in self.failed}

    def start(self, session_id: str) -> None:
        """Queues the session's audio if there is something to make now (does nothing otherwise)."""
        if not self.voice.available or self._stop.is_set() or session_id in self.failed:
            return
        job = self.jobs(session_id)
        if job is None or not self.voice.speaks(job.get("language", "en")) or self._finished(session_id, job):
            return
        with self._lock:
            if session_id in self._queue:
                return
            self._queue.append(session_id)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._work, name="session-audio", daemon=True)
                self._thread.start()

    def resume(self) -> None:
        """Queues every session whose audio was being made when the app stopped."""
        if self.root.is_dir():
            for folder in sorted(self.root.iterdir()):
                if (folder / "job.json").exists():
                    self.start(folder.name)

    def close(self, wait: float = 10.0) -> None:
        """Stops at the next piece (ending any ffmpeg run); what is made so far is kept for next time."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(wait)

    def cleanup(self, days: float) -> list[str]:
        """Deletes every session's audio that was last made more than `days` ago. Returns their session ids."""
        removed = []
        if not self.root.is_dir():
            return removed
        limit = time.time() - days * 86_400
        for folder in self.root.iterdir():
            if not folder.is_dir():
                continue
            with self._lock:
                if folder.name in self._queue:
                    continue  # being made right now
            newest = max((item.stat().st_mtime for item in folder.iterdir()), default=folder.stat().st_mtime)
            if newest >= limit:
                continue
            if self.on_expired is not None:
                self.on_expired(folder.name)  # first, so nothing starts making it again
            shutil.rmtree(folder, ignore_errors=True)
            self.progress.pop(folder.name, None)
            self.failed.discard(folder.name)
            removed.append(folder.name)
            log.info("Session %s: audio deleted after %s days", folder.name[:6], days)
        return removed

    def _wants_preview(self, session_id: str) -> bool:
        job = self.jobs(session_id)
        return job is not None and job["phase"] == "preview"

    def _preview_waiting(self, current: str) -> bool:
        """Whether another queued client is waiting for their preview (they are deciding: they go first)."""
        with self._lock:
            others = [queued for queued in self._queue if queued != current]
        return any(self._wants_preview(queued) for queued in others)

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queue or self._stop.is_set():
                    return
                queued = list(self._queue)
            session_id = next((q for q in queued if self._wants_preview(q)), queued[0])  # previews first
            try:
                while True:  # the session may move on while its audio is made (e.g. paid during the preview)
                    job = self.jobs(session_id)
                    if job is None or self._finished(session_id, job):
                        break
                    self.make(session_id, job)
            except Stopped:
                return
            except Deferred:  # another client's preview first; this one goes to the back and carries on later
                with self._lock:
                    self._queue.remove(session_id)
                    self._queue.append(session_id)
                continue
            except Exception:  # noqa: BLE001 - one failed session must not stop the others
                log.exception("Session %s: its audio could not be made", session_id[:6])
                self.failed.add(session_id)
            with self._lock:
                self._queue.remove(session_id)

    def _finished(self, session_id: str, job: dict) -> bool:
        folder = self.folder(session_id)
        if job["phase"] == "full":
            return self.download(session_id) is not None
        if not (folder / "preview.mp3").exists():
            return False
        if job["phase"] == "preview":
            return True
        return len(self._pieces(folder, delivery_plan(job["script"]))) == len(delivery_plan(job["script"]))

    @staticmethod
    def _read(path: Path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    @staticmethod
    def _write(path: Path, value) -> None:
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path)

    def _pieces(self, folder: Path, plan: list[dict]) -> list[dict]:
        """The pieces already spoken that are still the start of this plan (same words, same delivery), and whose
        audio is all in speech.pcm."""
        saved = self._read(folder / "pieces.json")
        speech = folder / "speech.pcm"
        available = speech.stat().st_size // 2 if speech.exists() else 0
        kept, samples = [], 0
        for piece, item in zip(saved if isinstance(saved, list) else [], plan):
            if not isinstance(piece, dict) or (piece.get("text"), piece.get("style")) != (item["text"], item["style"]):
                break
            samples += piece["samples"]
            if samples > available:
                break
            kept.append(piece)
        return kept

    def make(self, session_id: str, job: dict) -> None:
        """Makes what the session needs now (job["phase"]: "preview", "opening" or "full")."""
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("ffmpeg is not installed (winget install ffmpeg): session audio cannot be made")
        folder = self.folder(session_id)
        folder.mkdir(parents=True, exist_ok=True)
        plan = delivery_plan(job["script"])
        state = self._read(folder / "job.json")
        if state is None:
            state = {"created": time.time(), **{key: job[key] for key in self.SETTINGS}}
            self._write(folder / "job.json", state)
        hall = folder.parent / "hall.wav"
        if not hall.exists():
            write_wav(hall, hall_impulse(), OUTPUT_RATE)
        music = Path(state["music"]) if state["music"] and Path(state["music"]).exists() else None
        preview = folder / "preview.mp3"
        pieces = self._pieces(folder, plan)
        speech_file = folder / "speech.pcm"
        with speech_file.open("r+b" if speech_file.exists() else "wb") as out:
            out.truncate(sum(p["samples"] for p in pieces) * 2)  # anything not in the list is dropped
            out.seek(0, 2)
            for item in plan[len(pieces):]:
                if job["phase"] == "preview" and preview.exists():
                    break  # the preview is all that is made before the session is paid for
                if self._stop.is_set():
                    raise Stopped
                if job["phase"] != "preview" and self._preview_waiting(session_id):
                    raise Deferred  # (everything spoken so far is kept)
                style = item["style"]
                pcm = self.voice.render(item["text"], rate_for(style, state["session_rate"]), fades_for(style),
                                       job.get("language", "en"))
                out.write(pcm)
                out.flush()
                pieces.append({"text": item["text"], "style": style, "samples": len(pcm) // 2})
                self._write(folder / "pieces.json", pieces)
                if job["phase"] == "full":
                    self.progress[session_id] = len(pieces) / len(plan) * 0.95
                if not preview.exists():
                    self._preview(ffmpeg, folder, plan, pieces, state, hall, music)
        if not preview.exists():  # the opening is shorter than the preview
            self._preview(ffmpeg, folder, plan, pieces, state, hall, music, force=True)
        if job["phase"] != "full":
            return
        lengths = [p["samples"] for p in pieces]
        fixed = sum(lengths) / SAMPLE_RATE + LEAD_IN + SESSION_END["tail"]  # everything but the pauses
        scale = pause_scale(fixed, sum(item["pause_ms"] for item in plan[:-1]) / 1000)
        seconds = self._voice_track(folder / "voice.pcm", speech_file, plan, lengths, scale)
        mix(ffmpeg, folder / "voice.pcm", seconds, folder / "session.mp3", hall=hall, reverb=state["reverb"],
            music=music, music_gain=state["music_gain"], end=SESSION_END, stop=self._stop)
        (folder / "voice.pcm").unlink(missing_ok=True)
        state.update(pause_scale=scale, minutes=round((seconds + SESSION_END["tail"]) / 60), session_key=job["key"])
        self._write(folder / "job.json", state)
        speech_file.unlink(missing_ok=True)  # only the finished file is kept
        (folder / "pieces.json").unlink(missing_ok=True)
        self.progress[session_id] = 1.0
        log.info("Session %s: audio ready (%d minutes)", session_id[:6], state["minutes"])
        if self.on_ready is not None:
            self.on_ready(session_id)

    def _preview(self, ffmpeg, folder, plan, pieces, state, hall, music, force=False) -> None:
        """The first five minutes (at most), ending after a finished piece, once enough is spoken. Its pauses are as
        written (the whole session's length is not known yet)."""
        seconds, count = LEAD_IN, 0
        for index, piece in enumerate(pieces):
            if seconds + piece["samples"] / SAMPLE_RATE + PREVIEW_END["tail"] > PREVIEW_SECONDS:
                break
            seconds += piece["samples"] / SAMPLE_RATE + plan[index]["pause_ms"] / 1000
            count += 1
        else:
            if not force:
                return  # not yet five minutes spoken
        track = folder / "preview_voice.pcm"
        seconds = self._voice_track(track, folder / "speech.pcm", plan, [p["samples"] for p in pieces[:count]], 1.0)
        mix(ffmpeg, track, seconds, folder / "preview.mp3", hall=hall, reverb=state["reverb"], music=music,
            music_gain=state["music_gain"], end=PREVIEW_END, stop=self._stop)
        track.unlink(missing_ok=True)
        if self.on_preview is not None:
            self.on_preview(folder.name)

    @staticmethod
    def _voice_track(track: Path, speech_file: Path, plan, lengths: list[int], scale: float) -> float:
        """The lead-in, then the spoken pieces with their pauses (scaled), as one 24 kHz track; returns its length
        in seconds."""
        samples = round(LEAD_IN * SAMPLE_RATE)
        with speech_file.open("rb") as source, track.open("wb") as out:
            out.write(b"\x00\x00" * samples)
            for index, length in enumerate(lengths):
                out.write(source.read(length * 2))
                last = index == len(lengths) - 1  # the file's own ending follows the last piece
                pause = 0 if last else round(plan[index]["pause_ms"] / 1000 * scale * SAMPLE_RATE)
                out.write(b"\x00\x00" * pause)
                samples += length + pause
        return samples / SAMPLE_RATE


# --- a short sample to listen to: the practice voice as in a session, over the session's music -------------------

SAMPLE_INDUCTION = {  # a few hypnotic lines after the natural pre-talk, so both paces are heard
    "en": "Now, let your eyes close... and let your breathing slow down, all by itself. Each breath a little "
          "softer, a little deeper... and you can let go.",
    "de": "Lassen Sie jetzt Ihre Augen sich schließen... und Ihr Atem wird ganz von selbst ruhiger. Jeder Atemzug "
          "ein wenig sanfter, ein wenig tiefer... und Sie können loslassen.",
    "fr": "Maintenant, laissez vos yeux se fermer... et laissez votre respiration ralentir, toute seule. Chaque "
          "respiration un peu plus douce, un peu plus profonde... et vous pouvez lâcher prise.",
    "es": "Ahora, deje que sus ojos se cierren... y deje que su respiración se calme, por sí sola. Cada respiración "
          "un poco más suave, un poco más profunda... y puede soltarse.",
}


def sample(language: str, music_file: str | None = None) -> Path:
    """Makes data/voice/sample-<language>.mp3: a short pre-talk and induction in the practice voice, with the
    session's pace, soft starts and ends, reverb and background music, exactly as a client's preview is made.
    The music is the one chosen in the admin dashboard, or `music_file` (a file in data/voice)."""
    from .config import load_settings  # noqa: PLC0415
    from .db import Database  # noqa: PLC0415
    from .speech import PREVIEW_TEXTS, PracticeVoice  # noqa: PLC0415

    settings = load_settings()
    voice_dir = settings.data_dir / "voice"
    db = Database(settings.data_dir / "platform.sqlite3")
    chosen = db.get_setting("session_background") or {}
    music_name = music_file or chosen.get("file")
    music = voice_dir / music_name if music_name and (voice_dir / music_name).exists() else None
    if music_name and music is None:
        raise SystemExit(f"No such music file in {voice_dir}: {music_name}")
    voice = PracticeVoice(voice_dir)
    voice.start()
    if not voice.speaks(language):
        raise SystemExit(f"The practice voice is not prepared for {language!r} (see app/speech.py prepare)")
    script = f"## PRE-TALK\n{PREVIEW_TEXTS[language]}\n\n## INDUCTION\n{SAMPLE_INDUCTION[language]}"
    job = {"language": language, "phase": "preview", "script": script, "key": "sample",
           "session_rate": (db.get_setting("voice") or {}).get("session_rate", 0.8),
           "reverb": (db.get_setting("session_reverb") or {}).get("amount", 0.5),
           "music": str(music) if music else None,
           "music_gain": chosen.get("volume", 1.43) if music_name == chosen.get("file") else 1.43}
    work = settings.data_dir / "sessions"
    audio = SessionAudio(settings.data_dir, voice, lambda _id: job)
    sample_id = f"sample-{language}"
    shutil.rmtree(work / sample_id, ignore_errors=True)
    audio.make(sample_id, job)
    out = voice_dir / f"sample-{language}.mp3"
    shutil.move(str(work / sample_id / "preview.mp3"), out)
    shutil.rmtree(work / sample_id, ignore_errors=True)
    voice.close()
    print(f"Sample ({language}, music: {music_name or 'none'}) -> {out}")
    return out


if __name__ == "__main__":
    import argparse  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.session_audio sample",
                                     description="A short sample of the session voice over its music.")
    parser.add_argument("command", choices=["sample"])
    parser.add_argument("--language", default="en", choices=["en", "de", "fr", "es"])
    parser.add_argument("--music", help="a music file in data/voice (default: the one chosen in the dashboard)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    sample(args.language, args.music)
