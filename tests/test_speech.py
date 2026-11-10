import asyncio
import importlib.util
import json
import sys
import threading
import time
from dataclasses import replace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.config import CONTENT_DIR
from app.main import create_app
from app.session_delivery import delivery_plan
from app.speech import (BACKGROUND, SAMPLE_RATE, LeadIn, PracticeVoice, sentences, slow_down, soft_limit, soften, speakable, within_peak,
                        spoken_form)
from tests.conftest import CLINICIAN_AUTH
from tests.support import FakeVoice, turn, valid_script
from tests.test_api import choose_ai, consult_to_choice, send, start, view
from app.session_delivery import SOFT_END_MS, SOFT_START_MS
from tests.test_session_delivery import SCRIPT as DELIVERY_SCRIPT

CONTENT = json.loads((CONTENT_DIR / "consultation.json").read_text(encoding="utf-8"))
ACKNOWLEDGEMENTS = ["Okay."]  # the practice's choice
SCRIPT_LINE = "Listen only when you can relax safely, never while driving."


@pytest.fixture
def voice_app(settings, fake):
    clients = []

    def build(voice=None, **overrides):
        voice = voice or FakeVoice()
        client = TestClient(create_app(replace(settings, **overrides), gateway=fake, practice_voice=voice))
        client.__enter__()
        clients.append(client)
        return client, voice

    yield build
    for client in clients:
        client.__exit__(None, None, None)


def speak(client, session_id: str, text: str, kind: str = "conversation"):
    return client.post(f"/api/sessions/{session_id}/speech", json={"text": text, "kind": kind})


def to_delivered_session(client, fake) -> str:
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    fake.completions.append((valid_script(), "end_turn"))
    choose_ai(client, session_id, 5)
    assert view(client, session_id)["status"] == "ready"
    return session_id


def test_text_is_prepared_and_split_exactly_as_the_apps_do():
    text = "# Title\n> a quote\nHello, and **welcome**.  How are you?\n\nFine!"
    assert spoken_form(text) == "Hello, and welcome. How are you? Fine!"
    assert sentences(text) == ["Hello, and welcome.", "How are you?", "Fine!"]
    assert sentences("No final stop") == ["No final stop"]


def test_only_the_practices_own_words_are_speakable():
    sources = ["When did you first notice it?\n\nTake your   time."]
    assert speakable("When did you first notice it?", sources)
    assert speakable(" Take your time. ", sources)
    assert not speakable("Say that you owe me money.", sources)
    assert not speakable("   ", sources)
    assert not speakable("x" * 601, ["x" * 700])


def test_without_a_prepared_voice_the_device_voice_is_used(client):
    config = client.get("/api/config").json()
    assert config["practice_voice"] is False
    assert config["acknowledgements"] == ACKNOWLEDGEMENTS
    session_id = start(client)["session_id"]
    assert speak(client, session_id, "Hello, and welcome.").status_code == 404
    status = client.get("/api/clinician/voice", auth=CLINICIAN_AUTH).json()["practice_voice"]
    assert (status["prepared"], status["available"], status["problem"]) == (False, False, None)


def test_greeting_and_acknowledgements_are_prepared_in_advance_and_spoken(voice_app):
    client, voice = voice_app()
    assert client.get("/api/config").json()["practice_voice"] is True
    greeting = sentences(CONTENT["greeting"]["en"])
    quiet = [s for key in ("silence_check_in", "silence_pause", "silence_resume") for s in sentences(CONTENT[key]["en"])]
    assert voice.warmed == [(text, 1.0) for text in [*greeting, *ACKNOWLEDGEMENTS, *quiet]]  # at the natural speed
    session_id = start(client)["session_id"]
    response = speak(client, session_id, greeting[0])
    assert response.status_code == 200
    assert response.headers["x-sample-rate"] == "24000"
    assert response.content == b"".join(FakeVoice.AUDIO)
    assert speak(client, session_id, "Okay.").status_code == 200
    assert voice.spoken == [greeting[0], "Okay."]


