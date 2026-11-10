"""Card payments with Stripe: the payment page, coming back, Stripe's notice, the saved card, and what can go wrong.
Stripe's servers are replaced by a stand-in; the notice's signature check is Stripe's own."""

import email
import hashlib
import hmac
import json
import time
from email import policy

import pytest

from app.payments import CardNeedsCheckout, PaymentUnavailable, StripePayments
from tests.support import valid_script
from tests.test_api import CONSULTATION_PAYMENT, consult_to_choice, start, view

WEBHOOK_SECRET = "whsec_test_secret"


class FakeStripe(StripePayments):
    """Stripe's servers, as far as the app is concerned (the signature check of notices is the real one)."""

    def __init__(self):
        super().__init__(secret_key="sk_test_fake", webhook_secret=WEBHOOK_SECRET, client_page="https://ai.example/")
        self.pages: dict[str, dict] = {}
        self.charges: list[dict] = []
        self.charge_error: Exception | None = None
        self.down = False

    def checkout(self, **params) -> dict:
        if self.down:
            raise PaymentUnavailable("Stripe is down")
        checkout_id = f"cs_test_{len(self.pages) + 1}"
        self.pages[checkout_id] = {**params, "paid": False}
        return {"id": checkout_id, "url": f"https://checkout.stripe.test/{checkout_id}"}

    def pay_page(self, checkout_id: str, **overrides) -> None:
        """The client pays on Stripe's page."""
        self.pages[checkout_id].update(paid=True, **overrides)

    def checkout_status(self, checkout_id: str) -> dict:
        page = self.pages[checkout_id]
        return {"paid": page["paid"], "amount_pence": page.get("charged", page["amount_pence"]), "currency": "gbp",
                "session_id": page["session_id"], "purpose": page["purpose"], "payment_intent": "pi_page",
                "customer": "cus_1" if page["save_card"] else None,
                "payment_method": "pm_1" if page["save_card"] else None}

    def charge_saved_card(self, **params) -> str:
        self.charges.append(params)
        if self.charge_error:
            raise self.charge_error
        return "pi_saved_card"


@pytest.fixture
def stripe_client(make_client):
    client = make_client(payment_provider="stripe", require_clinician_review=False, stripe_secret_key="sk_test_fake",
                         stripe_webhook_secret=WEBHOOK_SECRET, public_url="https://ai.example")
    client.app.state.service.payments = FakeStripe()
    return client


def fake(client) -> FakeStripe:
    return client.app.state.service.payments


def pay(client, session_id, **body):
    return client.post(f"/api/sessions/{session_id}/pay", json={**CONSULTATION_PAYMENT, **body})


def confirm(client, session_id, checkout_id):
    return client.post(f"/api/sessions/{session_id}/confirm-payment", json={"checkout_id": checkout_id})


def outbox(settings) -> list:
    return [email.message_from_bytes(p.read_bytes(), policy=policy.default)
            for p in sorted((settings.data_dir / "outbox").glob("*.eml"))]


