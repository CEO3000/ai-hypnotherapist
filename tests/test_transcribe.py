"""Spoken answers recorded by the browser and turned into words on the server (app/transcribe.py)."""

from pathlib import Path

import pytest

from app import transcribe
from tests.test_api import start

RECORDING = Path(__file__).resolve().parent.parent / "data" / "voice" / "Clear sound.mp3"


def test_empty_and_oversized_recordings_are_refused():
    with pytest.raises(transcribe.Invalid, match="empty"):
        transcribe.transcribe(b"")
    with pytest.raises(transcribe.Invalid, match="too long"):
        transcribe.transcribe(b"x" * (transcribe.MAX_AUDIO_BYTES + 1))


def test_config_says_whether_answers_can_be_transcribed(client, monkeypatch):
    monkeypatch.setattr(transcribe, "installed", lambda: True)
    assert client.get("/api/config").json()["transcribe"] is True
    monkeypatch.setattr(transcribe, "_failed", "the model could not be downloaded")
    assert client.get("/api/config").json()["transcribe"] is False  # the browser's recogniser is used instead
    monkeypatch.setattr(transcribe, "_failed", None)
    monkeypatch.setattr(transcribe, "installed", lambda: False)
    assert client.get("/api/config").json()["transcribe"] is False


def test_recorded_answer_comes_back_as_words(client, monkeypatch):
    seen = {}

    def fake_transcribe(audio, language=None):
        seen.update(audio=audio, language=language)
        return "I have been sleeping badly"

    monkeypatch.setattr(transcribe, "available", lambda language="en": True)
    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    session_id = start(client)["session_id"]
    response = client.post(f"/api/sessions/{session_id}/transcribe", content=b"webm-audio",
                           headers={"Content-Type": "audio/webm"})
    assert response.status_code == 200
    assert response.json() == {"text": "I have been sleeping badly"}
    assert seen == {"audio": b"webm-audio", "language": "en"}


def test_unusable_recording_is_a_clear_error(client, monkeypatch):
    def refuse(audio, language=None):
        raise transcribe.Invalid("the recording could not be read")

    monkeypatch.setattr(transcribe, "available", lambda language="en": True)
    monkeypatch.setattr(transcribe, "transcribe", refuse)
    session_id = start(client)["session_id"]
    response = client.post(f"/api/sessions/{session_id}/transcribe", content=b"not audio")
    assert response.status_code == 400
    assert "could not be read" in response.json()["detail"]


def test_without_speech_to_text_the_endpoint_says_so(client, monkeypatch):
    monkeypatch.setattr(transcribe, "available", lambda language="en": False)
    session_id = start(client)["session_id"]
    assert client.post(f"/api/sessions/{session_id}/transcribe", content=b"audio").status_code == 503


def test_unknown_session_is_refused(client, monkeypatch):
    monkeypatch.setattr(transcribe, "available", lambda language="en": True)
    assert client.post("/api/sessions/" + "a" * 32 + "/transcribe", content=b"audio").status_code == 404


@pytest.mark.skipif(not transcribe.installed() or not RECORDING.exists(),
                    reason="needs faster-whisper and data/voice/Clear sound.mp3")
def test_real_recording_is_transcribed():
    text = transcribe.transcribe(RECORDING.read_bytes(), "en").lower()
    assert "drop your voice" in text