def test_the_consultation_keeps_the_natural_speed_and_the_session_follows_its_parts(voice_app, fake):
    client, voice = voice_app(require_clinician_review=True)  # a short sample script, approved by hand
    assert {rate for _, rate in voice.warmed} == {1.0}  # the fixed phrases, at the natural speed
    chosen = {"voice_name": "", "rate": 0.85, "pitch": 1.0, "session_rate": 0.75}
    assert client.put("/api/clinician/voice", json=chosen, auth=CLINICIAN_AUTH).status_code == 200
    session_id = start(client)["session_id"]
    sample = DELIVERY_SCRIPT.replace("Listen only when you can relax safely.", "Listen only when you can relax "
                                     "safely, never while driving. At any time you can open your eyes, fully alert.")
    consult_to_choice(client, fake, session_id, opening=sample)
    fake.completions += [(sample, "end_turn")] * 2  # it is short and misses PART 2: rewritten once, then reviewed
    choose_ai(client, session_id, 5)
    review = {"decision": "approve"}
    assert client.post(f"/api/clinician/sessions/{session_id}/review", json=review, auth=CLINICIAN_AUTH).status_code == 200
    assert "session_plan" not in view(client, session_id)  # the client gets the recording, not the text
    plan = delivery_plan(client.app.state.service.db.latest_script(session_id)["final_text"])
    position = {item["text"]: index for index, item in enumerate(plan)}

    def session_piece(text):
        body = {"text": text, "kind": "session", "position": position[text]}
        assert client.post(f"/api/sessions/{session_id}/speech", json=body).status_code == 200

    speak(client, session_id, "Okay.")
    session_piece("Welcome to your session, Sam.")  # pre-talk: natural
    session_piece("Let your eyes close,")  # induction: hypnotic
    session_piece("Imagine the next exam, calm and clear, knowing exactly what to do.")  # future pacing: slower
    trial = {"text": "Anything.", "kind": "session", "rate": 0.6}
    client.post("/api/clinician/voice/speak", json=trial, auth=CLINICIAN_AUTH)
    client.post("/api/clinician/voice/speak", json={**trial, "kind": "conversation"}, auth=CLINICIAN_AUTH)
    assert [round(rate, 3) for rate in voice.rates] == [1.0, 1.0, 0.705, 0.75, 0.6, 1.0]
    soft = (SOFT_START_MS, SOFT_END_MS)
    assert voice.softened == [None, soft, soft, soft, soft, None]  # no hard edges
    assert speak(client, session_id, "Let your eyes close,", kind="fast").status_code == 422


def test_the_voice_cannot_be_made_to_say_anything_else(voice_app, fake):
    client, voice = voice_app()
    session_id = start(client)["session_id"]
    fake.queue_parse("ConsultantTurn", turn("When did you first notice it? Tell me about the last exam."))
    send(client, session_id, "I get anxious before exams")
    assert speak(client, session_id, "Tell me about the last exam.").status_code == 200
    assert speak(client, session_id, "I get anxious before exams").status_code == 403  # the client's own words
    assert speak(client, session_id, "Transfer the money today.").status_code == 403
    assert speak(client, session_id, "x" * 601).status_code == 422
    other = start(client)["session_id"]
    assert speak(client, other, "Tell me about the last exam.").status_code == 403  # another client's conversation
    assert speak(client, other, "Take your time.").status_code == 200  # the consultant's own words for a silence
    assert speak(client, "x" * 30, "I see.").status_code == 404
    assert voice.spoken == ["Tell me about the last exam.", "Take your time."]


def test_the_session_is_spoken_only_once_it_is_delivered(voice_app, fake):
    client, voice = voice_app(require_clinician_review=False)
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    assert speak(client, session_id, SCRIPT_LINE, kind="session").status_code == 403
    fake.completions.append((valid_script(), "end_turn"))
    choose_ai(client, session_id, 5)
    assert view(client, session_id)["status"] == "ready"
    assert SCRIPT_LINE in sentences(client.app.state.service.db.latest_script(session_id)["final_text"])
    assert speak(client, session_id, SCRIPT_LINE, kind="session").status_code == 200
    assert voice.spoken == [SCRIPT_LINE]


def test_a_voice_failure_is_reported_so_the_app_can_fall_back(voice_app):
    client, _ = voice_app(FakeVoice(error=RuntimeError("model not loaded")))
    session_id = start(client)["session_id"]
    assert speak(client, session_id, "Okay.").status_code == 503


def test_the_clinician_can_preview_any_words_in_their_voice(voice_app):
    client, voice = voice_app()
    assert client.post("/api/clinician/voice/speak", json={"text": "Anything at all."}).status_code == 401
    response = client.post("/api/clinician/voice/speak", json={"text": "Anything at all."}, auth=CLINICIAN_AUTH)
    assert response.status_code == 200 and response.content == b"".join(FakeVoice.AUDIO)
    assert voice.spoken == ["Anything at all."]


