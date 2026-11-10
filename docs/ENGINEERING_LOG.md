# Engineering log

Real problems found while the product was used, taken from its commit history (30 September to 25 October 2026). Each entry gives the symptom, the cause, the fix and what covers it. Where no automated test covers it, it says so.

## 1. The microphone dropped back to typing in the middle of an answer

**Symptom.** One failed transcription, a recorder hiccup or a microphone stream that ended switched hands-free listening off, so the client suddenly had to type.

**Cause.** A passing problem was treated as a permanent one. The typing box also took focus on its own, which scrolled the page away from the microphone and opened a phone's keyboard while the client was talking.

**Fix.** Listening now retries by itself, up to three times in a row. The typing box only takes focus when the client chooses "Type instead". If the server cannot transcribe, the browser's own recogniser takes over, and a browser with neither is told to type instead of listening for ever. Speech to text is loaded in the background at start-up and a failed load is remembered, so a broken model is not retried on every answer.

**Covered by.** No automated test. This is browser client behaviour, and the client has no tests of its own yet.

## 2. Abandoned consultations stayed open

**Symptom.** A consultation that the client left open (app closed, phone off) was only closed when they came back, so its summary was written late.

**Cause.** The time limit was only checked when the client acted.

**Fix.** The server checks every minute and closes any consultation that is past its limit, so the summary is written either way.

**Covered by.** New tests for the 20-minute limit, with and without the client present.

## 3. Closing the app inside the website lost the consultation

**Symptom.** Inside the practice's website page, closing the app (or pressing the phone's back button) and then "Continue your consultation" reopened the app at its start.

**Cause.** The app resumes only from a private link, and the website page did not have it.

**Fix.** The app tells the website page which session is open, and that it is gone when the client starts over. Because that link is the client's key to their own session, it is posted only to the websites allowed to show the app, never to "any".

**Covered by.** `tests/test_embed.py`, which covers which websites may show the app and be told about the open session.

## 4. "Continue" after a pause repeated the whole message and restarted the clock

**Symptom.** After a silence pause, pressing Continue repeated the consultant's entire last message ("Thanks for answering that." again) and started the client's clock at once.

**Fix.** The consultant now says "Welcome back." and asks only the question again, and the clock stays stopped until the client answers. The same applies when a paused consultation is reopened.

**Covered by.** No automated test, for the same reason.

## 5. The "stop consultation" warning could be skipped

**Symptom.** Stopping a consultation is final, so it needs a clear confirmation, but the warning was a browser pop-up.

**Cause.** A browser's own confirm pop-up can be suppressed without asking, inside an embedded page and in some phones and apps.

**Fix.** The warning is now the app's own box with "Yes, stop" and "No, carry on".

**Covered by.** No automated test, for the same reason.

## 6. The dashboard could not start

**Symptom.** After a change to the dashboard, it would not load.

**Cause.** A name in the sample-text code clashed with another name in the same page.

**Fix.** Renamed.

**Lesson.** This is the kind of failure a basic browser test would catch immediately. It is the main reason I listed browser tests as the next improvement.

## 7. On a phone, pressing Start scrolled the consultation out of view

**Symptom.** The avatar and the timer went off the top of the screen and only the buttons were left.

**Fix.** The consultation now fills the screen like a video call: the timer at the top, the avatar in the remaining space, the question in its own scrolling box, then the message area, the typing box and the buttons, all in view even with the keyboard open. The site header is hidden during the consultation.

**Covered by.** Checked at 390 by 800 and 360 by 640 screen sizes. No automated test.

## 8. The question appeared in full before it was spoken

**Symptom.** The whole question showed the moment the consultant began to speak.

**Fix.** The words now appear as they are spoken, like live captions. The device voice reports each word. The practice voice reports none, so its words follow its own pace, learnt sentence by sentence from when its audio really starts and ends. The words still to come keep their place invisibly so nothing moves. Screen readers hear the question once, when it is complete, and the whole question is shown if the client interrupts or there is no voice.

## Design changes made for cost and safety

These were decisions rather than bugs, and each is visible in the code.

- **Silence costs nothing.** The check-in after a minute and the pause after two use fixed phrases and never call the model.
- **No session from too little.** A session is only written from enough of the client's own words (at least 10 answers, or fewer, longer ones). Otherwise nothing is spent, and the client can continue once or ask for the qualified hypnotherapist.
- **Cheaper model for routine work.** Reading a book and writing its notes uses a smaller model, which is about four times cheaper, while consultations and sessions keep the main one.
- **Confirm before a safety stop.** A condition that the client says they have is confirmed once before the consultation stops, because speech to text can mishear.

## What these have in common

Almost every problem here came from real conditions that unit tests do not reach: phones, embedded web views, silence, interruptions and screen sizes. The server has 294 tests. The browser client has none of its own, and most of these fixes have no automated test. Closing that gap is the next job.
