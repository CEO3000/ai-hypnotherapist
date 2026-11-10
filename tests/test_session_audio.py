import json
import os
import shutil
import sqlite3
import subprocess
import time
import wave

import numpy as np
import pytest

from app import session_audio
from app.db import Database
from app.session_audio import PAUSE_SCALE, SessionAudio, Stopped, pause_scale
from app.session_delivery import delivery_plan, fades_for, rate_for
from tests.conftest import CLINICIAN_AUTH
from tests.support import split_script, valid_script
from tests.test_api import CONSULTATION_PAYMENT, HUMAN, choose_ai, consult_to_choice, start, view
from tests.test_speech import to_delivered_session, voice_app  # noqa: F401 - voice_app is a fixture

SCRIPT = valid_script(words_per_section=40)
OPENING = split_script(SCRIPT)[0]
SPEECH_RATE = 24_000
WORD_SECONDS = 0.05  # how long ToneVoice takes to say a word
VOICE_HZ, MUSIC_HZ = 220, 660


class ToneVoice:
    """Says each piece as a quiet tone, WORD_SECONDS per word; can fail (or run a check) before a given piece."""

    available = True

    def speaks(self, language):
        return language == "en"

    def __init__(self, fail_at=None, before=None):
        self.rendered = []
        self.fail_at = fail_at
        self.before = before or (lambda count: None)

    def render(self, text, rate, soften_ms, language="en"):
        self.before(len(self.rendered))
        if len(self.rendered) == self.fail_at:
            raise RuntimeError("the voice failed")
        self.rendered.append((text, rate, soften_ms))
        t = np.arange(int(WORD_SECONDS * SPEECH_RATE * len(text.split()))) / SPEECH_RATE
        return (0.3 * np.sin(2 * np.pi * VOICE_HZ * t) * 32767).astype("<i2").tobytes()


class Jobs:
    """Stands in for Service.audio_job: the session "s1" moves from its preview to its full script."""

    def __init__(self, music=None, phase="full", script=SCRIPT, key="script-1"):
        self.music, self.phase, self.script, self.key = music, phase, script, key
        self.expired = []

    def __call__(self, session_id):
        if session_id != "s1" or self.expired:
            return None
        base = {"session_rate": 0.8, "reverb": 0.5, "music": str(self.music) if self.music else None,
                "music_gain": 1.43}
        if self.phase == "full":
            return {**base, "phase": "full", "script": self.script, "key": self.key}
        return {**base, "phase": self.phase, "script": OPENING, "key": "opening-1"}

    def expire(self, session_id):
        self.expired.append(session_id)