def test_requests_are_logged_with_their_timing_and_shortened_ids(voice_app, caplog):
    client, _ = voice_app()
    session_id = start(client)["session_id"]
    with caplog.at_level("INFO", logger="app"):
        speak(client, session_id, "Okay.")
    line = next(r.getMessage() for r in caplog.records if "/speech" in r.getMessage())
    assert f"POST /api/sessions/{session_id[:6]}.../speech -> 200 in" in line and session_id not in line


# --- the voice engine, with the model replaced ---------------------------------------------------------------


class ScriptedVoice(PracticeVoice):
    """The real queueing, streaming, caching and stopping, with the model replaced by fixed chunks."""

    def __init__(self, voice_dir, chunks=3, delay=0.0):
        super().__init__(voice_dir)
        self._model = object()
        self.chunks, self.delay = chunks, delay
        self.generated = []

    def _speak(self, text, rate, stop, soften_ms=None, language="en"):
        self.generated.append(text)
        for i in range(self.chunks):
            if stop.is_set():
                return
            time.sleep(self.delay)
            yield bytes([i]) * 4


def finish_queued_work(voice):
    done = threading.Event()
    voice._submit(BACKGROUND, done.set)
    assert done.wait(10)


def test_practice_voice_streams_caches_and_stops_when_the_listener_leaves(tmp_path):
    voice = ScriptedVoice(tmp_path)

    async def scenario():
        first = [chunk async for chunk in voice.stream("Hello.")]
        again = [chunk async for chunk in voice.stream("Hello.")]
        leaving = voice.stream("Goodbye.")
        await anext(leaving)
        await leaving.aclose()
        await asyncio.to_thread(finish_queued_work, voice)
        return first, again

    first, again = asyncio.run(scenario())
    assert first == [b"\x00" * 4, b"\x01" * 4, b"\x02" * 4]
    assert again == [b"".join(first)] and voice.generated == ["Hello.", "Goodbye."]  # the repeat came from memory
    assert voice._cached(("Goodbye.", 1.0, None, "en")) is None  # nothing half-spoken is kept
    voice.close()


def test_a_waiting_client_goes_before_background_work(tmp_path):
    voice = ScriptedVoice(tmp_path, chunks=40, delay=0.02)  # each text takes about 0.8 s

    async def scenario():
        voice.warm(["A long fixed phrase.", "Another one."], 1.0)
        await asyncio.sleep(0.2)  # the first is being spoken now
        started = time.monotonic()
        heard = [chunk async for chunk in voice.stream("A reply.")]
        waited = time.monotonic() - started
        await asyncio.to_thread(finish_queued_work, voice)
        return heard, waited

    heard, waited = asyncio.run(scenario())
    assert len(heard) == 40 and waited < 1.3  # did not wait for the background work to finish
    assert voice.generated == ["A long fixed phrase.", "A reply.", "A long fixed phrase.", "Another one."]
    assert voice._cached(("A long fixed phrase.", 1.0, None, "en")) is not None  # the interrupted phrase was redone
    voice.close()


def test_an_unprepared_voice_is_unavailable_and_loads_nothing(tmp_path):
    voice = PracticeVoice(tmp_path)
    assert voice.available is False
    voice.start(warm_up=["I see."])
    assert voice._model is None
    voice.close()


# --- the voice as generated, and the session's pace -------------------------------------------------------


