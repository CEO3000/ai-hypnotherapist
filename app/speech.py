"""The practice voice: a real person's recorded voice (the clinician's, or a speaker who consented), spoken on
this computer by Kyutai's Pocket TTS (code MIT, model weights CC BY 4.0; https://github.com/kyutai-labs/pocket-tts).
No cloud service, no fees.

The voice is used exactly as recorded and as the model speaks it: nothing is cut, filtered or re-pitched. What is
done, all at the clinician's request:
- de-clicking: the model sometimes starts or ends a sentence with a tiny pop, within its first or last few
  milliseconds, before the first word and after the last; a 20 ms fade at each end of a sentence removes it;
- a stray sound the model sometimes makes before it speaks (a short mouth sound, then a gap, then the words; it
  is heard as a click) is replaced by silence (LeadIn);
- in the session only: its slower pace, by Signalsmith Stretch (MIT licence, free for any use; studio-quality
  time-stretching that keeps the pitch), and a soft start and smooth end to each piece (session_delivery.py:
  short enough not to sound like a fade, but the voice never starts or stops with a hard edge).
The consultation keeps the voice's natural speed. Everything runs here: no outside service, no licence fees.

Optional: without the pocket-tts package or a prepared voice, the apps fall back to the device's own voice.

Pocket TTS has a model for each language (LANGUAGES below). The voice is prepared once for each of them from the
same clean recording (needs the voice-cloning model, see `prepare` below):
    .venv\\Scripts\\python.exe -m app.speech prepare "data\\voice\\Clear sound.mp3"
A language whose voice is not prepared uses the device's own voice, and its session has no audio file.

Only text the practice itself produced is ever spoken in the practice voice (see `speakable`): a client
cannot make the voice say arbitrary words.
"""

import argparse
import asyncio
import importlib.util
import itertools
import logging
import queue
import re
import sys
import threading
import time
import wave
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable, Iterator
from concurrent.futures import Future
from functools import partial
from pathlib import Path

import numpy as np

log = logging.getLogger("app.speech")

VOICE_STATE_FILE = "practice_voice.safetensors"  # English; other languages: practice_voice.<code>.safetensors
PREVIEW_FILE = "preview.wav"
SAMPLE_RATE = 24_000
# The languages the practice voice can speak, and Pocket TTS's model for each (a voice is prepared for each model).
LANGUAGES = {"en": "english", "de": "german", "fr": "french", "es": "spanish"}
MAX_TEXT_CHARS = 600  # one sentence or script segment at a time
CACHE_ENTRIES = 300  # finished audio kept in memory only (replies are personal: never written to disk)
MIN_RATE, MAX_RATE = 0.5, 1.5
LIVE, BACKGROUND = 0, 1  # job priorities: a client waiting always goes first
EDGE_FADE = SAMPLE_RATE * 20 // 1000  # where the model's start/end pops are: before the first word, after the last
PREVIEW_TEXTS = {
    "en": "Take a moment to settle in. There's no rush at all. "
          "When did you first notice this feeling, and what was happening in your life around that time?",
    "de": "Nehmen Sie sich einen Moment, um anzukommen. Es gibt überhaupt keine Eile. "
          "Wann haben Sie dieses Gefühl zum ersten Mal bemerkt, und was war damals in Ihrem Leben los?",
    "fr": "Prenez un moment pour vous installer. Rien ne presse. "
          "Quand avez-vous remarqué ce sentiment pour la première fois, "
          "et que se passait-il dans votre vie à ce moment-là ?",
    "es": "Tómese un momento para acomodarse. No hay ninguna prisa. "
          "¿Cuándo notó este sentimiento por primera vez, y qué estaba pasando en su vida en ese momento?",
}


def state_file(language: str) -> str:
    return VOICE_STATE_FILE if language == "en" else f"practice_voice.{language}.safetensors"


def preview_file(language: str) -> str:
    return PREVIEW_FILE if language == "en" else f"preview.{language}.wav"


