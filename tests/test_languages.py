"""The client's language: English, German, French or Spanish. Everything the client reads and hears follows it; the
practice (its emails, the summary, the clinician notes) stays in English, and the qualified hypnotherapist is offered
in English only."""

import json
import re
from types import SimpleNamespace

from app import transcribe
from app.config import CONTENT_DIR, STATIC_DIR, Settings
from app.payments import StripePayments
from app.script_writer import validate_opening, validate_script
from app.speech import LANGUAGES as VOICE_LANGUAGES
from app.speech import PREVIEW_TEXTS
from tests.support import FakeVoice
from tests.test_api import CONSULTATION_PAYMENT, HUMAN, clear_answers, consult_to_choice, start, view
from tests.test_emails import body, outbox
from tests.test_speech import speak, voice_app  # noqa: F401 - voice_app is a fixture

LANGUAGES = Settings.__dataclass_fields__["languages"].default  # the languages offered unless .env says otherwise
OTHERS = [code for code in LANGUAGES if code != "en"]


def load(name: str) -> dict:
    return json.loads((CONTENT_DIR / name).read_text(encoding="utf-8"))


def placeholders(text: str) -> list[str]:
    return sorted(re.findall(r"\{\w+\}", text))


# --- every language has all its words ---------------------------------------------------------------------