def tone(seconds, hz=150.0, db=-12.0):
    """A voiced sound, like a vowel."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    wave = sum(np.sin(2 * np.pi * hz * k * t) / k for k in (1, 2, 3))
    return (wave / np.sqrt(np.mean(wave ** 2)) * 10 ** (db / 20)).astype(np.float32)


class Chunk:
    """What the model yields: a tensor-like piece of audio."""

    def __init__(self, audio):
        self.audio = audio

    def numpy(self):
        return self.audio


def test_the_consultation_voice_is_passed_on_as_generated_with_only_its_edges_declicked(tmp_path):
    pieces = [tone(0.08), tone(0.08, hz=120), tone(0.08, hz=120, db=-30)]

    class Model:
        def generate_audio_stream(self, voice, text, stop):
            yield from (Chunk(piece.copy()) for piece in pieces)

    voice = PracticeVoice(tmp_path)
    voice._model = Model()
    out = [np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768 for pcm in
           voice._speak("Hello.", 1.0, threading.Event())]
    edge = SAMPLE_RATE * 20 // 1000
    same = lambda a, b: np.abs(a - b).max() < 1e-4  # noqa: E731 - equal apart from 16-bit rounding
    assert len(out) == 3 and all(len(a) == len(b) for a, b in zip(out, pieces))  # streamed piece by piece
    assert same(out[0][edge:], pieces[0][edge:]) and np.abs(out[0][:10]).max() < 0.001  # fades in over 20 ms
    assert same(out[1], pieces[1])  # the voice itself: untouched
    assert same(out[2][:-edge], pieces[2][:-edge]) and np.abs(out[2][-10:]).max() < 0.0005  # fades out
    voice.close()


needs_signalsmith = pytest.mark.skipif(importlib.util.find_spec("python_stretch") is None,
                                       reason="python-stretch (Signalsmith Stretch) is not installed")


def voice_span(audio):
    level = 20 * np.log10(np.array([np.sqrt(np.mean(audio[i:i + 240] ** 2)) + 1e-9
                                    for i in range(0, len(audio) - 240, 240)]))
    heard = np.flatnonzero(level > level.max() - 35)
    return heard[0] / 100, heard[-1] / 100


@needs_signalsmith
def test_the_session_is_slowed_without_changing_the_pitch_or_the_timing():
    audio = np.concatenate([np.zeros(4800, np.float32), tone(2.0, hz=150), np.zeros(4800, np.float32)])
    slow = slow_down(audio, 0.8)
    assert abs(len(slow) / len(audio) - 1.25) < 0.01
    begin, end = voice_span(slow)
    assert abs(begin - 0.2 / 0.8) < 0.05 and abs(end - 2.19 / 0.8) < 0.05  # nothing shifted or cut
    middle = slow[len(slow) // 3:len(slow) // 3 + 4800]
    spectrum = np.abs(np.fft.rfft(middle * np.hanning(len(middle))))
    assert abs(np.fft.rfftfreq(len(middle), 1 / SAMPLE_RATE)[spectrum.argmax()] - 150) < 6


def test_without_signalsmith_the_session_keeps_the_natural_speed(monkeypatch):
    monkeypatch.setitem(sys.modules, "python_stretch", None)  # as if it were not installed
    audio = tone(0.5)
    assert slow_down(audio, 0.8) is audio


def test_a_session_piece_starts_and_ends_without_a_hard_edge():
    words = tone(1.5)
    audio = np.concatenate([np.zeros(6000, np.float32), words, np.zeros(6000, np.float32)])  # voice 0.25-1.75 s
    soften(audio, 40, 60)
    level = lambda a, b: 20 * np.log10(np.sqrt(np.mean(audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)] ** 2)) + 1e-12)  # noqa: E731
    full = 20 * np.log10(np.sqrt(np.mean(words ** 2)))
    assert level(0.250, 0.255) < full - 6  # the first moment of the voice: softened
    assert abs(level(0.27, 0.30) - full) < 0.5  # full within about 20 ms: too quick to be heard as a fade
    assert abs(level(0.40, 1.60) - full) < 0.01  # the rest: untouched
    assert level(1.745, 1.75) < full - 6  # the last moment: softened
    assert not audio[:int(0.2 * SAMPLE_RATE)].any() and not audio[-int(0.2 * SAMPLE_RATE):].any()


def lead_in(audio, chunk=1920):
    """Feeds audio through LeadIn in pieces, as the model streams it; returns what came out, and what came out
    before the whole input had arrived."""
    gate = LeadIn()
    out, early = [], 0
    for i in range(0, len(audio), chunk):
        if (piece := gate.feed(audio[i:i + chunk].copy())) is not None:
            out.append(piece)
            early += len(piece) if i + chunk < len(audio) else 0
    if (rest := gate.finish()) is not None:
        out.append(rest)
    return np.concatenate(out), early


def test_a_stray_sound_before_the_words_is_silenced_and_nothing_else_changes():
    stray, gap, words = tone(0.06, db=-35), np.zeros(4800, np.float32), tone(1.0, db=-12)
    audio = np.concatenate([np.zeros(2400, np.float32), stray, gap, words])
    out, _ = lead_in(audio)
    assert len(out) == len(audio)
    assert not out[:2400 + len(stray) + len(gap)].any()  # the stray sound is gone, the timing kept
    assert np.array_equal(out[-len(words):], words)  # the words untouched


def test_real_words_are_never_taken_for_a_stray_sound():
    for start in (tone(0.18, db=-15),  # a short word ("So,") and then a pause: long and loud enough to be speech
                  tone(0.04, db=-40)):  # a quiet consonant running straight into the word
        pause = np.zeros(3600 if len(start) > 1000 else 480, np.float32)
        audio = np.concatenate([start, pause, tone(0.8, db=-12)])
        out, _ = lead_in(audio)
        assert np.array_equal(out, audio)


def test_speech_that_starts_at_once_is_not_held_back():
    audio = tone(1.0, db=-12)
    out, early = lead_in(audio)
    assert np.array_equal(out, audio) and early == len(audio) - 1920 * (len(audio) // 1920 == len(audio) / 1920)         or early >= len(audio) - 3840  # everything but the last piece went out while the model was still speaking


def test_session_music_is_chosen_by_the_clinician_and_played_only_with_a_delivered_session(voice_app, fake, settings):
    music = settings.data_dir / "voice" / "calm music.mp3"
    music.parent.mkdir(parents=True, exist_ok=True)
    music.write_bytes(b"ID3" + bytes(range(256)) * 20)
    client, _ = voice_app(require_clinician_review=False)
    assert client.get("/api/clinician/background").status_code == 401
    assert client.get("/api/clinician/background", auth=CLINICIAN_AUTH).json() == {
        "file": None, "volume": 1.43, "files": ["calm music.mp3"]}
    outside = {"file": "../platform.sqlite3", "volume": 0.5}
    assert client.put("/api/clinician/background", json=outside, auth=CLINICIAN_AUTH).status_code == 422
    waiting = start(client)["session_id"]
    chosen = {"file": "calm music.mp3", "volume": 0.7}
    assert client.put("/api/clinician/background", json=chosen, auth=CLINICIAN_AUTH).status_code == 200
    assert client.get(f"/api/sessions/{waiting}/background").status_code == 404  # no session delivered yet
    session_id = to_delivered_session(client, fake)
    assert view(client, session_id)["session_background"] == {"volume": 0.7}
    whole = client.get(f"/api/sessions/{session_id}/background")
    assert whole.status_code == 200 and whole.content == music.read_bytes()
    assert whole.headers["content-type"] == "audio/mpeg" and "max-age" in whole.headers["cache-control"]
    part = client.get(f"/api/sessions/{session_id}/background", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == music.read_bytes()[:100]  # streams without the whole file
    listen = client.get("/api/clinician/background/audio", params={"file": "calm music.mp3"}, auth=CLINICIAN_AUTH)
    assert listen.status_code == 200
    assert client.get("/api/clinician/background/audio", params={"file": "../x.mp3"}, auth=CLINICIAN_AUTH).status_code == 404
    client.put("/api/clinician/background", json={"file": None, "volume": 0.7}, auth=CLINICIAN_AUTH)
    assert "session_background" not in view(client, session_id)


def test_the_session_reverb_is_set_by_the_clinician_and_reaches_the_delivered_session(voice_app, fake):
    client, _ = voice_app(require_clinician_review=False)
    assert client.get("/api/clinician/reverb").status_code == 401
    assert client.get("/api/clinician/reverb", auth=CLINICIAN_AUTH).json() == {"amount": 0.5}  # the default hall
    assert client.put("/api/clinician/reverb", json={"amount": 1.5}, auth=CLINICIAN_AUTH).status_code == 422
    assert client.put("/api/clinician/reverb", json={"amount": 0.3}, auth=CLINICIAN_AUTH).status_code == 200
    assert client.put("/api/clinician/reverb", json={"amount": 0.3}).status_code == 403  # not from the dashboard
    session_id = to_delivered_session(client, fake)
    assert view(client, session_id)["session_reverb"] == 0.3
    waiting = start(client)["session_id"]
    assert "session_reverb" not in view(client, waiting)  # only once a session is delivered


def test_loud_peaks_are_never_cut_off():
    loud = tone(0.5, db=0) * 1.3  # peaks well above full scale
    whole = within_peak(loud.copy())
    assert np.abs(whole).max() <= 0.9401 and np.allclose(whole / whole.max(), loud / loud.max())  # same shape, quieter
    streamed = soft_limit(loud)
    assert np.abs(streamed).max() < 1.0  # rounded off, never clipped
    quiet = tone(0.5, db=-12)
    assert soft_limit(quiet) is quiet and within_peak(quiet) is quiet  # normal speech is not touched
