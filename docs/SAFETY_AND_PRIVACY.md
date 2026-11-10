# Safety and privacy

This project works with personal and sensitive information, so safety and privacy shaped the design from the start. It is a **clinician-support workflow**. It does not diagnose, it is not an autonomous clinical system, and it is not a medical device.

## Principles

1. **Deterministic where possible.** Eligibility is decided by fixed rules with no AI involved.
2. **Independent checks.** The safety classifier is separate from the assistant that talks to the client, and a keyword net sits beside it.
3. **Fail safe.** When a check cannot run, the system stops or treats the message as a crisis. It never carries on unchecked.
4. **Humans decide what the AI learns and reads.** Lessons and book notes need approval before they have any effect.
5. **Keep as little as possible.** Audio is deleted after a set period, and the practice's own mailbox and local speech mean no extra outside services see client words.

## Measures, where they are and what tests them

| Measure | Where | Tests |
|---|---|---|
| Screening with an explicit outcome order, versioned content, a rationale for every question, invalid configuration rejected | `app/screening.py`, `app/content/screening.json` | `tests/test_screening.py` |
| Safety monitor: keyword net, independent classifier, fail-safe paths | `app/risk.py` | `tests/test_risk.py` |
| Client text escaped before the classifier reads it | `app/risk.py` | `test_classifier_input_escapes_tags_to_resist_injection` |
| Voice measurements reach the model as wording written by code, never as client text | `app/voice_cues.py` | `tests/test_voice_cues.py` |
| Script checks before delivery | `app/script_writer.py` | `tests/test_script_validation.py` |
| Fail-closed knowledge exclusion | `app/knowledge.py` | `tests/test_knowledge.py` |
| Books checked for suitability, notes approved by the practice | `app/books.py` | `tests/test_books.py` |
| Lessons approved by a clinician | `app/learning.py` | covered in `tests/test_api.py` |
| Admin sign-in: scrypt password hash, robot check, one admin email | `app/admin_auth.py` | `tests/test_admin.py` |
| Payments counted only after Stripe confirms them | `app/payments.py` | `tests/test_stripe.py` |
| Only the practice's own website may show the client page, never the admin, and the private session link is posted to that exact website, never to "any" | `app/main.py` (frame policy), `app/static/app.js` | `tests/test_embed.py` |

## What happens in the consultation

| Safety result | Action |
|---|---|
| none | The reply is shown. |
| check (ambiguous words) | The practice's fixed safety question is asked. The generated reply is discarded. |
| concern | A stated condition that would stop the consultation is confirmed once first, because speech to text can mishear. Then the consultation stops. |
| crisis | The consultation stops and the client sees verified crisis resources for their country and language (United Kingdom, Germany, Austria, Switzerland, France and Spain). |
| unverified (the classifier is down and no keyword matched) | An error is returned and no reply is made. |

Each decision is stored with its evidence and rationale.

## Privacy

- **Clients never sign in.** They use a private link to their own session. That link is the key to their session, so it is only sent to websites that are allowed to show the app, never to "any".
- **The admin dashboard** is open only to the practice's admin email. The password is never stored, only a slow hash of it, and the dashboard asks for a robot check.
- **Card details** never reach this app. Payment happens on Stripe's own page.
- **Speech** is processed on the server by local models, so no outside speech service receives a client's voice. Session audio is deleted a set number of days after it is made (7 by default).
- **Email** goes through the practice's own mailbox. Reminders go only to clients who did not opt out.
- **This repository holds no client data.** Sessions, recordings, databases, passwords and the practice's know-how are not in it.

## What is not covered

- This repository does not contain a clinical safety case, a data protection impact assessment or a penetration test.
- The keyword net is a net, not a clinical instrument, and the classifier can be wrong. Both can miss risk, and neither replaces a trained person.
- Clinician review of every script is a setting that is off in the sample configuration.
- The prompts and question sets in this copy are short stand-ins. The live ones are private, so the live behaviour is richer than what is shown here.