def spoken_form(text: str) -> str:
    """Text as the apps speak it: headings, quotes and emphasis marks removed, whitespace collapsed
    (mirrors cleanForSpeech and sessionSegments in static/voice.js)."""
    lines = [line for line in text.splitlines() if not re.match(r"\s*(#|>)", line)]
    return re.sub(r"\s+", " ", " ".join(lines).replace("*", "")).strip()


def sentences(text: str) -> list[str]:
    """How the apps split a reply into sentences (mirrors speakReply in static/voice.js)."""
    return [s.strip() for s in re.findall(r"[^.!?]+[.!?]*", spoken_form(text)) if s.strip()]


def speakable(text: str, sources: Iterable[str]) -> bool:
    """Whether `text` is part of something the practice produced (a message, the session script or a fixed phrase)."""
    needle = spoken_form(text)
    return bool(needle) and len(needle) <= MAX_TEXT_CHARS and any(needle in spoken_form(s) for s in sources)


def pocket_tts_installed() -> bool:
    return importlib.util.find_spec("pocket_tts") is not None


PEAK = 0.94  # -0.5 dBFS: the loudest a sample may be (the voice's rare peaks are never cut off, which crackles)


def to_pcm(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1, 1) * 32767).round().astype("<i2").tobytes()


def within_peak(audio: np.ndarray) -> np.ndarray:
    """A whole piece: turned down just enough that its loudest moment stays under PEAK (no distortion at all)."""
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    return audio * (PEAK / peak) if peak > PEAK else audio


def soft_limit(audio: np.ndarray) -> np.ndarray:
    """Streaming speech (its loudest moment not yet known): anything above PEAK is rounded off smoothly into the
    last 0.5 dB instead of being cut off. Leaves everything below PEAK exactly as it is."""
    over = np.abs(audio) > PEAK
    if not over.any():
        return audio
    out = audio.copy()
    excess = np.abs(out[over]) - PEAK
    out[over] = np.sign(out[over]) * (PEAK + (0.999 - PEAK) * np.tanh(excess / (1 - PEAK)))  # never reaches full scale
    return out


class LeadIn:
    """The start of a sentence, held until the words begin. The model sometimes makes a stray sound before it speaks
    (a short, quiet mouth sound, then a clear gap, then the words), heard as a click: that sound is replaced by
    silence. Real words are never removed: even a short one ("Now,", "So,") runs on into the sentence without such
    a gap, and anything longer or louder than a stray sound counts as speech at once, so it is not held."""

    FRAME = SAMPLE_RATE // 100  # 10 ms
    QUIET_DB = -55.0  # dBFS: below this is silence
    STRAY_MAX_MS = 120  # a stray sound is shorter than a word
    STRAY_MAX_DB = -22.0  # and quieter than speech
    GAP_MS = 100  # and followed by at least this much silence

    def __init__(self):
        self.held: list[np.ndarray] = []
        self.speaking = False

    def feed(self, chunk: np.ndarray) -> np.ndarray | None:
        """The audio that can go out now (None while still deciding)."""
        if self.speaking:
            return chunk
        self.held.append(chunk)
        audio = np.concatenate(self.held)
        if self._examine(audio):
            self.speaking = True
            self.held = []
            return audio
        self.held = [audio]
        return None

    def finish(self) -> np.ndarray | None:
        """Whatever is still held when the sentence ends (a sentence that never clearly began: kept as it is)."""
        audio = np.concatenate(self.held) if self.held else None
        self.held = []
        return audio

    def _examine(self, audio: np.ndarray) -> bool:
        """True once the words have begun; stray sounds before them are silenced in place."""
        frames = len(audio) // self.FRAME
        level = 10 * np.log10(np.mean(audio[:frames * self.FRAME].reshape(frames, self.FRAME) ** 2, axis=1) + 1e-12)
        sound = level > self.QUIET_DB
        stray_frames, gap_frames = self.STRAY_MAX_MS // 10, self.GAP_MS // 10
        i = 0
        while i < frames:
            if not sound[i]:
                i += 1
                continue
            end = i
            while end < frames and sound[end]:
                end += 1
            if end - i > stray_frames or level[i:end].max() > self.STRAY_MAX_DB:
                return True  # speech
            if end == frames:
                return False  # a short, quiet sound so far: wait to see what follows
            gap_end = end
            while gap_end < frames and not sound[gap_end]:
                gap_end += 1
            if gap_end - end < gap_frames:
                if gap_end == frames:
                    return False  # the gap so far is short: wait
                return True  # the sound runs on into more sound: the start of a word
            audio[i * self.FRAME:end * self.FRAME] = 0  # a stray sound before the words
            i = gap_end
        return False


