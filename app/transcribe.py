"""Speech to text on this computer, by SYSTRAN's faster-whisper (MIT licence; the model weights come from
OpenAI's Whisper, released under the MIT licence). No cloud service, no fees.

The browser records the client's answer with the MediaRecorder API and sends the audio here; this module turns it
back into words. That way listening does not depend on the Web Speech API, which only works reliably in one
browser (Edge) and not at all over plain HTTP in Chrome.

Optional, like the practice voice: without the faster-whisper package the apps fall back to the browser's own
speech recognition (see static/voice.js).

Install it with:
    .venv\\Scripts\\python.exe -m pip install -r requirements-voice.txt
"""

import importlib.util
import io
import logging
import os
import threading

log = logging.getLogger("app.transcribe")

MODEL_SIZE = "base.en"  # English: small enough to run on a laptop while someone waits; accurate for a conversation
# Every other language: Whisper's multilingual model. "small" (about 480 MB) is the smallest that is reliably
# accurate in German, French and Spanish; TRANSCRIBE_MULTILINGUAL_MODEL in .env can choose another.
MULTILINGUAL_MODEL = os.getenv("TRANSCRIBE_MULTILINGUAL_MODEL", "").strip() or "small"
MAX_AUDIO_BYTES = 12 * 1024 * 1024  # ~12 MB, minutes of speech: longer than any single spoken answer
MAX_SECONDS = 120  # an answer longer than this is cut, so a stuck recording cannot hold a consultation up
SAMPLE_RATE = 16_000  # what Whisper expects, whatever the browser recorded


class Invalid(Exception):
    """The recording cannot be turned into words (empty, too long, or an unreadable format)."""


_models: dict[str, object] = {}  # model size -> the loaded model
_model_lock = threading.Lock()
_failed: str | None = None  # why the English model could not be loaded; not retried on every answer
_failed_multilingual: str | None = None  # the same for the other languages' model


def model_for(language: str | None) -> str:
    return MODEL_SIZE if language in (None, "en") else MULTILINGUAL_MODEL


def installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def _load(size: str = MODEL_SIZE):
    """A model, loaded once and kept (loading takes a few seconds; the first answer may wait for it)."""
    if (model := _models.get(size)) is not None:
        return model
    with _model_lock:
        if size not in _models:
            from faster_whisper import WhisperModel  # noqa: PLC0415

            # CPU with int8: quick enough for a conversation and needs no graphics card. One worker: one answer
            # is transcribed at a time, which is all a consultation ever needs.
            _models[size] = WhisperModel(size, device="cpu", compute_type="int8",
                                         cpu_threads=max(1, (os.cpu_count() or 2) // 2), num_workers=1)
            log.info("Speech to text ready (%s)", size)
    return _models[size]


def available(language: str | None = "en") -> bool:
    """Whether speech to text can be used for this language (the package is installed and its model loads)."""
    global _failed, _failed_multilingual
    english = model_for(language) == MODEL_SIZE
    if not installed() or (_failed if english else _failed_multilingual) is not None:
        return False
    if model_for(language) in _models:
        return True
    try:
        _load(model_for(language))
    except Exception as exc:  # a missing or unreadable model must not break the app
        if english:
            _failed = str(exc)
        else:
            _failed_multilingual = str(exc)
        log.warning("Speech to text (%s) is unavailable (the browser's own speech recognition is used instead): %s",
                    model_for(language), exc)
        return False
    return True


def usable(language: str | None = "en") -> bool:
    """Whether the apps should record answers for this server to transcribe: installed, and not known to fail."""
    return installed() and (_failed if model_for(language) == MODEL_SIZE else _failed_multilingual) is None


def warm_up(languages=("en",)) -> None:
    """Loads the models in the background when the app starts, so the first spoken answer does not wait for them
    (the first time, they are downloaded: about 150 MB for English, 480 MB for the other languages)."""
    if not installed():
        return
    wanted = {model_for(language): language for language in languages}
    for language in wanted.values():
        if model_for(language) not in _models:
            threading.Thread(target=available, args=(language,), name="speech-to-text", daemon=True).start()


def transcribe(audio: bytes, language: str | None = None) -> str:
    """The words in a recording. Rejects Invalid for audio that cannot be used; the caller turns that into a
    proper HTTP error."""
    if not audio:
        raise Invalid("the recording was empty")
    if len(audio) > MAX_AUDIO_BYTES:
        raise Invalid("the recording was too long")
    model = _load(model_for(language))
    try:
        segments, _ = model.transcribe(
            io.BytesIO(audio),
            language=language,
            beam_size=5,
            vad_filter=True,  # drop the silences, so a pause does not become words
            condition_on_previous_text=False,  # each answer stands alone
        )
        # transcribe() is lazy: the transcription happens as the segments are read.
        text = " ".join(segment.text.strip() for segment in segments if segment.end < MAX_SECONDS)
    except Invalid:
        raise
    except Exception as exc:
        log.warning("Could not transcribe a recording: %s", exc)
        raise Invalid("the recording could not be read") from exc
    return " ".join(text.split())