def notice(payload: dict, secret: str = WEBHOOK_SECRET) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    stamp = int(time.time())
    signature = hmac.new(secret.encode(), f"{stamp}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"Stripe-Signature": f"t={stamp},v1={signature}", "Content-Type": "application/json"}


def completed(checkout_id: str, session_id: str) -> dict:
    return {"id": "evt_1", "object": "event", "type": "checkout.session.completed",
            "data": {"object": {"id": checkout_id, "object": "checkout.session",
                                "metadata": {"session_id": session_id, "purpose": "consultation"}}}}


def test_the_client_pays_on_stripes_page_and_the_app_checks_it_with_stripe(stripe_client, settings):
    session_id = start(stripe_client, pay=False)["session_id"]
    payment = view(stripe_client, session_id)["payment"]
    assert (payment["provider"], payment["test_mode"]) == ("stripe", True)
    sent = pay(stripe_client, session_id, save_card=True)
    assert sent.json() == {"checkout_url": "https://checkout.stripe.test/cs_test_1"}
    page = fake(stripe_client).pages["cs_test_1"]
    assert (page["amount_pence"], page["email"], page["save_card"], page["purpose"]) == (
        500, "sam.client@example.com", True, "consultation")
    assert view(stripe_client, session_id)["status"] == "awaiting_consultation_payment"  # nothing paid yet

    unpaid = confirm(stripe_client, session_id, "cs_test_1")  # came back without paying
    assert unpaid.status_code == 409 and "not completed" in unpaid.json()["detail"]
    fake(stripe_client).pay_page("cs_test_1")
    back = confirm(stripe_client, session_id, "cs_test_1")
    assert back.status_code == 200 and back.json()["status"] == "consulting"
    assert back.json()["messages"][0]["role"] == "assistant"  # the greeting
    [receipt] = outbox(settings)
    assert "£5.00" in receipt.get_content()
    assert "TEST VERSION: no money was taken." in receipt.get_content()  # a Stripe test key: said so plainly
    again = confirm(stripe_client, session_id, "cs_test_1")  # a second visit (or Stripe's notice) changes nothing
    assert again.status_code == 200 and len(again.json()["messages"]) == 1 and len(outbox(settings)) == 1
    payments = stripe_client.app.state.service.db.list_payments(session_id)
    assert [(p["status"], p["provider"], p["customer_ref"], p["method_ref"]) for p in payments] == [
        ("paid", "stripe", "cus_1", "pm_1")]


def test_the_saved_card_pays_for_the_full_session_at_once(stripe_client):
    client = stripe_client
    session_id = start(client, pay=False)["session_id"]
    pay(client, session_id, save_card=True)
    fake(client).pay_page("cs_test_1")
    confirm(client, session_id, "cs_test_1")
    gateway = client.app.state.service.gateway
    consult_to_choice(client, gateway, session_id)
    assert client.post(f"/api/sessions/{session_id}/ai-session", json={"baseline_severity": 5}).status_code == 200
    gateway.completions.append((valid_script(), "end_turn"))
    paid = client.post(f"/api/sessions/{session_id}/pay", json={"purpose": "session", "acknowledged": True})
    assert paid.status_code == 200 and "checkout_url" not in paid.json()
    assert fake(client).charges == [{"customer": "cus_1", "payment_method": "pm_1", "amount_pence": 3500,
                                     "item": "Full personalised AI hypnotherapy session",
                                     "session_id": session_id, "purpose": "session"}]
    assert view(client, session_id)["status"] in ("generating", "ready")


def test_a_saved_card_that_needs_the_client_goes_to_the_payment_page(stripe_client):
    client = stripe_client
    session_id = start(client, pay=False)["session_id"]
    pay(client, session_id, save_card=True)
    fake(client).pay_page("cs_test_1")
    confirm(client, session_id, "cs_test_1")
    consult_to_choice(client, client.app.state.service.gateway, session_id)
    client.post(f"/api/sessions/{session_id}/ai-session", json={"baseline_severity": 5})
    fake(client).charge_error = CardNeedsCheckout("authentication_required")
    sent = client.post(f"/api/sessions/{session_id}/pay", json={"purpose": "session", "acknowledged": True})
    assert sent.json() == {"checkout_url": "https://checkout.stripe.test/cs_test_2"}
    statuses = [p["status"] for p in client.app.state.service.db.list_payments(session_id)]
    assert statuses == ["paid", "failed", "pending"] and view(client, session_id)["status"] == "awaiting_session_payment"


def test_stripes_notice_completes_a_payment_even_if_the_client_never_comes_back(stripe_client):
    session_id = start(stripe_client, pay=False)["session_id"]
    pay(stripe_client, session_id)
    fake(stripe_client).pay_page("cs_test_1")
    forged, headers = notice(completed("cs_test_1", session_id), secret="whsec_someone_else")
    assert stripe_client.post("/api/stripe/webhook", content=forged, headers=headers).status_code == 400
    assert view(stripe_client, session_id)["status"] == "awaiting_consultation_payment"
    body, headers = notice(completed("cs_test_1", session_id))
    assert stripe_client.post("/api/stripe/webhook", content=body, headers=headers).status_code == 200
    assert view(stripe_client, session_id)["status"] == "consulting"
    other, headers = notice(completed("cs_website_payment", "someone"))  # a payment on the practice's website
    assert stripe_client.post("/api/stripe/webhook", content=other, headers=headers).status_code == 200


def test_a_payment_that_does_not_match_is_not_accepted(stripe_client):
    session_id = start(stripe_client, pay=False)["session_id"]
    pay(stripe_client, session_id)
    fake(stripe_client).pay_page("cs_test_1", charged=100)  # £1 instead of £5
    refused = confirm(stripe_client, session_id, "cs_test_1")
    assert refused.status_code == 409 and view(stripe_client, session_id)["status"] == "awaiting_consultation_payment"
    other = start(stripe_client, pay=False)["session_id"]
    assert confirm(stripe_client, other, "cs_test_1").status_code == 422  # someone else's payment page


def test_when_stripe_is_down_the_client_is_asked_to_try_again(stripe_client):
    session_id = start(stripe_client, pay=False)["session_id"]
    fake(stripe_client).down = True
    down = pay(stripe_client, session_id)
    assert down.status_code == 503 and view(stripe_client, session_id)["status"] == "awaiting_consultation_payment"


def test_the_real_stripe_payment_page_request(monkeypatch):
    """What the app asks Stripe for (the request itself, not sent anywhere)."""
    sent = {}

    class Page:
        def to_dict(self):
            return {"id": "cs_real", "url": "https://checkout.stripe.com/c/pay/cs_real"}

    payments = StripePayments(secret_key="sk_test_x", webhook_secret="whsec_x", client_page="https://ai.example/")
    monkeypatch.setattr(payments.client.v1.checkout.sessions, "create", lambda params: sent.update(params) or Page())
    page = payments.checkout(session_id="S" * 32, purpose="consultation", item="Personal hypnotherapy consultation",
                             amount_pence=500, email="sam@example.com", save_card=True)
    assert page == {"id": "cs_real", "url": "https://checkout.stripe.com/c/pay/cs_real"}
    assert sent["line_items"][0]["price_data"] == {"currency": "gbp", "unit_amount": 500,
                                                   "product_data": {"name": "Personal hypnotherapy consultation"}}
    assert sent["success_url"] == f"https://ai.example/?checkout={{CHECKOUT_SESSION_ID}}#s={'S' * 32}"
    assert sent["cancel_url"] == f"https://ai.example/#s={'S' * 32}"
    assert sent["payment_intent_data"]["setup_future_usage"] == "off_session"
    assert sent["payment_method_types"] == ["card"]  # only a card can be saved and charged later
    assert (sent["customer_email"], sent["customer_creation"]) == ("sam@example.com", "always")


@pytest.mark.parametrize("key, test_mode", [("sk_test_abc", True), ("rk_test_abc", True), ("sk_live_abc", False),
                                            ("rk_live_abc", False)])
def test_live_keys_are_recognised_whatever_their_kind(make_client, key, test_mode):
    client = make_client(payment_provider="stripe", stripe_secret_key=key)
    assert client.app.state.service.payments_test_mode is test_mode