def _then(pieces: Iterable[np.ndarray], last) -> Iterator[np.ndarray]:
    """The pieces, then whatever `last()` returns (if anything) once they are used up."""
    yield from pieces
    if (tail := last()) is not None:
        yield tail


def fade_edge(audio: np.ndarray, start: bool) -> None:
    """A 20 ms raised-cosine fade at the start or end of a sentence (in place): removes the model's pop there."""
    count = min(EDGE_FADE, len(audio))
    ramp = (0.5 - 0.5 * np.cos(np.linspace(0, np.pi, count))).astype(np.float32)
    if start:
        audio[:count] *= ramp
    else:
        audio[len(audio) - count:] *= ramp[::-1]


def slow_down(audio: np.ndarray, rate: float) -> np.ndarray:
    """A finished sentence at `rate` of its speed with its pitch unchanged, by Signalsmith Stretch (MIT licence; the
    python-stretch package). Without it the sentence keeps its natural speed: never a lower-quality substitute."""
    try:
        import python_stretch  # noqa: PLC0415 - optional, like the voice itself
    except ImportError:
        log.warning("Practice voice: python-stretch is not installed, so the session keeps the natural speed")
        return audio
    stretch = python_stretch.Signalsmith.Stretch()
    stretch.preset(1, SAMPLE_RATE)
    stretch.timeFactor = min(MAX_RATE, max(MIN_RATE, rate))
    return stretch.process(audio[None, :].astype(np.float32))[0]


SWELL_DB = 24  # a rise or settle spans this much loudness, evenly: the first syllable soft, but still heard