def test_every_screen_is_translated_with_the_same_placeholders():
    english = json.loads((STATIC_DIR / "i18n" / "en.json").read_text(encoding="utf-8"))
    for code in OTHERS:
        words = json.loads((STATIC_DIR / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        assert words.keys() == english.keys(), code
        for key, text in english.items():
            assert placeholders(words[key]) == placeholders(text), (code, key)
            assert words[key] or not text, (code, key)  # (an empty English text, like the language note, may stay so)


def test_every_spoken_phrase_safety_text_helpline_and_email_is_in_every_language():
    content = load("consultation.json")
    for key, texts in content.items():
        if key != "wrap_up_instruction":
            assert set(LANGUAGES) <= texts.keys(), key
    for question in load("screening.json")["questions"]:
        assert set(LANGUAGES) <= question["text"].keys(), question["id"]
    crisis = load("crisis_resources.json")
    for entry in [*crisis["countries"].values(), crisis["default"]]:
        assert set(LANGUAGES) <= entry["name"].keys()
        for resource in entry["resources"]:
            assert set(LANGUAGES) <= resource["name"].keys() and set(LANGUAGES) <= resource["detail"].keys()
    assert set(LANGUAGES) <= load("risk_patterns.json")["patterns"].keys()
    emails = load("emails.json")
    for code in LANGUAGES:
        assert emails[code].keys() == emails["en"].keys(), code
        for key, text in emails["en"].items():
            if isinstance(text, list):
                assert len(emails[code][key]) == len(text), (code, key)
                assert [placeholders(line) for line in emails[code][key]] == [placeholders(line) for line in text]
    assert set(LANGUAGES) <= VOICE_LANGUAGES.keys() and set(LANGUAGES) <= PREVIEW_TEXTS.keys()


def test_the_pause_phrases_name_the_buttons_as_the_screen_shows_them():
    content = load("consultation.json")
    for code in OTHERS:
        words = json.loads((STATIC_DIR / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        assert words["stage.type"] in content["silence_check_in"][code], code
        assert words["stage.resume"] in content["silence_pause"][code], code


def test_wherever_the_hypnotherapist_is_offered_it_says_english_only():
    for code in OTHERS:
        words = json.loads((STATIC_DIR / "i18n" / f"{code}.json").read_text(encoding="utf-8"))
        english_only = {"de": "Englisch", "fr": "anglais", "es": "inglés"}[code]
        for key in ("welcome.price_human", "welcome.step4", "outcome.human_only.body", "stopped.concern.body",
                    "choice.human.body", "choice.not_suitable", "incomplete.body", "incomplete.body_final",
                    "incomplete.human", "pay.consultation.body", "human.language_note", "human.done.body",
                    "rejected.body"):
            assert english_only in words[key], (code, key)
        assert english_only in "".join(load("emails.json")[code]["reminder"]), code


# --- the client's journey in their language --------------------------------------------------------------------


def test_the_client_chooses_a_language_and_what_they_read_and_hear_follows_it(voice_app, fake, settings):
    client, _ = voice_app(require_clinician_review=False)
    config = client.get("/api/config").json()
    assert config["languages"] == [{"code": "en", "name": "English"}, {"code": "de", "name": "Deutsch"},
                                   {"code": "fr", "name": "Français"}, {"code": "es", "name": "Español"}]
    german = client.get("/api/config?language=de").json()
    assert german["acknowledgements"] == ["In Ordnung."]
    assert german["silence"]["silence_resume"] == "Willkommen zurück."
    assert {"code": "DE", "name": "Deutschland"} in german["countries"]
    questions = client.get("/api/screening?language=de").json()["questions"]
    assert questions[0]["text"] == "Sind Sie 18 Jahre oder älter?"

    session_id = start(client, country="DE", language="de")["session_id"]
    assert view(client, session_id)["language"] == "de"
    [receipt] = outbox(settings)
    assert receipt["Subject"] == "Ihre Quittung: Persönliche Hypnotherapie-Beratung"
    assert "Bezahlt am:" in body(receipt) and "TESTVERSION" in body(receipt) and "Widerrufsrecht" in body(receipt)

    consult_to_choice(client, fake, session_id, opening=german_script())
    consultant = next(c for c in fake.calls if c.get("model") == "ConsultantTurn")
    system = " ".join(block["text"] for block in consultant["system"])
    assert "Always write your reply in German." in system and "Hallo und herzlich willkommen" in system
    extraction = next(c for c in fake.calls if c.get("model") == "ConsultationIntake")
    assert "held in German" in extraction["system"][0]["text"]
    writer = next(c for c in fake.calls if c["kind"] == "complete")
    assert "in natural spoken German" in " ".join(block["text"] for block in writer["system"])

    # the hypnotherapist (English only) gets the request in English, told the client's language
    assert client.post(f"/api/sessions/{session_id}/human-request", json=HUMAN).status_code == 200
    request = next(m for m in outbox(settings) if m["Subject"].startswith("New hypnotherapist request"))
    text = request.get_body(preferencelist=("plain",)).get_content()  # (the consultation is attached as a PDF)
    assert "Their AI consultation was in German" in text and "English only" in text


def test_the_summary_is_shown_to_the_client_in_their_language(client, fake):
    from tests.test_api import intake

    session_id = start(client, language="fr")["session_id"]
    consult_to_choice(client, fake, session_id, opening=script_in("fr"), intake_record=intake(
        presenting_issue_for_client="Veut arrêter de fumer", goal_for_client="être libre des cigarettes"))
    summary = view(client, session_id)["summary"]
    assert summary["presenting_issue"] == "Veut arrêter de fumer" and summary["goal"] == "être libre des cigarettes"


def test_only_the_languages_set_in_the_settings_are_offered(make_client):
    client = make_client(languages=("en", "de"))
    assert [option["code"] for option in client.get("/api/config").json()["languages"]] == ["en", "de"]
    refused = client.post("/api/sessions", json={"country": "GB", "language": "fr", "answers": clear_answers()})
    assert refused.status_code == 422


def test_a_language_the_practice_voice_is_not_prepared_for_uses_the_device_voice(voice_app):
    client, voice = voice_app(FakeVoice(languages=("en",)))
    assert client.get("/api/config?language=en").json()["practice_voice"] is True
    assert client.get("/api/config?language=es").json()["practice_voice"] is False
    session_id = start(client, language="es")["session_id"]
    greeting = view(client, session_id)["messages"][0]["content"]
    assert speak(client, session_id, greeting.split(".")[0] + ".").status_code == 404
    assert not any("Hola" in text for text, _ in voice.warmed)


def test_the_practice_voice_speaks_each_language_it_is_prepared_for(voice_app):
    client, voice = voice_app(FakeVoice(languages=("en", "es")))
    assert client.get("/api/config?language=es").json()["practice_voice"] is True
    assert ("Hola, le doy la bienvenida.", 1.0) in voice.warmed  # its fixed phrases are ready in advance
    session_id = start(client, language="es")["session_id"]
    assert speak(client, session_id, "Hola, le doy la bienvenida.").status_code == 200
    assert voice.spoken[-1] == "Hola, le doy la bienvenida."


def test_a_german_risk_phrase_is_caught_and_the_standard_safety_question_is_asked(client):
    service = client.app.state.service
    keywords = load("risk_patterns.json")["patterns"]["de"]
    assert any(re.search(p, "Manchmal will ich einfach sterben", re.I) for p in keywords)
    assert not any(re.search(p, "Ich möchte mit dem Rauchen aufhören", re.I) for p in keywords)
    # the quoted form needs the classifier's English words: in German, the standard question
    assert service.safety_question("you can't go on", "de") == load("consultation.json")["safety_check"]["de"]["standard"]
    assert "you can't go on" in service.safety_question("you can't go on", "en")


# --- the session script in another language -------------------------------------------------------------------


SAFETY_LINES = {  # the pre-talk's safety lines and the emergence's last words, as the script writer writes them
    "de": ("Hören Sie nur, wenn Sie sich sicher entspannen können, niemals beim Autofahren. Sie können jederzeit, wenn "
           "Sie es brauchen, die Augen öffnen und sofort hellwach sein.", "Fünf... Augen auf, hellwach und erfrischt."),
    "fr": ("N'écoutez que lorsque vous pouvez vous détendre en toute sécurité, jamais en conduisant. À tout moment, si "
           "vous en avez besoin, vous pouvez ouvrir les yeux et être pleinement éveillé.",
           "Cinq... les yeux ouverts, pleinement éveillé et reposé."),
    "es": ("Escuche solo cuando pueda relajarse con seguridad, nunca mientras conduce. En cualquier momento, si lo "
           "necesita, puede abrir los ojos y estar completamente despierto.",
           "Cinco... ojos abiertos, completamente despierto y renovado."),
}


def script_in(language: str, words_per_section: int = 700) -> str:
    pre_talk, emergence = SAFETY_LINES[language]
    filler = " ".join(["ganz ruhig und entspannt atmen"] * (words_per_section // 5))
    return "\n\n".join([
        "# Sam",
        "## CLINICIAN NOTES\nUsed the coffee-break origin moment. Not read aloud.",
        f"## PRE-TALK\n{pre_talk} {filler}",
        f"## INDUCTION\n{filler}",
        f"## DEEPENER\n{filler}",
        f"## PART 1 - Die Laternenwerkstatt: Belief Archaeology & Transformation\n{filler}\n> slow down here",
        f"## PART 2 - Der stille Hafen: Parts Dialogue & Integration\n{filler}",
        f"## PART 3 - Future Pacing\n{filler}",
        f"## PART 4 - Post-Hypnotic Anchors\n{filler}",
        f"## EMERGENCE\n{filler} {emergence}",
    ])


def german_script(words_per_section: int = 700) -> str:
    return script_in("de", words_per_section)


def test_a_script_in_german_is_checked_with_german_safety_lines_and_counted_in_german_words():
    script = german_script()
    assert validate_script(script, category="smoking-cessation", stop_reason="end_turn", language="de") == []
    opening = script.split("## DEEPENER")[0].replace("## CLINICIAN NOTES\nUsed the coffee-break origin moment. "
                                                     "Not read aloud.\n\n", "")
    assert validate_opening(opening, stop_reason="end_turn", language="de") == []
    unsafe = script.replace("die Augen öffnen und sofort hellwach sein", "weitermachen").replace("Autofahren", "Essen")
    issues = validate_script(unsafe, category="smoking-cessation", stop_reason="end_turn", language="de")
    assert any("safety-net" in issue for issue in issues) and any("driving" in issue for issue in issues)
    flashing = script.replace("Fünf...", "Ein flackerndes Licht. Fünf...")
    assert any("flashing" in issue for issue in validate_script(flashing, category="smoking-cessation",
                                                                stop_reason="end_turn", language="de"))


def test_scripts_in_french_and_spanish_pass_their_own_checks():
    for code in ("fr", "es"):
        assert validate_script(script_in(code), category="smoking-cessation", stop_reason="end_turn",
                               language=code) == [], code


def test_words_with_accents_count_as_whole_words():
    script = german_script().replace("ganz ruhig und entspannt atmen", "élève à côté de l'été")
    issues = validate_script(script, category="smoking-cessation", stop_reason="end_turn", language="fr")
    assert not any(issue.startswith("Script is") for issue in issues)  # 5 words each, as in German


# --- payment page and speech to text ---------------------------------------------------------------------------


def test_stripes_payment_page_is_in_the_clients_language():
    payments = StripePayments(secret_key="sk_test_x", webhook_secret="", client_page="https://example.com/")
    sent = {}

    def create(params):
        sent.update(params)
        return SimpleNamespace(to_dict=lambda: {"id": "cs_test_1", "url": "https://checkout.stripe.com/x"})

    payments.client = SimpleNamespace(v1=SimpleNamespace(checkout=SimpleNamespace(
        sessions=SimpleNamespace(create=create))))
    payments.checkout(session_id="s", purpose="consultation", item="Consulta personal de hipnoterapia",
                      amount_pence=500, email=None, save_card=False, locale="es")
    assert sent["locale"] == "es" and sent["line_items"][0]["price_data"]["product_data"]["name"].startswith("Consulta")
    payments.checkout(session_id="s", purpose="consultation", item="x", amount_pence=500, email=None, save_card=False)
    assert sent["locale"] == "en-GB"


def test_answers_in_another_language_are_turned_into_words_by_the_multilingual_model():
    assert transcribe.model_for("en") == "base.en"
    assert {transcribe.model_for(code) for code in OTHERS} == {transcribe.MULTILINGUAL_MODEL}


def test_the_payment_acknowledgement_is_kept_in_the_words_the_client_agreed_to(client, settings):
    session_id = start(client, language="fr", pay=False)["session_id"]
    assert client.post(f"/api/sessions/{session_id}/pay", json=CONSULTATION_PAYMENT).status_code == 200
    assert "Je souhaite que ma consultation commence immédiatement" in body(outbox(settings)[0])


def test_a_spoken_answer_is_transcribed_in_the_sessions_language(client, monkeypatch):
    seen = {}
    monkeypatch.setattr(transcribe, "available", lambda language="en": True)
    monkeypatch.setattr(transcribe, "transcribe", lambda audio, language=None: seen.setdefault("language", language)
                        and "Ich rauche zu viel")
    session_id = start(client, language="de")["session_id"]
    response = client.post(f"/api/sessions/{session_id}/transcribe", content=b"webm-audio",
                           headers={"Content-Type": "audio/webm"})
    assert response.json() == {"text": "Ich rauche zu viel"} and seen == {"language": "de"}


def test_a_failed_multilingual_model_affects_only_the_other_languages(client, monkeypatch):
    monkeypatch.setattr(transcribe, "installed", lambda: True)
    monkeypatch.setattr(transcribe, "_failed_multilingual", "could not be downloaded")
    assert client.get("/api/config?language=en").json()["transcribe"] is True
    assert client.get("/api/config?language=fr").json()["transcribe"] is False  # the browser's recogniser instead