def decode(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "f32le", "-ac", "2", "-ar", "48000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).reshape(-1, 2).mean(axis=1)


def level(audio, start, end):
    part = audio[int(start * 48000):int(end * 48000)]
    return 20 * np.log10(np.sqrt((part ** 2).mean()) + 1e-9)


def tone(audio, start, end, hz):
    """How loud one frequency is in that stretch (dB of its amplitude)."""
    part = audio[int(start * 48000):int(end * 48000)]
    t = np.arange(len(part)) / 48000
    return 20 * np.log10(2 * np.abs(np.sum(part * np.exp(-2j * np.pi * hz * t))) / len(part) + 1e-9)


def run(audio, session_id="s1"):
    """Makes everything the session needs now, as the background worker does."""
    while (job := audio.jobs(session_id)) is not None and not audio._finished(session_id, job):
        audio.make(session_id, job)


def music_file(path):
    with wave.open(str(path), "wb") as out:  # 4 s of a soft 660 Hz tone, repeated as needed
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(44100)
        wave_ = (0.2 * np.sin(2 * np.pi * MUSIC_HZ * np.arange(4 * 44100) / 44100) * 32767).astype("<i2")
        out.writeframes(np.repeat(wave_, 2).tobytes())
    return path


def test_the_pauses_are_fitted_so_the_session_lasts_60_to_85_minutes():
    assert pause_scale(3000, 1000) == 1.0  # 66 minutes: already fits
    assert pause_scale(2000, 1000) == pytest.approx((3660 - 2000) / 1000)  # lengthened to 61 minutes
    assert pause_scale(3500, 2000) == pytest.approx((5040 - 3500) / 2000)  # shortened to 84 minutes
    assert pause_scale(1000, 500) == PAUSE_SCALE[1]  # never beyond the limits
    assert pause_scale(4900, 2000) == PAUSE_SCALE[0]
    assert pause_scale(1000, 0) == 1.0  # nothing to fit with


@pytest.mark.real_ffmpeg
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_the_preview_is_made_first_then_after_payment_the_whole_session_continues_from_it(tmp_path, monkeypatch):
    plan = delivery_plan(SCRIPT)
    opening_plan = delivery_plan(OPENING)
    speech = sum(len(item["text"].split()) for item in plan) * WORD_SECONDS
    pauses = sum(item["pause_ms"] for item in plan[:-1]) / 1000
    goal = speech + session_audio.LEAD_IN + session_audio.SESSION_END["tail"] + pauses * 1.5  # pauses lengthened
    monkeypatch.setattr(session_audio, "MIN_SECONDS", goal)
    monkeypatch.setattr(session_audio, "MAX_SECONDS", goal + 10)
    preview_limit = session_audio.LEAD_IN + session_audio.PREVIEW_END["tail"] + 2  # room for about 2 s of speech
    monkeypatch.setattr(session_audio, "PREVIEW_SECONDS", preview_limit)
    jobs = Jobs(music=music_file(tmp_path / "music.wav"), phase="preview")
    voice = ToneVoice()
    audio = SessionAudio(tmp_path, voice, jobs)
    folder = tmp_path / "sessions" / "s1"

    run(audio)  # before payment: only the preview, and then it stops
    assert audio.status("s1")["preview_ready"] and not audio.status("s1")["download_ready"]
    assert 0 < len(voice.rendered) < len(opening_plan)
    preview = decode(folder / "preview.mp3")
    assert len(preview) / 48000 <= preview_limit + 0.1
    assert level(preview, 0, 0.3) < -35 and level(preview, len(preview) / 48000 - 0.2, len(preview) / 48000) < -40
    before_payment = len(voice.rendered)

    jobs.phase = "opening"  # paid: the rest of the opening is spoken while the rest of the script is written
    run(audio)
    assert len(voice.rendered) == len(opening_plan)
    assert not audio.status("s1")["download_ready"]

    jobs.phase = "full"  # the whole script is ready
    run(audio)
    assert [text for text, _, _ in voice.rendered] == [item["text"] for item in plan]  # each piece spoken once
    assert [(rate, fades) for _, rate, fades in voice.rendered] == [
        (rate_for(item["style"], 0.8), fades_for(item["style"])) for item in plan]
    assert before_payment < len(opening_plan) < len(plan)
    status = audio.status("s1")
    assert status["download_ready"] and status["progress"] == 100 and status["minutes"] is not None
    assert sorted(p.name for p in folder.iterdir()) == ["job.json", "preview.mp3", "session.mp3"]  # nothing else
    assert json.loads((folder / "job.json").read_text())["pause_scale"] > 1.3

    session = decode(folder / "session.mp3")
    length = len(session) / 48000
    assert goal - 1 <= length <= goal + 11  # within the (shortened) range
    assert np.abs(session).max() < 0.93  # the limiter keeps it below -0.6 dBFS
    lead_in = session_audio.LEAD_IN
    assert level(session, 0, 0.3) < -35  # the music starts softly
    assert tone(session, 0, lead_in - 0.1, VOICE_HZ) < -60  # and plays alone before the first word
    assert tone(session, 3, lead_in - 0.1, MUSIC_HZ) > -40
    assert tone(session, lead_in + 0.05, lead_in + 0.3, VOICE_HZ) > -30  # the voice begins after the lead-in
    after_last_word = length - session_audio.SESSION_END["tail"]
    assert tone(session, after_last_word + 2.5, after_last_word + 3, VOICE_HZ) < -60  # the hall has died away
    assert tone(session, after_last_word + 2.5, after_last_word + 3, MUSIC_HZ) > -30  # the music plays on
    assert level(session, length - 0.3, length) < -50  # and has faded out at the very end


def test_an_interrupted_session_carries_on_from_the_last_finished_piece(tmp_path):
    folder = tmp_path / "sessions" / "s1"
    with pytest.raises(RuntimeError):
        run(SessionAudio(tmp_path, ToneVoice(fail_at=3), Jobs()))
    pieces = json.loads((folder / "pieces.json").read_text())
    assert len(pieces) == 3
    with (folder / "speech.pcm").open("ab") as out:
        out.write(b"\x05\x00" * 100)  # half a piece written when it stopped

    voice = ToneVoice()
    audio = SessionAudio(tmp_path, voice, Jobs())
    plan = delivery_plan(SCRIPT)
    pcm_before = (folder / "speech.pcm").read_bytes()[:2 * sum(p["samples"] for p in pieces)]
    job = audio.jobs("s1")
    audio.make("s1", {**job, "phase": "opening", "script": SCRIPT})  # speak everything, keep the working files
    assert [text for text, _, _ in voice.rendered] == [item["text"] for item in plan[3:]]
    pieces = json.loads((folder / "pieces.json").read_text())
    assert [p["text"] for p in pieces] == [item["text"] for item in plan]
    speech = (folder / "speech.pcm").read_bytes()
    assert len(speech) == 2 * sum(p["samples"] for p in pieces)  # the half piece was dropped
    assert speech.startswith(pcm_before)


def test_a_changed_script_is_spoken_again_only_from_the_first_change(tmp_path):
    audio = SessionAudio(tmp_path, ToneVoice(), Jobs())
    job = audio.jobs("s1")
    audio.make("s1", {**job, "phase": "opening"})  # the whole script spoken, working files kept
    plan = delivery_plan(SCRIPT)
    changed_script = SCRIPT.replace("## PART 3 - Future Pacing\n", "## PART 3 - Future Pacing\nA new line here.\n")
    changed = delivery_plan(changed_script)
    first_change = next(i for i, (a, b) in enumerate(zip(plan, changed)) if a["text"] != b["text"])
    voice = ToneVoice()
    audio = SessionAudio(tmp_path, voice, Jobs(script=changed_script, key="script-2"))
    run(audio)
    assert [text for text, _, _ in voice.rendered] == [item["text"] for item in changed[first_change:]]
    assert audio.status("s1")["download_ready"]


def test_closing_the_app_stops_between_pieces_and_keeps_what_is_made(tmp_path):
    audio = None
    voice = ToneVoice(before=lambda count: audio.close() if count == 2 else None)
    audio = SessionAudio(tmp_path, voice, Jobs())
    with pytest.raises(Stopped):
        run(audio)
    assert len(json.loads((tmp_path / "sessions" / "s1" / "pieces.json").read_text())) == 3
    audio.start("s1")  # nothing new starts once closing
    assert audio.status("s1")["download_ready"] is False and not audio._queue


def test_a_session_whose_audio_fails_says_so_and_is_not_retried_in_a_loop(tmp_path):
    audio = SessionAudio(tmp_path, ToneVoice(fail_at=0), Jobs())
    audio.start("s1")
    audio._thread.join(10)
    assert audio.status("s1")["failed"] is True
    audio.start("s1")
    assert not audio._queue  # not queued again until the app restarts


def test_unfinished_session_audio_is_resumed_when_the_app_starts(tmp_path):
    SessionAudio(tmp_path, ToneVoice(fail_at=2), Jobs()).start("s1")
    time.sleep(0.5)
    audio = SessionAudio(tmp_path, ToneVoice(), Jobs())
    audio.resume()
    audio._thread.join(20)
    assert audio.status("s1")["download_ready"] is True


def test_the_worker_follows_the_session_when_it_is_paid_for_during_the_preview(tmp_path):
    jobs = Jobs(phase="preview")
    voice = ToneVoice(before=lambda count: setattr(jobs, "phase", "full") if count == 1 else None)
    audio = SessionAudio(tmp_path, voice, jobs)
    audio.start("s1")
    audio._thread.join(20)
    assert audio.status("s1")["download_ready"] is True
    assert len(voice.rendered) == len(delivery_plan(SCRIPT))


def test_a_waiting_clients_preview_goes_before_another_clients_full_session(tmp_path):
    phases = {"s1": "full", "s2": "preview"}

    def jobs(session_id):
        if session_id not in phases:
            return None
        base = {"session_rate": 0.8, "reverb": 0.5, "music": None, "music_gain": 1.43}
        if phases[session_id] == "full":
            return {**base, "phase": "full", "script": SCRIPT, "key": "script-1"}
        return {**base, "phase": "preview", "script": OPENING, "key": "opening-1"}

    audio = None

    def before(count):
        if count == 4:  # while s1's full session is being made, s2 finishes its consultation
            audio.start("s2")

    voice = ToneVoice(before=before)
    audio = SessionAudio(tmp_path, voice, jobs)
    audio.start("s1")
    audio._thread.join(30)
    assert audio.status("s1")["download_ready"] and audio.status("s2")["preview_ready"]
    s2_preview = (tmp_path / "sessions" / "s2" / "preview.mp3").stat().st_mtime
    s1_session = (tmp_path / "sessions" / "s1" / "session.mp3").stat().st_mtime
    assert s2_preview <= s1_session  # the waiting client's preview was made first
    s2_pieces = len(json.loads((tmp_path / "sessions" / "s2" / "pieces.json").read_text()))
    assert len(voice.rendered) == len(delivery_plan(SCRIPT)) + s2_pieces  # s1 carried on: nothing spoken twice


def test_session_audio_is_deleted_after_the_retention_period(tmp_path):
    jobs = Jobs()
    audio = SessionAudio(tmp_path, ToneVoice(), jobs, on_expired=jobs.expire)
    run(audio)
    folder = tmp_path / "sessions" / "s1"
    assert audio.cleanup(7) == [] and folder.exists()  # made just now: kept
    eight_days_ago = time.time() - 8 * 86_400
    for item in folder.iterdir():
        os.utime(item, (eight_days_ago, eight_days_ago))
    assert audio.cleanup(7) == ["s1"]
    assert not folder.exists() and jobs.expired == ["s1"]
    audio.start("s1")  # an expired session is never made again
    assert not audio._queue and not folder.exists()


def wait_for_audio(client, session_id, key="download_ready"):
    for _ in range(200):
        audio = view(client, session_id)["audio"]
        if audio[key]:
            return audio
        time.sleep(0.05)
    raise AssertionError(f"the session audio never became {key}")


def test_before_payment_only_the_preview_exists_and_after_payment_the_download(voice_app, fake):
    client, voice = voice_app(require_clinician_review=False)
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    current = view(client, session_id)
    assert current["status"] == "awaiting_choice" and current["preview_state"] == "ready"
    wait_for_audio(client, session_id, "preview_ready")
    assert client.get(f"/api/sessions/{session_id}/preview").headers["content-type"] == "audio/mpeg"
    assert client.get(f"/api/sessions/{session_id}/download").status_code == 402
    opening_texts = {item["text"] for item in delivery_plan(split_script(valid_script())[0])}
    assert {text for text, _, _ in voice.rendered} <= opening_texts  # nothing beyond the opening before payment
    assert [c["part"] for c in fake.calls if c["kind"] == "complete"] == ["opening"]  # nor written

    fake.completions.append((valid_script(), "end_turn"))
    paid = choose_ai(client, session_id, 5)
    assert paid.status_code == 200 and "audio" in paid.json()  # the page goes straight to the session's audio
    audio = wait_for_audio(client, session_id)
    assert audio["preview_ready"] and audio["expired"] is False
    assert [c["part"] for c in fake.calls if c["kind"] == "complete"] == ["opening", "rest"]
    final = client.app.state.service.db.latest_script(session_id)["final_text"]
    assert [text for text, _, _ in voice.rendered] == [item["text"] for item in delivery_plan(final)]  # each once
    current = view(client, session_id)
    assert "session_script" not in current and "session_plan" not in current  # the recording, not the text
    download = client.get(f"/api/sessions/{session_id}/download")
    assert download.status_code == 200 and download.headers["content-type"] == "audio/mpeg"
    assert download.headers["content-disposition"] == "attachment; filename*=utf-8''Hypnotherapy%20session.mp3"


def test_payments_come_before_the_consultation_and_before_the_full_session(voice_app, fake):
    client, _ = voice_app(require_clinician_review=False)
    unpaid = start(client, pay=False)
    session_id = unpaid["session_id"]
    assert unpaid["status"] == "awaiting_consultation_payment"
    acknowledgement = ("I want my consultation to start straight away. I understand that I lose my 14-day right to "
                       "cancel once the consultation has been completed. I confirm that my answers to the safety "
                       "questions are true. I understand that if my consultation shows otherwise, no session or "
                       "preview is made and my consultation fee is not refunded.")
    assert unpaid["payment"] == {"purpose": "consultation", "amount": 5, "currency": "GBP", "provider": "test",
                                 "saved_card": False, "acknowledgement": acknowledgement, "test_mode": True}
    assert unpaid["messages"] == []  # nothing is said before the consultation is paid for
    assert client.post(f"/api/sessions/{session_id}/messages", json={"text": "hi"}).status_code == 409
    assert client.post(f"/api/sessions/{session_id}/pay", json={"purpose": "session"}).status_code == 409
    pay_url = f"/api/sessions/{session_id}/pay"
    assert client.post(pay_url, json={**CONSULTATION_PAYMENT, "acknowledged": False}).status_code == 422  # must agree
    assert client.post(pay_url, json={**CONSULTATION_PAYMENT, "email": None}).status_code == 422  # must give email
    assert client.post(pay_url, json={**CONSULTATION_PAYMENT, "email": "not an email"}).status_code == 422
    paid = client.post(pay_url, json={**CONSULTATION_PAYMENT, "save_card": True})
    assert paid.status_code == 200 and paid.json()["status"] == "consulting"
    assert paid.json()["email"] == "sa***@example.com"  # the page shows it masked
    assert paid.json()["consultation_started"] is False and paid.json()["consultation_elapsed_s"] == 0
    assert client.post(f"/api/sessions/{session_id}/pay", json=CONSULTATION_PAYMENT).status_code == 409
    assert client.post(f"/api/sessions/{session_id}/start-consultation").status_code == 200
    assert view(client, session_id)["consultation_started"] is True

    consult_to_choice(client, fake, session_id)
    assert client.post(f"/api/sessions/{session_id}/ai-session", json={"baseline_severity": 4}).status_code == 200
    waiting = view(client, session_id)
    assert waiting["status"] == "awaiting_session_payment"
    assert {k: v for k, v in waiting["payment"].items() if k != "acknowledgement"} == {
        "purpose": "session", "amount": 35, "currency": "GBP", "provider": "test", "saved_card": True, "test_mode": True}
    assert "lose my 14-day right to cancel once its supply begins" in waiting["payment"]["acknowledgement"]
    assert client.post(f"/api/sessions/{session_id}/pay", json={"purpose": "session"}).status_code == 422
    assert client.post(f"/api/sessions/{session_id}/back-to-choice").status_code == 200  # changed their mind
    assert view(client, session_id)["status"] == "awaiting_choice"
    fake.completions.append((valid_script(), "end_turn"))
    choose_ai(client, session_id, 4)
    assert view(client, session_id)["status"] == "ready"
    payments = client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()["payments"]
    assert [(p["purpose"], p["amount_pence"], p["status"], p["provider"]) for p in payments] == [
        ("consultation", 500, "paid", "test"), ("session", 3500, "paid", "test")]
    assert payments[0]["acknowledgement"] == acknowledgement  # the exact words agreed, kept with the payment


def test_an_unpaid_preview_expires_after_the_retention_period(voice_app, fake):
    client, _ = voice_app(require_clinician_review=False)
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    wait_for_audio(client, session_id, "preview_ready")
    client.app.state.service.audio_expired(session_id)  # what the daily clean-up does after 7 days
    assert view(client, session_id)["status"] == "expired"
    assert client.post(f"/api/sessions/{session_id}/ai-session", json={"baseline_severity": 4}).status_code == 409


def test_a_hypnotherapist_request_is_emailed_to_the_practice(make_client, fake, settings):
    client = make_client(require_clinician_review=False)
    session_id = start(client)["session_id"]
    consult_to_choice(client, fake, session_id)
    for missing in ("name", "phone", "description"):
        assert client.post(f"/api/sessions/{session_id}/human-request",
                           json={**HUMAN, missing: " "}).status_code == 422
    assert client.post(f"/api/sessions/{session_id}/human-request", json={**HUMAN, "phone": "call me"}).status_code == 422
    assert client.post(f"/api/sessions/{session_id}/human-request", json=HUMAN).status_code == 200
    saved = [e.read_text(encoding="utf-8") for e in (settings.data_dir / "outbox").glob("*.eml")]  # no mailbox
    requests = [e for e in saved if "Subject: New hypnotherapist request" in e]  # (the other is the client's receipt)
    assert len(requests) == 1
    email = requests[0]
    assert "To: practice@example.com" in email and "Reply-To: sam@example.com" in email
    for field in ("Sam Test", "sam@example.com", "07700 900123", "I get very anxious before exams.",
                  "Wants to stop smoking"):
        assert field in email
    detail = client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()["human_request"]
    assert (detail["phone"], detail["description"]) == ("07700 900123", "I get very anxious before exams.")


def test_without_the_practice_voice_there_is_no_file_to_preview(make_client, fake):
    client = make_client(require_clinician_review=False)
    session_id = to_delivered_session(client, fake)
    assert "audio" not in view(client, session_id)
    assert client.get(f"/api/sessions/{session_id}/preview").status_code == 404
    assert client.get(f"/api/sessions/{session_id}/download").status_code == 404


def test_feedback_is_a_comment_alone(make_client, fake):
    client = make_client(require_clinician_review=False)
    session_id = to_delivered_session(client, fake)
    assert client.post(f"/api/sessions/{session_id}/feedback", json={"comments": "   "}).status_code == 422
    assert client.post(f"/api/sessions/{session_id}/feedback", json={"comments": "Very calming."}).status_code == 200
    assert view(client, session_id)["feedback_given"] is True
    detail = client.get(f"/api/clinician/sessions/{session_id}", auth=CLINICIAN_AUTH).json()
    assert detail["feedback"]["comments"] == "Very calming."
    assert detail["feedback"]["rating"] is None


def test_older_databases_let_feedback_be_a_comment_alone(tmp_path):
    path = tmp_path / "old.sqlite3"
    db = Database(path)
    db.init()
    db.create_session(session_id="old", country="GB", language="en", status="ready", screening_version="1",
                      screening_answers={}, screening_outcome="proceed")
    with sqlite3.connect(path) as conn:  # the feedback table as it was before comments could stand alone
        conn.executescript("""
            DROP TABLE feedback;
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL REFERENCES sessions(id),
                created_at TEXT NOT NULL,
                rating INTEGER NOT NULL,
                severity_after INTEGER NOT NULL,
                comments TEXT NOT NULL
            );
            INSERT INTO feedback (session_id, created_at, rating, severity_after, comments)
                VALUES ('old', '2026-01-01', 4, 3, 'kept');
        """)
    db.init()  # the app starting with that database
    db.add_feedback(session_id="old", rating=None, severity_after=None, comments="a comment alone")
    with sqlite3.connect(path) as conn:
        columns = {row[1]: row[3] for row in conn.execute("PRAGMA table_info(feedback)")}
        assert columns["rating"] == 0 and columns["severity_after"] == 0  # no longer required
        assert conn.execute("SELECT rating, comments FROM feedback ORDER BY id").fetchall() == [
            (4, "kept"), (None, "a comment alone")]


def test_a_sample_of_the_session_voice_over_its_music(tmp_path, monkeypatch, settings):
    import wave as wave_module
    from dataclasses import replace

    from app import config, session_audio, speech
    from app.db import Database

    voice_dir = settings.data_dir / "voice"
    voice_dir.mkdir(parents=True)
    with wave_module.open(str(voice_dir / "music.wav"), "wb") as out:  # 30 s of a quiet tone as the music
        out.setnchannels(1), out.setsampwidth(2), out.setframerate(24_000)
        t = np.arange(30 * 24_000) / 24_000
        out.writeframes((0.1 * np.sin(2 * np.pi * MUSIC_HZ * t) * 32767).astype("<i2").tobytes())
    db = Database(settings.data_dir / "platform.sqlite3")
    db.init()
    db.set_setting("session_background", {"file": "music.wav", "volume": 1.0})

    class SampleVoice(ToneVoice):
        def __init__(self, _voice_dir):
            super().__init__()

        def start(self, *args):
            pass

        def speaks(self, language):
            return language == "de"

        def close(self):
            pass

    monkeypatch.setattr(config, "load_settings", lambda: replace(settings))
    monkeypatch.setattr(speech, "PracticeVoice", SampleVoice)
    out = session_audio.sample("de")
    assert out == voice_dir / "sample-de.mp3" and out.exists()  # (ffmpeg itself is stood in for by conftest)
    assert not (settings.data_dir / "sessions" / "sample-de").exists()  # nothing left behind