def swell(count: int) -> np.ndarray:
    """Gains rising evenly in loudness (the same number of dB every moment) from true silence to full: the ear
    hears a steady, gentle rise, where a cosine curve lingers near silence and then jumps up."""
    gain = 10 ** (np.linspace(-SWELL_DB, 0, count) / 20)
    lead = max(1, count // 20)
    gain[:lead] *= np.linspace(0, 1, lead)  # the first moment starts from exactly nothing
    return gain.astype(np.float32)


def soften(audio: np.ndarray, rise_ms: int, settle_ms: int) -> None:
    """In place: a short fade where the voice begins (rise_ms) and where it ends (settle_ms), placed on the voice
    itself rather than on the silence around it. Only the volume changes, and only there."""
    hop = SAMPLE_RATE // 100
    frames = len(audio) // hop
    if frames == 0:
        return
    level = 10 * np.log10(np.mean(audio[:frames * hop].reshape(frames, hop) ** 2, axis=1) + 1e-12)
    heard = np.flatnonzero(level > level.max() - 35)  # where the voice is, as opposed to the silence around it
    begin = max(0, heard[0] * hop - SAMPLE_RATE // 50)
    end = min(len(audio), (heard[-1] + 1) * hop + SAMPLE_RATE // 50)  # both fades sit on the voice itself
    rise = min(SAMPLE_RATE * rise_ms // 1000, (end - begin) // 2)
    settle = min(SAMPLE_RATE * settle_ms // 1000, (end - begin) // 2)
    audio[:begin] = 0
    audio[begin:begin + rise] *= swell(rise)
    audio[end - settle:end] *= swell(settle)[::-1]
    audio[end:] = 0


def prefer_this_process() -> None:
    """On a computer that also runs the app's browser (the Windows test set-up), speaking goes before the browser's
    animations: the server runs at above-normal priority. Servers without a desktop are left as they are."""
    if sys.platform != "win32":
        return
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    above_normal = 0x00008000
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE  # a 64-bit handle: the default int would corrupt it
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    if kernel.SetPriorityClass(kernel.GetCurrentProcess(), above_normal):
        log.info("Practice voice: server running at above-normal priority")
    else:
        log.warning("Practice voice: could not raise the server's priority (error %s)", ctypes.get_last_error())


class PracticeVoice:
    """Speaks with the prepared practice voice: one text at a time on a single worker thread, each streamed as
    16-bit mono PCM at 24 kHz while it is generated. A client waiting always goes before background work."""

    def __init__(self, voice_dir: Path):
        self.voice_dir = voice_dir
        self.state_path = voice_dir / VOICE_STATE_FILE
        self.failed: str | None = None  # why the voice could not be loaded at all
        self.failed_languages: dict[str, str] = {}  # why one language's model or voice could not be loaded
        self._model = None  # English
        self._voice = None
        self._others: dict[str, tuple] = {}  # the other languages: code -> (model, voice)
        self._jobs: queue.PriorityQueue = queue.PriorityQueue()
        self._order = itertools.count()
        self._worker: threading.Thread | None = None
        self._background: threading.Event | None = None  # lets a waiting client interrupt background work
        self._cache: OrderedDict[tuple, bytes] = OrderedDict()
        self._cache_lock = threading.Lock()  # the cache is filled from the worker thread and the event loop

    @property
    def available(self) -> bool:
        """The English practice voice (the clinician's own language) is ready to use."""
        return self.failed is None and self.state_path.exists() and pocket_tts_installed()

    def prepared(self) -> list[str]:
        """The languages whose voice has been prepared from the recording."""
        return [code for code in LANGUAGES if (self.voice_dir / state_file(code)).exists()]

    def speaks(self, language: str) -> bool:
        """Whether the practice voice can speak this language (otherwise the device's own voice is used)."""
        if language == "en":
            return self.available
        return (self.available and language in LANGUAGES and language not in self.failed_languages
                and (self.voice_dir / state_file(language)).exists())

    def start(self, warm_up: dict[str, Iterable[str]] | Iterable[str] = (), rate: float = 1.0) -> None:
        """Load the models (takes seconds for each language: call before accepting clients), then pre-speak fixed
        phrases (a list of English ones, or {language: phrases}) in the background so they play without delay."""
        if not self.available:
            return
        started = time.monotonic()
        logging.getLogger("pocket_tts").setLevel(logging.WARNING)  # it logs several lines for every sentence
        try:
            from pocket_tts import TTSModel  # noqa: PLC0415 - heavy optional import (PyTorch)
            self._model = TTSModel.load_model()
            self._voice = self._model.get_state_for_audio_prompt(self.state_path)
        except Exception as exc:  # noqa: BLE001 - any failure means: use the device voice instead
            self.failed = f"{type(exc).__name__}: {exc}"
            log.exception("Practice voice could not be loaded; the apps will use the device voice")
            return
        for code in self.prepared():
            if code == "en":
                continue
            try:
                model = TTSModel.load_model(language=LANGUAGES[code])
                self._others[code] = (model, model.get_state_for_audio_prompt(self.voice_dir / state_file(code)))
            except Exception as exc:  # noqa: BLE001 - that language uses the device voice instead
                self.failed_languages[code] = f"{type(exc).__name__}: {exc}"
                log.exception("Practice voice (%s) could not be loaded; that language uses the device voice", code)
        log.info("Practice voice ready in %.1f s (%s)", time.monotonic() - started,
                 ", ".join(code for code in LANGUAGES if self.speaks(code)))
        prefer_this_process()
        phrases = warm_up if isinstance(warm_up, dict) else {"en": warm_up}
        for code, texts in phrases.items():
            if self.speaks(code):
                self.warm(texts, rate, code)

    def warm(self, texts: Iterable[str], rate: float, language: str = "en") -> None:
        for text in dict.fromkeys(texts):
            order = next(self._order)
            self._submit(BACKGROUND, partial(self._warm, text, rate, order, language), order)

    def close(self) -> None:
        if self._worker is not None:
            self._jobs.put((-1, -1, None))
        if (background := self._background) is not None:
            background.set()

    def _submit(self, priority: int, job, order: int | None = None) -> None:
        if priority == LIVE and (background := self._background) is not None:
            background.set()  # pause the background work; it is picked up again afterwards
        if self._worker is None:
            self._worker = threading.Thread(target=self._work, name="practice-voice", daemon=True)
            self._worker.start()
        self._jobs.put((priority, next(self._order) if order is None else order, job))

    def _work(self) -> None:
        while True:
            _, _, job = self._jobs.get()
            if job is None:
                return
            try:
                job()
            except Exception:  # noqa: BLE001 - one failed job must not stop the voice
                log.exception("Practice voice job failed")

    def _speak(self, text: str, rate: float, stop: threading.Event,
               soften_ms: tuple[int, int] | None = None, language: str = "en") -> Iterator[bytes]:
        """The model's speech for `text` as generated, without a stray sound before the words, de-clicked at both
        ends. In the consultation it streams as it comes (held only until the words begin, then one piece behind,
        so the end of the sentence is known). In the session (a pace other than natural, or a gentle rise and
        settle) the whole piece is generated first, then shaped as one."""
        model, voice = (self._model, self._voice) if language == "en" else self._others.get(language, (None, None))
        if model is None:
            raise RuntimeError(self.failed or self.failed_languages.get(language)
                               or f"the practice voice ({language}) is not loaded")
        lead_in = LeadIn()
        generated = (np.array(c.numpy(), dtype=np.float32) for c in model.generate_audio_stream(voice, text, stop=stop))
        ready = (piece for chunk in generated if (piece := lead_in.feed(chunk)) is not None)
        if abs(rate - 1) >= 0.01 or soften_ms:
            audio = np.concatenate([*_then(ready, lead_in.finish), np.zeros(0, np.float32)])
            if len(audio) and not stop.is_set():
                fade_edge(audio, start=True)
                fade_edge(audio, start=False)
                if abs(rate - 1) >= 0.01:
                    audio = slow_down(audio, rate)
                    fade_edge(audio, start=True)  # the time-stretcher's own faint start and end, too
                    fade_edge(audio, start=False)
                if soften_ms:
                    soften(audio, *soften_ms)
                yield to_pcm(within_peak(audio))
            return
        held = None
        for chunk in _then(ready, lead_in.finish):
            if held is None:
                fade_edge(chunk, start=True)
            else:
                yield to_pcm(soft_limit(held))
            held = chunk
        if held is not None and not stop.is_set():
            fade_edge(held, start=False)
            yield to_pcm(soft_limit(held))

    def render(self, text: str, rate: float, soften_ms: tuple[int, int] | None = None,
               language: str = "en") -> bytes:
        """A piece for a session's audio file: spoken on the voice's worker after any client who is waiting (if one
        arrives in the middle, the piece is spoken again afterwards). Waits until it is done."""
        done: Future = Future()
        order = next(self._order)

        def job():
            stop = threading.Event()
            self._background = stop
            try:
                audio = b"".join(self._speak(text, rate, stop, soften_ms, language))
            except Exception as exc:  # noqa: BLE001 - handed to the caller
                done.set_exception(exc)
                return
            finally:
                self._background = None
            if stop.is_set():
                self._submit(BACKGROUND, job, order)
            else:
                done.set_result(audio)

        self._submit(BACKGROUND, job, order)
        return done.result()

    def _warm(self, text: str, rate: float, order: int, language: str = "en") -> None:
        key = (text, round(rate, 3), None, language)
        if self._model is None or self._cached(key) is not None:
            return
        stop = threading.Event()
        self._background = stop
        try:
            audio = b"".join(self._speak(text, rate, stop, None, language))
        finally:
            self._background = None
        if stop.is_set():  # a client needed the voice: carry on afterwards, in the same place in the queue
            self._submit(BACKGROUND, partial(self._warm, text, rate, order, language), order)
        else:
            self._remember(key, audio)

    def _cached(self, key: tuple) -> bytes | None:
        with self._cache_lock:
            audio = self._cache.get(key)
            if audio is not None:
                self._cache.move_to_end(key)
            return audio

    def _remember(self, key: tuple, audio: bytes) -> None:
        with self._cache_lock:
            self._cache[key] = audio
            self._cache.move_to_end(key)
            while len(self._cache) > CACHE_ENTRIES:
                self._cache.popitem(last=False)

    async def stream(self, text: str, rate: float = 1.0, soften_ms: tuple[int, int] | None = None,
                     language: str = "en") -> AsyncIterator[bytes]:
        """PCM chunks for `text` as they are generated. Stops generating if the listener goes away."""
        key = (text, round(rate, 3), soften_ms, language)
        if (cached := self._cached(key)) is not None:
            yield cached
            return
        loop = asyncio.get_running_loop()
        chunks: asyncio.Queue = asyncio.Queue()
        stop = threading.Event()

        def work():
            try:
                for chunk in self._speak(text, rate, stop, soften_ms, language):
                    loop.call_soon_threadsafe(chunks.put_nowait, chunk)
            except Exception as exc:  # noqa: BLE001 - handed to the request below
                loop.call_soon_threadsafe(chunks.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(chunks.put_nowait, None)

        self._submit(LIVE, work)
        parts = []
        try:
            while (item := await chunks.get()) is not None:
                if isinstance(item, Exception):
                    raise item
                parts.append(item)
                yield item
            if not stop.is_set():
                self._remember(key, b"".join(parts))
        finally:
            stop.set()


# --- preparing the voice from a recording ------------------------------------------------------------------


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm)


def prepare(recording: Path, voice_dir: Path, languages: Iterable[str] = tuple(LANGUAGES)) -> None:
    """The practice voice for each language from the recording exactly as provided (the model itself reads it as
    mono, 24 kHz), with a short preview of each to listen to."""
    from pocket_tts import TTSModel, export_model_state  # noqa: PLC0415

    voice_dir.mkdir(parents=True, exist_ok=True)
    for code in languages:
        model = TTSModel.load_model(language=LANGUAGES[code])
        if not model.has_voice_cloning:
            raise SystemExit(
                "The voice-cloning model could not be downloaded: either this computer is not logged in to Hugging "
                "Face with an account that accepted the terms at https://huggingface.co/kyutai/pocket-tts (log in "
                "with .venv\\Scripts\\hf.exe auth login), or the internet connection dropped (just try again).")
        started = time.monotonic()
        state = voice_dir / state_file(code)
        export_model_state(model.get_state_for_audio_prompt(recording), state)
        print(f"Practice voice ({code}) made from {recording.name} in {time.monotonic() - started:.1f} s -> {state}")

        voice = PracticeVoice(voice_dir)
        voice._model, voice._voice = model, model.get_state_for_audio_prompt(state)
        started = time.monotonic()
        pause = b"\x00\x00" * int(0.4 * SAMPLE_RATE)
        audio = pause.join(b"".join(voice._speak(s, 1.0, threading.Event())) for s in sentences(PREVIEW_TEXTS[code]))
        took = time.monotonic() - started
        write_wav(voice_dir / preview_file(code), audio)
        length = len(audio) / 2 / SAMPLE_RATE
        print(f"Preview ({code}): {length:.1f} s of speech generated in {took:.1f} s ({length / took:.2f}x real time) "
              f"-> {voice_dir / preview_file(code)}")


def main(argv: list[str] | None = None) -> None:
    from .config import load_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.speech", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare", help="create the practice voice from a clean recording, used as it is")
    make.add_argument("recording", type=Path)
    make.add_argument("--languages", nargs="+", choices=list(LANGUAGES), default=list(LANGUAGES),
                      help="the languages to prepare (default: all)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    if args.command == "prepare":
        prepare(args.recording, load_settings().data_dir / "voice", args.languages)


if __name__ == "__main__":
    main(sys.argv[1:])
